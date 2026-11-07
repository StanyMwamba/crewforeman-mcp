"""P31 review-terminal regression tests (pure fakes, no network)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepseek_mcp import jobs, status, work

RUN_ID = "20261106-221837-45f2a3"


class ReviewTerminalTests(unittest.TestCase):
    def _run_case(self, run):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        logs = root / "logs"
        jobs_dir = root / "jobs"
        state = root / "state"
        for directory in (logs, jobs_dir, state):
            directory.mkdir(parents=True)
        cfg = SimpleNamespace(jobs_dir=str(jobs_dir), log_dir=str(logs),
                              state_dir=str(state))
        store = work.WorkStore(state / "work")
        work_id = store.admit("p31-review-terminal-" + self.id())["work_id"]
        record = dict({"id": RUN_ID}, **run)
        (logs / (RUN_ID + ".json")).write_text(
            json.dumps(record), encoding="utf-8")
        spec_path = jobs_dir / (RUN_ID + ".job.json")
        spec_path.write_text(json.dumps({
            "run_id": RUN_ID, "tool": "fake", "work_id": work_id,
            "args": {},
        }), encoding="utf-8")
        jobs.run_job(spec_path,
                     {"fake": lambda args, cfg: "final answer"}, cfg)
        job = json.loads(
            (jobs_dir / (RUN_ID + ".status.json")).read_text(encoding="utf-8"))
        item = store.load(work_id)
        return job, item

    def test_in_flight_review_is_not_terminal(self):
        job, item = self._run_case({
            "status": "running",
            "independent_review": True,
            "review_in_flight": True,
            "phase": "review-wave1",
            "child_sessions_active": 1,
        })
        self.assertEqual(job["status"], "error")
        self.assertIn("review", job["error"].lower())
        self.assertEqual(item["state"], "finished")
        self.assertEqual(item["terminal_status"], "error")

    def test_unrecorded_review_with_success_status_is_error(self):
        job, item = self._run_case({
            "status": "ok",
            "independent_review": True,
            "phase": "review_done",
        })
        self.assertEqual(job["status"], "error")
        self.assertIn("no review round", job["error"])
        self.assertEqual(item["terminal_status"], "error")

    def test_recorded_review_failure_keeps_done(self):
        job, _item = self._run_case({
            "status": "ok_unreviewed",
            "independent_review": True,
            "review_status": "error",
            "review_rounds": [{"kind": "review", "score": 5.0}],
        })
        self.assertEqual(job["status"], "done")

    def test_old_run_without_new_fields_is_unchanged(self):
        job, item = self._run_case({"status": "ok", "quality_score": 9.0})
        self.assertEqual(job["status"], "done")
        self.assertEqual(item["terminal_status"], "ok")

    def test_error_status_review_is_not_terminal(self):
        # Review repro: the reviewer child was still writing after exit, so
        # the harness recorded a terminal run error while a review round was
        # present.  The job wrapper must not turn that into done/finished.
        job, item = self._run_case({
            "status": "error",
            "independent_review": True,
            "review_in_flight": False,
            "review_status": "error",
            "phase": "review_done",
            "review_error": "reviewer child wrote after exit",
            "review_rounds": [{"kind": "review", "round": 1,
                               "status": "error"}],
        })
        self.assertEqual(job["status"], "error")
        self.assertIn("review", job["error"].lower())
        self.assertEqual(item["state"], "finished")
        self.assertEqual(item["terminal_status"], "error")

    def test_superseded_attempt_uses_canonical_review_gate(self):
        stale_run = "20261106-221838-abcdef"
        canonical_run = "20261106-221837-123abc"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        logs = root / "logs"
        jobs_dir = root / "jobs"
        state = root / "state"
        for directory in (logs, jobs_dir, state):
            directory.mkdir(parents=True)
        cfg = SimpleNamespace(jobs_dir=str(jobs_dir), log_dir=str(logs),
                              state_dir=str(state))
        store = work.WorkStore(state / "work")
        admitted = store.admit("p31-superseded-" + self.id())
        work_id = admitted["work_id"]
        store.bind_run(work_id, admitted.get("attempt_no", 1), canonical_run)
        (logs / (canonical_run + ".json")).write_text(json.dumps({
            "id": canonical_run,
            "status": "error",
            "independent_review": True,
            "review_in_flight": False,
            "review_status": "error",
            "phase": "review_done",
            "review_error": "reviewer child wrote after exit",
            "review_rounds": [{"kind": "review", "round": 1,
                               "status": "error"}],
        }), encoding="utf-8")
        spec_path = jobs_dir / (stale_run + ".job.json")
        spec_path.write_text(json.dumps({
            "run_id": stale_run, "tool": "fake", "work_id": work_id,
            "args": {},
        }), encoding="utf-8")
        jobs.run_job(spec_path,
                     {"fake": lambda args, cfg: "stale handler answer"}, cfg)
        job = json.loads(
            (jobs_dir / (stale_run + ".status.json")).read_text(
                encoding="utf-8"))
        item = store.load(work_id)
        self.assertEqual(job["status"], "error")
        self.assertIn("review", job["error"].lower())
        self.assertEqual(item["canonical_run_id"], canonical_run)
        self.assertIsNone(item.get("terminal_status"))


class ReviewRecordedStatusTests(unittest.TestCase):
    def test_unrecorded_requested_review_is_error(self):
        code, detail, passed = status.final_status(
            "ok", "accepted", 9.5, 0, review_recorded=False)
        self.assertEqual(code, "error")
        self.assertIn("no review round", detail)
        self.assertIsNone(passed)

    def test_default_keeps_existing_contract(self):
        self.assertEqual(
            status.final_status("ok", "accepted", 9.5, 0)[0], "ok")
        self.assertEqual(
            status.final_status("ok", "disabled", None, 0,
                                review_recorded=False)[0], "ok")



class HarnessReviewInFlightTests(unittest.TestCase):
    """The run-level gate must not return a success status for a live review."""

    def test_harness_refuses_ok_when_review_phase_is_in_flight(self):
        from unittest import mock
        from deepseek_mcp import harness
        from deepseek_mcp.config import load_config
        from tests.fakes import base_env, write_fake_dsh

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({"FAKE_DSH_REVIEW_SCORES": "9.0",
                    "FAKE_DSH_REVIEW_BLOCKING": "0",
                    "DSH_QUALITY_CONVERGENCE": "observe"})
        original = harness._execute_once

        def wrapped(cfg, workspace, task, *args, **kwargs):
            result = original(cfg, workspace, task, *args, **kwargs)
            if "INDEPENDENT REVIEWER" in str(task):
                result["review_phase_in_flight"] = True
                result["child_sessions_in_flight"] = True
            return result

        args = {
            "prompt": "Build one tiny deliverable.",
            "workspace": str(ws), "sandbox": "danger",
            "quality": True, "independent_review": True,
            "quality_rounds": 1, "subagents": 1,
            "max_seconds": 30, "job_max_seconds": 120,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            with mock.patch.object(harness, "_execute_once",
                                   side_effect=wrapped):
                _text, run, _path = harness.run_harness(args, cfg)
        self.assertEqual(run.get("status"), "error")
        self.assertEqual(run.get("review_status"), "error")
        self.assertIn("in flight", (run.get("review_error") or "").lower())
        self.assertFalse(run.get("review_passed"))
        review = [item for item in run.get("review_rounds") or []
                  if item.get("kind") == "review"][0]
        self.assertEqual(review.get("status"), "error")


class ResumeReviewFinalizeTests(unittest.TestCase):
    def test_all_phases_done_with_in_flight_review_is_not_ok(self):
        from deepseek_mcp import harness
        result = harness._resume_final_status(
            {"status": "running", "independent_review": True,
             "review_in_flight": True},
            [], spec={"independent_review": True})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["review_status"], "error")
        self.assertIn("in flight", result["review_error"])

    def test_all_phases_done_without_recorded_review_is_not_ok(self):
        from deepseek_mcp import harness
        result = harness._resume_final_status(
            {"status": "running", "independent_review": True},
            [], spec={"independent_review": True})
        self.assertEqual(result["status"], "error")
        self.assertIn("no review round", result["review_error"])

    def test_recorded_accepted_review_still_finishes_ok(self):
        from deepseek_mcp import harness
        review = {"kind": "review", "round": 1, "score": 9.0,
                  "blocking_defects": 0}
        result = harness._resume_final_status(
            {"status": "running", "independent_review": True,
             "review_status": "accepted"},
            [review], spec={"independent_review": True},
            accepted_review=review)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["review_passed"])



class ResumeReviewInFlightStatusTests(unittest.TestCase):
    def test_saved_running_status_after_a_recorded_round_is_not_ok(self):
        from deepseek_mcp import harness
        prior = [{"kind": "review", "round": 1, "score": 9.0,
                  "blocking_defects": 0}]
        result = harness._resume_final_status(
            {"status": "running", "independent_review": True,
             "review_status": "running"},
            prior, spec={"independent_review": True})
        self.assertEqual(result["status"], "error")

    def test_saved_disabled_status_with_recorded_below_gate_is_not_ok(self):
        from deepseek_mcp import harness
        prior = [{"kind": "review", "round": 1, "score": 7.0,
                  "blocking_defects": 2}]
        result = harness._resume_final_status(
            {"status": "running", "independent_review": True,
             "review_status": "disabled"},
            prior, spec={"independent_review": True})
        self.assertEqual(result["status"], "ok_below_gate")
        self.assertFalse(result["review_passed"])


if __name__ == "__main__":
    unittest.main()
