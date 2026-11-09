"""P3 vendor-aware paid-error classification (Kimi must not claim DeepSeek)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness
from deepseek_mcp.config import Config, load_config
from tests.fakes import base_env, write_fake_dsh


class VendorMessageTests(unittest.TestCase):
    def test_kimi_quota_429_names_moonshot_not_deepseek(self):
        message = harness.provider_error_message(
            {"error": "HTTP 429 exceeded_current_quota_error: quota exhausted",
             "model": "kimi-k2"})
        self.assertIn("Kimi", message)
        self.assertIn("Moonshot", message)
        self.assertIn("platform.moonshot.cn", message)
        self.assertNotIn("DeepSeek", message)
        self.assertNotIn("platform.deepseek.com", message)

    def test_deepseek_insufficient_balance_keeps_deepseek_remedy(self):
        message = harness.provider_error_message(
            {"error": "Insufficient Balance", "model": "deepseek-flash"})
        self.assertIn("DeepSeek", message)
        self.assertIn("platform.deepseek.com", message)

    def test_generic_429_is_a_rate_limit_not_a_balance_problem(self):
        message = harness.provider_error_message(
            {"error": "HTTP 429 Too Many Requests", "model": "kimi-k2"})
        self.assertIn("rate limit", message)
        self.assertIn("429", message)
        self.assertNotIn("balance", message.lower())
        self.assertNotIn("quota", message.lower())

    def test_auth_names_vendor_and_key_environment(self):
        message = harness.provider_error_message(
            {"error": "HTTP 401 unauthorized invalid api key",
             "model": "kimi-k2"})
        self.assertIn("Kimi", message)
        self.assertIn("MOONSHOT_API_KEY", message)
        self.assertNotIn("DeepSeek", message)

    def test_provider_family_kimi_wins_when_both_appear(self):
        cfg = Config({"dsh_provider": "deepseek-official",
                      "dsh_model": "kimi-k2"})
        self.assertEqual(
            harness.provider_family(
                text="deepseek proxy error", cfg=cfg,
                execution={"model": "kimi-k2"}), "kimi")
        self.assertEqual(
            harness.provider_family(cfg=Config({"dsh_provider": "anthropic",
                                                "dsh_model": "claude-3"})),
            "anthropic")

    def test_execution_endpoint_overrides_conflicting_configuration_hints(self):
        # The builder/reviewer execution is the source of truth. A stale Kimi
        # dsh_* config must not turn a real DeepSeek failure into a Moonshot
        # remedy, and a stale DeepSeek default must not hide a Kimi run.
        kimi_cfg = Config({"dsh_provider": "kimi", "dsh_model": "kimi-k2",
                           "base_url": "https://api.moonshot.cn/v1"})
        self.assertEqual(harness.provider_family(
            text="HTTP 429 exceeded_current_quota_error",
            cfg=kimi_cfg,
            execution={"provider": "deepseek-official",
                       "model": "deepseek-flash",
                       "models": ["deepseek-flash"]}), "deepseek")
        message = harness.provider_error_message(
            {"error": "Insufficient Balance",
             "provider": "deepseek-official", "model": "deepseek-flash"},
            cfg=kimi_cfg)
        self.assertIn("DeepSeek", message)
        self.assertIn("platform.deepseek.com", message)
        self.assertNotIn("Moonshot", message)

        deepseek_cfg = Config({"dsh_provider": "deepseek-official",
                               "dsh_model": "deepseek-flash"})
        self.assertEqual(harness.provider_family(
            text="Insufficient Balance",
            cfg=deepseek_cfg,
            execution={"provider": "kimi", "model": "kimi-k2"}), "kimi")
        message = harness.provider_error_message(
            {"error": "HTTP 429 exceeded_current_quota_error",
             "provider": "kimi", "model": "kimi-k2"},
            cfg=deepseek_cfg)
        self.assertIn("Kimi", message)
        self.assertNotIn("platform.deepseek.com", message)

    def test_unknown_error_is_sanitized_and_never_claims_deepseek(self):
        # Compose the synthetic key at runtime so no credential-shaped
        # literal is committed; the assertion is unchanged.
        fake_key = "sk-" + "abcdefghijk"
        message = harness.provider_error_message(
            {"error": "weird failure " + fake_key, "model": "kimi-k2"})
        self.assertIn("Kimi", message)
        self.assertNotIn(fake_key, message)
        self.assertNotIn("DeepSeek", message)


class RunLevelVendorTests(unittest.TestCase):
    def test_fake_dsh_reviewer_quota_names_kimi_in_run_record(self):
        base = os.environ.get("TMPDIR") or os.getcwd()
        try:
            tmpdir = tempfile.TemporaryDirectory(prefix="p3-vendor-", dir=base)
        except OSError:
            tmpdir = tempfile.TemporaryDirectory(prefix="p3-vendor-", dir=os.getcwd())
        with tmpdir as tmp:
            root = Path(tmp).resolve()
            ws = root / "ws"
            ws.mkdir()
            fake = write_fake_dsh(root / "dsh")
            env = base_env(root, fake_dsh=fake)
            env.update({
                "FAKE_DSH_FAIL_REVIEW": "1",
                "FAKE_DSH_REVIEW_ERROR_MESSAGE":
                    "HTTP 429 exceeded_current_quota_error: balance/quota",
            })
            args = {
                "prompt": "Build a tiny vendor classification note.",
                "workspace": str(ws), "sandbox": "danger",
                "quality": True, "independent_review": True,
                "quality_rounds": 1, "subagents": 1,
                "max_seconds": 30, "job_max_seconds": 120,
            }
            with mock.patch.dict(os.environ, env, clear=False):
                cfg = load_config(config_path=root / "config.json", env=env)
                # 3.12.0: the reviewer is selected independently of the
                # builder. Keep the builder on DeepSeek and route a Kimi
                # wrapper candidate so the same-vendor rule does not pause.
                cfg._values["reviewers"] = [{
                    "name": "kimi", "vendor": "Kimi (Moonshot)",
                    "dsh_bin": str(fake),
                }]
                _text, run, _path = harness.run_harness(args, cfg)
        self.assertEqual(run["builder"]["status"], "ok")
        self.assertIsNotNone(run.get("review_error"))
        self.assertIn("Kimi", run["review_error"])
        self.assertIn("Moonshot", run["review_error"])
        self.assertNotIn("platform.deepseek.com", run["review_error"])


if __name__ == "__main__":
    unittest.main()
