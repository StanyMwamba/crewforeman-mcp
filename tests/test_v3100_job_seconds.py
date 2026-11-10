"""3.11.0 S3: explicit job_max_seconds and backup reviewer config keys.

Covers the reported 7200-s clamp bug and the backup reviewer defaults.
Stdlib unittest; temp dirs only, no dsh process and no network.
"""

import json
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import authority
from deepseek_mcp.config import (DEFAULTS, ENV_MAP, PATH_KEYS,
                                 SERVER_VERSION, Config, load_config)


class JobMaxSecondsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.config_path = self.root / "config.json"

    def cfg(self):
        return load_config(config_path=self.config_path, env={})

    def direct(self, cfg, **args):
        args.setdefault("workspace", str(self.workspace))
        return authority.direct(args, cfg)

    # -- explicit-key tracking -----------------------------------------
    def test_load_config_default_is_not_explicit(self):
        cfg = self.cfg()
        self.assertFalse(cfg.is_explicit("job_max_seconds"))
        self.assertEqual(cfg.job_max_seconds, 7200.0)

    def test_config_file_marks_job_max_seconds_explicit(self):
        self.config_path.write_text(json.dumps({"job_max_seconds": 900}),
                                    encoding="utf-8")
        cfg = self.cfg()
        self.assertTrue(cfg.is_explicit("job_max_seconds"))
        self.assertEqual(cfg.job_max_seconds, 900.0)

    def test_environment_marks_job_max_seconds_explicit(self):
        cfg = load_config(config_path=self.config_path,
                          env={"DSH_JOB_MAX_SECONDS": "1800"})
        self.assertTrue(cfg.is_explicit("job_max_seconds"))
        self.assertEqual(cfg.job_max_seconds, 1800.0)

    def test_direct_mapping_is_explicit_and_does_not_leak(self):
        cfg = Config({"job_max_seconds": 600})
        self.assertTrue(cfg.is_explicit("job_max_seconds"))
        self.assertFalse(cfg.is_explicit("job_max_cost_usd"))
        self.assertNotIn("_explicit", cfg.as_dict())
        self.assertFalse(Config().is_explicit("job_max_seconds"))

    # -- direct() -------------------------------------------------------
    def test_direct_caller_not_clamped_by_default(self):
        env = self.direct(self.cfg(), job_max_seconds=10800)
        self.assertEqual(float(env.job_max_seconds), 10800.0)

    def test_direct_caller_clamped_by_explicit_config(self):
        cfg = self.cfg()
        cfg = Config(dict(cfg.as_dict(), job_max_seconds=7200.0))
        self.assertTrue(cfg.is_explicit("job_max_seconds"))
        env = self.direct(cfg, job_max_seconds=10800)
        self.assertEqual(float(env.job_max_seconds), 7200.0)

    def test_direct_caller_under_explicit_lower_value(self):
        cfg = Config({"job_max_seconds": 600})
        env = self.direct(cfg, job_max_seconds=10800)
        self.assertEqual(float(env.job_max_seconds), 600.0)

    # -- revalidate -----------------------------------------------------
    def test_revalidate_keeps_saved_seconds_under_default(self):
        env = self.direct(Config({"job_max_seconds": 10800}))
        self.assertEqual(float(env.job_max_seconds), 10800.0)
        effective, drift = env.revalidate(self.cfg())
        self.assertEqual(float(effective.job_max_seconds), 10800.0)
        self.assertNotIn("job_max_seconds", drift["changed"])

    def test_revalidate_explicit_lower_caps_saved_seconds(self):
        env = self.direct(Config({"job_max_seconds": 10800}))
        effective, drift = env.revalidate(Config({"job_max_seconds": 600}))
        self.assertEqual(float(effective.job_max_seconds), 600.0)
        self.assertIn("job_max_seconds", drift["changed"])

    # -- backup keys ----------------------------------------------------
    def test_backup_defaults_and_env_aliases(self):
        cfg = self.cfg()
        self.assertEqual(cfg.backup_dsh_bin, "")
        self.assertEqual(cfg.backup_reviewer_vendor, "")
        self.assertEqual(cfg.backup_reviewer_model, "")
        self.assertIn("backup_dsh_bin", PATH_KEYS)
        self.assertIn("backup_dsh_bin", DEFAULTS)
        self.assertEqual(ENV_MAP["DSH_BACKUP_BIN"], "backup_dsh_bin")
        self.assertEqual(ENV_MAP["DEEPSEEK_BACKUP_BIN"], "backup_dsh_bin")
        self.assertEqual(ENV_MAP["DSH_BACKUP_REVIEWER_VENDOR"],
                         "backup_reviewer_vendor")
        self.assertEqual(ENV_MAP["DSH_BACKUP_REVIEWER_MODEL"],
                         "backup_reviewer_model")

    def test_backup_path_expansion_and_env(self):
        self.config_path.write_text(
            json.dumps({"backup_dsh_bin": "~/bin/backup-dsh"}), encoding="utf-8")
        cfg = self.cfg()
        self.assertFalse(cfg.backup_dsh_bin.startswith("~"))
        self.assertEqual(Path(cfg.backup_dsh_bin).parts[-2:],
                         ("bin", "backup-dsh"))
        self.assertTrue(cfg.is_explicit("backup_dsh_bin"))

        cfg = load_config(config_path=self.config_path, env={
            "DEEPSEEK_BACKUP_BIN": "~/bin/other-dsh",
            "DEEPSEEK_BACKUP_REVIEWER_VENDOR": "examplevendor",
            "DSH_BACKUP_REVIEWER_MODEL": "examplevendor-large",
        })
        self.assertEqual(Path(cfg.backup_dsh_bin).parts[-2:],
                         ("bin", "other-dsh"))
        self.assertEqual(cfg.backup_reviewer_vendor, "examplevendor")
        self.assertEqual(cfg.backup_reviewer_model, "examplevendor-large")

    def test_release_sanity(self):
        # This slice must not bump the version; the lead owns 3.12.0.
        self.assertEqual(SERVER_VERSION, "3.14.1")
        self.assertEqual(DEFAULTS["job_max_seconds"], 7200.0)


if __name__ == "__main__":
    unittest.main()
