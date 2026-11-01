import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from deepseek_mcp.config import DEFAULTS, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def test_generic_defaults(self):
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(cfg.model, "deepseek-flash")
        self.assertEqual(cfg.reasoning, "max")
        self.assertEqual(cfg.subagent_soft_cap, 32)
        self.assertEqual(cfg.default_subagents, 16)
        self.assertIsNone(cfg.max_concurrent_children)
        self.assertTrue(cfg.independent_review)
        self.assertTrue(str(cfg.state_dir).endswith("DeepSeek-MCP/state"))
        self.assertEqual(cfg.quality_max_rounds, 3)
        self.assertIsNone(cfg.daily_alert_usd)
        self.assertIsNone(cfg.daily_limit_usd)
        self.assertTrue(str(cfg.log_dir).endswith("DeepSeek-MCP/runs"))

    def test_file_overrides_defaults(self):
        self.config_path.write_text(json.dumps({
            "log_dir": str(self.root / "runs-from-file"),
            "default_subagents": 20,
            "max_concurrent_children": 6,
            "independent_review": False,
            "quality": False,
            "daily_alert_usd": 2.5,
        }), encoding="utf-8")
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(str(cfg.log_dir), str(self.root / "runs-from-file"))
        self.assertEqual(cfg.default_subagents, 20)
        self.assertEqual(cfg.max_concurrent_children, 6)
        self.assertFalse(cfg.independent_review)
        self.assertFalse(cfg.quality)
        self.assertEqual(cfg.daily_alert_usd, 2.5)

    def test_environment_overrides_file(self):
        self.config_path.write_text(json.dumps({"model": "from-file", "timeout": 10}),
                                    encoding="utf-8")
        env = {
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
            "DEEPSEEK_TIMEOUT": "42.5",
            "DEEPSEEK_MCP_LOG_DIR": str(self.root / "env-runs"),
            "DSH_SUBAGENTS": "18",
            "DSH_MAX_CONCURRENT_CHILDREN": "5",
            "DSH_INDEPENDENT_REVIEW": "0",
            "DSH_QUALITY": "0",
        }
        cfg = load_config(config_path=self.config_path, env=env)
        self.assertEqual(cfg.model, "deepseek-v4-pro")
        self.assertEqual(cfg.timeout, 42.5)
        self.assertEqual(str(cfg.log_dir), str(self.root / "env-runs"))
        self.assertEqual(cfg.default_subagents, 18)
        self.assertEqual(cfg.max_concurrent_children, 5)
        self.assertFalse(cfg.independent_review)
        self.assertFalse(cfg.quality)

    def test_default_paths_are_expanded(self):
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(str(cfg.log_dir), os.path.expanduser("~/DeepSeek-MCP/runs"))
        self.assertEqual(str(cfg.jobs_dir), os.path.expanduser("~/DeepSeek-MCP/jobs"))
        self.assertEqual(str(cfg.harness_workspace),
                         os.path.expanduser("~/DeepSeek-MCP/harness-workspace"))
        self.assertEqual(str(cfg.state_dir), os.path.expanduser("~/DeepSeek-MCP/state"))
        self.assertFalse(str(cfg.log_dir).startswith("~"))

    def test_path_expansion(self):
        cfg = load_config(config_path=self.config_path, env={"DEEPSEEK_MCP_LOG_DIR": "~/my-runs"})
        self.assertEqual(str(cfg.log_dir), os.path.expanduser("~/my-runs"))

    def test_explicit_tilde_config_path_is_expanded(self):
        home = self.root / "home"
        config_dir = home / ".config" / "deepseek-harness-mcp"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({
            "stall_max_seconds": 1234.5,
            "log_dir": str(home / "runs-from-tilde"),
        }), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "USERPROFILE": str(home)}):
            cfg = load_config(
                config_path="~/.config/deepseek-harness-mcp/config.json", env={})
        self.assertEqual(cfg.stall_max_seconds, 1234.5)
        self.assertEqual(str(cfg.log_dir), str(home / "runs-from-tilde"))
        self.assertEqual(cfg._config_path, str(config_file.resolve()))
        self.assertTrue(Path(cfg._config_path).is_absolute())
        self.assertNotIn("~", cfg._config_path)

    def test_unknown_config_keys_ignored(self):
        self.config_path.write_text(json.dumps({"not_a_setting": 1, "model": "deepseek-flash"}),
                                    encoding="utf-8")
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(cfg.model, "deepseek-flash")

    def test_invalid_config_raises(self):
        self.config_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            load_config(config_path=self.config_path, env={})

    def test_defaults_are_generic(self):
        generic = ("log_dir", "jobs_dir", "harness_workspace", "dsh_sessions_dir", "pid_file")
        joined = " ".join(str(DEFAULTS[key]) for key in generic)
        self.assertNotIn("Users/", joined)
        self.assertNotIn("@", joined)
        self.assertNotIn("Library/" + "CloudStorage", joined)


if __name__ == "__main__":
    unittest.main()
