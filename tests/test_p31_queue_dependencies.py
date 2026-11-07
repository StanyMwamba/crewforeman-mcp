"""P3.1 queue dependencies, urgent ordering, and the idle-slot safety net.

Pure-stdlib unittest tests.  Scratch directories honour TMPDIR and fall back to
the workspace when the platform temp area is not writable in the sandbox.
"""

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import admission, jobs, server
from deepseek_mcp.admission import (
    AdmissionController, AdmissionStore, DependencyError, PRIORITY_BUILD,
    PRIORITY_RESUME,
)
from deepseek_mcp.config import load_config


def _tmpdir(case, prefix="p31-"):
    base = os.environ.get("TMPDIR") or os.getcwd()
    try:
        tmp = tempfile.TemporaryDirectory(prefix=prefix, dir=base)
    except OSError:
        tmp = tempfile.TemporaryDirectory(prefix=prefix, dir=os.getcwd())
    case.addCleanup(tmp.cleanup)
    return tmp.name


def _cfg(root, max_jobs=1, reserve=True, **extra):
    values = {
        "state_dir": os.path.join(root, "state"),
        "jobs_dir": os.path.join(root, "jobs"),
        "harness_workspace": os.path.join(root, "ws"),
        "max_harness_jobs": max_jobs,
        "reserve_resume_slot": reserve,
        "admission_heartbeat_seconds": 30.0,
        "admission_lease_seconds": 90.0,
    }
    values.update(extra)
    return types.SimpleNamespace(**values)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.root = _tmpdir(self)
        self.clock = [1000.0]
        self.alive = {os.getpid()}
        self.extra_probe = {}
        self.store = self.make_store()

    def make_store(self, probe=None):
        if probe is None:
            probe = self.extra_probe
        return AdmissionStore(
            os.path.join(self.root, "queue"),
            heartbeat_seconds=30, lease_seconds=90,
            now=lambda: self.clock[0],
            pid_alive=lambda pid: pid in self.alive,
            dependency_probe=probe)

    def controller(self, store=None, max_jobs=1, reserve=True, **kwargs):
        cfg = _cfg(self.root, max_jobs=max_jobs, reserve=reserve)
        return AdmissionController(
            cfg, store=store or self.store, detected_limit=8, **kwargs)


class UrgentOrderingTests(_StoreCase):
    def test_urgent_is_after_resume_and_before_normal(self):
        self.store.enqueue("normal")
        self.store.enqueue("urgent", urgent=True)
        self.store.enqueue("resume", PRIORITY_RESUME)
        self.assertEqual([r["work_id"] for r in self.store.pending()],
                         ["resume", "urgent", "normal"])
        fields = self.store.status_fields("urgent")
        self.assertEqual(fields["priority_label"], "P1 urgent")
        self.assertTrue(fields["urgent"])
        self.assertEqual(
            self.store.status_fields("normal")["priority_label"],
            "P1 new build")

    def test_urgent_controller_starts_before_older_normal(self):
        started = []
        ctrl = self.controller(spawn=lambda r: started.append(r["work_id"]))
        ctrl.submit("holder", PRIORITY_BUILD, spawn=started.append)
        ctrl.submit("older", PRIORITY_BUILD, spawn=started.append)
        ctrl.submit("urgent", PRIORITY_BUILD, spawn=started.append,
                    urgent=True)
        self.store.release("holder", "done")
        started_records = ctrl.run_once(spawn=lambda r: started.append(
            r["work_id"]))
        self.assertEqual([r["work_id"] for r in started_records], ["urgent"])
        self.assertEqual(self.store.position("older"), 1)

    def test_submit_does_not_bypass_queued_urgent(self):
        ctrl = self.controller(
            max_jobs=1, spawn=lambda r: None)
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        ctrl.submit("urgent", PRIORITY_BUILD, urgent=True,
                    spawn=lambda r: None)
        self.store.release("holder", "done")
        result = ctrl.submit("new-normal", PRIORITY_BUILD,
                             spawn=lambda r: None)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.position("urgent"), 1)
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["urgent"])

    def test_submit_urgent_still_jumps_queued_normal(self):
        ctrl = self.controller(
            max_jobs=1, spawn=lambda r: None)
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        ctrl.submit("older", PRIORITY_BUILD, spawn=lambda r: None)
        self.store.release("holder", "done")
        result = ctrl.submit("urgent", PRIORITY_BUILD, urgent=True,
                             spawn=lambda r: None)
        self.assertEqual(result["state"], "started")
        self.assertEqual(self.store.position("older"), 1)

    def test_p0_resume_still_beats_urgent(self):
        ctrl = self.controller(spawn=lambda r: None)
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        ctrl.submit("urgent", PRIORITY_BUILD, spawn=lambda r: None,
                    urgent=True)
        ctrl.submit("resume", PRIORITY_RESUME, spawn=lambda r: None)
        self.store.release("holder", "done")
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["resume"])
        self.assertEqual(self.store.position("urgent"), 1)


