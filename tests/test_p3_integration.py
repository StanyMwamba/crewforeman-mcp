"""P3 integration tests: admission job status, review convergence, and the
connector-run signed test evidence / diff context in the real review loop."""

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import (admission, checkpoints, events, harness, jobs,
                          review_context, server)
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh


def _root(case):
    base = os.environ.get("TMPDIR") or os.getcwd()
    try:
        tmp = tempfile.TemporaryDirectory(prefix="p3-int-", dir=base)
    except OSError:
        tmp = tempfile.TemporaryDirectory(prefix="p3-int-", dir=os.getcwd())
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name).resolve()


class ReviewLoopConvergenceTests(unittest.TestCase):
    def _run(self, env_extra, rounds=3):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update(env_extra)
        args = {
            "prompt": "Build a tiny deliverable note with one required fact.",
            "workspace": str(ws), "sandbox": "danger",
            "quality": True, "independent_review": True,
            "quality_rounds": rounds, "subagents": 1,
            "max_seconds": 30, "job_max_seconds": 120,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            text, run, _path = harness.run_harness(args, cfg)
        return text, run, cfg, root, ws

    def test_repeated_signature_stops_and_asks_one_question(self):
        text, run, _cfg, _root_dir, _ws = self._run({
            "DSH_QUALITY_CONVERGENCE": "auto",
            "FAKE_DSH_REVIEW_SCORES": "7.0,7.0,7.0",
            "FAKE_DSH_REVIEW_BLOCKING": "1,1,1",
        })
        reviews = [item for item in run["review_rounds"]
                   if item.get("kind") == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(run.get("convergence_stopped"))
        self.assertEqual(run["quality_rounds_used"], 2)
        self.assertEqual(run["status"], "ok_below_gate")
        attention = run.get("convergence_attention") or {}
        self.assertEqual(attention.get("options"),
                         ["resume_with_scope", "accept_below_gate", "stop"])
        self.assertEqual((attention.get("question") or "").count("?"), 1)
        self.assertTrue(reviews[-1].get("signatures"))
        self.assertEqual(reviews[-1]["first_seen_round"],
                         {reviews[-1]["signatures"][0]: 1})
        self.assertIn("```attention", text)

    def test_auto_stop_then_resume_with_scope_runs_revision_and_new_review(self):
        env_extra = {
            "DSH_QUALITY_CONVERGENCE": "auto",
            "FAKE_DSH_REVIEW_SCORES": "7.0,7.0,7.0,7.0",
            "FAKE_DSH_REVIEW_BLOCKING": "1,1,1,1",
        }
        _text, run, _cfg, root, _ws = self._run(env_extra, rounds=3)
        first_reviews = [item for item in run["review_rounds"]
                         if item.get("kind") == "review"]
        self.assertEqual([item["round"] for item in first_reviews], [1, 2])
        self.assertTrue(run.get("convergence_stopped"))
        self.assertFalse(any(item.get("kind") == "revision"
                             and item.get("round") == 2
                             for item in run["review_rounds"]))

        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update(env_extra)
        captured = []
        original = harness.build_revision_task

        def capture(*call_args, **call_kwargs):
            captured.append(dict(call_kwargs))
            return original(*call_args, **call_kwargs)

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            with mock.patch.object(harness, "build_revision_task",
                                   side_effect=capture):
                _text2, run2, _path2 = harness.resume_harness({
                    "resume_run_id": run["id"],
                    "option": "resume_with_scope",
                    "answer": "Scope: repair only the missing fact.",
                }, cfg)

        self.assertEqual(run2["resumed_from"], run["id"])
        self.assertTrue(captured)
        self.assertIn("Scope: repair only the missing fact",
                      captured[0].get("scope_note") or "")
        revision_2 = [item for item in run2["review_rounds"]
                      if item.get("kind") == "revision"
                      and int(item.get("round") or 0) == 2]
        self.assertEqual(len(revision_2), 1)
        self.assertEqual(revision_2[0].get("status"), "ok")
        reviews = [item for item in run2["review_rounds"]
                   if item.get("kind") == "review"]
        self.assertEqual([item["round"] for item in reviews], [1, 2, 3])
        self.assertEqual(
            (run2.get("manual_scope_resume") or {}).get(
                "suppressed_stop_round"), 2)

    def test_observe_records_without_stopping(self):
        text, run, _cfg, _root_dir, _ws = self._run({
            "DSH_QUALITY_CONVERGENCE": "observe",
            "FAKE_DSH_REVIEW_SCORES": "7.0,7.0,7.0",
            "FAKE_DSH_REVIEW_BLOCKING": "1,1,1",
        })
        reviews = [item for item in run["review_rounds"]
                   if item.get("kind") == "review"]
        self.assertEqual(len(reviews), 3)
        self.assertTrue(run.get("convergence_observed"))
        self.assertFalse(run.get("convergence_stopped"))
        self.assertEqual(run["quality_convergence"]["action"], "observe_stop")


class TaskBridgeAdmissionTests(unittest.TestCase):
    """The MCP task must not complete while its detached job is queued."""

    def setUp(self):
        self.root = _root(self)
        self.cfg = load_config(include_file=False)
        self.cfg._values.update({
            "state_dir": str(self.root / "state"),
            "jobs_dir": str(self.root / "jobs"),
            "harness_workspace": str(self.root / "ws"),
            "max_harness_jobs": 1,
            "reserve_resume_slot": True,
            "admission_heartbeat_seconds": 30.0,
            "admission_lease_seconds": 90.0,
            "task_completion": "job",
            "task_ttl": 3600,
        })
        (self.root / "ws").mkdir()
        server._task_stores.clear()
        self.addCleanup(server._task_stores.clear)
        self.addCleanup(lambda: setattr(server._context, "value", None))
        notify = mock.patch.object(server, "notify", return_value=None)
        self.addCleanup(notify.stop)
        notify.start()

    def _start_task(self, prompt):
        store = server._task_store(self.cfg)
        task = store.create("harness_deepseek", {"prompt": prompt}, ttl=3600)
        ctx = {
            "store": store,
            "task_id": task["taskId"],
            "cancel_event": threading.Event(),
            "on_start": lambda run_id, _path: server._task_job_started(
                store, task["taskId"], run_id),
        }
        server._execute_task(
            "harness_deepseek",
            {"prompt": prompt, "wait_seconds": 0}, self.cfg, ctx)
        return task["taskId"], store.get(task["taskId"])

    def test_cap_one_queued_task_keeps_job_id_and_waits_for_journal(self):
        proc = mock.Mock(pid=os.getpid())
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen", return_value=proc), \
                mock.patch.object(admission.AdmissionController, "start",
                                  return_value=None):
            first_id, first_task = self._start_task("first task")
            second_id, second_task = self._start_task("second task")
        specs = sorted((self.root / "jobs").glob("*.job.json"))
        ids = {json.loads(path.read_text())["args"].get("prompt"):
               json.loads(path.read_text())["run_id"] for path in specs}
        self.assertEqual(second_task["job_id"], ids["second task"])
        self.assertEqual(second_task["status"], "working")
        self.assertNotIn("result", second_task)
        self.assertEqual(first_task["status"], "working")
        self.assertEqual(first_task["job_id"], ids["first task"])

        status_path = (self.root / "jobs" /
                       (second_task["job_id"] + ".status.json"))
        state = json.loads(status_path.read_text())
        self.assertEqual(state["status"], "queued")
        work_id = state.get("work_id")
        self.assertTrue(work_id)
        jobs.write_status(status_path, status="done", work_id=work_id)
        jobs._result(work_id, self.cfg, "terminal journal answer", "done",
                     second_task["job_id"])
        events.EventLog(
            self.root / "state" / "work" / work_id).append(
                "result", {"run_id": second_task["job_id"],
                           "result": "terminal journal answer"})
        task = server._refresh_task_from_job(
            server._task_store(self.cfg), second_task["taskId"], self.cfg)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["content"][0]["text"],
                         "terminal journal answer")


