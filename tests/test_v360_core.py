"""v3.6.0 core contract tests (pure fakes, no network, no host dependency)."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepseek_mcp import budget, clients, platforms, retention, scaling, status


class StatusTests(unittest.TestCase):
    def test_accepted_above_gate_is_ok(self):
        self.assertEqual(status.final_status("ok", "accepted", 9.0, 0)[0], "ok")

    def test_below_gate_is_not_ok(self):
        self.assertEqual(status.final_status("ok", "accepted", 7.0, 0)[0], "ok_below_gate")

    def test_blocking_defects_are_not_ok(self):
        self.assertEqual(status.final_status("ok", "accepted", 9.5, 1)[0], "ok_below_gate")

    def test_review_error_is_unreviewed(self):
        self.assertEqual(status.final_status("ok", "error", None, 0)[0], "ok_unreviewed")

    def test_stop_reasons(self):
        self.assertEqual(status.final_status("ok", "disabled", None, 0,
                                             stop_reason="deadline")[0], "deadline_exceeded")
        self.assertEqual(status.final_status("ok", "disabled", None, 0,
                                             stop_reason="budget")[0], "budget_exceeded")
        self.assertEqual(status.final_status("ok", "disabled", None, 0,
                                             cancelled=True)[0], "cancelled")

    def test_old_fields_are_kept(self):
        code, detail, passed = status.final_status("ok", "accepted", 9.0, 0)
        self.assertIsInstance(code, str)
        self.assertIsInstance(detail, str)
        self.assertTrue(passed)


class ScalingTests(unittest.TestCase):
    BRIEF = "Build a status page. Required facts: uptime, incidents, owner."

    def strong(self):
        return [{"name": "a", "answer": "Uptime incidents owner status page " * 30,
                 "files": ["index.html"], "review_score": 9.0}]

    def weak(self):
        return [{"name": "a", "answer": "todo", "files": [], "review_score": 2.0}]

    def test_weak_is_detected(self):
        self.assertTrue(scaling.score_candidates(self.weak(), self.BRIEF)["weak"])
        self.assertFalse(scaling.score_candidates(self.strong(), self.BRIEF)["weak"])

    def test_weak_wave_adds_one(self):
        decision = scaling.plan_extra_wave(1, 2, self.weak(), self.BRIEF,
                                           elapsed_seconds=5, deadline_seconds=1000)
        self.assertTrue(decision["add"])
        self.assertEqual(decision["wave"], 2)
        self.assertTrue(decision["approaches"])

    def test_strong_wave_adds_none(self):
        decision = scaling.plan_extra_wave(1, 2, self.strong(), self.BRIEF,
                                           elapsed_seconds=5, deadline_seconds=1000)
        self.assertFalse(decision["add"])

    def test_max_extra_waves_respected(self):
        decision = scaling.plan_extra_wave(2, 1, self.weak(), self.BRIEF,
                                           elapsed_seconds=5, deadline_seconds=1000)
        self.assertFalse(decision["add"])
        self.assertIn("limit", decision["reason"])

    def test_time_exhausted_adds_none(self):
        decision = scaling.plan_extra_wave(1, 2, self.weak(), self.BRIEF,
                                           elapsed_seconds=950, deadline_seconds=1000)
        self.assertFalse(decision["add"])

    def test_budget_exhausted_adds_none(self):
        decision = scaling.plan_extra_wave(1, 2, self.weak(), self.BRIEF,
                                           elapsed_seconds=5, deadline_seconds=1000,
                                           cost_usd=1.0, budget_usd=1.0)
        self.assertFalse(decision["add"])

    def test_unknown_cost_with_budget_adds_none(self):
        decision = scaling.plan_extra_wave(1, 2, self.weak(), self.BRIEF,
                                           elapsed_seconds=5, deadline_seconds=1000,
                                           cost_usd=None, budget_usd=1.0,
                                           cost_known=False)
        self.assertFalse(decision["add"])
        self.assertIn("unknown", decision["reason"].lower())
        self.assertFalse(decision["budget"]["cost_known"])
        # Without a ceiling, unknown cost does not block a weak wave.
        free = scaling.plan_extra_wave(1, 2, self.weak(), self.BRIEF,
                                       elapsed_seconds=5, deadline_seconds=1000,
                                       cost_usd=None, cost_known=False)
        self.assertTrue(free["add"])


class RetentionSafetyTests(unittest.TestCase):
    """A partial/empty config must never scan "None" or the current directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "old.json").write_text('{"old": true}', encoding="utf-8")
        (self.root / "None").mkdir()
        (self.root / "None" / "old.json").write_text("{}", encoding="utf-8")
        self.old_cwd = os.getcwd()
        os.chdir(str(self.root))
        self.addCleanup(os.chdir, self.old_cwd)

    def _cleanup(self, **overrides):
        fields = dict(retention_days=1, keep_runs=0, keep_jobs=0)
        fields.update(overrides)
        cfg = SimpleNamespace(**fields)
        return retention.cleanup(cfg, now=time.time() + 10 * 86400)

    def test_partial_config_creates_no_none_or_cwd_root(self):
        result = self._cleanup()
        self.assertEqual(result["roots"], {})
        self.assertEqual(result["deleted"], 0)
        self.assertTrue((self.root / "old.json").exists())
        self.assertTrue((self.root / "None" / "old.json").exists())

    def test_empty_directory_values_do_not_scan_the_cwd(self):
        result = self._cleanup(log_dir="", jobs_dir="", state_dir="")
        self.assertEqual(result["roots"], {})
        self.assertEqual(result["deleted"], 0)
        self.assertTrue((self.root / "old.json").exists())
        self.assertTrue((self.root / "None" / "old.json").exists())

    def test_explicit_none_directory_values_are_skipped(self):
        result = self._cleanup(log_dir=None, jobs_dir=None, state_dir=None)
        self.assertEqual(result["roots"], {})
        self.assertEqual(result["deleted"], 0)
        self.assertTrue((self.root / "old.json").exists())

    def test_secure_tree_empty_path_does_not_chmod_the_cwd(self):
        sub = self.root / "sub"
        sub.mkdir()
        target = self.root / "data.txt"
        target.write_text("x", encoding="utf-8")
        self.assertEqual(retention.secure_tree(""), (0, 0))
        self.assertEqual(retention.secure_tree(None), (0, 0))
        if os.name != "nt":
            # POSIX modes are the regression signal; Windows has no POSIX mode.
            os.chmod(str(self.root), 0o755)
            os.chmod(str(sub), 0o755)
            os.chmod(str(target), 0o644)
            self.assertEqual(stat.S_IMODE(os.stat(str(self.root)).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(str(sub)).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(str(target)).st_mode), 0o644)


class BudgetTests(unittest.TestCase):
    def test_whole_job_clock(self):
        now = [100.0]
        job = budget.JobBudget(max_seconds=60, clock=lambda: now[0])
        self.assertEqual(job.remaining_seconds(), 60.0)
        now[0] = 161.0
        self.assertTrue(job.time_exceeded())
        self.assertEqual(job.phase_limit(300), 0.0)

    def test_phase_limit_caps_to_remaining(self):
        now = [0.0]
        job = budget.JobBudget(max_seconds=100, clock=lambda: now[0])
        now[0] = 40.0
        self.assertEqual(job.phase_limit(300), 60.0)

    def test_unpriced_cost_exceeds(self):
        job = budget.JobBudget(max_cost_usd=5, clock=lambda: 0.0)
        self.assertTrue(job.cost_exceeded(None, cost_known=False))
        self.assertFalse(job.cost_exceeded(1.0))


if __name__ == "__main__":
    unittest.main()
