import os
import unittest
from types import SimpleNamespace

from deepseek_mcp import reviewer_backup as rb


def cfg(**overrides):
    values = {
        "backup_dsh_bin": "",
        "backup_reviewer_vendor": "",
        "backup_reviewer_model": "",
        "dsh_model": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ConfigKeysTests(unittest.TestCase):
    def test_config_keys_exact(self):
        self.assertEqual(
            rb.CONFIG_KEYS,
            (
                "backup_dsh_bin",
                "backup_reviewer_vendor",
                "backup_reviewer_model",
            ),
        )


class ConfiguredTests(unittest.TestCase):
    def test_blank_default_returns_none(self):
        self.assertIsNone(rb.configured(cfg()))
        self.assertIsNone(rb.configured(cfg(backup_dsh_bin="   ")))
        self.assertIsNone(rb.configured(None))

    def test_path_expansion(self):
        result = rb.configured(cfg(backup_dsh_bin="~/bin/dsh-backup"))
        self.assertEqual(result["bin"], os.path.expanduser("~/bin/dsh-backup"))
        self.assertTrue(os.path.isabs(result["bin"]))
        self.assertNotIn("~", result["bin"])
        self.assertIsNone(result["vendor"])
        self.assertIsNone(result["model"])

    def test_vendor_and_model_are_trimmed(self):
        result = rb.configured(
            cfg(
                backup_dsh_bin=" /opt/reviewer ",
                backup_reviewer_vendor=" Anthropic ",
                backup_reviewer_model=" claude-sonnet-4 ",
            )
        )
        self.assertEqual(
            result,
            {"bin": "/opt/reviewer", "vendor": "Anthropic", "model": "claude-sonnet-4"},
        )


class IdentityTests(unittest.TestCase):
    def test_explicit_override_wins(self):
        result = rb.identity(
            cfg(
                backup_dsh_bin="/opt/bin/kimi-reviewer",
                backup_reviewer_vendor="ExampleVendor",
                backup_reviewer_model="m1",
                dsh_model="ignored",
            )
        )
        self.assertEqual(result, {"vendor": "ExampleVendor", "model": "m1", "label": "ExampleVendor/m1"})

    def test_sonnet_filename_inference(self):
        result = rb.identity(
            cfg(backup_dsh_bin="/opt/wrappers/dsh-sonnet", dsh_model="claude-sonnet-4")
        )
        self.assertEqual(result["vendor"], "Anthropic")
        self.assertEqual(result["model"], "claude-sonnet-4")
        self.assertEqual(result["label"], "Anthropic/claude-sonnet-4")

    def test_kimi_filename_inference(self):
        result = rb.identity(cfg(backup_dsh_bin="~/bin/kimi-backup", dsh_model="kimi-k2"))
        self.assertEqual(result["vendor"], "Kimi (Moonshot)")
        self.assertEqual(result["label"], "Kimi (Moonshot)/kimi-k2")

    def test_execution_models_used_when_config_model_blank(self):
        result = rb.identity(
            cfg(backup_dsh_bin="/opt/sonnet-reviewer", dsh_model="fallback-model"),
            {"models": "exec-model"},
        )
        self.assertEqual(result["model"], "exec-model")
        self.assertEqual(result["label"], "Anthropic/exec-model")

    def test_execution_models_list_uses_last_value(self):
        result = rb.identity(
            cfg(backup_dsh_bin="/opt/openai-reviewer", dsh_model="fallback"),
            {"models": ["first-model", "final-model"]},
        )
        self.assertEqual(result["model"], "final-model")

    def test_no_known_model_is_not_invented(self):
        result = rb.identity(cfg(backup_dsh_bin="/opt/gemini-reviewer"))
        self.assertEqual(result["vendor"], "Google")
        self.assertEqual(result["model"], "unknown")
        self.assertEqual(result["label"], "Google/unknown")


class SwitchClassTests(unittest.TestCase):
    def assertClass(self, expected, execution, retries_exhausted=False):
        self.assertEqual(
            rb.switch_class(execution, retries_exhausted=retries_exhausted),
            expected,
            msg=repr(execution),
        )

    def test_credit_budget(self):
        for text in (
            "Insufficient balance",
            "insufficient_quota: please add credits",
            "HTTP 402 Payment Required",
            "429 quota exceeded",
            "429 Too Many Requests: budget exhausted",
            "your credit balance is too low",
        ):
            with self.subTest(text=text):
                self.assertClass("credit/budget", {"status": "error", "error": text})

    def test_auth(self):
        for text in (
            "HTTP 401 Unauthorized",
            "HTTP 403 Forbidden",
            "invalid api key",
            "expired key",
            "Authentication failed",
        ):
            with self.subTest(text=text):
                self.assertClass("auth", {"status": "error", "error": text})

    def test_service_unavailable(self):
        for text in (
            "HTTP 500 Internal Server Error",
            "502 Bad Gateway",
            "503 Service Unavailable",
            "504 Gateway Timeout",
            "provider overloaded",
        ):
            with self.subTest(text=text):
                self.assertClass(
                    "service unavailable (5xx)", {"status": "error", "error": text}
                )

    def test_stream_dropped(self):
        for text in (
            "flush on a closed handle",
            "STREAM_CLOSED while reading",
            "read ECONNRESET",
            "socket hang up",
        ):
            with self.subTest(text=text):
                self.assertClass("stream dropped", {"status": "error", "error": text})

    def test_5xx_and_stream_return_class_before_retries_exhausted(self):
        self.assertClass(
            "service unavailable (5xx)",
            {"status": "error", "error": "503"},
            retries_exhausted=False,
        )
        self.assertClass(
            "stream dropped",
            {"status": "error", "error": "STREAM_CLOSED"},
            retries_exhausted=False,
        )

    def test_status_ok_quality_outcomes_never_switch(self):
        executions = (
            {"status": "ok", "score": 2.0},
            {"status": "ok", "score": 2.0, "blocking_defects": ["bug"]},
            {"status": "ok", "score": None},
            # Even transport-looking text must not switch a successful process.
            {"status": "ok", "score": 1.0, "error": "insufficient balance"},
        )
        for execution in executions:
            with self.subTest(execution=execution):
                self.assertClass(None, execution)

    def test_stalls_deadlines_and_cancellations_never_switch(self):
        executions = (
            {"status": "stalled"},
            {"status": "timed_out"},
            {"status": "cancelled"},
            {"status": "time_limit"},
            {"status": "error", "error": "job deadline exceeded"},
            {"status": "error", "error": "request cancelled by operator"},
        )
        for execution in executions:
            with self.subTest(execution=execution):
                self.assertClass(None, execution)

    def test_plain_unknown_and_plain_429_do_not_switch(self):
        self.assertClass(None, {"status": "error", "error": "segfault"})
        self.assertClass(None, {"status": "error", "error": "HTTP 429 Too Many Requests"})
        self.assertClass(None, None)


class BannerTests(unittest.TestCase):
    def test_banner_exact(self):
        text = rb.banner(
            cfg(backup_dsh_bin="/opt/sonnet-reviewer", dsh_model="claude-sonnet-4"),
            "credit/budget",
        )
        self.assertEqual(
            text,
            "Reviewed by: backup (Anthropic/claude-sonnet-4) \u2014 "
            "main failed: credit/budget",
        )

    def test_banner_with_override_and_execution_identity(self):
        text = rb.banner(
            cfg(
                backup_dsh_bin="/opt/reviewer",
                backup_reviewer_vendor="Kimi (Moonshot)",
                backup_reviewer_model="kimi-k2",
            ),
            "service unavailable (5xx)",
            {"models": "ignored"},
        )
        self.assertEqual(
            text,
            "Reviewed by: backup (Kimi (Moonshot)/kimi-k2) \u2014 "
            "main failed: service unavailable (5xx)",
        )


if __name__ == "__main__":
    unittest.main()