class AdmissionFailClosedTests(unittest.TestCase):
    def _cfg(self):
        root = _root(self)
        cfg = load_config(include_file=False)
        cfg._values.update({
            "state_dir": str(root / "state"),
            "jobs_dir": str(root / "jobs"),
            "harness_workspace": str(root / "ws"),
            "max_harness_jobs": 1,
            "reserve_resume_slot": True,
            "admission_heartbeat_seconds": 30.0,
            "admission_lease_seconds": 90.0,
        })
        (root / "ws").mkdir()
        return cfg, root

    def test_admission_failure_fails_closed_without_popen(self):
        cfg, root = self._cfg()
        with mock.patch.object(
                admission.AdmissionStore, "_work_lock",
                side_effect=admission.AdmissionError(
                    "admission lock unavailable for items/a.json")), \
                mock.patch.object(jobs.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError) as raised:
                jobs.start_job("harness_deepseek",
                               {"prompt": "blocked", "workspace":
                                str(root / "ws")},
                               cfg=cfg, wait=0)
        popen.assert_not_called()
        self.assertIn("Admission unavailable", str(raised.exception))
        statuses = list((root / "jobs").glob("*.status.json"))
        self.assertEqual(len(statuses), 1)
        state = json.loads(statuses[0].read_text())
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["progress"], "failed")
        work = jobs._work_store(cfg).load(state["work_id"])
        self.assertEqual(work.get("terminal_status"), "error")

    def test_ask_consult_swarm_keep_direct_start_outside_the_queue(self):
        cfg, root = self._cfg()
        proc = mock.Mock(pid=os.getpid())
        proc.poll.return_value = None
        proc.wait.return_value = 0
        for tool in ("ask_deepseek", "consult_deepseek", "swarm_deepseek"):
            with mock.patch.object(jobs, "_worker_env", return_value={}), \
                    mock.patch.object(jobs.subprocess, "Popen",
                                      return_value=proc) as popen, \
                    mock.patch.object(admission.AdmissionController, "submit",
                                      side_effect=AssertionError(
                                          "non-Harness tool must not queue")):
                text = jobs.start_job(tool, {"prompt": tool},
                                      cfg=cfg, wait=0)
            self.assertIn("still running", text.lower())
            self.assertEqual(popen.call_count, 1)
        # The Harness slot is never involved, so no job was queued.
        for status_path in (root / "jobs").glob("*.status.json"):
            self.assertNotEqual(
                json.loads(status_path.read_text()).get("status"), "queued")


