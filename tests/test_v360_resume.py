"""v3.6.0 resume integration tests (stdlib unittest, fakes only).

Covers:
  R1  the durable "running" skeleton exists before the first DSH spawn;
  R2  resume from checkpoint only (no runs/<id>.json) reuses the builder;
  R3  a real SIGKILL mid-review resumes without re-running the builder;
  R4  an accepted review report is restored verbatim without any DSH process;
  R5  finished rejected review + revision are reused, the next reviewer runs;
  R5a an over-budget accepted checkpoint resumes as budget_exceeded;
  R5b a resume past the whole-job deadline starts no paid reviewer.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import checkpoints, harness
from deepseek_mcp.config import load_config
from tests import fakes


PROBE_WRAPPER = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log_dir = Path(os.environ["DEEPSEEK_MCP_LOG_DIR"])
records = []
for path in sorted(log_dir.glob("*.json")):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    records.append({"file": path.name, "status": data.get("status")})
Path(os.environ["PROBE_OUT"]).write_text(json.dumps(records), encoding="utf-8")
fake = os.environ["PROBE_FAKE_DSH"]
os.execv(fake, [fake] + sys.argv[1:])
'''

COUNT_WRAPPER = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

task = sys.argv[-1] if len(sys.argv) > 1 else ""
first = task.splitlines()[0] if task else ""
with open(os.environ["COUNT_OUT"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"first": first, "chars": len(task)}) + "\n")
fake = os.environ["COUNT_FAKE_DSH"]
os.execv(fake, [fake] + sys.argv[1:])
'''

KILL_WRAPPER = r'''#!/usr/bin/env python3
import os
import signal
import sys
from pathlib import Path

task = sys.argv[-1] if len(sys.argv) > 1 else ""
first = task.splitlines()[0] if task else ""
kind = ("review" if "INDEPENDENT REVIEWER" in task
        else "revision" if "REVISION agent" in task else "builder")
with open(os.environ["COUNT_OUT"], "a", encoding="utf-8") as handle:
    handle.write(kind + "\n")
if kind == "review":
    marker = Path(os.environ["KILL_MARKER"])
    if not marker.exists():
        # SIGKILL the fake reviewer mid-phase, exactly what a hard kill of a
        # real DSH process leaves behind: a saved run plus a durable checkpoint
        # whose review phase never reached "done".
        marker.write_text("killed once", encoding="utf-8")
        os.kill(os.getpid(), signal.SIGKILL)
fake = os.environ["COUNT_FAKE_DSH"]
os.execv(fake, [fake] + sys.argv[1:])
'''


def _write_script(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class ResumeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = fakes.write_fake_dsh(self.root / "fake-dsh")
        self.env = fakes.base_env(self.root, fake_dsh=self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, dsh=None):
        env = dict(self.env)
        if dsh is not None:
            env["DSH_BIN"] = str(dsh)
        return load_config(config_path=self.root / "config.json", env=env)

    def _checkpoint(self, run_id, plan, metadata):
        checkpoint = checkpoints.Checkpoint(self.root / "state" / "checkpoints", run_id)
        checkpoint.create(plan, metadata=metadata)
        return checkpoint

    def test_r1_running_skeleton_before_spawn(self):
        wrapper = _write_script(self.root / "probe-wrapper", PROBE_WRAPPER)
        probe = self.root / "probe.json"
        env = dict(self.env)
        env.update({"PROBE_FAKE_DSH": str(self.fake), "PROBE_OUT": str(probe),
                    "DSH_BIN": str(wrapper), "FAKE_DSH_MODE": "ok"})
        cfg = self._cfg(wrapper)
        args = {"prompt": "skeleton probe task", "workspace": str(self.workspace),
                "subagents": 0, "quality": False, "quality_rounds": 0,
                "independent_review": False}
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, _path = harness.run_harness(args, cfg)
        self.assertEqual(run.get("status"), "ok")
        self.assertTrue(probe.is_file(), "wrapper never observed the log dir")
        records = json.loads(probe.read_text(encoding="utf-8"))
        self.assertTrue(records, "no run JSON was present at DSH process start")
        statuses = [item.get("status") for item in records]
        self.assertIn("running", statuses,
                      "skeleton was not durable before spawn: %r" % (records,))

    def test_r2_resume_from_checkpoint_only_reuses_builder(self):
        run_id = "resume-only-1"
        metadata = {"spec": {
            "prompt": "R2 synthetic resume prompt",
            "workspace": str(self.workspace),
            "title": "R2 synthetic",
            "subagents": 0,
            "quality_used": False,
            "independent_review": False,
            "quality_rounds": 0,
        }}
        checkpoint = self._checkpoint(
            run_id, ["builder", "review-1", "revision-1"], metadata)
        checkpoint.finish("builder", status="done", answer="saved builder answer",
                          output_files=["deliverable.md"], seconds=1.0)
        # Deliberately no runs/<id>.json: the synthetic saved-run path must work.
        self.assertFalse((self.root / "runs" / (run_id + ".json")).exists())

        counter = self.root / "calls.jsonl"
        wrapper = _write_script(self.root / "count-wrapper", COUNT_WRAPPER)
        env = dict(self.env)
        env.update({"COUNT_FAKE_DSH": str(self.fake), "COUNT_OUT": str(counter),
                    "DSH_BIN": str(wrapper), "FAKE_DSH_MODE": "ok"})
        cfg = self._cfg(wrapper)
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)
        self.assertTrue(run.get("resumed") is True)
        self.assertEqual(run.get("resumed_from"), run_id)
        self.assertIn(run.get("status"), ("ok", "ok_unreviewed", "ok_below_gate"))
        self.assertTrue(Path(path).is_file())
        calls = []
        if counter.is_file():
            calls = [json.loads(line) for line in
                     counter.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(calls, [], "resumed run re-executed a DSH phase: %r" % (calls,))
        self.assertTrue((self.root / "runs").is_dir())

    def test_r4_accepted_review_restored_without_dsh(self):
        run_id = "accepted-review-1"
        report_text = (
            "# Independent review\n\n"
            "## Non-blocking defects\n"
            "- exact restored sentence XYZ-42\n\n"
            "Final quality score: 8.7/10\n")
        review_md = self.workspace / "REVIEW.md"
        metadata = {"spec": {
            "prompt": "R4 accepted review prompt",
            "workspace": str(self.workspace),
            "title": "R4 accepted",
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
        }}
        checkpoint = self._checkpoint(
            run_id, ["builder", "review-1", "revision-1"], metadata)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=1.0)
        checkpoint.finish(
            "review-1", status="done", accepted=True, score=8.7, blocking=0,
            defects=["Non-blocking: exact restored sentence XYZ-42"],
            fixes=[], report_text=report_text, report=str(review_md),
            seconds=2.0)
        # runs/<id>.json present, as required for the normal saved-run path.
        runs_dir = self.root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / (run_id + ".json")).write_text(json.dumps({
            "id": run_id, "kind": "harness",
            "prompt": "R4 accepted review prompt",
            "workspace": str(self.workspace), "title": "R4 accepted",
            "subagents": 0, "quality_used": False, "independent_review": True,
            "quality_rounds": 1, "status": "running",
            "created": "2026-01-01T00:00:00+00:00",
            "output_files": ["deliverable.md"],
        }), encoding="utf-8")
        if review_md.exists():
            review_md.unlink()
        # A missing DSH binary proves this path launches no process at all.
        missing = self.root / "no-such-dsh-binary"
        cfg = self._cfg(missing)
        _text, run, _path = harness.resume_harness(
            {"resume_run_id": run_id}, cfg)
        self.assertTrue(run.get("resumed") is True)
        self.assertEqual(run.get("review_status"), "accepted")
        self.assertEqual(float(run.get("quality_score")), 8.7)
        # Nothing was left to execute, so the pre-spawn "running" skeleton must
        # be finalized rather than returned forever-running.
        self.assertEqual(run.get("status"), "ok")
        self.assertTrue(run.get("status_detail"))
        self.assertTrue(run.get("review_passed"))
        saved = json.loads(
            (runs_dir / (run_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved.get("status"), "ok")
        self.assertTrue(saved.get("status_detail"))
        self.assertTrue(review_md.is_file(), "REVIEW.md was not restored")
        self.assertIn("exact restored sentence XYZ-42",
                      review_md.read_text(encoding="utf-8"))

    def test_r4b_accepted_review_expands_redacted_home_workspace(self):
        """A saved ``~/...`` workspace (the redacted form) is expanded.

        Run records go through ``api.redact_secrets``, which rewrites
        ``<home>/...`` to ``~/...``.  Resuming must restore REVIEW.md into the
        real home workspace, not a literal ``~`` directory.  The temp HOME
        makes the expected destination unambiguous.
        """
        run_id = "accepted-home-workspace-1"
        home = self.root / "home"
        workspace = home / "DeepSeek-MCP" / "harness-workspace"
        workspace.mkdir(parents=True)
        review_md = workspace / "REVIEW.md"
        report_text = ("# Independent review\n\n"
                       "Final quality score: 8.7/10\n")
        metadata = {"spec": {
            "prompt": "R4b redacted workspace resume",
            "workspace": str(workspace),
            "title": "R4b redacted workspace",
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
        }}
        checkpoint = self._checkpoint(run_id, ["builder", "review-1"], metadata)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=1.0)
        checkpoint.finish("review-1", status="done", accepted=True, score=8.7,
                          blocking=0, report_text=report_text,
                          report="~/DeepSeek-MCP/harness-workspace/REVIEW.md",
                          seconds=2.0)
        runs_dir = self.root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / (run_id + ".json")).write_text(json.dumps({
            "id": run_id, "kind": "harness",
            "prompt": "R4b redacted workspace resume",
            # Exactly what api.redact_secrets writes for a home workspace.
            "workspace": "~/DeepSeek-MCP/harness-workspace",
            "title": "R4b redacted workspace",
            "subagents": 0, "quality_used": False, "independent_review": True,
            "quality_rounds": 1, "status": "running",
            "created": "2026-01-01T00:00:00+00:00",
            "output_files": ["deliverable.md"],
        }), encoding="utf-8")
        cfg = self._cfg(self.root / "no-such-dsh-binary")
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "USERPROFILE": str(home)}, clear=False):
            _text, run, _path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("review_status"), "accepted")
        self.assertTrue(review_md.is_file(),
                        "REVIEW.md was not restored under the real HOME")
        self.assertIn("Final quality score: 8.7/10",
                      review_md.read_text(encoding="utf-8"))
        literal = Path("~") / "DeepSeek-MCP" / "harness-workspace" / "REVIEW.md"
        self.assertFalse(literal.exists(),
                         "REVIEW.md was written into a literal ~ directory")

    def test_r4c_checkpoint_only_accepted_review_saves_terminal_run(self):
        """No runs/<id>.json + accepted checkpoint must not KeyError.

        ``_synthetic_saved_run`` rebuilds the record from checkpoint metadata;
        it must carry ``created`` (used by the Markdown writer) and expand the
        redacted workspace before restoring REVIEW.md.
        """
        run_id = "accepted-checkpoint-only-1"
        home = self.root / "home"
        workspace = home / "DeepSeek-MCP" / "harness-workspace"
        workspace.mkdir(parents=True)
        review_md = workspace / "REVIEW.md"
        metadata = {"spec": {
            "prompt": "R4c checkpoint-only accepted review",
            # Deliberately the redacted form the metadata writer stores.
            "workspace": "~/DeepSeek-MCP/harness-workspace",
            "title": "R4c checkpoint only",
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
        }}
        checkpoint = self._checkpoint(run_id, ["builder", "review-1"], metadata)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=1.0)
        checkpoint.finish(
            "review-1", status="done", accepted=True, score=8.7, blocking=0,
            report_text="# Independent review\n\nFinal quality score: 8.7/10\n",
            report="~/DeepSeek-MCP/harness-workspace/REVIEW.md", seconds=2.0)
        self.assertFalse((self.root / "runs" / (run_id + ".json")).exists())
        cfg = self._cfg(self.root / "no-such-dsh-binary")
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "USERPROFILE": str(home)}, clear=False):
            _text, run, _path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("review_status"), "accepted")
        saved_path = self.root / "runs" / (run_id + ".json")
        self.assertTrue(saved_path.is_file(),
                        "checkpoint-only resume did not save a run record")
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("status"), "ok")
        self.assertTrue(saved.get("created"))
        self.assertTrue(saved.get("status_detail"))
        self.assertTrue(review_md.is_file(),
                        "REVIEW.md was not restored under the real HOME")

    def test_r3_killed_reviewer_resumes_without_rerunning_builder(self):
        """SIGKILL a real fake-DSH reviewer, then resume from the checkpoint.

        The builder phase must be reused (it is already done), the killed
        review round must run again, and the resumed run must end in a terminal
        status with a restored REVIEW.md.
        """
        counter = self.root / "kill-calls.txt"
        marker = self.root / "killed-once.txt"
        wrapper = _write_script(self.root / "kill-wrapper", KILL_WRAPPER)
        env = dict(self.env)
        env.update({
            "COUNT_FAKE_DSH": str(self.fake),
            "COUNT_OUT": str(counter),
            "KILL_MARKER": str(marker),
            "DSH_BIN": str(wrapper),
            "FAKE_DSH_MODE": "ok",
            "FAKE_DSH_REVIEW_SCORE": "8.7",
            "FAKE_DSH_STATE": str(self.root / "kill-dsh-state.json"),
        })
        cfg = self._cfg(wrapper)
        args = {"prompt": "Kill/resume integration prompt", "workspace": str(self.workspace),
                "subagents": 0, "quality": False, "quality_rounds": 1,
                "independent_review": True}
        with mock.patch.dict(os.environ, env, clear=False):
            _text, killed, _path = harness.run_harness(args, cfg)
        self.assertTrue(marker.is_file(), "the fake reviewer was never killed")
        self.assertNotEqual(killed.get("status"), "running")
        self.assertEqual(killed.get("status"), "ok_unreviewed")
        killed_calls = counter.read_text(encoding="utf-8").split()
        self.assertEqual(killed_calls.count("builder"), 1)
        self.assertEqual(killed_calls.count("review"), 1)

        with mock.patch.dict(os.environ, env, clear=False):
            _text, resumed, _path = harness.resume_harness(
                {"resume_run_id": killed["id"]}, cfg)
        resumed_calls = counter.read_text(encoding="utf-8").split()
        self.assertEqual(resumed_calls.count("builder"), 1,
                         "resume re-executed the completed builder: %r" % (resumed_calls,))
        self.assertEqual(resumed_calls.count("review"), 2,
                         "resume did not rerun the killed reviewer: %r" % (resumed_calls,))
        self.assertEqual(resumed.get("resumed_from"), killed["id"])
        self.assertEqual(resumed.get("review_status"), "accepted")
        self.assertEqual(resumed.get("status"), "ok")
        self.assertTrue(resumed.get("status_detail"))
        review_md = self.workspace / "REVIEW.md"
        self.assertTrue(review_md.is_file(), "REVIEW.md missing after resume")
        self.assertIn("Final quality score: 8.7/10",
                      review_md.read_text(encoding="utf-8"))

    def test_r5a_accepted_review_over_budget_resumes_as_budget_exceeded(self):
        """A kill between the accepted checkpoint and the cost gate must not
        resurrect as a clean "ok" on resume."""
        run_id = "accepted-over-budget-1"
        report_text = (
            "# Independent review\n\n"
            "Final quality score: 8.7/10\n")
        review_md = self.workspace / "REVIEW.md"
        metadata = {"spec": {
            "prompt": "R5a over-budget accepted review",
            "workspace": str(self.workspace),
            "title": "R5a over budget",
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
            "job_max_cost_usd": 0.01,
        }}
        checkpoint = self._checkpoint(run_id, ["builder", "review-1"], metadata)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=1.0)
        checkpoint.finish(
            "review-1", status="done", accepted=True, score=8.7, blocking=0,
            report_text=report_text, report=str(review_md), seconds=2.0,
            cost=5.0)
        runs_dir = self.root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / (run_id + ".json")).write_text(json.dumps({
            "id": run_id, "kind": "harness",
            "prompt": "R5a over-budget accepted review",
            "workspace": str(self.workspace), "title": "R5a over budget",
            "subagents": 0, "quality_used": False, "independent_review": True,
            "quality_rounds": 1, "status": "running",
            "created": "2026-01-01T00:00:00+00:00",
            "output_files": ["deliverable.md"],
        }), encoding="utf-8")
        missing = self.root / "no-such-dsh-binary"
        cfg = self._cfg(missing)
        _text, run, _path = harness.resume_harness(
            {"resume_run_id": run_id}, cfg)
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(run.get("review_status"), "stopped")
        self.assertFalse(run.get("review_passed"))
        saved = json.loads(
            (runs_dir / (run_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved.get("status"), "budget_exceeded")
        self.assertTrue(review_md.is_file(),
                        "the accepted REPORT text should still be restored")

    def test_r5b_resume_does_not_regrant_whole_job_deadline(self):
        """A resume continues the same clock and stops before a paid reviewer."""
        run_id = "resume-deadline-1"
        counter = self.root / "deadline-calls.txt"
        wrapper = _write_script(self.root / "deadline-wrapper", COUNT_WRAPPER)
        metadata = {"spec": {
            "prompt": "R5b deadline resume",
            "workspace": str(self.workspace),
            "title": "R5b deadline",
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 1,
            "job_max_seconds": 1.0,
        }}
        checkpoint = self._checkpoint(run_id, ["builder", "review-1"], metadata)
        checkpoint.finish("builder", status="done", answer="done",
                          output_files=["deliverable.md"], seconds=10.0)
        runs_dir = self.root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / (run_id + ".json")).write_text(json.dumps({
            "id": run_id, "kind": "harness",
            "prompt": "R5b deadline resume",
            "workspace": str(self.workspace), "title": "R5b deadline",
            "subagents": 0, "quality_used": False, "independent_review": True,
            "quality_rounds": 1, "status": "running",
            "created": "2026-01-01T00:00:00+00:00",
            "output_files": ["deliverable.md"],
        }), encoding="utf-8")
        env = dict(self.env)
        env.update({"COUNT_FAKE_DSH": str(self.fake), "COUNT_OUT": str(counter),
                    "DSH_BIN": str(wrapper), "FAKE_DSH_MODE": "ok"})
        cfg = self._cfg(wrapper)
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, _path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)
        self.assertEqual(run.get("status"), "deadline_exceeded")
        self.assertEqual(run.get("stop_reason"), "deadline")
        self.assertEqual(run.get("review_status"), "stopped")
        calls = []
        if counter.is_file():
            calls = counter.read_text(encoding="utf-8").split()
        self.assertEqual(calls, [],
                         "resume started a paid phase after the deadline: %r"
                         % (calls,))

    def test_r5_rejected_review_and_revision_reused(self):
        run_id = "revision-reuse-1"
        metadata = {"spec": {
            "prompt": "R5 revision reuse prompt",
            "workspace": str(self.workspace),
            "title": "R5 revision reuse",
            "subagents": 0,
            "quality_used": False,
            "independent_review": True,
            "quality_rounds": 2,
        }}
        checkpoint = self._checkpoint(
            run_id,
            ["builder", "review-1", "revision-1", "review-2", "revision-2"],
            metadata)
        checkpoint.finish("builder", status="done", answer="built",
                          output_files=["deliverable.md"], seconds=1.0)
        checkpoint.finish(
            "review-1", status="done", accepted=False, score=7.0, blocking=1,
            defects=["Blocking: missing fact"], fixes=["add the fact"],
            report_text="# Independent review\n\nFinal quality score: 7.0/10\n",
            report=str(self.workspace / "REVIEW.md"), seconds=2.0)
        checkpoint.finish("revision-1", status="done", files=["deliverable.md"],
                          seconds=3.0)
        (self.workspace / "deliverable.md").write_text(
            "# Fake deliverable revised\n", encoding="utf-8")
        counter = self.root / "calls.jsonl"
        wrapper = _write_script(self.root / "count-wrapper", COUNT_WRAPPER)
        env = dict(self.env)
        env.update({"COUNT_FAKE_DSH": str(self.fake), "COUNT_OUT": str(counter),
                    "DSH_BIN": str(wrapper), "FAKE_DSH_MODE": "ok",
                    "FAKE_DSH_REVIEW_SCORE": "8.7"})
        cfg = self._cfg(wrapper)
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, _path = harness.resume_harness(
                {"resume_run_id": run_id}, cfg)
        self.assertTrue(run.get("resumed") is True)
        calls = [json.loads(line) for line in
                 counter.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(calls), 1,
                         "expected exactly one new reviewer call, got %r" % (calls,))
        self.assertIn("INDEPENDENT REVIEWER", calls[0]["first"])
        resumed = run.get("review_rounds") or []
        self.assertTrue(any(item.get("resumed") for item in resumed),
                        "prior review/revision rounds were not reused: %r" % (resumed,))


if __name__ == "__main__":
    unittest.main()
