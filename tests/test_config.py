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

    def assertSamePath(self, actual, expected):
        """Compare the path spelling after separator normalisation.

        ``Path`` equality is case-insensitive on Windows, which would hide a
        product regression that lower-cases a workspace path; ``normpath``
        only repairs the separator spelling.
        """
        self.assertEqual(os.path.normpath(str(actual)),
                         os.path.normpath(str(expected)))

    def test_generic_defaults(self):
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(cfg.model, "deepseek-flash")
        self.assertEqual(cfg.reasoning, "max")
        self.assertEqual(cfg.subagent_soft_cap, 32)
        self.assertEqual(cfg.default_subagents, 16)
        self.assertIsNone(cfg.max_concurrent_children)
        self.assertTrue(cfg.independent_review)
        self.assertEqual(Path(cfg.state_dir).parts[-2:],
                         ("DeepSeek-MCP", "state"))
        self.assertEqual(cfg.quality_max_rounds, 3)
        self.assertIsNone(cfg.daily_alert_usd)
        self.assertIsNone(cfg.daily_limit_usd)
        self.assertEqual(Path(cfg.log_dir).parts[-2:],
                         ("DeepSeek-MCP", "runs"))

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
        # The product keeps the historical ~/DeepSeek-MCP root on POSIX but
        # uses %LOCALAPPDATA%\DeepSeek-MCP on Windows (config._default_root).
        if os.name == "nt":
            root = os.path.join(
                os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                "DeepSeek-MCP")
        else:
            root = os.path.expanduser("~/DeepSeek-MCP")
        self.assertSamePath(cfg.log_dir, os.path.join(root, "runs"))
        self.assertSamePath(cfg.jobs_dir, os.path.join(root, "jobs"))
        self.assertSamePath(cfg.harness_workspace,
                             os.path.join(root, "harness-workspace"))
        self.assertSamePath(cfg.state_dir, os.path.join(root, "state"))
        self.assertFalse(str(cfg.log_dir).startswith("~"))

    def test_path_expansion(self):
        cfg = load_config(config_path=self.config_path, env={"DEEPSEEK_MCP_LOG_DIR": "~/my-runs"})
        self.assertSamePath(cfg.log_dir, os.path.expanduser("~/my-runs"))

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

    def test_state_root_defaults_unchanged_without_root(self):
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(cfg.state_root, "")
        for key in ("state_dir", "log_dir", "jobs_dir", "pid_file"):
            self.assertEqual(str(getattr(cfg, key)),
                             os.path.expanduser(DEFAULTS[key]))
        self.assertSamePath(cfg.dsh_sessions_dir,
                         os.path.expanduser(DEFAULTS["dsh_sessions_dir"]))

    def test_state_root_env_relocates_four_connector_roots(self):
        root = self.root / "state-root"
        cfg = load_config(config_path=self.config_path,
                          env={"CREWFOREMAN_STATE_DIR": str(root)})
        self.assertEqual(str(cfg.state_root), str(root))
        self.assertEqual(str(cfg.state_dir), str(root / "state"))
        self.assertEqual(str(cfg.log_dir), str(root / "runs"))
        self.assertEqual(str(cfg.jobs_dir), str(root / "jobs"))
        self.assertEqual(str(cfg.pid_file), str(root / "keep_awake.pid"))
        # B1: the Harness session home is not derived from CrewForeman's root.
        self.assertSamePath(cfg.dsh_sessions_dir,
                         os.path.expanduser(DEFAULTS["dsh_sessions_dir"]))

    def test_explicit_state_env_wins_for_state_only(self):
        root = self.root / "root"
        state = self.root / "explicit-state"
        cfg = load_config(config_path=self.config_path, env={
            "CREWFOREMAN_STATE_DIR": str(root),
            "DSH_STATE_DIR": str(state),
        })
        self.assertEqual(str(cfg.state_dir), str(state))
        self.assertEqual(str(cfg.log_dir), str(root / "runs"))
        self.assertEqual(str(cfg.jobs_dir), str(root / "jobs"))
        self.assertEqual(str(cfg.pid_file), str(root / "keep_awake.pid"))
        self.assertSamePath(cfg.dsh_sessions_dir,
                         os.path.expanduser(DEFAULTS["dsh_sessions_dir"]))

    def test_config_state_root_relocates_defaults(self):
        root = self.root / "file-root"
        self.config_path.write_text(json.dumps({"state_root": str(root)}),
                                    encoding="utf-8")
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(str(cfg.state_root), str(root))
        self.assertEqual(str(cfg.state_dir), str(root / "state"))
        self.assertEqual(str(cfg.log_dir), str(root / "runs"))
        self.assertEqual(str(cfg.jobs_dir), str(root / "jobs"))
        self.assertEqual(str(cfg.pid_file), str(root / "keep_awake.pid"))
        self.assertSamePath(cfg.dsh_sessions_dir,
                         os.path.expanduser(DEFAULTS["dsh_sessions_dir"]))

    def test_env_state_root_beats_config_state_root(self):
        file_root = self.root / "file-root"
        env_root = self.root / "env-root"
        self.config_path.write_text(json.dumps({"state_root": str(file_root)}),
                                    encoding="utf-8")
        cfg = load_config(config_path=self.config_path,
                          env={"CREWFOREMAN_STATE_DIR": str(env_root)})
        self.assertEqual(str(cfg.state_root), str(env_root))
        self.assertEqual(str(cfg.state_dir), str(env_root / "state"))
        self.assertEqual(str(cfg.log_dir), str(env_root / "runs"))
        self.assertEqual(str(cfg.jobs_dir), str(env_root / "jobs"))
        self.assertSamePath(cfg.dsh_sessions_dir,
                         os.path.expanduser(DEFAULTS["dsh_sessions_dir"]))

    def test_config_file_explicit_path_beats_derived_root(self):
        root = self.root / "file-root"
        explicit = self.root / "explicit-runs"
        self.config_path.write_text(json.dumps({
            "state_root": str(root),
            "log_dir": str(explicit),
        }), encoding="utf-8")
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(str(cfg.log_dir), str(explicit))
        self.assertEqual(str(cfg.jobs_dir), str(root / "jobs"))
        self.assertEqual(str(cfg.state_dir), str(root / "state"))
        self.assertSamePath(cfg.dsh_sessions_dir,
                         os.path.expanduser(DEFAULTS["dsh_sessions_dir"]))

    def test_state_root_tilde_is_expanded(self):
        home = self.root / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "USERPROFILE": str(home)}):
            cfg = load_config(config_path=self.config_path,
                              env={"CREWFOREMAN_STATE_DIR": "~/mcp-root"})
        self.assertEqual(str(cfg.state_dir), str(home / "mcp-root" / "state"))
        self.assertEqual(str(cfg.dsh_sessions_dir),
                         str(home / ".dsh" / "sessions"))

    def test_explicit_sessions_env_wins_with_state_root(self):
        root = self.root / "root"
        sessions = self.root / "explicit-sessions"
        cfg = load_config(config_path=self.config_path, env={
            "CREWFOREMAN_STATE_DIR": str(root),
            "DSH_SESSIONS_DIR": str(sessions),
        })
        self.assertEqual(str(cfg.dsh_sessions_dir), str(sessions))
        self.assertEqual(str(cfg.state_dir), str(root / "state"))

    def test_older_sessions_alias_wins_with_state_root(self):
        root = self.root / "root"
        sessions = self.root / "older-sessions"
        cfg = load_config(config_path=self.config_path, env={
            "CREWFOREMAN_STATE_DIR": str(root),
            "DEEPSEEK_DSH_SESSIONS_DIR": str(sessions),
        })
        self.assertEqual(str(cfg.dsh_sessions_dir), str(sessions))

    def test_config_file_sessions_wins_over_derived_root(self):
        root = self.root / "root"
        sessions = self.root / "file-sessions"
        self.config_path.write_text(json.dumps({
            "state_root": str(root),
            "dsh_sessions_dir": str(sessions),
        }), encoding="utf-8")
        cfg = load_config(config_path=self.config_path, env={})
        self.assertEqual(str(cfg.dsh_sessions_dir), str(sessions))
        self.assertEqual(str(cfg.state_dir), str(root / "state"))

    def test_v3110_gate_plan_notes_pipeline_defaults_and_env(self):
        expected = {
            "gate_enabled": True,
            "gate_tests": "auto",
            "gate_test_command": "",
            "gate_lint": "auto",
            "gate_lint_command": "",
            "gate_secret_scan": True,
            "gate_secret_patterns": [],
            "gate_timeout_seconds": 300.0,
            "gate_max_revisions": 2,
            "gate_max_output_chars": 4000,
            "plan_contract": "warn",
            "plan_file": "PLAN.json",
            "plan_max_files": 200,
            "plan_max_risks": 20,
            "failure_notes": "on",
            "failure_notes_max": 3,
            "failure_notes_half_life_days": 30,
            "failure_notes_min_occurrences": 2,
            "failure_notes_max_store": 200,
            "failure_notes_store": "",
            "pipeline_enabled": True,
            "pipeline_max_extra_jobs": 1,
            "pipeline_require_disjoint": True,
            "pipeline_isolation": "worktree",
            "pipeline_worktree_root": "",
            "pipeline_patch_dir": "",
            "pipeline_install": "auto",
            "pipeline_min_remaining_usd": 1.0,
            "pipeline_max_pending_patches": 8,
            "pipeline_queue_scan": "head",
            "pipeline_patch_retention_days": 3,
        }
        cfg = load_config(config_path=self.config_path, env={})
        for key, value in expected.items():
            self.assertEqual(getattr(cfg, key), value, key)
        self.assertEqual(len(expected), 31)

        env = {
            "DSH_GATE_ENABLED": "0",
            "DSH_PIPELINE_MAX_EXTRA_JOBS": "2",
            "DSH_PLAN_CONTRACT": "block",
            "DSH_GATE_SECRET_PATTERNS": "a,b",
            "DEEPSEEK_GATE_TIMEOUT_SECONDS": "12.5",
            "DEEPSEEK_PIPELINE_INSTALL": "manual",
        }
        cfg = load_config(config_path=self.config_path, env=env)
        self.assertFalse(cfg.gate_enabled)
        self.assertEqual(cfg.pipeline_max_extra_jobs, 2)
        self.assertEqual(cfg.plan_contract, "block")
        self.assertEqual(cfg.gate_secret_patterns, ["a", "b"])
        self.assertEqual(cfg.gate_timeout_seconds, 12.5)
        self.assertEqual(cfg.pipeline_install, "manual")
        self.assertEqual(cfg.failure_notes_store, "")

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