class ConnectorReviewEvidenceTests(unittest.TestCase):
    def test_connector_runs_signed_suite_and_diff_before_review(self):
        root = _root(self)
        ws = root / "ws"
        tests = ws / "tests"
        tests.mkdir(parents=True)
        (tests / "test_ok.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n", encoding="utf-8")
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({"FAKE_DSH_REVIEW_SCORE": "9.0",
                    "DSH_QUALITY_CONVERGENCE": "observe"})
        args = {
            "prompt": "Build a tiny Python module with tests.",
            "workspace": str(ws), "sandbox": "danger",
            "quality": True, "independent_review": True,
            "quality_rounds": 1, "subagents": 1,
            "max_seconds": 30, "job_max_seconds": 120,
        }
        captured = {}
        original = harness.build_review_task

        def capture(*call_args, **call_kwargs):
            captured.update(call_kwargs)
            return original(*call_args, **call_kwargs)

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            cfg._values.update({"review_test_suite": "auto",
                                "review_test_timeout_seconds": 60})
            with mock.patch.object(harness, "build_review_task",
                                   side_effect=capture):
                _text, run, _path = harness.run_harness(args, cfg)
        evidence = (run.get("review_test_evidence") or [])
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["status"], "ok")
        self.assertGreaterEqual(evidence[0]["tests"], 1)
        self.assertTrue(evidence[0]["tree_sha256"])
        self.assertTrue(evidence[0]["signature"])
        self.assertEqual(evidence[0]["key_id"], "connector")
        # The exact signed payload handed to the reviewer verifies through
        # connector-controlled state; changing it must fail.
        self.assertTrue(review_context.verify_evidence(evidence[0],
                                                       cfg.state_dir))
        tampered = dict(evidence[0])
        tampered["tests"] = int(tampered.get("tests") or 0) + 1
        self.assertFalse(review_context.verify_evidence(tampered,
                                                        cfg.state_dir))
        key_path = (Path(cfg.state_dir) / "review-evidence" / "keys" /
                    "connector.key")
        self.assertTrue(key_path.is_file())
        if os.name != "nt":
            # Windows privacy is an ACL, not POSIX mode bits.
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode) & 0o777, 0o600)
        self.assertEqual(run["review_status"], "accepted")
        # The reviewer prompt must carry the signed evidence and the diff.
        self.assertIn("Signed test evidence (connector-run)",
                      captured.get("test_evidence") or "")
        self.assertIn(evidence[0]["tree_sha256"],
                      captured.get("test_evidence") or "")
        self.assertIn("Connector diff (start here)",
                      captured.get("review_context") or "")
        self.assertIn("do not read the whole repo",
                      (captured.get("review_context") or "").lower())


class AdmissionStatusTests(unittest.TestCase):
    def test_structured_status_reports_queue_position_for_queued_job(self):
        root = _root(self)
        state = root / "state"
        jobs_dir = root / "jobs"
        jobs_dir.mkdir(parents=True)
        cfg = load_config(include_file=False)
        cfg._values.update({"state_dir": str(state), "jobs_dir": str(jobs_dir),
                            "max_harness_jobs": 1,
                            "admission_heartbeat_seconds": 30.0,
                            "admission_lease_seconds": 90.0})
        # A live lease held by this process occupies the only slot.
        controller = jobs._admission_controller(cfg)
        controller.store.enqueue("holder", priority=1)
        controller.store.claim("holder", pid=os.getpid())
        # Create a queued job record the way start_job would.
        run_id = "queuedjob1"
        (jobs_dir / (run_id + ".status.json")).write_text(json.dumps({
            "status": "queued", "admission_work_id": run_id,
            "queue_position": 1, "priority": 1,
            "priority_label": "P1 new build",
        }), encoding="utf-8")
        controller.store.enqueue(run_id, priority=1, run_id=run_id)
        payload = jobs.tool_status({"run_id": run_id, "events_since": 0}, cfg)
        self.assertEqual(payload["queue_position"], 1)
        self.assertEqual(payload["priority_label"], "P1 new build")
        self.assertEqual(payload["priority"], 1)
        text = jobs.tool_status({"run_id": run_id}, cfg)
        self.assertIn("queued at position 1", text)

    def test_cancelled_queued_job_reports_zero_position(self):
        root = _root(self)
        jobs_dir = root / "jobs"
        jobs_dir.mkdir(parents=True)
        cfg = load_config(include_file=False)
        cfg._values.update({"state_dir": str(root / "state"),
                            "jobs_dir": str(jobs_dir),
                            "max_harness_jobs": 1,
                            "admission_heartbeat_seconds": 30.0,
                            "admission_lease_seconds": 90.0})
        run_id = "cancelledjob1"
        (jobs_dir / (run_id + ".status.json")).write_text(json.dumps({
            "status": "queued", "admission_work_id": run_id,
            "queue_position": 1, "priority": 1,
            "priority_label": "P1 new build",
        }), encoding="utf-8")
        store = jobs._admission_store_for(cfg)
        store.enqueue(run_id, priority=1, run_id=run_id)
        store.release(run_id, status="cancelled")
        payload = jobs.tool_status({"run_id": run_id, "events_since": 0}, cfg)
        # The store is authoritative and reports 0 for a terminal item; the
        # stale status file's queue_position=1 must not win through truthiness.
        self.assertEqual(payload["queue_position"], 0)

    def test_start_job_queues_when_cap_is_full_and_dispatches_after_release(self):
        root = _root(self)
        cfg = load_config(include_file=False)
        cfg._values.update({
            "state_dir": str(root / "state"),
            "jobs_dir": str(root / "jobs"),
            "harness_workspace": str(root / "ws"),
            "max_harness_jobs": 1,
            "reserve_resume_slot": True,
            "admission_heartbeat_seconds": 30.0,
            "admission_lease_seconds": 90.0,
        })
        (root / "ws").mkdir()
        proc = mock.Mock(pid=os.getpid())
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with mock.patch.object(jobs, "_worker_env", return_value={}),                 mock.patch.object(jobs.subprocess, "Popen", return_value=proc),                 mock.patch.object(admission.AdmissionController, "start",
                                  return_value=None):
            first = jobs.start_job("harness_deepseek",
                                   {"prompt": "first", "workspace": str(root / "ws")},
                                   cfg=cfg, wait=0)
            second = jobs.start_job("harness_deepseek",
                                    {"prompt": "second", "workspace": str(root / "ws")},
                                    cfg=cfg, wait=0)
        self.assertIn("still running", first.lower())
        self.assertIn("queued at position 1", second)
        specs = sorted((root / "jobs").glob("*.job.json"))
        self.assertEqual(len(specs), 2)
        second_id = [json.loads(p.read_text())["run_id"] for p in specs
                     if json.loads(p.read_text())["args"].get("prompt") == "second"][0]
        status = json.loads((root / "jobs" / (second_id + ".status.json")).read_text())
        self.assertEqual(status["status"], "queued")
        self.assertEqual(status["queue_position"], 1)
        # Release the first lease and drain the persistent queue with a new
        # controller, exactly as a restarted serve() would.
        first_id = [json.loads(p.read_text())["run_id"] for p in specs
                    if json.loads(p.read_text())["args"].get("prompt") == "first"][0]
        jobs._admission_store_for(cfg).release(first_id, status="done")
        started = []
        fresh = jobs._admission_controller(cfg)
        fresh.run_once(spawn=lambda record: started.append(record["work_id"]))
        self.assertEqual(started, [second_id])


