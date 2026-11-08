"""Focused 3.11.0 tests for pipelined admission (F4 transport contract).

Pure stdlib; temp admission store, fakes for the review probe and spawn. No
network and no real Harness process.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepseek_mcp import admission


def _cfg(root, **overrides):
    values = {
        "state_dir": str(root / "state"),
        "max_harness_jobs": 1,
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
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PipelineAdmissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "state").mkdir()

    def _controller(self, cfg=None, reviewed=("base",)):
        self.cfg = cfg or _cfg(self.root)
        store = admission.AdmissionStore(str(self.root / "queue"))
        reviewed = set(reviewed)
        return admission.AdmissionController(
            self.cfg, store=store, detected_limit=1,
            review_probe=lambda record: str(record.get("work_id")) in reviewed)

    @staticmethod
    def _pipeline(write_set, **extra):
        data = {
            "pipeline_eligible": True,
            "write_set": list(write_set),
            "write_set_source": "arg",
            "job_max_cost_usd": 2.0,
            "install_status": "pending",
            "canonical_workspace": "/tmp/canonical",
        }
        data.update(extra)
        return data

    def _base_active(self, controller, write_set=("src/api/",)):
        controller.store.enqueue(
            "base", run_id="base",
            pipeline=self._pipeline(write_set))
        claimed = controller.store.claim("base")
        self.assertIsNotNone(claimed)
        return claimed

    def _queued(self, work_id, write_set, **extra):
        controller = getattr(self, "controller", None)
        controller.store.enqueue(
            work_id, run_id=work_id,
            pipeline=self._pipeline(write_set, **extra))
        return controller.store._load_item(work_id)

    def test_review_phase_starts_disjoint_job(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        record = self._queued("next", ["docs/"])
        self.assertTrue(self.controller._may_start(record))
        claimed = self.controller.store.claim(
            "next", extra=self.controller._pipeline_extra_allowed(record))
        self.assertTrue(claimed.get("pipeline_extra"))

    def test_overlap_waits(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        record = self._queued("next", ["src/api/new.py"])
        self.assertFalse(self.controller._may_start(record))
        self.assertFalse(self.controller._pipeline_extra_allowed(record))

    def test_unknown_write_set_overlaps_everything(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        record = self.controller.store.enqueue(
            "next", run_id="next",
            pipeline={"pipeline_eligible": True, "write_set": [],
                      "job_max_cost_usd": 2.0})
        self.assertFalse(self.controller._pipeline_extra_allowed(record))

    def test_capacity_plus_one_cap(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        first = self._queued("first", ["docs/"])
        self.assertTrue(self.controller._may_start(first))
        self.controller.store.claim(
            "first", extra=self.controller._pipeline_extra_allowed(first))
        # Capacity 1 -> hard ceiling 2; a further extra must be refused.
        second = self._queued("second", ["tests/"])
        self.assertFalse(self.controller._pipeline_extra_allowed(second))
        self.assertFalse(self.controller._may_start(second))

    def test_cost_refusal_fail_closed(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        record = self._queued("next", ["docs/"], job_max_cost_usd=None)
        self.assertFalse(self.controller._pipeline_extra_allowed(record))

    def test_pending_patch_backlog_blocks_extras(self):
        self.controller = self._controller(
            _cfg(self.root, pipeline_max_pending_patches=2))
        self._base_active(self.controller)
        patch_dir = self.root / "state" / "patches"
        patch_dir.mkdir(parents=True)
        for index in range(2):
            (patch_dir / ("run-%d.patch" % index)).write_text("diff", encoding="utf-8")
            (patch_dir / ("run-%d.json" % index)).write_text(json.dumps({
                "run_id": "run-%d" % index, "status": "pending",
                "sha256": "abc", "patch": "run-%d.patch" % index,
            }), encoding="utf-8")
        record = self._queued("next", ["docs/"])
        self.assertFalse(self.controller._pipeline_extra_allowed(record))

    def test_dependency_waits_for_installed_patch(self):
        self.controller = self._controller()
        patch_dir = self.root / "state" / "patches"
        patch_dir.mkdir(parents=True)
        sidecar = patch_dir / "base.json"
        sidecar.write_text(json.dumps({
            "run_id": "base", "status": "pending", "sha256": "abc",
            "patch": "base.patch",
        }), encoding="utf-8")
        record = {"depends_on": "base",
                  "canonical_workspace": "/tmp/canonical"}
        self.assertFalse(self.controller._dependency_install_ready(record))
        sidecar.write_text(json.dumps({
            "run_id": "base", "status": "installed", "sha256": "abc",
            "patch": "base.patch",
        }), encoding="utf-8")
        self.assertTrue(self.controller._dependency_install_ready(record))

    def test_installed_patches_do_not_block_extras(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        patch_dir = self.root / "state" / "patches"
        patch_dir.mkdir(parents=True)
        for index in range(9):
            (patch_dir / ("run-%d.json" % index)).write_text(json.dumps({
                "run_id": "run-%d" % index, "status": "installed",
                "sha256": "abc", "patch": "run-%d.patch" % index,
            }), encoding="utf-8")
        record = self._queued("next", ["docs/"])
        self.assertTrue(self.controller._pipeline_extra_allowed(record))

    def test_plan_widening_on_run_record_defers_extras(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        logs = self.root / "runs"
        logs.mkdir()
        self.cfg.log_dir = str(logs)
        (logs / "base.json").write_text(json.dumps({
            "id": "base", "status": "running",
            "pipeline": {"defer_next_start": True},
        }), encoding="utf-8")
        record = self._queued("next", ["docs/"])
        self.assertFalse(self.controller._pipeline_extra_allowed(record))

    def test_noneligible_job_waits_for_pipeline_drain(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        record = self.controller.store.enqueue(
            "serial", run_id="serial",
            pipeline={"pipeline_eligible": False, "write_set": ["docs/"]})
        self.assertFalse(self.controller._may_start(record))

    def test_eligible_job_waits_for_canonical_builder(self):
        self.controller = self._controller(reviewed=("serial",))
        self.controller.store.enqueue(
            "serial", run_id="serial",
            pipeline={"pipeline_eligible": False, "write_set": ["docs/"]})
        self.controller.store.claim("serial")
        record = self._queued("next", ["tests/"])
        self.assertFalse(self.controller._may_start(record))

    def test_review_probe_dead_lease_is_never_reviewable(self):
        self.controller = self._controller(reviewed=())
        self._base_active(self.controller)
        record = self._queued("next", ["docs/"])
        self.assertFalse(self.controller._pipeline_extra_allowed(record))

    def test_head_scan_blocks_starvation(self):
        self.controller = self._controller()
        self._base_active(self.controller, write_set=("src/",))
        self._queued("head", ["src/api/"])
        self._queued("tail", ["docs/"])
        started = self.controller.run_once(spawn=lambda record: None)
        self.assertEqual(started, [])

    def test_first_fit_skips_waited_head(self):
        self.controller = self._controller(
            _cfg(self.root, pipeline_queue_scan="first_fit"))
        self._base_active(self.controller, write_set=("src/",))
        self._queued("head", ["src/api/"])
        self._queued("tail", ["docs/"])
        started = self.controller.run_once(spawn=lambda record: None)
        self.assertEqual([item.get("work_id") for item in started], ["tail"])

    def test_install_status_starts_pending(self):
        self.controller = self._controller()
        self._base_active(self.controller)
        record = self._queued("next", ["docs/"])
        fields = self.controller.store.status_fields("next")
        self.assertEqual(fields.get("write_set"), ["docs/"])
        self.assertEqual(fields.get("install_status"), "pending")
        self.assertFalse(fields.get("pipeline_extra"))


if __name__ == "__main__":
    unittest.main()
