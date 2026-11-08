"""3.12.0 end-to-end acceptance with a fake DSH (no network, no real key)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import builder_backup, harness, model_policy
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh


class BuilderOverrideEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.argv = self.root / "argv.json"
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.root / "sessions")
        self.env.update({
            "FAKE_DSH_ARGV": str(self.argv),
            "FAKE_DSH_SESSIONS": str(self.root / "sessions"),
            "FAKE_DSH_MODEL": "kimi-fake",
            "DSH_TRANSIENT_RETRY": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
        })
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)
        self.cfg._values["providers"] = [{
            "name": "fake-provider",
            "vendor": "Kimi (Moonshot)",
            "base_url": "https://example.invalid",
            "api": "openai-chat",
            "api_key_env": "FAKE_MODEL_KEY",
            "models": [{"id": "kimi-fake", "name": "kimi-fake"}],
        }]
        os.environ["FAKE_MODEL_KEY"] = "test-key-not-real"

    def tearDown(self):
        os.environ.pop("FAKE_MODEL_KEY", None)

    def _run(self, explicit=None):
        args = {"prompt": "Write one note.", "subagents": 0, "quality": False,
                "workspace": str(self.ws)}
        if explicit:
            args.update(explicit)
        with mock.patch.dict(os.environ, self.env, clear=False):
            return harness.run_harness(args, self.cfg)

    def test_override_uses_overlay_and_default_argv_is_unchanged(self):
        _text, run, _path = self._run({
            "builder_provider": "fake-provider",
            "builder_model": "kimi-fake",
        })
        argv = json.loads(self.argv.read_text(encoding="utf-8"))
        self.assertIn("--patch", argv)
        patch = Path(argv[argv.index("--patch") + 1])
        self.assertFalse(patch.exists(), "overlay must be removed in finally")
        self.assertFalse(list(self.ws.glob(".dsh-model-overlay-*/overlay.yml")))
        self.assertEqual(run["builder_model"]["provider"], "fake-provider")
        self.assertEqual(run["builder_model"]["model"], "kimi-fake")
        self.assertEqual(run["builder_model"]["vendor"], "Kimi (Moonshot)")
        self.assertTrue(any("kimi-fake" in str(label)
                            for label in (run.get("model_usage") or {}).get(
                                "models", {})))
        serialised = json.dumps(run, default=str)
        self.assertNotIn("test-key-not-real", serialised)
        # The env-var name is allowed; the value is never stored.
        self.assertNotIn("test-key-not-real", serialised)

    def test_slice_plan_is_injected_and_missing_audit_is_unverified(self):
        _text, run, _path = self._run({
            "builder_provider": "fake-provider",
            "builder_model": "kimi-fake",
            "slice_models": {"api": "fake-provider/kimi-fake"},
        })
        self.assertIn("## Model plan (connector)", run["harness_task"])
        self.assertIn("slice api: fake-provider/kimi-fake",
                      run["harness_task"])
        self.assertEqual(run["model_plan"]["status"], "unverified")
        self.assertTrue(run["degraded"])
        self.assertFalse(any(item.get("verified") for item in
                             run["builder_models"] if item.get("role") == "slice"))

    def test_default_target_adds_no_patch_argument(self):
        _text, run, _path = self._run()
        argv = json.loads(self.argv.read_text(encoding="utf-8"))
        self.assertNotIn("--patch", argv)
        self.assertEqual(run["builder_model"]["provider"], "deepseek-official")


class BackupCeiling(unittest.TestCase):
    def test_own_ceiling_and_unknown_cost(self):
        cfg = load_config(config_path="/nonexistent/config.json", env={
            "DEEPSEEK_MCP_CONFIG": "/nonexistent/config.json"})
        cfg._values["backup_builder_model"] = "kimi-fake"
        self.assertEqual(builder_backup.effective_ceiling(1.0, 2.0), 1.0)
        self.assertEqual(builder_backup.effective_ceiling(1.0, 0.25), 0.25)
        self.assertEqual(builder_backup.effective_ceiling(0.0, 2.0), 0.0)
        self.assertIsNone(builder_backup.effective_ceiling(
            1.0, 2.0, cost_known=False))


class BackupBuilderEndToEnd(unittest.TestCase):
    def test_backup_switches_after_stream_drop_and_header_is_exact(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        ws = root / "workspace"
        ws.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        env.update({
            "FAKE_DSH_MODE": "flaky",
            "FAKE_DSH_SESSIONS": str(root / "sessions"),
            "DSH_TRANSIENT_RETRY": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
        })
        cfg = load_config(config_path=root / "config.json", env=env)
        cfg._values["providers"] = [{
            "name": "fake-provider", "vendor": "Kimi (Moonshot)",
            "api_key_env": "FAKE_MODEL_KEY",
            "models": [{"id": "kimi-fake"}],
        }]
        cfg._values["backup_builder_model"] = "kimi-fake"
        cfg._values["backup_builder_provider"] = "fake-provider"
        cfg._values["backup_builder_min_attempts"] = 1
        cfg._values["model_prices"] = {
            "kimi-fake": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}
        os.environ["FAKE_MODEL_KEY"] = "test-key-not-real"
        self.addCleanup(os.environ.pop, "FAKE_MODEL_KEY", None)
        with mock.patch.dict(os.environ, env, clear=False):
            text, run, _path = harness.run_harness({
                "prompt": "Write one note.", "subagents": 0,
                "quality": False, "workspace": str(ws)}, cfg)
        self.assertEqual(run["status"], "ok")
        record = run["backup_builder"]
        self.assertTrue(record["used"])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["reason_class"], "stream dropped")
        self.assertEqual(record["vendor"], "Kimi (Moonshot)")
        self.assertEqual(record["model"], "kimi-fake")
        self.assertEqual(record["settlement"]["cost_known"], True)
        self.assertEqual(
            record["line"],
            "Built by: backup (Kimi (Moonshot)/kimi-fake) — main failed: "
            "stream dropped")
        self.assertIn(
            " | backup builder: Kimi (Moonshot)/kimi-fake "
            "(main failed: stream dropped)", text.splitlines()[0])
        self.assertNotIn("test-key-not-real", json.dumps(run, default=str))



if __name__ == "__main__":
    unittest.main()
