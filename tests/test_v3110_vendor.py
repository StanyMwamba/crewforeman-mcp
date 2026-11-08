"""S5a reviewer vendor-attribution tests (crewforeman-mcp 3.11.0).

Unit-level only: Config/SimpleNamespace, no network and no harness import.
"""

import unittest

from deepseek_mcp import reviewer_backup
from deepseek_mcp.config import Config


def _kimi_cfg(**overrides):
    values = {
        "dsh_provider": "deepseek-official",
        "dsh_model": "deepseek-flash",
        "backup_dsh_bin": "/opt/backup/kimi-wrapper",
        "backup_reviewer_vendor": "Kimi (Moonshot)",
        "backup_reviewer_model": "kimi-k3.1-pro",
    }
    values.update(overrides)
    return Config(values)


def _execution(error, status="error", **extra):
    execution = {"status": status, "error": error}
    execution.update(extra)
    return execution


class VendorAttributionTests(unittest.TestCase):
    def test_backup_kimi_quota_names_kimi_not_deepseek(self):
        # The main reviewer is DeepSeek, but the backup's own error is a Kimi
        # quota failure; attribution must follow the backup.
        cfg = _kimi_cfg()
        message = reviewer_backup.error_message(
            cfg, _execution("429 insufficient quota")
        )
        self.assertIn("Kimi", message)
        self.assertIn("Moonshot", message)
        self.assertIn("quota is exhausted", message)
        self.assertNotIn("DeepSeek account balance is empty", message)
        self.assertIn("DeepSeek balance is unaffected", message)

    def test_backup_deepseek_quota_keeps_deepseek_remedy(self):
        cfg = _kimi_cfg(
            backup_dsh_bin="/opt/backup/deepseek-wrapper",
            backup_reviewer_vendor="DeepSeek",
            backup_reviewer_model="deepseek-chat",
        )
        message = reviewer_backup.error_message(
            cfg, _execution("429 insufficient quota")
        )
        self.assertIn("DeepSeek", message)
        self.assertIn("DeepSeek account balance is empty", message)
        self.assertIn("top up", message)
        self.assertIn("platform.deepseek.com", message)
        self.assertNotIn("balance is unaffected", message.lower())

    def test_backup_unknown_vendor_never_says_deepseek(self):
        cfg = Config(
            {
                "dsh_model": "generic-model",
                "backup_dsh_bin": "",
                "backup_reviewer_vendor": "",
                "backup_reviewer_model": "",
            }
        )
        message = reviewer_backup.error_message(cfg, _execution("boom"))
        self.assertIn("unknown/generic-model", message)
        self.assertIn("reviewer failed (unclassified)", message)
        # The generic non-DeepSeek clause may name the DeepSeek balance, but
        # the reviewer itself is never attributed to DeepSeek.
        self.assertNotIn("DeepSeek account balance is empty", message)
        self.assertNotIn("DeepSeek reviewer", message)
        self.assertIn("DeepSeek balance is unaffected", message)

    def test_backup_auth_names_vendor_key(self):
        message = reviewer_backup.error_message(
            _kimi_cfg(), _execution("401 invalid api key")
        )
        self.assertIn("authentication failed", message)
        self.assertIn("MOONSHOT_API_KEY", message)
        self.assertNotIn("ANTHROPIC_API_KEY", message)
        self.assertNotIn("OPENAI_API_KEY", message)
        self.assertIn("DeepSeek balance is unaffected", message)

    def test_backup_explicit_vendor_wins(self):
        cfg = Config(
            {
                "backup_dsh_bin": "/usr/local/bin/claude-wrapper",
                "backup_reviewer_vendor": "Kimi (Moonshot)",
                "backup_reviewer_model": "kimi-k3.1-pro",
            }
        )
        message = reviewer_backup.error_message(
            cfg, _execution("402 payment required")
        )
        self.assertIn("Kimi", message)
        self.assertIn("Moonshot", message)
        self.assertNotIn("Anthropic", message)
        self.assertNotIn("Claude", message)

    def test_backup_generic_failure_is_attributed(self):
        message = reviewer_backup.error_message(
            _kimi_cfg(), _execution("totally unrecognised transport failure")
        )
        self.assertIn(
            "Kimi (Moonshot)/kimi-k3.1-pro reviewer failed (unclassified)",
            message,
        )
        self.assertIn("see the redacted run record", message)
        self.assertNotIn("DeepSeek account balance is empty", message)

    def test_backup_error_message_has_no_raw_text(self):
        message = reviewer_backup.error_message(
            _kimi_cfg(),
            _execution(
                "429 insufficient quota: key sk-live-" "ABCDEF0123456789 "
                "token=supersecretvalue"
            ),
        )
        self.assertIn("Kimi", message)
        for raw in (
            "sk-live",
            "ABCDEF0123456789",
            "supersecretvalue",
            "insufficient quota",
            "Insufficient Balance",
        ):
            self.assertNotIn(raw, message)

    def test_backup_preflight_failure_names_kimi_in_run_record(self):
        cfg = _kimi_cfg()
        backup_error = _execution("429 insufficient quota")
        ident = reviewer_backup.identity(cfg, backup_error)
        backup_message = reviewer_backup.error_message(cfg, backup_error)
        record = {
            "reviewer_preflight": {
                "vendor": ident["vendor"],
                "status": "error",
            },
            "reviewer_backup": {
                "vendor": ident["vendor"],
                "model": ident["model"],
                "reason_class": reviewer_backup.switch_class(backup_error),
                "main_error": "DeepSeek account balance is empty: top up",
                "backup_error": backup_message,
            },
            "review_error": backup_message,
            "reviewed_by": "backup",
        }
        self.assertIn("Kimi", record["reviewer_preflight"]["vendor"])
        self.assertIn("Kimi", record["reviewer_backup"]["vendor"])
        self.assertEqual(
            record["reviewer_backup"]["reason_class"], "credit/budget"
        )
        self.assertIn("Kimi", record["review_error"])
        self.assertNotIn(
            "DeepSeek account balance is empty", record["review_error"]
        )
        self.assertIn(
            "DeepSeek balance is unaffected", record["review_error"]
        )


if __name__ == "__main__":
    unittest.main()
