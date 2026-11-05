"""P1 task-bridge candidate tests (CrewForeman 3.8.0).

Contract: INTERFACES.md section C "Task bridge".  The tests drive the bridge
without subprocesses, network or a real detached worker:

* a fake tool handler records the job via ``server._context.value["on_start"]``
  and returns the "still running" marker, exactly like ``bg_harness`` does;
* job status files are written with ``jobs.write_status`` (the real writer);
* journal events are written directly to ``<state_dir>/work/<work_id>/events.jsonl``;
* ``server._refresh_task_from_job(store, task_id, cfg)`` is the single helper the
  bridge must provide (see task_bridge.md for its implementation snippet).

Stdlib unittest only; run with:
    python -m unittest candidates.test_p1_task_bridge -v
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1] / "repo"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deepseek_mcp import events, jobs, server, tasks  # noqa: E402

STILL_RUNNING = (
    "[Job %s started in the background. Poll deepseek_job_status "
    "run_id=%s to follow it.]"
)
DONE_TEXT = "the finished harness answer"


class Cfg(dict):
    """Minimal config mapping that also supports attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class TaskBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state_dir = root / "state"
        self.jobs_dir = self.state_dir / "jobs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = Cfg(
            state_dir=str(self.state_dir),
            jobs_dir=str(self.jobs_dir),
            timeout=5,
            task_ttl=3600,
            task_completion="job",
        )
        # _task_store(cfg) uses state_dir/mcp-tasks; share this store so that
        # tasks/get exercises the same files as the test.
        self.store = tasks.TaskStore(str(self.state_dir / "mcp-tasks"))
        server._task_stores.clear()
        self._orig_handlers = dict(server.HANDLERS)
        self.addCleanup(self._restore)

    def _restore(self):
        server.HANDLERS.clear()
        server.HANDLERS.update(self._orig_handlers)
        server._task_stores.clear()
        server._context.value = None
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def install_handler(self, calls=None):
        calls = calls if calls is not None else []

        def fake(args, cfg):
            calls.append(dict(args))
            ctx = server._context.value
            run_id = args.get("run_id") or "job-1"
            status_path = Path(cfg.jobs_dir) / (run_id + ".status.json")
            ctx["on_start"](run_id, str(status_path))
            return STILL_RUNNING % (run_id, run_id)

        server.HANDLERS["harness_deepseek"] = fake
        return calls

    def new_task(self, tool="harness_deepseek", args=None):
        task = self.store.create(
            tool, args or {"prompt": "build it"}, ttl=3600)
        return task["taskId"]

    def ctx(self, task_id):
        def on_start(run_id, path):
            self.store.update(task_id, job_id=run_id)
            self.store.progress(task_id, 1, message="job %s started" % run_id)

        return {
            "store": self.store,
            "task_id": task_id,
            "cancel_event": threading.Event(),
            "on_start": on_start,
        }

    def run_handler(self, task_id, args=None):
        self.install_handler()
        server._execute_task(
            "harness_deepseek",
            args or {"prompt": "build it", "run_id": "job-1"},
            self.cfg,
            self.ctx(task_id),
        )
        return self.store.get(task_id)

    def write_job(self, run_id="job-1", **fields):
        jobs.write_status(
            Path(self.cfg.jobs_dir) / (run_id + ".status.json"), **fields)

    def write_events(self, work_id, events):
        work = Path(self.cfg.state_dir) / "work" / work_id
        work.mkdir(parents=True, exist_ok=True)
        with open(str(work / "events.jsonl"), "w", encoding="utf-8") as handle:
            for seq, item in enumerate(events, 1):
                item = dict(item)
                item.setdefault("seq", seq)
                item.setdefault("event_id", "ev-%s-%s" % (work_id, seq))
                item.setdefault("at", "2026-01-01T00:00:00+00:00")
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def needs_input(question="Ship the fix?", options=None):
        return {
            "type": "needs_input",
            "payload": {
                "question": question,
                "options": options or ["yes", "no"],
            },
        }

    # -- 1. detached handler keeps the task working with job_id ----------
    def test_detached_handler_leaves_task_working_with_job_id(self):
        task_id = self.new_task()
        task = self.run_handler(task_id)

        self.assertEqual(task["status"], "working")
        self.assertEqual(task["job_id"], "job-1")
        # It must not have been completed on handler return.
        self.assertNotIn("result", task)
        self.assertFalse(task.get("isError"))

    # -- 2. completion only on terminal job state ------------------------
    def test_tasks_get_completes_only_on_terminal_job_state(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        self.write_job(status="running", pid=os.getpid(), progress="building")

        wire = server._handle_task_method(
            "tasks/get", {"taskId": task_id}, self.cfg)["result"]
        self.assertEqual(wire["status"], "working")

        # Progress alone never completes the task.
        server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(self.store.get(task_id)["status"], "working")

        self.write_job(status="done", result=DONE_TEXT, progress="finished")
        wire = server._handle_task_method(
            "tasks/get", {"taskId": task_id}, self.cfg)["result"]
        self.assertEqual(wire["status"], "completed")
        task = self.store.get(task_id)
        self.assertEqual(task["result"]["content"][0]["text"], DONE_TEXT)
        self.assertFalse(task["result"]["isError"])

    # -- 3. job failure fails the task -----------------------------------
    def test_job_failure_fails_task(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        self.write_job(status="error", error="harness blew up", progress="failed")

        server._refresh_task_from_job(self.store, task_id, self.cfg)
        task = self.store.get(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("harness blew up", task.get("error") or "")
        self.assertTrue(task.get("isError"))
        self.assertEqual(
            server._handle_task_method(
                "tasks/get", {"taskId": task_id}, self.cfg)["result"]["status"],
            "failed",
        )

    # -- 4. cancellation is terminal and never resurrects ----------------
    def test_cancelled_never_resurrects(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        self.store.cancel(task_id, reason="cancelled by client")
        self.write_job(status="done", result=DONE_TEXT)

        server._refresh_task_from_job(self.store, task_id, self.cfg)
        task = self.store.get(task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertNotIn("result", task)
        self.assertEqual(
            server._handle_task_method(
                "tasks/get", {"taskId": task_id}, self.cfg)["result"]["status"],
            "cancelled",
        )
        # A cancelled job status is also a no-op.  Use a fresh job id: job-1
        # is already terminal from the first half of this test.
        other = self.new_task()
        self.run_handler(other, args={"prompt": "build it", "run_id": "job-2"})
        self.store.cancel(other)
        self.write_job(run_id="job-2", status="cancelled", error=None)
        server._refresh_task_from_job(self.store, other, self.cfg)
        self.assertEqual(self.store.get(other)["status"], "cancelled")

    # -- 5. journal terminal event completes even without a status file --
    def test_terminal_journal_event_completes_task(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        self.store.update(task_id, work_id="w-journal")
        self.write_events("w-journal", [
            {"type": "admitted", "payload": {}},
            {"type": "result", "payload": {"result": "journal answer"}},
        ])

        server._refresh_task_from_job(self.store, task_id, self.cfg)
        task = self.store.get(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["content"][0]["text"], "journal answer")

    # -- 6. status string form is unchanged without events_since ---------
    def test_attention_then_answer_completes_real_job(self):
        # A real jobs.run_job result with one validated question keeps the
        # task input_required; a later admitted/result clears it.
        run_id = "job-attention"
        work_id = "w" + "c" * 19
        spec = self.jobs_dir / (run_id + ".job.json")
        spec.write_text(json.dumps({
            "tool": "harness_deepseek", "run_id": run_id,
            "work_id": work_id, "attempt_no": 1,
            "args": {"prompt": "x", "_work_id": work_id,
                     "_attempt_no": 1},
        }), encoding="utf-8")
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="queued", work_id=work_id, attempt_no=1)
        text = ("Built.\n\n```attention\n%s\n```\n"
                % json.dumps({"question": "Ship it?",
                              "options": ["yes", "no"]}))
        jobs.run_job(spec, {"harness_deepseek": lambda args, cfg: text},
                     self.cfg)
        task_id = self.new_task()
        self.store.update(task_id, job_id=run_id)
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "input_required")
        self.assertEqual(task["question"], "Ship it?")
        # An answer starts the next attempt: admitted clears the question,
        # then a terminal result completes the task.
        from deepseek_mcp import events
        log = events.EventLog(Path(self.cfg.state_dir) / "work" / work_id)
        log.append("admitted", {"run_id": "job-attention-2"})
        log.append("result", {"status": "ok", "run_id": "job-attention-2"})
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="done", result="answered")
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["content"][0]["text"], "answered")


    # -- 12. a failed journal terminal with a done status fails the task ----
    def test_failed_journal_with_done_status_fails_task(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        work_id = "w" + "e" * 19
        self.store.update(task_id, work_id=work_id)
        self.write_job(status="done", result="stale success", work_id=work_id)
        log = events.EventLog(Path(self.cfg.state_dir) / "work" / work_id)
        log.append("admitted", {"run_id": "job-1"})
        log.append("failed", {"run_id": "job-1", "status": "error",
                              "error": "deterministic failure"})
        log.write_result("deterministic failure", status="error",
                         run_id="job-1")
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "failed")
        self.assertIn("deterministic failure", task.get("error") or "")
        self.assertTrue(task.get("isError"))

    def test_old_terminal_before_new_admission_does_not_complete(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        work_id = "w" + "f" * 19
        self.store.update(task_id, work_id=work_id)
        log = events.EventLog(Path(self.cfg.state_dir) / "work" / work_id)
        log.append("admitted", {"run_id": "run-1"})
        log.append("needs_input", {"question": "Ship it?",
                                   "options": ["yes", "no"]})
        self.write_job(status="done", result="old question", work_id=work_id)
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "input_required")

        # A new attempt starts; the old done status/result must not complete
        # the task while run-2 is still running.
        log.append("admitted", {"run_id": "run-2"})
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "working")
        self.assertNotIn("result", task)

        events.record_terminal(
            log, run={"run_id": "run-2", "status": "ok"}, text="new answer")
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["content"][0]["text"], "new answer")
    def test_status_string_unchanged_without_events_since(self):
        self.write_job(status="running", pid=os.getpid(), progress="building")
        running = jobs.tool_status({"run_id": "job-1"}, self.cfg)
        self.assertIsInstance(running, str)
        self.assertTrue(
            running.startswith("[Job job-1 still running: building"),
            running)

        self.write_job(status="done", result=DONE_TEXT)
        done = jobs.tool_status({"run_id": "job-1"}, self.cfg)
        self.assertIsInstance(done, str)
        self.assertEqual(done, DONE_TEXT)

    def test_status_dict_form_with_events_since(self):
        self.write_job(status="running", pid=os.getpid(), progress="building")
        out = jobs.tool_status(
            {"run_id": "job-1", "events_since": 0}, self.cfg)
        self.assertIsInstance(out, dict)
        for key in ("events", "next_seq", "stage", "attention", "question",
                    "options", "next_action", "terminal", "result"):
            self.assertIn(key, out)
        self.assertFalse(out["terminal"])

        self.write_job(status="done", result=DONE_TEXT)
        out = jobs.tool_status(
            {"run_id": "job-1", "events_since": 0}, self.cfg)
        self.assertTrue(out["terminal"])
        self.assertEqual(out["result"], DONE_TEXT)

    # -- 7. task_completion="request" restores complete-on-return --------
    def test_task_completion_request_completes_on_return(self):
        cfg = Cfg(dict(self.cfg))
        cfg["task_completion"] = "request"
        task_id = self.new_task()
        self.install_handler()
        server._execute_task(
            "harness_deepseek", {"prompt": "build it"}, cfg,
            self.ctx(task_id))

        task = self.store.get(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(
            task["result"]["content"][0]["text"], STILL_RUNNING % ("job-1", "job-1"))
        self.assertEqual(task["job_id"], "job-1")

    # -- 8. input_required only from a validated question ----------------
    def test_input_required_from_validated_needs_input_event(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        self.store.update(task_id, work_id="w-question")
        self.write_events("w-question", [
            {"type": "admitted", "payload": {}},
            self.needs_input("Ship the fix?", ["yes", "no", "later"]),
        ])
        self.write_job(status="running", pid=os.getpid(), progress="waiting",
                       work_id="w-question")

        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "input_required")
        self.assertEqual(task["question"], "Ship the fix?")
        wire = tasks.TaskStore.to_wire(task)
        self.assertEqual(wire.get("question"), "Ship the fix?")
        self.assertEqual(wire.get("options"), ["yes", "no", "later"])
        result = server._handle_task_method(
            "tasks/result", {"taskId": task_id}, self.cfg)["result"]
        self.assertEqual(result["structuredContent"]["question"],
                         "Ship the fix?")
        self.assertEqual(result["structuredContent"]["options"],
                         ["yes", "no", "later"])
        self.assertEqual(task["options"], ["yes", "no", "later"])

    def test_unvalidated_question_stays_working_or_completes(self):
        # A bare status-file question is not a validated question.
        task_id = self.new_task()
        self.run_handler(task_id)
        self.write_job(status="running", pid=os.getpid(), progress="waiting",
                       question="Ship the fix?", options=["yes", "no"])
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "working")

        # Prose (not a fenced ``attention`` block) never creates input_required;
        # a done job completes normally.
        self.write_job(status="done",
                       result="Question: should I ship the fix?")
        task = server._refresh_task_from_job(self.store, task_id, self.cfg)
        self.assertEqual(task["status"], "completed")
        self.assertNotIn("question", task)

    # -- 9. no job yet means still working -------------------------------
    def test_missing_job_state_leaves_task_working(self):
        task_id = self.new_task()
        self.run_handler(task_id)
        missing = self.new_task()
        self.store.update(missing, job_id="job-does-not-exist")
        server._refresh_task_from_job(self.store, missing, self.cfg)
        self.assertEqual(self.store.get(missing)["status"], "working")


if __name__ == "__main__":
    unittest.main(verbosity=2)