class DependencyStoreTests(_StoreCase):
    def test_waiting_dependency_holds_no_slot_or_lease(self):
        self.store.enqueue("dep", run_id="dep")
        claimed = self.store.claim("dep", pid=os.getpid())
        self.assertIsNotNone(claimed)
        self.store.enqueue("a", run_id="a", depends_on="dep")
        self.assertEqual(self.store.active_count(), 1)
        self.assertEqual(self.store.active()[0]["work_id"], "dep")
        self.assertFalse(os.path.exists(
            os.path.join(self.store.leases_dir, "a.json")))
        fields = self.store.status_fields("a")
        self.assertEqual(fields["waiting_on"], "dep")
        self.assertEqual(fields["depends_on"], "dep")
        self.assertEqual(fields["dependency_status"], "waiting")
        self.assertEqual(fields["status"], "queued")
        # Even a direct claim must not lease a dependency-waiting record.
        self.assertIsNone(self.store.claim("a"))

    def test_dependency_gets_queue_priority(self):
        # C is older, then B (the dependency), then A which waits on B.
        self.store.enqueue("c")
        self.clock[0] += 1
        self.store.enqueue("b")
        self.clock[0] += 1
        self.extra_probe["b"] = {"status": "running"}
        self.store.enqueue("a", depends_on="b")
        self.assertEqual([r["work_id"] for r in self.store.pending()],
                         ["b", "c", "a"])
        self.assertEqual(self.store.position("b"), 1)

    def test_waiting_record_survives_restart(self):
        self.store.enqueue("dep", run_id="dep")
        self.store.claim("dep", pid=os.getpid())
        self.store.enqueue("a", run_id="a", depends_on="dep")
        reopened = AdmissionStore(
            self.store.root, heartbeat_seconds=30, lease_seconds=90,
            now=lambda: self.clock[0],
            pid_alive=lambda pid: pid in self.alive,
            dependency_probe=self.extra_probe)
        self.assertEqual(reopened.dependency_state("a")["state"], "waiting")
        self.assertEqual(reopened.status_fields("a")["waiting_on"], "dep")
        self.assertIsNone(reopened.claim("a", pid=os.getpid()))

    def test_old_record_without_new_fields_is_normal(self):
        self.store.enqueue("old")
        fields = self.store.status_fields("old")
        self.assertFalse(fields["urgent"])
        self.assertIsNone(fields["depends_on"])
        self.assertIsNone(fields["waiting_on"])
        self.assertEqual(fields["priority_label"], "P1 new build")

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(DependencyError) as ctx:
            self.store.enqueue("a", depends_on="missing")
        self.assertIn("Unknown dependency", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))
        self.assertEqual(self.store.pending(), [])

    def test_dependency_finished_with_error_rejected(self):
        self.extra_probe["bad"] = {"status": "error", "error": "model exploded"}
        with self.assertRaises(DependencyError) as ctx:
            self.store.enqueue("a", depends_on="bad")
        message = str(ctx.exception)
        self.assertIn("finished with error", message)
        self.assertIn("model exploded", message)

    def test_cancelled_dependency_rejected(self):
        self.extra_probe["gone"] = {"status": "cancelled"}
        with self.assertRaises(DependencyError):
            self.store.enqueue("a", depends_on="gone")

    def test_cycle_rejected_and_chain_reported(self):
        self.store.enqueue("b")
        self.store.enqueue("a", depends_on="b")
        with self.assertRaises(DependencyError) as ctx:
            self.store.enqueue("b", depends_on="a")
        message = str(ctx.exception)
        self.assertIn("cycle", message.lower())
        self.assertIn("b -> a -> b", message)

    def test_self_dependency_rejected(self):
        with self.assertRaises(DependencyError) as ctx:
            self.store.enqueue("a", depends_on="a")
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_satisfied_dependency_starts_immediately(self):
        self.extra_probe["done"] = {"status": "done"}
        spawned = []
        ctrl = self.controller(spawn=lambda r: spawned.append(r["work_id"]))
        result = ctrl.submit("a", depends_on="done",
                             spawn=lambda r: spawned.append(r["work_id"]))
        self.assertEqual(result["state"], "started")
        self.assertEqual(spawned, ["a"])
        self.assertEqual(self.store.status_fields("a")["dependency_status"],
                         "ready")

    def test_dependent_failed_when_dependency_fails(self):
        failures = []
        self.store.enqueue("dep")
        self.store.enqueue("a", depends_on="dep")
        ctrl = self.controller(fail_sink=lambda record, message:
                               failures.append((record["work_id"], message)))
        self.store.release("dep", "error")
        ctrl.run_once(spawn=lambda r: None)
        item = self.store._load_item("a")
        self.assertEqual(item["status"], "failed")
        self.assertIn("Dependency run_id 'dep'", item["error"])
        self.assertTrue(failures)
        self.assertEqual(self.store.pending(), [])