if __name__ == "__main__":
    unittest.main()


class ConvergenceAttentionEventTests(unittest.TestCase):
    def test_one_needs_input_event_and_projection(self):
        root = _root(self)
        cfg = load_config(include_file=False)
        cfg._values.update({"state_dir": str(root / "state")})
        run = {"id": "run-attention", "work_id": "wattention1"}
        payload = {
            "question": "Round 2 repeated the same blocking defect "
                        "(missing required fact in report.md). Resume with a "
                        "narrower scope, accept below the gate, or stop?",
            "options": ["resume_with_scope", "accept_below_gate", "stop"],
            "evidence": ["round 1: missing required fact in report.md",
                         "round 2: missing required fact in report.md"],
        }
        first = harness._emit_convergence_attention(run, cfg, payload)
        second = harness._emit_convergence_attention(run, cfg, payload)
        self.assertIsNotNone(first)
        self.assertEqual(first.get("seq"), second.get("seq"))
        log = events.EventLog(Path(cfg.state_dir) / "work" / "wattention1")
        all_events = log.read_since(0)
        self.assertEqual(
            len([event for event in all_events
                 if event.get("type") == "needs_input"]), 1)
        projection = events.project(run, {}, all_events, None)
        self.assertEqual(projection["attention"], "answer_question")
        self.assertEqual(projection["options"],
                         ["resume_with_scope", "accept_below_gate", "stop"])
        self.assertEqual(projection["question"].count("?"), 1)


