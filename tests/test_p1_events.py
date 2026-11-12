"""Contract tests for ``deepseek_mcp.events`` (P1-1/P1-3, INTERFACES.md section B).

These tests are intentionally behaviour-level: they exercise the public surface
described in the integration contract, not private helpers.  Everything runs in
``tempfile`` directories and never touches the network.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Allow running this candidate directly from ./candidates as well as after it is
# copied into ./repo/tests.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "repo"):
    if (_candidate / "deepseek_mcp" / "events.py").exists() or (
            _candidate / "deepseek_mcp" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from deepseek_mcp import events  # noqa: E402
from deepseek_mcp import jobs, work  # noqa: E402
from deepseek_mcp.config import load_config  # noqa: E402
from tests.fakes import base_env  # noqa: E402


def attention_text(question="Proceed with the deploy?", options=None,
                   evidence=None):
    payload = {"question": question,
               "options": ["yes", "no"] if options is None else options}
    if evidence is not None:
        payload["evidence"] = evidence
    return ("Context prose that must be ignored.\n\n"
            "```attention\n%s\n```\n\nTrailing prose." % json.dumps(payload))


def event(seq, event_type, payload=None):
    return {"seq": seq, "event_id": "ev-%d" % seq, "type": event_type,
            "at": "2025-01-01T00:00:00+00:00", "payload": payload or {}}


class EventsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="p1-events-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work = self.root / "work"
        self.work.mkdir(parents=True)
        self.log = events.EventLog(self.work, clock=lambda: 1700000000.0)

    # ------------------------------------------------------------------ basics
    def test_contract_constants(self):
        self.assertEqual(events.EVENT_TYPES,
                         ("admitted", "phase", "progress", "needs_input",
                          "result", "failed", "cancelled",
                          "reviewer_backup"))
        self.assertEqual(events.STAGES,
                         ("queued", "running", "needs_human", "review_ready",
                          "terminal"))
        self.assertEqual(events.ATTENTIONS,
                         ("none", "answer_question", "review_result",
                          "inspect_failure"))
        self.assertEqual(events.EVENT_SCHEMA, 1)
        self.assertEqual(events.DEFAULT_MAX_BYTES, 1_000_000)

    def test_append_event_shape_and_next_seq(self):
        first = self.log.append("admitted", {"run_id": "r1"})
        second = self.log.append("phase", {"phase": "build"})
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        for item in (first, second):
            for key in ("seq", "event_id", "type", "at", "payload"):
                self.assertIn(key, item)
            self.assertIsInstance(item["event_id"], str)
            self.assertTrue(item["event_id"])
            self.assertIsInstance(item["payload"], dict)
        self.assertEqual(self.log.next_seq(), 2)
        with self.assertRaises(Exception):
            self.log.append("not-an-event-type", {})

    def test_read_since_cursor_dedupe_and_corruption(self):
        self.log.append("phase", {"n": 1})
        self.log.append("phase", {"n": 2})
        self.assertEqual([e["seq"] for e in self.log.read_since(0)], [1, 2])
        self.assertEqual([e["seq"] for e in self.log.read_since(1)], [2])
        self.assertEqual(self.log.read_since(2), [])

        # A mixed log: duplicate seq, blank/malformed lines and a partial final
        # line (no trailing newline) must be tolerated and deduped.
        corrupt_dir = self.root / "corrupt"
        corrupt_dir.mkdir()
        path = corrupt_dir / "events.jsonl"
        e1 = event(1, "phase")
        e2 = event(2, "progress")
        path.write_text(
            json.dumps(e1) + "\n" + json.dumps(e1) + "\nnot json\n\n"
            + json.dumps(e2) + "\n" + '{"seq": 3, "type": "pha',
            encoding="utf-8")
        corrupt = events.EventLog(corrupt_dir)
        self.assertEqual([e["seq"] for e in corrupt.read_since(0)], [1, 2])
        self.assertEqual(corrupt.next_seq(), 2)
        self.assertEqual(events.EventLog(self.root / "missing").read_since(0), [])
        self.assertEqual(events.EventLog(self.root / "missing").next_seq(), 0)

    def test_append_fails_closed_when_lock_cannot_open(self):
        # Raise from __enter__, not from the factory call: the deleted
        # d24479b fallback caught exactly this TimeoutError and yielded
        # without the lock, so a call-time exception would not exercise the
        # regression at all.
        lock_path = self.log.lock_path

        class FailingLock(object):
            def __enter__(self):
                raise TimeoutError("cannot open lock %s" % lock_path)

            def __exit__(self, *exc_info):
                return False

        # A lock that cannot be opened must never degrade into an unlocked
        # append: no seq is consumed and nothing reaches the journal.
        self.log.append("phase", {"n": 1})
        with mock.patch.object(events.platforms, "file_lock",
                               return_value=FailingLock()):
            with self.assertRaises(TimeoutError):
                self.log.append("phase", {"n": 2})
        self.assertEqual(len(self.log.read_since(0)), 1)
        self.assertEqual(self.log.next_seq(), 1)
        # The lock is usable again once the failure clears.
        self.log.append("phase", {"n": 3})
        self.assertEqual(self.log.next_seq(), 2)

    # ------------------------------------------------------------- concurrency
    def test_seq_monotonic_under_concurrent_appends(self):
        count_threads, per_thread = 8, 3
        errors = []
        barrier = threading.Barrier(count_threads)

        def worker(index):
            try:
                barrier.wait(timeout=10)
                local = events.EventLog(self.work, clock=lambda: 1700000000.0)
                for n in range(per_thread):
                    local.append("progress", {"thread": index, "n": n})
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(errors, "concurrent append raised: %r" % (errors,))
        retained = self.log.read_since(0)
        seqs = [item["seq"] for item in retained]
        self.assertEqual(seqs, list(range(1, count_threads * per_thread + 1)))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(self.log.next_seq(), count_threads * per_thread)

    def test_seq_monotonic_under_16x50_appends(self):
        # Higher-contention version of the 8x3 test above.  The raw file is
        # inspected in write order: only an actually held lock can keep both
        # sequence allocation and the O_APPEND write atomic.
        count_threads, per_thread = 16, 50
        total = count_threads * per_thread
        errors = []
        barrier = threading.Barrier(count_threads)

        def worker(index):
            try:
                barrier.wait(timeout=30)
                for n in range(per_thread):
                    self.log.append("progress", {"thread": index, "n": n})
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,),
                                    name="event-appender-%d" % index)
                   for index in range(count_threads)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        elapsed = time.monotonic() - started

        self.assertEqual([t.name for t in threads if t.is_alive()], [],
                         "appender thread(s) did not finish within 60s")
        self.assertFalse(errors, "concurrent append raised: %r" % (errors,))

        raw_lines = [line for line in self.log.events_path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(raw_lines), total)
        seqs = [json.loads(line)["seq"] for line in raw_lines]
        for line_number, (previous, current) in enumerate(zip(seqs, seqs[1:])):
            self.assertLess(
                previous, current,
                "seq not strictly increasing in file order at lines %d->%d: "
                "%r -> %r" % (line_number + 1, line_number + 2,
                              previous, current))
        self.assertEqual(len(set(seqs)), total)
        self.assertEqual(set(seqs), set(range(1, total + 1)))
        self.assertEqual(self.log.next_seq(), total)
        self.assertEqual(len(self.log.read_since(0)), total)
        self.assertLess(elapsed, 10.0,
                        "16x50 appends took %.3fs (limit 10.0s)" % elapsed)

    # ---------------------------------------------------------------- rotation
    def test_rotation_preserves_cursor_semantics(self):
        rotation_dir = self.root / "rotation"
        rotation_dir.mkdir()
        original = events.EventLog(rotation_dir, max_bytes=10_000)
        for index in range(3):
            original.append("progress", {"n": index})

        # A tiny max_bytes makes the next append rotate the existing file.
        rotating = events.EventLog(rotation_dir, max_bytes=1)
        rotating.append("progress", {"n": 3})
        self.assertTrue((rotation_dir / "events.1.jsonl").exists())
        cursor = 2
        after = rotating.read_since(cursor)
        self.assertTrue(after)
        self.assertEqual([e["seq"] for e in after],
                         sorted({e["seq"] for e in after}))
        self.assertTrue(all(e["seq"] > cursor for e in after))

        # Append again: rotation overwrites events.1.jsonl.  A cursor older than
        # the retained window must still return retained events, never crash.
        rotating.append("progress", {"n": 4})
        retained = rotating.read_since(cursor)
        self.assertTrue(retained)
        self.assertTrue(all(e["seq"] > cursor for e in retained))
        self.assertEqual(rotating.read_since(0), sorted(
            rotating.read_since(0), key=lambda e: e["seq"]))
        self.assertIn(5, [e["seq"] for e in rotating.read_since(4)])

        hwm_path = rotation_dir / "events.hwm.json"
        self.assertTrue(hwm_path.exists())
        hwm = json.loads(hwm_path.read_text(encoding="utf-8"))
        self.assertIn("retained_from", hwm)
        self.assertIn("next_seq", hwm)
        self.assertGreaterEqual(int(hwm["retained_from"]), 1)
        self.assertGreaterEqual(int(hwm["next_seq"]), 5)

    # ------------------------------------------------------------------ result
    def test_result_survives_status_rewrite(self):
        status_path = self.root / "jobs" / "run123.status.json"
        env = {
            "DEEPSEEK_MCP_JOBS_DIR": str(status_path.parent),
            "DEEPSEEK_MCP_LOG_DIR": str(self.root / "runs"),
            "DEEPSEEK_MCP_CONFIG": str(self.root / "config.json"),
            "DSH_STATE_DIR": str(self.root / "state"),
            "DEEPSEEK_NO_CAFFEINATE": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            if hasattr(jobs, "_notify"):
                patcher = mock.patch.object(jobs, "_notify",
                                            lambda *a, **k: None)
                patcher.start()
                self.addCleanup(patcher.stop)
            jobs.write_status(status_path, status="running", progress="build")
            written = self.log.write_result("THE ANSWER", status="ok",
                                            run_id="run123")
            self.assertIsInstance(written, dict)
            jobs.write_status(status_path, status="done",
                              result="THE ANSWER", work_id="w" + "a" * 20)

        result = self.log.read_result()
        self.assertIsNotNone(result)
        self.assertEqual(result["result"], "THE ANSWER")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["run_id"], "run123")
        self.assertEqual(result["schema"], 1)
        self.assertIn("at", result)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["result"], "THE ANSWER")

        # Empty/missing result is tolerated.
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(events.EventLog(empty).read_result())

    # --------------------------------------------------------------- attention
    def test_validate_attention_valid_case_insensitive_fence(self):
        value = events.validate_attention(attention_text(
            "Choose a migration path?",
            ["fast", "safe"],
            {"file": "db.py", "line": 12}))
        self.assertIsNotNone(value)
        self.assertEqual(value["question"], "Choose a migration path?")
        self.assertEqual(value["options"], ["fast", "safe"])
        self.assertEqual(value["evidence"], {"file": "db.py", "line": 12})

        uppercase = attention_text("Uppercase fence?")
        uppercase = uppercase.replace("```attention", "```ATTENTION")
        self.assertIsNotNone(events.validate_attention(uppercase))

    def test_validate_attention_prose_is_never_inferred(self):
        for text in (
                "Should I continue with the deploy? yes/no",
                "Question: pick one of these options: a, b, c?",
                'What about `{"question": "inline?", "options": ["a", "b"]}`?',
                "",
                "No question here at all."):
            self.assertIsNone(events.validate_attention(text), text)

    def test_validate_attention_malformed_and_duplicate_blocks(self):
        cases = [
            # broken JSON
            "```attention\n{\"question\": \"Q?\", \"options\": [\"a\", \"b\"]\n```",
            # not a dict
            "```attention\n[\"a\", \"b\"]\n```",
            # empty question
            "```attention\n{\"question\": \"\", \"options\": [\"a\", \"b\"]}\n```",
            # question too long
            "```attention\n%s\n```" % json.dumps(
                {"question": "q" * 501, "options": ["a", "b"]}),
            # too few / too many options
            "```attention\n{\"question\": \"Q?\", \"options\": [\"a\"]}\n```",
            "```attention\n%s\n```" % json.dumps(
                {"question": "Q?", "options": ["a", "b", "c", "d",
                                                "e", "f", "g", "h", "i"]}),
            # empty option string
            "```attention\n{\"question\": \"Q?\", \"options\": [\"a\", \"\"]}\n```",
            # a different fence language is not an attention block
            "```json\n{\"question\": \"Q?\", \"options\": [\"a\", \"b\"]}\n```",
        ]
        for text in cases:
            self.assertIsNone(events.validate_attention(text), text)

        duplicate = (attention_text("First?")
                     + "\n\n" + attention_text("Second?"))
        self.assertIsNone(events.validate_attention(duplicate))

    def test_record_attention_appends_one_needs_input(self):
        text = attention_text("Which branch?", ["main", "release"],
                              "branch list")
        recorded = events.record_attention(self.log, text, source="job:run1")
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded["type"], "needs_input")
        self.assertEqual(recorded["payload"]["question"], "Which branch?")
        self.assertEqual(recorded["payload"]["options"], ["main", "release"])
        self.assertEqual(recorded["payload"]["source"], "job:run1")
        self.assertIn("evidence", recorded["payload"])
        retained = self.log.read_since(0)
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["type"], "needs_input")

        self.assertIsNone(events.record_attention(
            self.log, "Just a prose question? yes/no"))
        self.assertEqual(len(self.log.read_since(0)), 1)

    # ---------------------------------------------------------------- project
    def test_terminal_is_scoped_to_latest_admission(self):
        log = events.EventLog(self.work / "scoped")
        log.append("admitted", {"run_id": "run-1"})
        log.append("result", {"run_id": "run-1", "status": "ok"})
        self.assertEqual(
            events.terminal_after_latest_admission(log.read_since(0))["payload"]["run_id"],
            "run-1")
        log.append("admitted", {"run_id": "run-2"})
        # The old result is before the new admission: not terminal for run-2.
        self.assertIsNone(
            events.terminal_after_latest_admission(log.read_since(0)))
        log.append("failed", {"run_id": "run-3", "status": "error"})
        # A terminal named for a different run id must not close run-2.
        self.assertIsNone(
            events.terminal_after_latest_admission(log.read_since(0)))
        log.append("result", {"run_id": "run-2", "status": "ok"})
        current = events.terminal_after_latest_admission(log.read_since(0))
        self.assertEqual(current["payload"]["run_id"], "run-2")

    def test_project_pending_question(self):
        projected = events.project(
            run={"run_id": "run1", "status": "running"},
            job={"status": "running", "pid": 4242},
            events=[event(1, "admitted", {"run_id": "run1"}),
                    event(2, "needs_input",
                          {"question": "Which DB?", "options": ["pg", "sqlite"]})],
        )
        self.assertEqual(projected["stage"], "needs_human")
        self.assertEqual(projected["attention"], "answer_question")
        self.assertFalse(projected["terminal"])
        self.assertEqual(projected["question"], "Which DB?")
        self.assertEqual(projected["options"], ["pg", "sqlite"])
        self.assertIsInstance(projected["question"], str)
        action = projected["next_action"]
        self.assertIn("resume_deepseek", action)
        self.assertIn("answer", action.lower())

    def test_pending_question_overrides_job_done(self):
        log = events.EventLog(self.work / "override")
        log.append("needs_input", {"question": "Proceed?",
                                   "options": ["yes", "no"]})
        projected = events.project(
            job={"status": "done", "result": "handler returned"},
            events=log.read_since(0))
        self.assertFalse(projected["terminal"])
        self.assertEqual(projected["stage"], "needs_human")
        self.assertEqual(projected["attention"], "answer_question")
        self.assertEqual(projected["question"], "Proceed?")

    def test_project_question_cleared_by_later_terminal_event(self):
        for clearing_type in ("admitted", "result", "failed", "cancelled"):
            projected = events.project(
                run={"run_id": "run1", "status": "running"},
                events=[event(1, "needs_input",
                              {"question": "Still there?",
                               "options": ["yes", "no"]}),
                        event(2, clearing_type, {})],
            )
            self.assertIsNone(projected["question"], clearing_type)
            self.assertNotEqual(projected["attention"], "answer_question",
                                clearing_type)
            self.assertIn(projected["stage"], ("running", "terminal"))

    def test_project_terminal_attention_mapping(self):
        expected = {
            "ok": "none",
            "ok_rescued": "none",
            "ok_below_gate": "review_result",
            "ok_unreviewed": "inspect_failure",
            "error": "inspect_failure",
            "no_balance": "inspect_failure",
            "deadline_exceeded": "inspect_failure",
            "budget_exceeded": "inspect_failure",
            "cancelled": "none",
        }
        for status, attention in expected.items():
            projected = events.project(run={"run_id": "run1",
                                            "status": status})
            self.assertTrue(projected["terminal"], status)
            self.assertEqual(projected["stage"], "terminal", status)
            self.assertEqual(projected["attention"], attention, status)

        below = events.project(run={"run_id": "run1",
                                    "status": "ok_below_gate"})
        self.assertIn("REVIEW.md", below["next_action"])
        self.assertIn("resume_deepseek", below["next_action"])

        failure = events.project(run={"run_id": "run1", "status": "error"})
        self.assertIn("harness_deepseek", failure["next_action"])

        budget = events.project(run={"run_id": "run1",
                                     "status": "budget_exceeded"})
        self.assertIn("job_max_cost_usd", budget["next_action"])
        deadline = events.project(run={"run_id": "run1",
                                       "status": "deadline_exceeded"})
        self.assertIn("job_max_seconds", deadline["next_action"])

    def test_project_queued_versus_running(self):
        self.assertEqual(
            events.project(job={"status": "queued"})["stage"], "queued")
        self.assertEqual(
            events.project(job={"status": "running"})["stage"], "queued")
        self.assertEqual(
            events.project(job={"status": "running", "pid": 999999})["stage"],
            "running")

    def test_project_result_precedence(self):
        self.assertEqual(
            events.project(run={"answer": "run-answer"},
                           job={"result": "job-answer"},
                           result="explicit")["result"],
            "explicit")
        self.assertEqual(
            events.project(run={"answer": "run-answer"},
                           job={"result": "job-answer"})["result"],
            "run-answer")
        self.assertEqual(
            events.project(job={"result": "job-answer"})["result"],
            "job-answer")
        self.assertIsNone(events.project()["result"])

    # ---------------------------------------------------------- record_terminal
    def test_record_terminal_writes_result_and_is_idempotent(self):
        first = events.record_terminal(
            self.log, run={"run_id": "run1", "status": "ok"}, text="all good")
        second = events.record_terminal(
            self.log, run={"run_id": "run1", "status": "ok"}, text="all good")
        self.assertIsInstance(first, dict)
        self.assertIsInstance(second, dict)
        terminal = [e for e in self.log.read_since(0)
                    if e["type"] in ("result", "failed", "cancelled")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["type"], "result")
        self.assertEqual(self.log.read_result()["result"], "all good")
        self.assertEqual(self.log.read_result()["status"], "ok")

        events.record_terminal(
            self.log, run={"run_id": "run1", "status": "error"},
            text="boom")
        events.record_terminal(
            self.log, run={"run_id": "run1", "status": "cancelled"},
            text="stopped")
        types = [e["type"] for e in self.log.read_since(0)]
        self.assertEqual(types.count("failed"), 1)
        self.assertEqual(types.count("cancelled"), 1)

    # ------------------------------------------------------ jobs.tool_status
    def test_job_tool_status_string_unchanged_and_dict_with_events_since(self):
        jobs_dir = self.root / "jobs"
        jobs_dir.mkdir()
        run_id = "runabc123"
        work_id = "w" + "a" * 20
        work_dir = self.root / "state" / "work" / work_id
        log = events.EventLog(work_dir, clock=lambda: 1700000000.0)
        log.append("admitted", {"run_id": run_id})
        log.append("result", {"status": "ok"})

        status_path = jobs_dir / (run_id + ".status.json")
        env = {
            "DEEPSEEK_MCP_JOBS_DIR": str(jobs_dir),
            "DEEPSEEK_MCP_LOG_DIR": str(self.root / "runs"),
            "DEEPSEEK_MCP_CONFIG": str(self.root / "config.json"),
            "DSH_STATE_DIR": str(self.root / "state"),
            "DEEPSEEK_NO_CAFFEINATE": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            if hasattr(jobs, "_notify"):
                patcher = mock.patch.object(jobs, "_notify",
                                            lambda *a, **k: None)
                patcher.start()
                self.addCleanup(patcher.stop)
            jobs.write_status(status_path, status="done",
                              result="FINAL TEXT", work_id=work_id,
                              work_dir=str(work_dir),
                              events_path=str(work_dir / "events.jsonl"))

        cfg = SimpleNamespace(
            jobs_dir=str(jobs_dir),
            state_dir=str(self.root / "state"),
            log_dir=str(self.root / "runs"),
            work_dir=str(self.root / "state" / "work"),
        )

        # Without events_since the historical string output is preserved bit
        # for bit.
        plain = jobs.tool_status({"run_id": run_id}, cfg)
        self.assertIsInstance(plain, str)
        self.assertEqual(plain, "FINAL TEXT")

        payload = jobs.tool_status(
            {"run_id": run_id, "events_since": 0}, cfg)
        self.assertIsInstance(payload, dict)
        for key in ("events", "next_seq", "stage", "attention", "question",
                    "options", "next_action", "terminal", "result"):
            self.assertIn(key, payload)
        self.assertEqual(payload["result"], "FINAL TEXT")
        self.assertEqual(payload["next_seq"], 2)
        self.assertEqual([e["seq"] for e in payload["events"]], [1, 2])
        self.assertIsInstance(payload["terminal"], bool)
        self.assertIn(payload["attention"], events.ATTENTIONS)
        self.assertIn(payload["stage"], events.STAGES)

        after = jobs.tool_status(
            {"run_id": run_id, "events_since": 1}, cfg)
        self.assertEqual([e["seq"] for e in after["events"]], [2])

        # The advertised next_seq is a cursor, not one-past: using it must not
        # skip the next event that arrives.
        new_event = log.append("phase", {"phase": "review-1"})
        self.assertEqual(new_event["seq"], 3)
        cursor = jobs.tool_status(
            {"run_id": run_id, "events_since": after["next_seq"]}, cfg)
        self.assertEqual([e["seq"] for e in cursor["events"]], [3])


class JobsJournalIntegrationTest(unittest.TestCase):
    """jobs.run_job emits the journal the status reader projects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="p1-events-jobs-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir(parents=True)
        self.env = base_env(self.root)
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)
        self.work_id = "w" + "b" * 19

    def _spec(self, run_id, text):
        spec = self.jobs_dir / (run_id + ".job.json")
        spec.write_text(json.dumps({
            "tool": "harness_deepseek", "run_id": run_id,
            "work_id": self.work_id, "attempt_no": 1,
            "args": {"prompt": "x", "_work_id": self.work_id,
                     "_attempt_no": 1},
        }), encoding="utf-8")
        return spec

    def test_attention_keeps_work_open_and_is_projected(self):
        run_id = "attention-run"
        text = ("Built the thing.\n\n```attention\n%s\n```\n"
                % json.dumps({"question": "Ship it?",
                              "options": ["yes", "no"],
                              "evidence": "tests pass"}))
        spec = self._spec(run_id, text)
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="queued", work_id=self.work_id,
                          attempt_no=1)
        jobs.run_job(spec, {"harness_deepseek": lambda args, cfg: text},
                     self.cfg)
        state = json.loads((self.jobs_dir / (run_id + ".status.json"))
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["progress"], "needs_input")
        self.assertEqual(state["question"], "Ship it?")
        log = events.EventLog(Path(self.cfg.state_dir) / "work"
                              / self.work_id)
        kinds = [event["type"] for event in log.read_since(0)]
        self.assertIn("needs_input", kinds)
        self.assertNotIn("result", kinds)
        self.assertIsNone(log.read_result())
        tool = jobs.tool_status(
            {"run_id": run_id, "events_since": 0}, self.cfg)
        self.assertEqual(tool["stage"], "needs_human")
        self.assertEqual(tool["attention"], "answer_question")
        self.assertFalse(tool["terminal"])
        self.assertEqual(tool["question"], "Ship it?")
        self.assertEqual(tool["options"], ["yes", "no"])

    def test_cancel_clears_a_held_question(self):
        run_id = "held-question"
        work_id = self.work_id
        wstore = work.WorkStore(Path(self.cfg.state_dir) / "work")
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": "x", "workspace": str(self.root)})
        admission = wstore.admit(fp)
        wstore.bind_run(admission["work_id"], 1, run_id)
        wstore.hold(admission["work_id"], run_id=run_id)
        log = events.EventLog(Path(self.cfg.state_dir) / "work"
                              / admission["work_id"])
        log.append("admitted", {"run_id": run_id})
        log.append("needs_input", {"question": "Ship it?",
                                   "options": ["yes", "no"]})
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="done", progress="needs_input",
                          question="Ship it?", work_id=admission["work_id"])
        state = jobs.cancel_job(run_id, self.cfg)
        self.assertEqual(state["status"], "cancelled")
        kinds = [event["type"] for event in log.read_since(0)]
        self.assertIn("cancelled", kinds)
        self.assertEqual(log.read_result()["status"], "cancelled")
        self.assertEqual(wstore.load(admission["work_id"])["state"], "finished")
        self.assertEqual(wstore.load(admission["work_id"])["terminal_status"],
                         "cancelled")

    def test_old_job_without_work_id_is_still_readable(self):
        run_id = "old-job-1"
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="running", pid=os.getpid(),
                          progress="legacy builder")
        plain = jobs.tool_status({"run_id": run_id}, self.cfg)
        self.assertIsInstance(plain, str)
        self.assertIn("legacy builder", plain)
        structured = jobs.tool_status(
            {"run_id": run_id, "events_since": 0}, self.cfg)
        self.assertEqual(structured["events"], [])
        self.assertEqual(structured["next_seq"], 0)
        self.assertEqual(structured["stage"], "running")
        self.assertFalse(structured["terminal"])

    def test_result_survives_status_rewrite(self):
        run_id = "result-run"
        text = "durable answer"
        spec = self._spec(run_id, text)
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="queued", work_id=self.work_id,
                          attempt_no=1)
        jobs.run_job(spec, {"harness_deepseek": lambda args, cfg: text},
                     self.cfg)
        log = events.EventLog(Path(self.cfg.state_dir) / "work"
                              / self.work_id)
        stored = log.read_result()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["result"], text)
        # A later status rewrite must not touch the durable result file.
        jobs.write_status(self.jobs_dir / (run_id + ".status.json"),
                          status="running", progress="late-progress")
        self.assertEqual(log.read_result()["result"], text)
        structured = jobs.tool_status(
            {"run_id": run_id, "events_since": 0}, self.cfg)
        self.assertEqual(structured["result"], text)
        self.assertEqual(structured["stage"], "running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