class SavedRunProbeTests(unittest.TestCase):
    def test_saved_run_record_fallback(self):
        root = _tmpdir(self)
        runs = os.path.join(root, "runs")
        os.makedirs(runs)
        cfg = _cfg(root, log_dir=runs)
        with open(os.path.join(runs, "done-run.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"status": "ok_below_gate"}, handle)
        with open(os.path.join(runs, "bad-run.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"status": "error", "error": "vendor down"}, handle)
        probe = admission._status_probe(cfg)
        self.assertEqual(probe("done-run")["status"], "ok_below_gate")
        store = AdmissionStore(os.path.join(root, "queue"),
                               dependency_probe=probe)
        store.enqueue("a", depends_on="done-run")
        self.assertEqual(store.dependency_state("a")["state"], "ready")
        with self.assertRaises(DependencyError) as ctx:
            store.enqueue("b", depends_on="bad-run")
        self.assertIn("vendor down", str(ctx.exception))


class DeadlockTests(_StoreCase):
    def test_cap_one_dependency_waits_for_queued_dependency(self):
        spawned = []
        ctrl = self.controller(max_jobs=1,
                               spawn=lambda r: spawned.append(r["work_id"]))
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        ctrl.submit("b", PRIORITY_BUILD, spawn=lambda r: None)
        waiting = ctrl.submit("a", PRIORITY_BUILD, depends_on="b",
                              spawn=lambda r: None)
        self.assertEqual(waiting["state"], "queued")
        self.assertEqual(waiting["waiting_on"], "b")
        self.assertEqual(self.store.active_count(), 1)
        self.assertEqual(self.store.position("b"), 1)
        self.assertEqual(self.store.position("a"), 2)

        self.store.release("holder", "done")
        first = ctrl.run_once(spawn=lambda r: spawned.append(r["work_id"]))
        self.assertEqual([r["work_id"] for r in first], ["b"])
        self.assertEqual(self.store.active_count(), 1)

        self.store.release("b", "done")
        second = ctrl.run_once(spawn=lambda r: spawned.append(r["work_id"]))
        self.assertEqual([r["work_id"] for r in second], ["a"])
        self.assertEqual(spawned, ["b", "a"])

    def test_submit_does_not_bypass_queued_dependency(self):
        ctrl = self.controller(max_jobs=1, spawn=lambda r: None)
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        ctrl.submit("b", PRIORITY_BUILD, spawn=lambda r: None)
        ctrl.submit("a", PRIORITY_BUILD, depends_on="b",
                    spawn=lambda r: None)
        self.store.release("holder", "done")
        result = ctrl.submit("new-normal", PRIORITY_BUILD,
                             spawn=lambda r: None)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.position("b"), 1)
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["b"])

    def test_dependency_wait_not_claimable_even_with_free_slot(self):
        # B is queued (not running), A waits on B.  A must not take the free
        # slot, otherwise B could never finish and the pair would deadlock.
        self.store.enqueue("b")
        self.store.enqueue("a", depends_on="b")
        ctrl = self.controller(max_jobs=2)
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["b"])
        self.assertEqual(self.store.status_fields("a")["waiting_on"], "b")


class SafetyNetTests(_StoreCase):
    def _active(self, work_id, pid=None):
        self.store.enqueue(work_id, run_id=work_id)
        self.store.claim(work_id, pid=pid or os.getpid())

    def test_all_idle_admits_one_and_records_event(self):
        self._active("x")
        self.store.enqueue("y", run_id="y")
        ctrl = self.controller(idle_probe=lambda record: True)
        self.assertEqual(ctrl.hard_ceiling, 2)
        claimed = ctrl.safety_net(spawn=lambda r: None)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["work_id"], "y")
        self.assertEqual(self.store.active_count(), 2)
        event_path = os.path.join(self.store.root, "events.jsonl")
        self.assertTrue(os.path.exists(event_path))
        with open(event_path, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(events[-1]["type"], "admission_safety_net")
        self.assertEqual(events[-1]["payload"]["work_id"], "y")
        self.assertEqual(events[-1]["payload"]["ceiling"], 2)
        self.assertEqual(events[-1]["payload"]["active_count"], 2)
        self.assertIn("x", events[-1]["payload"]["idle"])
        # Common event fields are also readable at the top level.
        self.assertEqual(events[-1]["run_id"], "y")
        self.assertEqual(events[-1]["ceiling"], 2)

    def test_safety_net_requires_a_spawn_function(self):
        self._active("x")
        self.store.enqueue("y", run_id="y")
        ctrl = self.controller(idle_probe=lambda record: True)
        self.assertIsNone(ctrl.safety_net())
        self.assertEqual(self.store.active_count(), 1)
        self.assertEqual(self.store.position("y"), 1)

    def test_busy_slot_refuses(self):
        self._active("x")
        self.store.enqueue("y", run_id="y")
        ctrl = self.controller(
            idle_probe=lambda record: record["work_id"] != "x")
        self.assertIsNone(ctrl.safety_net(spawn=lambda r: None))
        self.assertEqual(self.store.active_count(), 1)
        self.assertEqual(self.store.position("y"), 1)

    def test_ceiling_is_never_exceeded(self):
        self._active("x")
        self._active("y")
        self.assertEqual(self.store.active_count(), 2)
        self.store.enqueue("z", run_id="z")
        ctrl = self.controller(idle_probe=lambda record: True)
        self.assertEqual(ctrl.hard_ceiling, 2)
        for _ in range(5):
            self.assertIsNone(ctrl.safety_net(spawn=lambda r: None))
        self.assertEqual(self.store.active_count(), 2)
        self.assertFalse(os.path.exists(
            os.path.join(self.store.leases_dir, "z.json")))

    def test_no_admit_when_a_normal_slot_is_free(self):
        self._active("x")
        self.store.enqueue("y", run_id="y")
        ctrl = self.controller(max_jobs=2,
                               idle_probe=lambda record: True)
        self.assertIsNone(ctrl.safety_net(spawn=lambda r: None))
        self.assertEqual(self.store.active_count(), 1)

    def test_dispatcher_loop_invokes_safety_net(self):
        calls = []
        ctrl = self.controller()
        with mock.patch.object(ctrl, "run_once", return_value=[]), \
                mock.patch.object(ctrl, "maybe_safety_net",
                                  side_effect=lambda: calls.append(1)), \
                mock.patch.object(self.store, "pending", return_value=[]):
            ctrl._loop()
        self.assertEqual(calls, [1])

    def test_maybe_safety_net_force_entry_point(self):
        self._active("x")
        self.store.enqueue("y", run_id="y")
        ctrl = self.controller(idle_probe=lambda record: True)
        claimed = ctrl.maybe_safety_net(force=True, spawn=lambda r: None)
        self.assertEqual(claimed["work_id"], "y")
        self.assertEqual(self.store.active_count(), 2)

    def test_dependency_waiter_never_claimed(self):
        self._active("x")
        self.store.enqueue("b", run_id="b")
        self.store.enqueue("a", run_id="a", depends_on="b")
        ctrl = self.controller(idle_probe=lambda record: True)
        claimed = ctrl.safety_net(spawn=lambda r: None)
        self.assertEqual(claimed["work_id"], "b")
        self.assertEqual(self.store.active_count(), 2)
        self.assertEqual(self.store.status_fields("a")["waiting_on"], "b")
        # At the hard ceiling, the safety net stops; A waits for B to finish.
        self.assertIsNone(ctrl.safety_net(spawn=lambda r: None))


class SchemaTests(unittest.TestCase):
    def test_tool_names_unchanged_and_params_additive(self):
        names = [tool["name"] for tool in server.TOOLS]
        self.assertEqual(len(names), 20)
        self.assertEqual(sorted(names), sorted([
            "approve_skill", "ask_deepseek", "consult_deepseek",
            "deepseek_job_cancel", "deepseek_job_status",
            "deepseek_maintenance", "harness_deepseek", "keep_awake",
            "list_deepseek_runs", "list_skills", "rate_deepseek_run",
            "read_deepseek_run", "read_skill", "reject_skill",
            "restart_client_app", "resume_deepseek", "retire_skill",
            "rollback_skill", "swarm_deepseek", "take_screenshot",
        ]))
        self.assertIn("harness_deepseek", names)
        harness = next(tool for tool in server.TOOLS
                       if tool["name"] == "harness_deepseek")
        properties = harness["inputSchema"]["properties"]
        self.assertEqual(properties["urgent"]["type"], "boolean")
        self.assertEqual(properties["depends_on"]["type"], "string")
        # Existing properties remain; the new ones are optional.
        self.assertIn("prompt", properties)
        self.assertIn("workspace", properties)
        self.assertNotIn("urgent", harness["inputSchema"]["required"])
        self.assertNotIn("depends_on", harness["inputSchema"]["required"])


class JobStatusProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = _tmpdir(self)
        self.cfg = load_config(include_file=False)
        self.cfg._values.update({
            "state_dir": os.path.join(self.root, "state"),
            "jobs_dir": os.path.join(self.root, "jobs"),
            "harness_workspace": os.path.join(self.root, "ws"),
            "max_harness_jobs": 1,
            "reserve_resume_slot": True,
            "admission_heartbeat_seconds": 30.0,
            "admission_lease_seconds": 90.0,
        })
        os.makedirs(self.cfg.jobs_dir)
        os.makedirs(self.cfg.harness_workspace)

    def _status(self, run_id, **fields):
        path = os.path.join(self.cfg.jobs_dir, run_id + ".status.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dict(fields, status=fields.get("status", "queued")),
                      handle)
        return path

    def test_plain_and_structured_status_expose_waiting_on(self):
        store = jobs._admission_store_for(self.cfg)
        store.enqueue("dep", run_id="dep")
        store.claim("dep", pid=os.getpid())
        store.enqueue("a", run_id="a", depends_on="dep")
        self._status("a", status="queued", admission_work_id="a",
                     queue_position=1, priority=1,
                     priority_label="P1 new build")
        text = jobs.tool_status({"run_id": "a"}, self.cfg)
        self.assertIn("waiting for run dep", text)
        payload = jobs.tool_status(
            {"run_id": "a", "events_since": 0}, self.cfg)
        self.assertEqual(payload["waiting_on"], "dep")
        self.assertEqual(payload["depends_on"], "dep")
        self.assertEqual(payload["priority_label"], "P1 new build")

    def test_urgent_label_in_structured_status(self):
        store = jobs._admission_store_for(self.cfg)
        store.enqueue("u", run_id="u", urgent=True)
        self._status("u", status="queued", admission_work_id="u",
                     queue_position=1, priority=1,
                     priority_label="P1 urgent")
        payload = jobs.tool_status(
            {"run_id": "u", "events_since": 0}, self.cfg)
        self.assertTrue(payload["urgent"])
        self.assertEqual(payload["priority_label"], "P1 urgent")
        self.assertIsNone(payload["depends_on"])

    def test_old_status_file_reads_without_new_fields(self):
        self._status("old", status="queued", admission_work_id="old",
                     queue_position=1, priority=1,
                     priority_label="P1 new build")
        jobs._admission_store_for(self.cfg).enqueue("old", run_id="old")
        text = jobs.tool_status({"run_id": "old"}, self.cfg)
        self.assertIn("waiting for a free slot", text)
        payload = jobs.tool_status(
            {"run_id": "old", "events_since": 0}, self.cfg)
        self.assertIsNone(payload["waiting_on"])
        self.assertFalse(payload["urgent"])

    def test_dependency_rejection_and_no_prompt_guessing(self):
        captured = []

        def on_start(run_id, _path):
            captured.append(run_id)

        proc = mock.Mock(pid=os.getpid())
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen", return_value=proc), \
                mock.patch.object(admission.AdmissionController, "start",
                                  return_value=None):
            # Text mentioning another run id must never become a dependency.
            jobs.start_job("harness_deepseek", {
                "prompt": "please wait for run abcdef123456 before starting",
                "workspace": self.cfg.harness_workspace,
            }, cfg=self.cfg, wait=0, on_start=on_start)
            with open(os.path.join(self.cfg.jobs_dir,
                                   captured[0] + ".status.json"),
                      encoding="utf-8") as handle:
                guessed = json.load(handle)
            self.assertIsNone(guessed.get("depends_on"))
            self.assertIsNone(guessed.get("waiting_on"))

            with self.assertRaises(RuntimeError) as ctx:
                jobs.start_job("harness_deepseek", {
                    "prompt": "dependent",
                    "workspace": self.cfg.harness_workspace,
                    "depends_on": "nosuchrun",
                }, cfg=self.cfg, wait=0)
            self.assertIn("Unknown dependency", str(ctx.exception))
            self.assertNotIn("Admission unavailable", str(ctx.exception))

            self._status("badrun", status="error", error="model exploded")
            with self.assertRaises(RuntimeError) as ctx:
                jobs.start_job("harness_deepseek", {
                    "prompt": "dependent",
                    "workspace": self.cfg.harness_workspace,
                    "depends_on": "badrun",
                }, cfg=self.cfg, wait=0)
            self.assertIn("finished with error", str(ctx.exception))
            self.assertIn("model exploded", str(ctx.exception))

    def test_late_dependency_failure_fails_dependent_status(self):
        captured = []

        def on_start(run_id, _path):
            captured.append(run_id)

        proc = mock.Mock(pid=os.getpid())
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen", return_value=proc), \
                mock.patch.object(admission.AdmissionController, "start",
                                  return_value=None):
            jobs.start_job("harness_deepseek", {
                "prompt": "holder",
                "workspace": self.cfg.harness_workspace,
            }, cfg=self.cfg, wait=0, on_start=on_start)
            jobs.start_job("harness_deepseek", {
                "prompt": "dependency",
                "workspace": self.cfg.harness_workspace,
            }, cfg=self.cfg, wait=0, on_start=on_start)
            dependency_id = captured[1]
            jobs.start_job("harness_deepseek", {
                "prompt": "dependent",
                "workspace": self.cfg.harness_workspace,
                "depends_on": dependency_id,
            }, cfg=self.cfg, wait=0, on_start=on_start)
            dependent_id = captured[2]

            controller = jobs._admission_controller(self.cfg)
            controller.store.fail(dependency_id, "dependency exploded")
            controller.run_once(spawn=lambda record: None)
        status = json.loads((Path(self.cfg.jobs_dir) /
                             (dependent_id + ".status.json"))
                            .read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "error")
        self.assertIn("finished with error", status["error"])
        with self.assertRaises(RuntimeError) as ctx:
            jobs.tool_status({"run_id": dependent_id}, self.cfg)
        self.assertIn("dependency", str(ctx.exception).lower())

    def test_start_job_passes_urgent_and_dependency(self):
        captured = []

        def on_start(run_id, _path):
            captured.append(run_id)

        proc = mock.Mock(pid=os.getpid())
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen", return_value=proc), \
                mock.patch.object(admission.AdmissionController, "start",
                                  return_value=None):
            jobs.start_job("harness_deepseek", {
                "prompt": "holder",
                "workspace": self.cfg.harness_workspace,
            }, cfg=self.cfg, wait=0, on_start=on_start)
            holder = captured[0]
            jobs.start_job("harness_deepseek", {
                "prompt": "dependent",
                "workspace": self.cfg.harness_workspace,
                "urgent": True,
                "depends_on": holder,
            }, cfg=self.cfg, wait=0, on_start=on_start)
            dependent = captured[1]
        with open(os.path.join(self.cfg.jobs_dir,
                               dependent + ".status.json"),
                  encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["status"], "queued")
        self.assertTrue(status["urgent"])
        self.assertEqual(status["depends_on"], holder)
        self.assertEqual(status["waiting_on"], holder)
        self.assertEqual(status["priority_label"], "P1 urgent")