class ConvergenceAttentionHandoffTests(unittest.TestCase):
    """The convergence question must survive terminal save and MCP refresh."""

    def test_terminal_save_then_job_and_refresh_keeps_one_pending_question(self):
        root = _root(self)
        cfg = load_config(include_file=False)
        cfg._values.update({
            "state_dir": str(root / "state"),
            "jobs_dir": str(root / "jobs"),
            "log_dir": str(root / "runs"),
            "harness_workspace": str(root / "ws"),
            "auto_skills": "off",
            "task_completion": "job",
            "task_ttl": 3600,
        })
        (root / "ws").mkdir()
        run_id = "run-converge"
        work_id = "w" + "e" * 19
        payload = {
            "question": "Round 2 repeated the same blocking defect "
                        "(missing required fact in report.md). Resume with a "
                        "narrower scope, accept below the gate, or stop?",
            "options": ["resume_with_scope", "accept_below_gate", "stop"],
            "evidence": ["round 1: missing required fact in report.md",
                         "round 2: missing required fact in report.md"],
        }
        run = {
            "id": run_id, "work_id": work_id, "status": "ok_below_gate",
            "answer": "builder answer", "prompt": "build the report",
            "workspace": str(root / "ws"), "tool": "harness_deepseek",
            "created": "2026-11-06T10:00:00+00:00", "seconds": 1,
            "quality_score": 7.0, "convergence_attention": payload,
        }
        # The review loop emits the event before the terminal save; the save
        # used to append a later result which cleared it.
        harness._ensure_admitted(run, cfg)
        harness._emit_convergence_attention(run, cfg, payload)

        def handler(args, cfg):
            # Exactly what _save_terminal does in run_harness; the fenced
            # question is appended to the returned text only afterwards.
            path = harness._save_terminal(run, cfg, [], None, None)
            text = harness._result_text(run, path)
            return text.rstrip() + "\n\n```attention\n%s\n```\n" % json.dumps(
                payload, ensure_ascii=False, sort_keys=True)

        spec = root / "jobs" / (run_id + ".job.json")
        spec.parent.mkdir(parents=True)
        spec.write_text(json.dumps({
            "tool": "harness_deepseek", "run_id": run_id,
            "work_id": work_id, "attempt_no": 1,
            "args": {"prompt": "build the report", "_work_id": work_id,
                     "_attempt_no": 1},
        }), encoding="utf-8")
        jobs.write_status(root / "jobs" / (run_id + ".status.json"),
                          status="queued", work_id=work_id, attempt_no=1)
        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}):
            jobs.run_job(spec, {"harness_deepseek": handler}, cfg)

        log = events.EventLog(Path(cfg.state_dir) / "work" / work_id)
        journal = log.read_since(0)
        self.assertEqual(
            [event["type"] for event in journal].count("needs_input"), 1)
        self.assertIsNone(events.terminal_after_latest_admission(journal))
        self.assertEqual(events.pending_question(journal)["question"],
                         payload["question"])
        self.assertEqual(log.read_result().get("status"), "needs_input")
        state = json.loads(
            (root / "jobs" / (run_id + ".status.json")).read_text())
        self.assertEqual(state.get("status"), "done")
        self.assertEqual(state.get("progress"), "needs_input")
        self.assertEqual(state.get("question"), payload["question"])
        structured = jobs.tool_status(
            {"run_id": run_id, "events_since": 0}, cfg)
        self.assertEqual(structured.get("attention"), "answer_question")
        self.assertEqual(structured.get("question"), payload["question"])
        self.assertEqual(structured.get("options"), payload["options"])

        server._task_stores.clear()
        self.addCleanup(server._task_stores.clear)
        notify = mock.patch.object(server, "notify", return_value=None)
        notify.start()
        self.addCleanup(notify.stop)
        store = server._task_store(cfg)
        task = store.create("harness_deepseek",
                            {"prompt": "build the report"}, ttl=3600)
        store.update(task["taskId"], job_id=run_id)
        task = server._refresh_task_from_job(store, task["taskId"], cfg)
        self.assertEqual(task.get("status"), "input_required")
        self.assertEqual(task.get("question"), payload["question"])
        self.assertEqual(task.get("options"), payload["options"])


class FocusedRoundPromptTests(unittest.TestCase):
    def test_second_round_receives_prior_defects_and_targeted_diff(self):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({"FAKE_DSH_REVIEW_SCORES": "7.0,6.0",
                    "FAKE_DSH_REVIEW_BLOCKING": "1,1",
                    "DSH_QUALITY_CONVERGENCE": "observe"})
        args = {
            "prompt": "Build a tiny deliverable note with one required fact.",
            "workspace": str(ws), "sandbox": "danger",
            "quality": True, "independent_review": True,
            "quality_rounds": 2, "subagents": 1,
            "max_seconds": 30, "job_max_seconds": 120,
        }
        calls = []
        original = harness.build_review_task

        def capture(*call_args, **call_kwargs):
            calls.append(dict(call_kwargs))
            return original(*call_args, **call_kwargs)

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            with mock.patch.object(harness, "build_review_task",
                                   side_effect=capture):
                _text, run, _path = harness.run_harness(args, cfg)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get("prior_defects") or [], [])
        self.assertTrue(calls[1].get("prior_defects"))
        second = calls[1].get("review_context") or ""
        self.assertIn("Round 2 focus", second)
        self.assertIn("previously flagged", second.lower())
        self.assertIn("targeted", second.lower())
        self.assertIn("Connector diff", second)
        self.assertIn("P3-2 defect signatures", second)
        self.assertTrue(calls[1].get("prior_signatures"))
        # R3: the baseline is captured *before* revision 1 writes, so round 2
        # receives exactly the revision's diff.  The old post-revision capture
        # made this diff empty and hid the very fix the reviewer must inspect.
        self.assertNotIn("No text changes were detected", second)
        self.assertIn("# Fake deliverable revised", second)
        self.assertIn("-# Fake deliverable", second)
        # The round-1 review report is older than the revision baseline and
        # must not reappear as an unrelated change in the focused diff.
        self.assertNotIn("REVIEW.md", second)


