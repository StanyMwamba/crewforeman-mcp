"""v3.10.0 S4: resumes for a review cut off by the whole-job deadline.

A review phase that actually started (failed/interrupted/running, or a usable
partial) gets a fresh review window on resume even when the saved whole-job
clock is exhausted.  A still-pending review must keep the old contract: no paid
phase runs behind an expired deadline (R5b).
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import checkpoints, harness
from deepseek_mcp.config import load_config
from tests import fakes


COUNT_WRAPPER = r'''#!/usr/bin/env python3
import json
import os
import sys

task = sys.argv[-1] if len(sys.argv) > 1 else ""
first = task.splitlines()[0] if task else ""
with open(os.environ["COUNT_OUT"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"first": first, "chars": len(task)}) + "\n")
fake = os.environ["COUNT_FAKE_DSH"]
os.execv(fake, [fake] + sys.argv[1:])
'''


def _write_script(path, body):
    return fakes.write_fake_script(path, body)


class ResumePrefixTests(unittest.TestCase):
    def test_failed_review_is_not_a_consumed_round(self):
        prior = {1: {"round": 1, "kind": "review", "status": "error",
                     "score": None, "blocking_defects": 0}}
        self.assertEqual(
            harness._review_resume_start(2, prior, {}),
            (1, None, "disabled"),
            "a failed review must be re-run, not reused as a consumed round")


class ReviewResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = fakes.write_fake_dsh(self.root / "fake-dsh")
        self.env = fakes.base_env(self.root, fake_dsh=self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, dsh):
        env = dict(self.env)
        env["DSH_BIN"] = str(dsh)
        return load_config(config_path=self.root / "config.json", env=env)

    def _checkpoint(self, run_id, plan, spec):
        checkpoint = checkpoints.Checkpoint(
            self.root / "state" / "checkpoints", run_id)
        checkpoint.create(plan, metadata={"spec": spec})
        return checkpoint

    def _write_run(self, run_id, spec, status="running"):
        runs_dir = self.root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": run_id,
            "kind": "harness",
            "prompt": spec["prompt"],
            "workspace": spec["workspace"],
            "title": spec["title"],
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
            "status": status,
            "created": "2026-01-01T00:00:00+00:00",
            "output_files": ["deliverable.md"],
            # The whole-job clock already ran far past the 1-second ceiling.
            "job_time_seconds": 10.0,
        }
        (runs_dir / (run_id + ".json")).write_text(
            json.dumps(record), encoding="utf-8")
        return runs_dir / (run_id + ".json")

    def _spec(self, prompt):
        return {
            "prompt": prompt,
            "workspace": str(self.workspace),
            "title": prompt,
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
            "job_max_seconds": 1.0,
        }

    def _calls(self, counter):
        if not counter.is_file():
            return []
        return [json.loads(line) for line in
                counter.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _counted_env(self, counter, **extra):
        wrapper = _write_script(self.root / "count-wrapper", COUNT_WRAPPER)
        env = dict(self.env)
        env.update({"COUNT_FAKE_DSH": str(self.fake),
                    "COUNT_OUT": str(counter),
                    "DSH_BIN": str(wrapper)})
        env.update(extra)
        return wrapper, env

    def test_failed_review_after_deadline_gets_fresh_window(self):
        run_id = "review-deadline-reset-1"
        spec = self._spec("S4 deadline review resume")
        checkpoint = self._checkpoint(run_id, ["builder", "review-1"], spec)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=1.0)
        # The phase began and was cut off by the whole-job deadline; it never
        # produced a usable review round.
        checkpoint.finish("review-1", status="failed", seconds=0.2,
                          error="whole-job deadline reached")
        self._write_run(run_id, spec)

        counter = self.root / "calls.jsonl"
        wrapper, env = self._counted_env(
            counter, FAKE_DSH_MODE="ok", FAKE_DSH_REVIEW_SCORE="9.0")
        cfg = self._cfg(wrapper)
        real_monotonic = time.monotonic
        # Keep the 1-second whole-job ceiling but scale the test's monotonic
        # clock so reviewer startup overhead cannot itself cross the deadline;
        # the exhausted saved clock (10.0 s) still drove the reset above.
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("time.monotonic",
                           side_effect=lambda: real_monotonic() * 0.05):
            _text, run, _path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)

        self.assertTrue(run.get("review_deadline_reset") is True)
        self.assertEqual(run.get("resumed_from"), run_id)
        self.assertEqual(run.get("review_status"), "accepted")
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("quality_score"), 9.0)
        reviews = [item for item in run.get("review_rounds") or []
                   if item.get("kind") == "review"]
        self.assertEqual(len(reviews), 1, run.get("review_rounds"))
        self.assertEqual(reviews[0].get("status"), "ok")
        self.assertEqual(reviews[0].get("score"), 9.0)
        calls = self._calls(counter)
        self.assertEqual(len(calls), 1,
                         "resume launched the wrong number of DSH phases: %r"
                         % (calls,))
        self.assertIn("INDEPENDENT REVIEWER", calls[0].get("first") or "",
                      "the resumed paid phase was not the reviewer: %r" % (calls,))

    def test_pending_review_after_deadline_launches_nothing(self):
        run_id = "review-deadline-pending-1"
        spec = self._spec("S4 pending review resume")
        checkpoint = self._checkpoint(run_id, ["builder", "review-1"], spec)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=1.0)
        # review-1 has no phase entry: it never started, so the expired
        # whole-job deadline must still refuse it (R5b contract).
        self._write_run(run_id, spec)

        counter = self.root / "calls.jsonl"
        wrapper, env = self._counted_env(
            counter, FAKE_DSH_MODE="ok", FAKE_DSH_REVIEW_SCORE="9.0")
        cfg = self._cfg(wrapper)
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, _path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)

        self.assertEqual(run.get("status"), "deadline_exceeded")
        self.assertEqual(run.get("stop_reason"), "deadline")
        self.assertEqual(run.get("review_status"), "stopped")
        self.assertFalse(run.get("review_deadline_reset"))
        self.assertEqual(self._calls(counter), [],
                         "a pending review launched a paid phase after the "
                         "whole-job deadline")


if __name__ == "__main__":
    unittest.main()