class QueueOrderRegressionTests(_StoreCase):
    """Regressions: equal-rank self-claim and needs_input dependencies."""

    def test_equal_rank_urgent_does_not_jump_older_urgent(self):
        ctrl = self.controller(max_jobs=1, spawn=lambda r: None)
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        self.clock[0] += 1
        ctrl.submit("older-urgent", PRIORITY_BUILD, urgent=True,
                    spawn=lambda r: None)
        self.store.release("holder", "done")
        self.clock[0] += 1
        result = ctrl.submit("new-urgent", PRIORITY_BUILD, urgent=True,
                             spawn=lambda r: None)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.position("older-urgent"), 1)
        self.assertEqual(self.store.position("new-urgent"), 2)
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["older-urgent"])

    def test_no_reserve_urgent_never_bypasses_queued_p0(self):
        started = []
        ctrl = self.controller(max_jobs=2, reserve=False,
                               spawn=lambda r: started.append(r["work_id"]))
        ctrl.submit("holder", PRIORITY_BUILD,
                    spawn=lambda r: started.append(r["work_id"]))
        self.assertEqual(started, ["holder"])
        self.clock[0] += 1
        self.store.enqueue("p0", PRIORITY_RESUME)
        self.clock[0] += 1
        result = ctrl.submit("new-urgent", PRIORITY_BUILD, urgent=True,
                             spawn=lambda r: started.append(r["work_id"]))
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.position("p0"), 1)
        self.assertEqual(self.store.position("new-urgent"), 2)
        restarted = ctrl.run_once(spawn=lambda r: started.append(r["work_id"]))
        self.assertEqual([r["work_id"] for r in restarted], ["p0"])

    def test_new_urgent_does_not_bypass_queued_dependency_with_waiter(self):
        # Exact 7.8/10 review repro: one free slot, older dependency queued and
        # a waiting dependent, then submit a new urgent job.
        ctrl = self.controller(max_jobs=1, spawn=lambda r: None)
        ctrl.submit("holder", PRIORITY_BUILD, spawn=lambda r: None)
        self.store.enqueue("older-dependency")
        self.store.enqueue("waiting-dependent", depends_on="older-dependency")
        self.store.release("holder", "done")
        self.clock[0] += 1
        result = ctrl.submit("new-urgent", PRIORITY_BUILD, urgent=True,
                             spawn=lambda r: None)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.position("older-dependency"), 1)
        self.assertEqual(self.store.position("new-urgent"), 2)
        self.assertFalse(os.path.exists(
            os.path.join(self.store.leases_dir, "new-urgent.json")))
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started],
                         ["older-dependency"])
        self.assertEqual(self.store.position("new-urgent"), 1)
        self.assertEqual(self.store.position("waiting-dependent"), 2)

    def test_needs_input_dependency_stays_waiting_until_finished(self):
        self.extra_probe["dep"] = {"status": "done", "progress": "needs_input"}
        ctrl = self.controller(max_jobs=1, spawn=lambda r: None)
        result = ctrl.submit("a", depends_on="dep", spawn=lambda r: None)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.dependency_state("a")["state"], "waiting")
        self.assertEqual(self.store.status_fields("a")["dependency_status"],
                         "waiting")
        self.assertFalse(os.path.exists(
            os.path.join(self.store.leases_dir, "a.json")))
        self.assertEqual(ctrl.run_once(spawn=lambda r: None), [])
        self.assertEqual(self.store.active_count(), 0)
        self.extra_probe["dep"] = {"status": "done", "progress": "finished"}
        self.assertEqual(self.store.dependency_state("a")["state"], "ready")
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["a"])

    def test_done_queue_item_needs_input_probe_stays_waiting(self):
        # Review repro: jobs.py releases the admission record as done while
        # the job's progress is still done/needs_input.  The terminal queue
        # item must not mask the needs_input hold.
        self.store.enqueue("dep", run_id="dep")
        self.store.release("dep", "done")
        self.extra_probe["dep"] = {"status": "done",
                                   "progress": "needs_input"}
        ctrl = self.controller(max_jobs=1, spawn=lambda r: None)
        result = ctrl.submit("a", depends_on="dep", spawn=lambda r: None)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.store.dependency_state("a")["state"], "waiting")
        self.assertEqual(self.store.status_fields("a")["dependency_status"],
                         "waiting")
        self.assertFalse(os.path.exists(
            os.path.join(self.store.leases_dir, "a.json")))
        self.assertEqual(ctrl.run_once(spawn=lambda r: None), [])
        self.assertEqual(self.store.active_count(), 0)

        self.extra_probe["dep"] = {"status": "done", "progress": "finished"}
        self.assertEqual(self.store.dependency_state("a")["state"], "ready")
        started = ctrl.run_once(spawn=lambda r: None)
        self.assertEqual([r["work_id"] for r in started], ["a"])


if __name__ == "__main__":
    unittest.main()