class ResumedReviewContextTests(unittest.TestCase):
    def test_resumed_review_reuses_pre_revision_baseline(self):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({"FAKE_DSH_REVIEW_SCORE": "6.0",
                    "FAKE_DSH_REVIEW_BLOCKING": "1",
                    "DSH_QUALITY_CONVERGENCE": "observe"})
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            pre = root / "pre-revision"
            pre.mkdir()
            (pre / "deliverable.md").write_text(
                "# Fake deliverable\n", encoding="utf-8")
            (pre / "QUALITY_REPORT.md").write_text(
                "old report\n", encoding="utf-8")
            baseline_dir = (Path(cfg.state_dir) / "review-baselines" /
                            "original-run")
            baseline = review_context.capture_baseline(pre, baseline_dir)
            self.assertIsNotNone(review_context.load_baseline(baseline_dir))
            # The workspace already contains revision 1 and its report.
            (ws / "deliverable.md").write_text(
                "# Fake deliverable revised\n", encoding="utf-8")
            (ws / "QUALITY_REPORT.md").write_text(
                "revised report\n", encoding="utf-8")
            prior = [
                {"round": 1, "kind": "review", "score": 7.0,
                 "blocking_defects": 1,
                 "defects": ["Missing required fact in deliverable.md"],
                 "fixes": ["Add the fact"],
                 "signatures": ["a" * 64],
                 "first_seen_round": {"a" * 64: 1},
                 "status": "ok", "accepted": False, "report": None,
                 "report_text": None, "session_logs": [],
                 "visual_impact": None, "creative_cap": False,
                 "seconds": 1.0, "cost": None, "error": None,
                 "stalled": False, "stalled_step": None,
                 "write_violations": [], "resumed": True},
                {"round": 1, "kind": "revision", "score": None,
                 "blocking_defects": 0,
                 "defects": ["Missing required fact in deliverable.md"],
                 "fixes": ["Add the fact"], "seconds": 1.0, "cost": None,
                 "status": "ok", "error": None, "stalled": False,
                 "stalled_step": None, "files": ["deliverable.md"],
                 "session_logs": [], "escalation": None, "resumed": True},
            ]
            calls = []
            original = harness.build_review_task

            def capture(*call_args, **call_kwargs):
                calls.append(dict(call_kwargs))
                return original(*call_args, **call_kwargs)

            with mock.patch.object(harness, "build_review_task",
                                   side_effect=capture):
                _text, run, _path = harness.run_harness({
                    "prompt": "Build a tiny deliverable note with one required fact.",
                    "workspace": str(ws), "sandbox": "danger",
                    "quality": True, "independent_review": True,
                    "quality_rounds": 2, "subagents": 1,
                    "max_seconds": 30, "job_max_seconds": 120,
                    "live_scaling": False,
                    "_run_id": "run-resume-diff",
                    "_prior_rounds": prior,
                    "_review_baseline_dir": str(baseline_dir),
                    "_prior_builder": {
                        "status": "ok", "answer": "builder done",
                        "seconds": 1.0,
                        "output_files": ["deliverable.md"],
                        "session_logs": [], "estimated_cost_usd": None,
                        "tokens_in": None, "tokens_out": None,
                        "transport": "headless",
                        "transport_session_id": None,
                    },
                }, cfg)
        self.assertEqual(len(calls), 1)
        context = calls[0].get("review_context") or ""
        self.assertNotIn("No text changes were detected", context)
        self.assertIn("# Fake deliverable revised", context)
        self.assertIn("-# Fake deliverable", context)
        self.assertEqual((run.get("review_baseline") or {}).get("dir"),
                         str(baseline_dir))
        self.assertTrue(baseline.get("files"))

    def test_retried_revision_keeps_its_pre_revision_baseline(self):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({"FAKE_DSH_REVIEW_SCORE": "6.0",
                    "FAKE_DSH_REVIEW_BLOCKING": "1",
                    "DSH_QUALITY_CONVERGENCE": "observe"})
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            pre = root / "pre-revision"
            pre.mkdir()
            (pre / "deliverable.md").write_text(
                "# Fake deliverable\n", encoding="utf-8")
            baseline_dir = (Path(cfg.state_dir) / "review-baselines" /
                            "original-run")
            review_context.capture_baseline(pre, baseline_dir)
            # Simulate a revision killed after writing its output but before
            # checkpoint.finish("revision-1"): the workspace is post-revision
            # while prior_rounds contains only review-1.
            (ws / "deliverable.md").write_text(
                "# Fake deliverable revised\n", encoding="utf-8")
            review1 = {
                "round": 1, "kind": "review", "score": 7.0,
                "blocking_defects": 1,
                "defects": ["Missing required fact in deliverable.md"],
                "fixes": ["Add the fact"], "signatures": ["a" * 64],
                "first_seen_round": {"a" * 64: 1}, "status": "ok",
                "accepted": False, "report": None, "report_text": None,
                "session_logs": [], "visual_impact": None,
                "creative_cap": False, "seconds": 1.0, "cost": None,
                "error": None, "stalled": False, "stalled_step": None,
                "write_violations": [], "resumed": True,
            }
            calls = []
            original = harness.build_review_task

            def capture(*call_args, **call_kwargs):
                calls.append(dict(call_kwargs))
                return original(*call_args, **call_kwargs)

            with mock.patch.object(harness, "build_review_task",
                                   side_effect=capture):
                _text, run, _path = harness.run_harness({
                    "prompt": "Build a tiny deliverable note with one required fact.",
                    "workspace": str(ws), "sandbox": "danger",
                    "quality": True, "independent_review": True,
                    "quality_rounds": 2, "subagents": 1,
                    "max_seconds": 30, "job_max_seconds": 120,
                    "live_scaling": False,
                    "_run_id": "run-retry-revision",
                    "_prior_rounds": [review1],
                    "_review_baseline_dir": str(baseline_dir),
                    "_review_baseline_round": 1,
                    "_prior_builder": {
                        "status": "ok", "answer": "builder done",
                        "seconds": 1.0,
                        "output_files": ["deliverable.md"],
                        "session_logs": [], "estimated_cost_usd": None,
                        "tokens_in": None, "tokens_out": None,
                        "transport": "headless",
                        "transport_session_id": None,
                    },
                }, cfg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("round_no"), 2)
        context = calls[0].get("review_context") or ""
        self.assertNotIn("No text changes were detected", context)
        self.assertIn("# Fake deliverable revised", context)
        self.assertIn("-# Fake deliverable", context)
        self.assertEqual(
            (run.get("review_baseline") or {}).get("round"), 1)

    def test_resume_after_mid_revision_kill_keeps_pre_revision_baseline(self):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({"FAKE_DSH_REVIEW_SCORES": "7.0,6.0",
                    "FAKE_DSH_REVIEW_BLOCKING": "1,1",
                    "DSH_QUALITY_CONVERGENCE": "observe"})
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            args = {
                "prompt": "Build a tiny deliverable note with one required fact.",
                "workspace": str(ws), "sandbox": "danger",
                "quality": True, "independent_review": True,
                "quality_rounds": 2, "subagents": 1,
                "max_seconds": 30, "job_max_seconds": 120,
                "live_scaling": False,
                "_run_id": "run-kill-revision",
            }
            real_execute = harness._execute_once

            def killed(cfg, workspace, task, **kwargs):
                if str(kwargs.get("label") or "").startswith(
                        "revision round 1"):
                    # The revision has already captured and persisted its
                    # pre-revision baseline; the process dies before finish().
                    raise RuntimeError("simulated kill during revision-1")
                return real_execute(cfg, workspace, task, **kwargs)

            with mock.patch.object(harness, "_execute_once",
                                   side_effect=killed):
                with self.assertRaises(RuntimeError):
                    harness.run_harness(args, cfg)
            saved, _json_path, _md_path = harness.find_run(
                "run-kill-revision", cfg)
            self.assertEqual(
                (saved.get("review_baseline") or {}).get("round"), 1)

            calls = []
            original_review = harness.build_review_task

            def capture(*call_args, **call_kwargs):
                calls.append(dict(call_kwargs))
                return original_review(*call_args, **call_kwargs)

            with mock.patch.object(harness, "build_review_task",
                                   side_effect=capture):
                _text, run, _path = harness.resume_harness(
                    {"resume_run_id": "run-kill-revision"}, cfg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("round_no"), 2)
        context = calls[0].get("review_context") or ""
        self.assertNotIn("No text changes were detected", context)
        self.assertIn("# Fake deliverable revised", context)
        self.assertIn("-# Fake deliverable", context)
        self.assertEqual([item["round"] for item in run["review_rounds"]
                          if item.get("kind") == "review"], [1, 2])

    def test_resume_passes_the_saved_pre_revision_baseline(self):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        env = base_env(root, fake_dsh=write_fake_dsh(root / "dsh"))
        cfg = load_config(config_path=root / "config.json", env=env)
        baseline_dir = (Path(cfg.state_dir) / "review-baselines" /
                        "original-run")
        baseline_dir.mkdir(parents=True)
        spec = {
            "prompt": "Build a tiny deliverable note.",
            "workspace": str(ws), "subagents": 1, "quality": True,
            "independent_review": True, "quality_rounds": 2,
            "transport": "headless", "skill": None, "add_dirs": [],
        }
        cp = checkpoints.Checkpoint(Path(cfg.state_dir) / "checkpoints",
                                    "original-run")
        cp.create(["builder", "review-1", "revision-1", "review-2",
                   "revision-2"],
                  metadata={"spec": spec, "task_type": "code",
                            "subagents": 1, "workspace": str(ws)})
        cp.finish("builder", status="done", output_files=["deliverable.md"])
        cp.finish("review-1", status="done", score=7.0, blocking=1,
                  defects=["Missing required fact in deliverable.md"],
                  fixes=["Add the fact"], signatures=["a" * 64],
                  first_seen_round={"a" * 64: 1})
        cp.finish("revision-1", status="done", files=["deliverable.md"])
        saved = {
            "id": "original-run", "kind": "harness",
            "created": "2026-11-06T10:00:00+00:00",
            "prompt": spec["prompt"], "workspace": str(ws),
            "title": "resume", "subagents": 1, "quality_used": True,
            "independent_review": True, "quality_rounds": 2,
            "status": "ok_below_gate", "answer": "builder answer",
            "review_rounds": [
                {"round": 1, "kind": "review", "score": 7.0,
                 "blocking_defects": 1,
                 "defects": ["Missing required fact in deliverable.md"],
                 "fixes": ["Add the fact"], "signatures": ["a" * 64],
                 "first_seen_round": {"a" * 64: 1}, "status": "ok"},
                {"round": 1, "kind": "revision", "status": "ok",
                 "files": ["deliverable.md"]},
            ],
            "review_baseline": {"dir": str(baseline_dir),
                                "sha256": "0" * 64, "files": 1},
        }
        harness.save_run(saved, cfg)
        captured = {}

        def fake_run_harness(args, cfg, **kwargs):
            captured.update(args)
            return "resumed", {"id": "resumed-run"}, "run.md"

        with mock.patch.object(harness, "run_harness",
                               side_effect=fake_run_harness):
            harness.resume_harness({"resume_run_id": "original-run"}, cfg)
        self.assertEqual(os.path.expanduser(
            captured.get("_review_baseline_dir") or ""), str(baseline_dir))

