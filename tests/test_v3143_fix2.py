"""Focused tests for the depends_on policy fix (3.14.3, wave fix2).

Pure stdlib unittest.  Temporary directories only (tempfile), no network, no
Harness child, Windows-compatible paths.  Run from the repo root:

    python -m pytest tests/test_candidate.py -q
    python -m unittest tests.test_candidate -v
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import admission, jobs
from deepseek_mcp.admission import (
    AdmissionController,
    AdmissionStore,
    DependencyError,
    PRIORITY_RESUME,
)
from deepseek_mcp.config import load_config


class _Cfg(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def _cfg(root, **overrides):
    values = {
        "state_dir": str(root / "state"),
        "jobs_dir": str(root / "jobs"),
        "harness_workspace": str(root / "ws"),
        "max_harness_jobs": 2,
        "daily_limit_usd": None,
        "job_max_cost_usd": None,
        "pipeline_enabled": True,
        "pipeline_max_extra_jobs": 1,
        "pipeline_require_disjoint": True,
        "pipeline_max_pending_patches": 8,
        "pipeline_queue_scan": "head",
        "pipeline_min_remaining_usd": 1.0,
        "reserve_resume_slot": True,
        "admission_heartbeat_seconds": 30.0,
        "admission_lease_seconds": 90.0,
        "depends_on_policy": "success",
    }
    values.update(overrides)
    return _Cfg(**values)


class DependsOnPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="fix2-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "state").mkdir()
        self.cfg = _cfg(self.root)
        self.store = AdmissionStore(str(self.root / "queue"))

    def _controller(self, max_jobs=2):
        return AdmissionController(self.cfg, store=self.store,
                                   detected_limit=max_jobs)

    def _errored_dependency(self, run_id="dep"):
        self.store.enqueue(run_id, run_id=run_id)
        self.store.fail(run_id, "model exploded", status="error")
        return self.store._load_item(run_id)

    # -- policy=finished ---------------------------------------------------
    def test_finished_policy_starts_on_errored_dependency(self):
        self._errored_dependency("dep")
        self.store.enqueue(
            "probe", run_id="probe", depends_on="dep",
            depends_on_policy="finished")
        probe = self.store._load_item("probe")
        self.assertEqual(probe["depends_on_policy"], "finished")
        state = self.store.dependency_state(probe)
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["terminal"], "error")
        self.assertFalse(self._controller()._reject_bad_dependency_locked(
            probe))
        self.store.release("probe", status="done")

        spawned = []
        ctrl = self._controller()
        result = ctrl.submit(
            "started-child", run_id="started-child", depends_on="dep",
            depends_on_policy="finished",
            spawn=lambda rec: spawned.append(rec["work_id"]))
        self.assertEqual(result["state"], "started")
        self.assertEqual(spawned, ["started-child"])
        self.assertEqual(
            self.store.status_fields("started-child")["dependency_status"],
            "ready")

    # -- policy=success ----------------------------------------------------
    def test_success_policy_clear_message_and_does_not_start(self):
        self._errored_dependency("dep")
        spawned = []
        ctrl = self._controller()
        with self.assertRaises(DependencyError) as ctx:
            ctrl.submit("child", run_id="child", depends_on="dep",
                        spawn=lambda rec: spawned.append(rec["work_id"]))
        message = str(ctx.exception)
        self.assertIn("Dependency run_id", message)
        self.assertIn("dep", message)
        self.assertIn("finished with error", message)
        self.assertIn("error", message)
        self.assertIn("depends_on_policy='success'", message)
        self.assertIn("depends_on_policy='finished'", message)
        self.assertEqual(spawned, [])
        self.assertIsNone(self.store._load_item("child"))

    def test_unknown_policy_rejected_without_record(self):
        self.store.enqueue("dep", run_id="dep")
        with self.assertRaises(DependencyError) as ctx:
            self.store.enqueue("child", run_id="child", depends_on="dep",
                               depends_on_policy="sometimes")
        self.assertIn("Invalid depends_on_policy", str(ctx.exception))
        self.assertIn("'success'", str(ctx.exception))
        self.assertIn("'finished'", str(ctx.exception))
        self.assertIsNone(self.store._load_item("child"))

    def test_start_job_rejects_unknown_policy(self):
        with self.assertRaises(RuntimeError) as ctx:
            jobs.start_job(
                "harness_deepseek",
                {"prompt": "x", "depends_on_policy": "sometimes",
                 "wait_seconds": 0},
                cfg=self.cfg)
        self.assertIn("Invalid depends_on_policy", str(ctx.exception))

    # -- resume revival ----------------------------------------------------
    def _parked_dependent(self, ctrl):
        self.store.enqueue("dep", run_id="dep")
        self.store.enqueue("child", run_id="child", depends_on="dep")
        self.store.fail("dep", "model exploded", status="error")
        ctrl.run_once(spawn=lambda rec: None)
        parked = self.store._load_item("child")
        self.assertIn(parked["status"], ("failed", "error"))
        self.assertTrue(parked.get("parked_on_dependency"))
        return parked

    def test_start_job_resume_revives_parked_dependent(self):
        ctrl = self._controller()
        self._parked_dependent(ctrl)
        submitted = {}

        def fake_submit(work_id, **kwargs):
            submitted.update(kwargs)
            return {"state": "queued", "position": 0}

        with mock.patch.object(jobs, "_admission_controller",
                               return_value=ctrl), \
                mock.patch.object(ctrl, "submit", side_effect=fake_submit), \
                mock.patch.object(ctrl, "start") as start:
            jobs.start_job(
                "resume_deepseek",
                {"run_id": "dep", "prompt": "x", "wait_seconds": 0},
                cfg=self.cfg, wait=0)
        new_run = submitted.get("run_id")
        self.assertTrue(new_run)
        record = self.store._load_item("child")
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["depends_on"], new_run)
        self.assertTrue(start.called, "the revived dependent was not started")

    def test_resume_revives_parked_dependent(self):
        ctrl = self._controller()
        self._parked_dependent(ctrl)

        # The dependency is resumed under a new run id: exactly what
        # jobs.start_job does for resume_deepseek / resume_run_id.
        self.store.enqueue("dep2", run_id="dep2", priority=PRIORITY_RESUME)
        revived = ctrl.revive_dependents("dep2", source_run_id="dep")
        self.assertEqual(revived, ["child"])
        record = self.store._load_item("child")
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["depends_on"], "dep2")
        self.assertEqual(record["depends_on_policy"], "success")
        self.assertNotIn("error", record)
        self.assertFalse(record.get("parked_on_dependency"))
        # While the resumed attempt is running the dependent waits, it is not
        # dead; once the resumed attempt succeeds it becomes ready.
        self.assertEqual(self.store.dependency_state(record)["state"],
                         "waiting")
        self.store.release("dep2", status="done")
        self.assertTrue(ctrl._dependency_ready(record))
        self.assertFalse(ctrl._reject_bad_dependency_locked(record))

    def test_revive_leaves_unrelated_dependents_alone(self):
        ctrl = self._controller()
        self._parked_dependent(ctrl)
        self.assertEqual(ctrl.revive_dependents("other", source_run_id="other"),
                         [])
        self.assertIn(self.store._load_item("child")["status"],
                      ("failed", "error"))

    # -- config plumbing ---------------------------------------------------

    def test_config_rejects_unknown_policy(self):
        with self.assertRaises(ValueError) as ctx:
            load_config(env={"DSH_DEPENDS_ON_POLICY": "sometimes"},
                        include_file=False)
        self.assertIn("depends_on_policy", str(ctx.exception))
        self.assertIn("success", str(ctx.exception))
        self.assertIn("finished", str(ctx.exception))

    def test_config_default_and_env_mapping(self):
        default = load_config(env={}, include_file=False)
        self.assertEqual(default.get("depends_on_policy"), "success")
        env = {"DSH_DEPENDS_ON_POLICY": "finished"}
        configured = load_config(env=env, include_file=False)
        self.assertEqual(configured.get("depends_on_policy"), "finished")


if __name__ == "__main__":
    unittest.main(verbosity=2)
