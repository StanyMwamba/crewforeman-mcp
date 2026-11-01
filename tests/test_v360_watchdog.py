"""v3.6.0: absolute watchdog cap, hard ceiling and phase multipliers."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import config, harness
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh


class CapExtensionTests(unittest.TestCase):
    def test_progress_within_grace_extends_the_cap_twice(self):
        now = [0.0]
        watch = harness._StallWatch(
            None, clock=lambda: now[0], max_seconds=10.0,
            grace_seconds=5.0, extension_seconds=100.0,
            hard_max_seconds=1000.0)
        now[0] = 11.0
        watch.touch("workspace", "files changed")
        self.assertIsNone(watch.poll())
        self.assertEqual(watch.cap_extensions(), 1)
        now[0] = 312.0
        watch.touch("workspace", "files changed again")
        self.assertIsNone(watch.poll())
        self.assertEqual(watch.cap_extensions(), 2)
        self.assertFalse(watch.cap_hit())
        self.assertEqual(watch.poll(), None)

    def test_constant_progress_is_stopped_at_the_hard_ceiling(self):
        now = [0.0]
        watch = harness._StallWatch(
            None, clock=lambda: now[0], max_seconds=10.0,
            grace_seconds=5.0, extension_seconds=100.0,
            hard_max_seconds=250.0)
        # Each poll finds recent progress and extends, but never past 250 s.
        for moment in (11.0, 112.0, 213.0):
            now[0] = moment
            watch.touch("child", "still growing")
            self.assertIsNone(watch.poll())
        self.assertFalse(watch.cap_hit())
        self.assertEqual(watch.cap_extensions(), 3)
        # At the ceiling recent progress no longer buys an extension.
        now[0] = 250.0
        watch.touch("child", "grown at the ceiling")
        self.assertEqual(watch.poll(), "cap")
        self.assertTrue(watch.cap_hit())
        self.assertTrue(watch.cap_hard())
        self.assertEqual(watch.elapsed(), 250.0)
        text = watch.error("builder", child_events=4)
        self.assertIn("absolute watchdog cap", text)
        self.assertIn("hard ceiling", text)
        self.assertIn("extensions: 3", text)

    def test_silent_phase_dies_at_stall_seconds_before_the_cap(self):
        now = [0.0]
        watch = harness._StallWatch(
            10.0, clock=lambda: now[0], max_seconds=10000.0,
            grace_seconds=120.0, extension_seconds=300.0,
            hard_max_seconds=7200.0)
        now[0] = 10.0
        self.assertEqual(watch.poll(), "stall")
        self.assertFalse(watch.cap_hit())
        self.assertEqual(watch.elapsed(), 10.0)
        self.assertIn("stall", watch.error("builder"))
        self.assertNotIn("absolute watchdog cap", watch.error("builder"))


class PhaseScalingTests(unittest.TestCase):
    def _cfg(self, **overrides):
        values = dict(stall_max_seconds=1000.0,
                      cap_reviewer_multiplier=1.5,
                      cap_revision_multiplier=1.25,
                      cap_finisher_multiplier=0.5)
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_per_phase_multipliers_and_floor(self):
        cfg = self._cfg()
        self.assertEqual(harness._stall_max_seconds(cfg), 1000.0)
        self.assertEqual(harness._stall_max_seconds(cfg, "reviewer"), 1500.0)
        self.assertEqual(harness._stall_max_seconds(cfg, "revision"), 1250.0)
        self.assertEqual(harness._stall_max_seconds(cfg, "finisher"), 500.0)
        self.assertEqual(harness._stall_max_seconds(
            self._cfg(stall_max_seconds=10.0), "reviewer"), 60.0)

    def test_non_positive_multiplier_disables_the_cap(self):
        self.assertIsNone(harness._stall_max_seconds(
            self._cfg(cap_reviewer_multiplier=0), "reviewer"))
        self.assertIsNone(harness._stall_max_seconds(
            self._cfg(cap_revision_multiplier=-1), "revision"))
        self.assertIsNone(harness._stall_max_seconds(
            self._cfg(cap_finisher_multiplier=0), "finisher"))

    def test_hard_ceiling_default_and_override(self):
        self.assertEqual(config.DEFAULTS["stall_cap_hard_max_seconds"], 7200.0)
        self.assertEqual(harness._stall_cap_hard_max(self._cfg()), 7200.0)
        self.assertEqual(harness._stall_cap_hard_max(
            self._cfg(stall_cap_hard_max_seconds=900)), 900.0)
        self.assertIsNone(harness._stall_cap_hard_max(
            self._cfg(stall_cap_hard_max_seconds=0)))


class WatchdogIntegrationTests(unittest.TestCase):
    """A ticking child survives a tiny cap; a sleeping one stalls on silence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.sessions)
        self.env["FAKE_DSH_SESSIONS"] = str(self.sessions)

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def _run(self, **extra):
        env = dict(os.environ)
        env.update(self.env)
        env.update(extra)
        args = {"prompt": "Do a small task", "subagents": 0,
                "quality": False, "workspace": str(self.workspace)}
        with mock.patch.dict(os.environ, env, clear=False):
            return harness.run_harness(args, self.cfg(**extra))

    def test_ticking_child_survives_a_small_progress_gated_cap(self):
        with mock.patch.object(harness, "_stall_max_seconds", return_value=1.2):
            _text, run, _path = self._run(
                FAKE_DSH_MODE="tick",
                FAKE_DSH_TICK_SECONDS="3.0",
                FAKE_DSH_TICK_INTERVAL="0.15",
                DSH_CHILD_PROGRESS="1",
                DSH_STALL_SECONDS="0.8")
        self.assertEqual(run["status"], "ok")
        self.assertFalse(run["builder"]["stall_cap_hit"])
        self.assertGreaterEqual(run["builder"]["stall_cap_extensions"], 1)

    def test_sleeping_child_is_killed_by_stall_before_the_huge_cap(self):
        started = time.time()
        with self.assertRaises(RuntimeError) as caught:
            self._run(
                FAKE_DSH_MODE="sleep",
                FAKE_DSH_SLEEP="6",
                DSH_CHILD_PROGRESS="0",
                DSH_STALL_SECONDS="0.8",
                DSH_STALL_MAX_SECONDS="1000")
        text = str(caught.exception).lower()
        self.assertIn("stall", text)
        self.assertNotIn("absolute watchdog cap", text)
        self.assertLess(time.time() - started, 4.5)


if __name__ == "__main__":
    unittest.main()
