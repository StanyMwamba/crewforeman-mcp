"""FIX D: configurable phase absolute cap, disabled for unlimited jobs.

The 3.14.3 unlimited-job fix must not re-introduce a 7200 s ceiling for a
phase whose whole-job limit is None/0.  ``phase_absolute_cap_seconds`` is the
new key; ``stall_cap_hard_max_seconds`` remains a fallback alias.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import config, harness, safety
from deepseek_mcp.config import Config, load_config
from tests.fakes import base_env, write_fake_dsh


class AbsoluteCapConfigTests(unittest.TestCase):
    def test_default_is_7200_and_legacy_alias_is_kept(self):
        self.assertEqual(config.DEFAULTS["phase_absolute_cap_seconds"], 7200.0)
        self.assertEqual(config.DEFAULTS["stall_cap_hard_max_seconds"], 7200.0)
        self.assertEqual(harness._stall_cap_hard_max(SimpleNamespace()), 7200.0)

    def test_legacy_key_is_used_when_new_key_is_not_explicit(self):
        legacy = Config({
            "phase_absolute_cap_seconds": 7200.0,
            "stall_cap_hard_max_seconds": 900.0,
        }, explicit={"stall_cap_hard_max_seconds"})
        self.assertEqual(harness._stall_cap_hard_max(legacy), 900.0)

    def test_explicit_new_key_wins_over_legacy(self):
        cfg = Config({
            "phase_absolute_cap_seconds": 600.0,
            "stall_cap_hard_max_seconds": 900.0,
        }, explicit={"phase_absolute_cap_seconds",
                     "stall_cap_hard_max_seconds"})
        self.assertEqual(harness._stall_cap_hard_max(cfg), 600.0)

    def test_explicit_zero_new_key_disables_the_ceiling(self):
        cfg = Config({
            "phase_absolute_cap_seconds": 0,
            "stall_cap_hard_max_seconds": 900.0,
        }, explicit={"phase_absolute_cap_seconds",
                     "stall_cap_hard_max_seconds"})
        self.assertIsNone(harness._stall_cap_hard_max(cfg))

    def test_simple_namespace_with_only_the_legacy_key(self):
        cfg = SimpleNamespace(stall_cap_hard_max_seconds=900.0)
        self.assertEqual(harness._stall_cap_hard_max(cfg), 900.0)
        self.assertIsNone(harness._stall_cap_hard_max(
            SimpleNamespace(stall_cap_hard_max_seconds=0)))

    def test_env_new_key_and_legacy_fallback(self):
        legacy = load_config(
            config_path=None, include_file=False,
            env={"DSH_STALL_CAP_HARD_MAX_SECONDS": "900"})
        self.assertFalse(legacy.is_explicit("phase_absolute_cap_seconds"))
        self.assertEqual(harness._stall_cap_hard_max(legacy), 900.0)
        both = load_config(
            config_path=None, include_file=False,
            env={"DSH_STALL_CAP_HARD_MAX_SECONDS": "900",
                 "DSH_PHASE_ABSOLUTE_CAP_SECONDS": "600"})
        self.assertEqual(harness._stall_cap_hard_max(both), 600.0)

    def test_new_key_accepts_null_and_zero(self):
        for value in (None, 0):
            cfg = Config({"phase_absolute_cap_seconds": value},
                         explicit={"phase_absolute_cap_seconds"})
            self.assertIsNone(harness._stall_cap_hard_max(cfg))


class HardMaxForLimitTests(unittest.TestCase):
    def _cfg(self, phase_cap=7200.0, legacy=7200.0):
        return SimpleNamespace(phase_absolute_cap_seconds=phase_cap,
                               stall_cap_hard_max_seconds=legacy)

    def test_unlimited_job_has_no_absolute_cap(self):
        cfg = self._cfg()
        self.assertIsNone(harness._stall_hard_max_for_limit(cfg, None, 1800.0))
        self.assertIsNone(harness._stall_hard_max_for_limit(cfg, 0, 1800.0))

    def test_zero_or_none_phase_cap_disables_the_absolute_cap(self):
        self.assertIsNone(harness._stall_hard_max_for_limit(
            self._cfg(phase_cap=0), 7200.0, 1800.0))
        self.assertIsNone(harness._stall_hard_max_for_limit(
            SimpleNamespace(phase_absolute_cap_seconds=None), 7200.0, 1800.0))

    def test_positive_job_keeps_the_configured_cap(self):
        self.assertEqual(harness._stall_hard_max_for_limit(
            self._cfg(phase_cap=900.0), 30.0, 1800.0), 900.0)
        self.assertEqual(harness._stall_hard_max_for_limit(
            SimpleNamespace(stall_cap_hard_max_seconds=900.0),
            30.0, 1800.0), 900.0)

    def test_whole_job_limit_controls_the_unlimited_decision(self):
        cfg = self._cfg()
        self.assertIsNone(harness._stall_hard_max_for_limit(
            cfg, 9000.0, 1800.0, whole_job_limit=0))
        self.assertIsNone(harness._stall_hard_max_for_limit(
            cfg, 9000.0, 1800.0, whole_job_limit=None))
        self.assertEqual(harness._stall_hard_max_for_limit(
            cfg, 9000.0, 1800.0, whole_job_limit=7200.0), 7200.0)
        # Direct callers that omit the argument keep the limit fallback.
        self.assertEqual(harness._stall_hard_max_for_limit(
            cfg, 9000.0, 1800.0), 7200.0)


class ProgressAwareCeilingTests(unittest.TestCase):
    def test_unlimited_job_active_progress_at_7199_and_7201_is_not_capped(self):
        cfg = SimpleNamespace(phase_absolute_cap_seconds=7200.0)
        hard = harness._stall_hard_max_for_limit(cfg, 0, 1800.0)
        self.assertIsNone(hard)
        now = [0.0]
        watch = harness._StallWatch(
            None, clock=lambda: now[0], max_seconds=1800.0,
            grace_seconds=120.0, extension_seconds=300.0,
            hard_max_seconds=hard)
        moments = [1700.0 + 100.0 * index for index in range(54)]
        moments = [moment for moment in moments if moment < 7100.0]
        moments.extend([7199.0, 7201.0])
        for moment in moments:
            now[0] = moment
            watch.touch("workspace", "files changed")
            self.assertIsNone(watch.poll(), "capped at %.1f s" % moment)
        self.assertFalse(watch.cap_hit())
        self.assertGreater(watch.cap_extensions(), 0)

    def test_silent_phase_with_stall_disabled_fires_at_the_cap(self):
        now = [0.0]
        watch = harness._StallWatch(
            None, clock=lambda: now[0], max_seconds=10.0,
            grace_seconds=5.0, extension_seconds=100.0,
            hard_max_seconds=None)
        now[0] = 10.5
        self.assertEqual(watch.poll(), "cap")
        self.assertTrue(watch.cap_hit())
        self.assertFalse(watch.cap_hard())

    def test_active_progress_at_a_bounded_absolute_ceiling_still_fires(self):
        # For job_max_seconds > 0 the documented default is unchanged: the
        # absolute phase ceiling is unconditional even while progress flows.
        now = [0.0]
        watch = harness._StallWatch(
            None, clock=lambda: now[0], max_seconds=10.0,
            grace_seconds=5.0, extension_seconds=100.0,
            hard_max_seconds=250.0)
        now[0] = 250.0
        watch.touch("child", "still growing")
        self.assertEqual(watch.poll(), "cap")
        self.assertTrue(watch.cap_hit())
        self.assertTrue(watch.cap_hard())


class UnlimitedRunRecordTests(unittest.TestCase):
    def test_unlimited_job_ignores_an_explicit_phase_cap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        env["DSH_PHASE_ABSOLUTE_CAP_SECONDS"] = "900"
        cfg = load_config(config_path=root / "config.json", env=env)
        _text, run, _path = harness.run_harness({
            "prompt": "small task", "subagents": 0, "quality": False,
            "workspace": str(workspace), "job_max_seconds": 0,
        }, cfg)
        self.assertEqual(run["job_max_seconds"], 0)
        self.assertIsNone(
            run["builder"]["stall_cap_hard_max_seconds"],
            "an unlimited job must not apply an explicit phase ceiling")

    def test_unlimited_job_with_positive_max_seconds_records_no_cap(self):
        # A positive per-process max_seconds must not resurrect the 7200 s
        # ceiling when job_max_seconds is explicitly unlimited.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        _text, run, _path = harness.run_harness({
            "prompt": "small task", "subagents": 0, "quality": False,
            "workspace": str(workspace), "job_max_seconds": 0,
            "max_seconds": 9000,
        }, cfg)
        self.assertEqual(run["job_max_seconds"], 0)
        self.assertIsNone(
            run["builder"]["stall_cap_hard_max_seconds"],
            "job_max_seconds=0 + max_seconds=9000 re-applied the 7200 s cap")


class PositiveLimitRunRecordTests(unittest.TestCase):
    def test_positive_limit_records_the_configured_phase_cap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        env["DSH_PHASE_ABSOLUTE_CAP_SECONDS"] = "900"
        cfg = load_config(config_path=root / "config.json", env=env)
        env_for_child = dict(env)
        with mock.patch.dict(os.environ, env_for_child, clear=False):
            result = harness._execute_once_impl(
                cfg, workspace, "small task", limit=30.0,
                policy=safety.resolve({}, cfg), dsh_bin=str(dsh))
        self.assertEqual(result["stall_cap_hard_max_seconds"], 900.0)

    def test_positive_job_with_large_max_seconds_records_default_cap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        _text, run, _path = harness.run_harness({
            "prompt": "small task", "subagents": 0, "quality": False,
            "workspace": str(workspace), "job_max_seconds": 7200,
            "max_seconds": 9000,
        }, cfg)
        self.assertEqual(
            run["builder"]["stall_cap_hard_max_seconds"], 7200.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
