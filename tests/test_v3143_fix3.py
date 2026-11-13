"""FIX 3 tests: job_max_seconds=0 means "no whole-job deadline" everywhere.

Stdlib unittest only; no network; Windows-compatible paths via tempfile/pathlib.
Run with:  CF_REPO=<patched repo> python -m pytest test_candidate.py -q
       or  CF_REPO=<patched repo> python test_candidate.py -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_REPO = os.environ.get("CF_REPO") or str(_HERE.parents[2] / "repo")
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from deepseek_mcp import authority, budget, harness  # noqa: E402


class FakeCfg(dict):
    """Minimal config: mapping access plus is_explicit()."""

    def __init__(self, values=None, explicit=()):
        super().__init__(values or {})
        self._explicit = set(explicit)

    def is_explicit(self, name):
        return name in self._explicit


def make_cfg(explicit_seconds=False, **overrides):
    """Config mapping; the 7200 value is the built-in default unless marked."""
    values = {"sandbox": "workspace-write", "allowed_workspace_roots": []}
    values.update(overrides)
    explicit = (("job_max_seconds",)
                if explicit_seconds and "job_max_seconds" in overrides else ())
    return FakeCfg(values, explicit=explicit)


class ZeroJobDeadlineAuthorityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.cfg = make_cfg(job_max_seconds=7200.0)  # built-in default, not explicit

    def tearDown(self):
        self._tmp.cleanup()

    def _direct(self, cfg=None, **extra):
        args = {"workspace": self.workspace}
        args.update(extra)
        return authority.direct(args, cfg if cfg is not None else self.cfg)

    def test_direct_zero_is_preserved(self):
        env = self._direct(job_max_seconds=0)
        self.assertEqual(env.job_max_seconds, 0)
        self.assertEqual(env.to_dict()["job_max_seconds"], 0)
        self.assertEqual(harness._job_seconds_display(env.job_max_seconds),
                         "unlimited")

    def test_direct_zero_beats_builtin_default(self):
        env = self._direct(job_max_seconds=0.0)
        self.assertEqual(env.job_max_seconds, 0.0)

    def test_config_zero_is_unlimited(self):
        cfg = make_cfg(explicit_seconds=True, job_max_seconds=0)
        env = self._direct(cfg)  # no tool arg at all
        self.assertEqual(env.job_max_seconds, 0)

    def test_operator_ceiling_still_clamps_zero(self):
        cfg = make_cfg(explicit_seconds=True, job_max_seconds=3600.0)
        env = self._direct(cfg, job_max_seconds=0)
        self.assertEqual(env.job_max_seconds, 3600.0)

    def test_none_is_unchanged(self):
        env = self._direct()
        self.assertEqual(env.job_max_seconds, 7200.0)

    def test_negative_job_seconds_is_rejected(self):
        with self.assertRaises(authority.InvalidAuthority):
            self._direct(job_max_seconds=-1)

    def test_continuations_keep_zero(self):
        env = self._direct(job_max_seconds=0)
        for label in ("reviewer", "revision", "rescue"):
            with self.subTest(phase=label):
                child = env.continuation(label, self.cfg)
                self.assertEqual(child.job_max_seconds, 0)
                self.assertEqual(child.to_dict()["job_max_seconds"], 0)

    def test_narrow_zero_keeps_unlimited(self):
        env = self._direct(job_max_seconds=0)
        narrowed = env.narrow(job_max_seconds=0)
        self.assertEqual(narrowed.job_max_seconds, 0)

    def test_narrow_zero_from_bounded_is_widening(self):
        env = self._direct(job_max_seconds=1800)
        with self.assertRaises(authority.WideningError):
            env.narrow(job_max_seconds=0)

    def test_narrow_negative_is_rejected(self):
        env = self._direct(job_max_seconds=1800)
        with self.assertRaises(authority.WideningError):
            env.narrow(job_max_seconds=-1)

    def test_persisted_zero_round_trips(self):
        env = self._direct(job_max_seconds=0)
        parsed = authority.AuthorityEnvelope.from_dict(env.to_dict())
        self.assertEqual(parsed.job_max_seconds, 0)
        self.assertEqual(authority._strict_budget(
            0, "job_max_seconds", unlimited_zero=True), 0.0)
        with self.assertRaises(authority.InvalidAuthority):
            authority._strict_budget(-1, "job_max_seconds", unlimited_zero=True)

    def test_negative_config_is_rejected(self):
        from deepseek_mcp.config import load_config
        with self.assertRaises(ValueError):
            load_config(env={"DSH_JOB_MAX_SECONDS": "-1"},
                        include_file=False)

    def test_boolean_tool_value_is_rejected(self):
        with self.assertRaises(authority.InvalidAuthority):
            self._direct(job_max_seconds=True)


class ZeroJobDeadlineBudgetTest(unittest.TestCase):
    def _budget(self, seconds):
        clock = {"now": 1000.0}
        job = budget.JobBudget(seconds, None, started=1000.0,
                               clock=lambda: clock["now"])
        return job, clock

    def test_zero_job_never_exceeds_after_7200(self):
        job, clock = self._budget(0)
        clock["now"] = 1000.0 + 8000.0
        self.assertIsNone(job.remaining_seconds())
        self.assertFalse(job.time_exceeded())
        self.assertIsNone(job.phase_limit(None))
        self.assertEqual(job.phase_limit(900), 900.0)
        self.assertIsNone(job.snapshot()["max_seconds"])

    def test_builder_revision_reviewer_rescue_get_unlimited_remaining(self):
        job, clock = self._budget(0)
        clock["now"] = 1000.0 + 7500.0
        for requested in (3600.0, 4500.0, 1800.0, 900.0):
            with self.subTest(requested=requested):
                self.assertEqual(job.phase_limit(requested), requested)

    def test_none_is_unchanged(self):
        job, clock = self._budget(None)
        clock["now"] = 1000.0 + 8000.0
        self.assertIsNone(job.remaining_seconds())
        self.assertFalse(job.time_exceeded())


class ZeroJobDeadlineStallCapTest(unittest.TestCase):
    def test_unlimited_job_does_not_reintroduce_7200_stall_cap(self):
        cfg = SimpleNamespace(stall_cap_hard_max_seconds=7200.0)
        self.assertIsNone(harness._stall_hard_max_for_limit(cfg, None, 1800.0))
        self.assertIsNone(harness._stall_hard_max_for_limit(cfg, 0, 1800.0))
        self.assertEqual(
            harness._stall_hard_max_for_limit(cfg, 7200.0, 1800.0), 7200.0)

    def test_run_record_display_says_unlimited(self):
        self.assertEqual(harness._job_seconds_display(0), "unlimited")
        self.assertEqual(harness._job_seconds_display(0.0), "unlimited")
        self.assertIsNone(harness._job_seconds_display(None))
        self.assertEqual(harness._job_seconds_display(7200.0), "7200s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