class CheckpointSignatureRestoreTests(unittest.TestCase):
    def test_prior_rounds_restores_signatures_and_first_seen(self):
        manifest = {
            "plan": ["review-1", "review-2"],
            "phases": {
                "review-1": {
                    "status": "done",
                    "data": {
                        "score": 7.0, "blocking": 1,
                        "defects": ["Missing required fact in report.md"],
                        "signatures": ["a" * 64],
                        "first_seen_round": {"a" * 64: 1},
                    },
                },
                "review-2": {
                    "status": "done",
                    "data": {
                        "score": 7.0, "blocking": 1,
                        "defects": ["Missing required fact in report.md"],
                        "signatures": ["a" * 64],
                        "first_seen_round": {"a" * 64: 1},
                    },
                },
            },
        }
        rounds, _accepted = harness._prior_rounds(manifest)
        reviews = [item for item in rounds if item["kind"] == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["signatures"], ["a" * 64])
        self.assertEqual(reviews[1]["first_seen_round"], {"a" * 64: 1})


class ConvergenceResolutionTests(unittest.TestCase):
    """``accept_below_gate``/``stop`` must resolve the pending question."""

    def _stop_then_resume(self, option):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=fake)
        env.update({
            "DSH_QUALITY_CONVERGENCE": "auto",
            "FAKE_DSH_REVIEW_SCORES": "7.0,7.0",
            "FAKE_DSH_REVIEW_BLOCKING": "1,1",
        })
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            work = jobs._work_store(cfg).admit("resolution-" + option)
            args = {
                "prompt": "Build a tiny deliverable note with one required fact.",
                "workspace": str(ws), "sandbox": "danger",
                "quality": True, "independent_review": True,
                "quality_rounds": 2, "subagents": 1,
                "max_seconds": 30, "job_max_seconds": 120,
                "_run_id": "run-stop-" + option.replace("_", "-"),
                "_work_id": work["work_id"],
                "_attempt_no": work["attempt_no"],
            }
            _text, stopped, _path = harness.run_harness(args, cfg)
        self.assertTrue(stopped.get("convergence_attention"))
        work_id = work["work_id"]
        journal = events.EventLog(Path(cfg.state_dir) / "work" / work_id)
        self.assertIsNotNone(events.pending_question(journal.read_since(0)))

        job_id = "job-" + option.replace("_", "-")
        job_dir = Path(cfg.jobs_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        spec = job_dir / (job_id + ".job.json")
        spec.write_text(json.dumps({
            "tool": "harness_deepseek", "run_id": job_id,
            "work_id": work_id, "attempt_no": work["attempt_no"],
            "args": {"resume_run_id": stopped["id"], "option": option},
        }), encoding="utf-8")
        jobs.write_status(job_dir / (job_id + ".status.json"),
                          status="queued", work_id=work_id,
                          attempt_no=work["attempt_no"])
        captured = {}

        def handler(args, cfg):
            text, resumed, _path = harness.resume_harness(args, cfg)
            captured["run"] = resumed
            return text

        with mock.patch.dict(os.environ, env, clear=False):
            jobs.run_job(spec, {"harness_deepseek": handler}, cfg)
        return cfg, work_id, job_id, captured["run"], journal

    def _assert_resolved(self, option):
        cfg, work_id, job_id, resumed, journal = self._stop_then_resume(option)
        self.assertEqual(resumed.get("attention_resolution"), option)
        self.assertFalse(resumed.get("convergence_attention"))
        self.assertEqual(resumed.get("status"), "ok_below_gate")
        self.assertIs(resumed.get("review_passed"), False)

        all_events = journal.read_since(0)
        self.assertIsNone(events.pending_question(all_events))
        terminal = events.terminal_after_latest_admission(all_events)
        self.assertIsNotNone(terminal)
        self.assertIn(terminal.get("type"), ("result", "failed"))
        self.assertEqual(journal.read_result().get("status"), "ok_below_gate")

        work = jobs._work_store(cfg).load(work_id)
        self.assertEqual(work.get("state"), "finished")
        self.assertEqual(work.get("terminal_status"), "ok_below_gate")
        state = json.loads(
            (Path(cfg.jobs_dir) / (job_id + ".status.json")).read_text())
        self.assertEqual(state.get("status"), "done")
        self.assertEqual(state.get("progress"), "finished")
        self.assertIsNone(state.get("question"))

        server._task_stores.clear()
        self.addCleanup(server._task_stores.clear)
        notify = mock.patch.object(server, "notify", return_value=None)
        notify.start()
        self.addCleanup(notify.stop)
        store = server._task_store(cfg)
        task = store.create("harness_deepseek",
                            {"prompt": "build the report"}, ttl=3600)
        # The original convergence stop had left this task input_required.
        store.update(task["taskId"], job_id=job_id,
                     status="input_required", question="pending?",
                     options=["resume_with_scope", "accept_below_gate", "stop"])
        task = server._refresh_task_from_job(store, task["taskId"], cfg)
        self.assertEqual(task.get("status"), "completed")
        self.assertIsNone(task.get("question"))
        self.assertFalse(task.get("options"))

    def test_accept_below_gate_completes_the_task(self):
        self._assert_resolved("accept_below_gate")

    def test_stop_completes_the_task(self):
        self._assert_resolved("stop")
