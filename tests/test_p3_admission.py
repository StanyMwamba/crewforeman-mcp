import os
import signal
import subprocess
import sys
import time
import unittest
import tempfile
import threading
import types
from unittest import mock

from deepseek_mcp import admission, config
from deepseek_mcp.admission import (
    AdmissionController, AdmissionStore, PRIORITY_BUILD, PRIORITY_RESUME,
    resolved_capacity,
)


def _tmpdir(case):
    # TMPDIR when set, workspace fallback when the platform temp area is not
    # writable in the sandbox.
    base = os.environ.get("TMPDIR") or os.getcwd()
    try:
        d = tempfile.TemporaryDirectory(prefix="p3-admission-", dir=base)
    except OSError:
        d = tempfile.TemporaryDirectory(prefix="p3-admission-", dir=os.getcwd())
    case.addCleanup(d.cleanup)
    return d.name


def _cfg(root, max_jobs=1, reserve=True, hb=30.0, lease=90.0):
    return types.SimpleNamespace(
        state_dir=root, max_harness_jobs=max_jobs,
        reserve_resume_slot=reserve,
        admission_heartbeat_seconds=hb, admission_lease_seconds=lease,
    )


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(config.DEFAULTS["max_harness_jobs"], 4)
        self.assertEqual(config.DEFAULTS["reserve_resume_slot"], True)
        self.assertEqual(config.DEFAULTS["admission_heartbeat_seconds"], 30.0)
        self.assertEqual(config.DEFAULTS["admission_lease_seconds"], 90.0)
        self.assertIn("DSH_MAX_HARNESS_JOBS", config.ENV_MAP)
        self.assertIn("DEEPSEEK_MAX_HARNESS_JOBS", config.ENV_MAP)
        self.assertIn("DSH_RESERVE_RESUME_SLOT", config.ENV_MAP)
        self.assertIn("max_harness_jobs", config.INT_KEYS)
        self.assertIn("reserve_resume_slot", config.BOOL_KEYS)
        self.assertIn("admission_heartbeat_seconds", config.FLOAT_KEYS)

    def test_resolved_capacity(self):
        self.assertIsNone(resolved_capacity(_cfg("/tmp", max_jobs=0), detected_limit=4))
        self.assertEqual(resolved_capacity(_cfg("/tmp", max_jobs=3), detected_limit=8), 3)
        self.assertEqual(resolved_capacity(_cfg("/tmp", max_jobs=6), detected_limit=2), 2)
        self.assertIsNotNone(resolved_capacity(_cfg("/tmp", max_jobs=4), detected_limit=8))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(_tmpdir(self), "admission")
        self.clock = [1000.0]
        self.alive = set()
        self.store = AdmissionStore(
            self.root, heartbeat_seconds=30, lease_seconds=90,
            now=lambda: self.clock[0],
            pid_alive=lambda pid: pid in self.alive)

    def test_enqueue_position_and_labels(self):
        a = self.store.enqueue("a", PRIORITY_BUILD)
        self.assertEqual(a["priority_label"], "P1 new build")
        self.assertEqual(a["status"], "queued")
        self.assertEqual(a["fencing_token"], 0)
        self.store.enqueue("b", PRIORITY_RESUME)
        self.assertEqual(self.store.position("b"), 1)
        self.assertEqual(self.store.position("a"), 2)
        self.assertEqual(self.store.status_fields("b")["priority_label"], "P0 resume")
        self.assertEqual(self.store.position("missing"), 0)

    def test_claim_heartbeat_release(self):
        self.alive.add(111)
        self.store.enqueue("a")
        rec = self.store.claim("a", pid=111)
        self.assertEqual(rec["status"], "running")
        self.assertEqual(rec["fencing_token"], 1)
        self.assertEqual(self.store.active_count(), 1)
        self.assertTrue(self.store.heartbeat("a", rec["fencing_token"], pid=111))
        fields = self.store.status_fields("a")
        self.assertEqual(fields["queue_position"], 0)
        self.assertEqual(fields["holder"], 111)
        self.clock[0] += 10
        self.assertTrue(self.store.heartbeat("a", rec["fencing_token"], pid=111))
        done = self.store.release("a", status="done")
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.store.active_count(), 0)
        self.assertEqual(self.store.pending(), [])
        self.assertIsNone(self.store.claim("a"))

    def test_position_decreases(self):
        for work in ("a", "b", "c"):
            self.store.enqueue(work)
        self.assertEqual(self.store.position("c"), 3)
        self.alive.add(111)
        self.store.claim("a", pid=111)
        self.assertEqual(self.store.position("b"), 1)
        self.assertEqual(self.store.position("c"), 2)

    def test_dead_pid_reclaimed_and_late_heartbeat_fenced(self):
        self.store.enqueue("a")
        first = self.store.claim("a", pid=999)  # pid_alive False
        self.assertEqual(first["fencing_token"], 1)
        pending = self.store.pending()
        self.assertEqual([r["work_id"] for r in pending], ["a"])
        self.assertGreater(pending[0]["fencing_token"], 1)
        self.assertFalse(self.store.heartbeat("a", first["fencing_token"], pid=999))
        self.alive.add(111)
        second = self.store.claim("a", pid=111)
        self.assertGreater(second["fencing_token"], pending[0]["fencing_token"])
        self.assertFalse(self.store.heartbeat("a", pending[0]["fencing_token"], pid=111))
        self.assertTrue(self.store.heartbeat("a", second["fencing_token"], pid=111))
        self.assertEqual(self.store.active_count(), 1)

    def test_heartbeat_between_reclaim_check_and_lock_prevents_reclaim(self):
        """A renewal after the stale expiry check must fence the reclaimer.

        ``reclaim()`` reads the lease and decides to reclaim *before* taking
        the per-work lock.  A worker heartbeat can land in that window; the
        reclaimer must reload the lease under the lock and see the renewal
        instead of queueing a live item and deleting its lease.
        """
        self.alive.add(111)
        self.store.enqueue("a")
        rec = self.store.claim("a", pid=111)
        self.assertEqual(rec["fencing_token"], 1)
        self.clock[0] += 200  # stale outside the lock: expiry looks reclaimable
        real_work_lock = self.store._work_lock
        raced = {"done": False}

        def racing_work_lock(work_id):
            if work_id == "a" and not raced["done"]:
                raced["done"] = True
                # The live holder renews after reclaim's stale
                # _should_reclaim() check but before it acquires the lock.
                self.assertTrue(self.store.heartbeat(
                    "a", rec["fencing_token"], pid=111))
            return real_work_lock(work_id)

        with mock.patch.object(self.store, "_work_lock",
                               side_effect=racing_work_lock):
            reclaimed = self.store.reclaim()
        self.assertTrue(raced["done"])
        self.assertEqual(reclaimed, [])
        item = self.store._load_item("a")
        self.assertEqual(item["status"], "running")
        self.assertEqual(item["fencing_token"], rec["fencing_token"])
        self.assertIsNotNone(self.store._load_lease("a"))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.active_count(), 1)
        # A second dispatcher claim must not start a duplicate worker.
        self.assertIsNone(self.store.claim("a", pid=111))

    def test_lease_expiry_after_three_missed_heartbeats(self):
        self.alive.add(111)
        self.store.enqueue("a")
        rec = self.store.claim("a", pid=111)
        self.clock[0] += 89
        self.assertEqual(self.store.reclaim(), [])
        self.clock[0] += 2 + 3 * 30
        reclaimed = self.store.reclaim()
        self.assertEqual(len(reclaimed), 1)
        self.assertGreater(reclaimed[0]["fencing_token"], rec["fencing_token"])
        self.assertEqual(self.store.position("a"), 1)

    def test_old_worker_cannot_release_the_new_lease(self):
        self.alive.add(111)
        self.store.enqueue("a")
        first = self.store.claim("a", pid=111)
        self.clock[0] += 200
        self.assertEqual(len(self.store.reclaim()), 1)
        second = self.store.claim("a", pid=111)
        self.assertGreater(second["fencing_token"], first["fencing_token"])
        self.store.release("a", status="done", token=first["fencing_token"])
        self.assertEqual(self.store._load_item("a")["status"], "running")
        self.store.release("a", status="done", token=second["fencing_token"])
        self.assertEqual(self.store._load_item("a")["status"], "done")

    def test_sigkilled_holder_is_reclaimed(self):
        root = os.path.join(_tmpdir(self), "admission-live")
        store = AdmissionStore(root, heartbeat_seconds=30, lease_seconds=90)
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(30)"])
        try:
            store.enqueue("kill")
            claimed = store.claim("kill", pid=child.pid)
            self.assertIsNotNone(claimed)
            self.assertEqual(store.active_count(), 1)
            child.kill()
            child.wait(timeout=5)
            pending = store.pending()
            self.assertEqual([item["work_id"] for item in pending], ["kill"])
            self.assertFalse(store.heartbeat("kill", claimed["fencing_token"],
                                             pid=child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_new_store_same_root_resumes_queue(self):
        self.store.enqueue("a")
        other = AdmissionStore(self.root, now=lambda: self.clock[0],
                               pid_alive=lambda pid: pid in self.alive)
        self.assertEqual([r["work_id"] for r in other.pending()], ["a"])
        self.alive.add(222)
        self.assertIsNotNone(other.claim("a", pid=222))


class ControllerTest(unittest.TestCase):
    def _controller(self, max_jobs=1, reserve=True):
        root = os.path.join(_tmpdir(self), "admission")
        self.clock = [1000.0]
        # Claims default to this process's pid; keep it alive so pending()
        # does not reclaim a job the test just started.
        self.alive = {os.getpid()}
        self.store = AdmissionStore(
            root, heartbeat_seconds=30, lease_seconds=90,
            now=lambda: self.clock[0],
            pid_alive=lambda pid: pid in self.alive)
        self.started = []
        cfg = _cfg(os.path.dirname(root), max_jobs=max_jobs, reserve=reserve)
        ctrl = AdmissionController(cfg, store=self.store, detected_limit=8)
        return ctrl

    def _spawn(self, rec):
        self.started.append(rec["work_id"])

    def test_cap_one_serializes(self):
        ctrl = self._controller(1)
        r1 = ctrl.submit("a", PRIORITY_BUILD, spawn=self._spawn)
        self.assertEqual(r1["state"], "started")
        r2 = ctrl.submit("b", PRIORITY_BUILD, spawn=self._spawn)
        self.assertEqual(r2["state"], "queued")
        self.assertEqual(r2["position"], 1)
        self.assertEqual(self.started, ["a"])
        self.store.release("a", "done")
        started = ctrl.run_once(spawn=self._spawn)
        self.assertEqual([r["work_id"] for r in started], ["b"])
        self.assertEqual(self.started, ["a", "b"])

    def test_p0_dispatched_before_queued_p1(self):
        ctrl = self._controller(1)
        ctrl.submit("a", PRIORITY_BUILD, spawn=self._spawn)
        ctrl.submit("b", PRIORITY_BUILD, spawn=self._spawn)
        ctrl.submit("c", PRIORITY_RESUME, spawn=self._spawn)
        self.assertEqual(self.store.position("c"), 1)
        self.store.release("a", "done")
        started = ctrl.run_once(spawn=self._spawn)
        self.assertEqual([r["work_id"] for r in started], ["c"])
        self.assertIn("b", [r["work_id"] for r in self.store.pending()])

    def test_running_p1_never_suspended(self):
        ctrl = self._controller(1)
        ctrl.submit("a", PRIORITY_BUILD, spawn=self._spawn)
        token = self.store.status_fields("a")["fencing_token"]
        ctrl.submit("p0", PRIORITY_RESUME, spawn=self._spawn)
        self.assertEqual(self.store.active_count(), 1)
        self.assertEqual(self.store.active()[0]["work_id"], "a")
        self.assertEqual(self.store.status_fields("a")["fencing_token"], token)

    def test_reserve_switch_changes_last_slot(self):
        # reserve True: pending P0 blocks a P1 from the last free slot.
        ctrl = self._controller(2, reserve=True)
        ctrl.submit("a", PRIORITY_BUILD, spawn=self._spawn)
        self.store.enqueue("p0", PRIORITY_RESUME)
        r = ctrl.submit("b", PRIORITY_BUILD, spawn=self._spawn)
        self.assertEqual(r["state"], "queued")
        r0 = ctrl.submit("p0", PRIORITY_RESUME, spawn=self._spawn)
        self.assertEqual(r0["state"], "started")
        # reserve False: the earlier P1 takes the last slot; later P0 waits.
        ctrl2 = self._controller(2, reserve=False)
        ctrl2.submit("x", PRIORITY_BUILD, spawn=self._spawn)
        self.store.enqueue("p0", PRIORITY_RESUME)
        r = ctrl2.submit("y", PRIORITY_BUILD, spawn=self._spawn)
        self.assertEqual(r["state"], "started")
        r0 = ctrl2.submit("p0", PRIORITY_RESUME, spawn=self._spawn)
        self.assertEqual(r0["state"], "queued")
        self.assertEqual(self.store.position("p0"), 1)

    def test_zero_capacity_is_unlimited(self):
        ctrl = self._controller(0)
        for i in range(5):
            res = ctrl.submit("j%d" % i, PRIORITY_BUILD, spawn=self._spawn)
            self.assertEqual(res["state"], "started")
        self.assertEqual(len(self.started), 5)
        self.assertEqual(self.store.active_count(), 5)


class QueueLevelLockTest(unittest.TestCase):
    """The cap check and the claim are one queue-wide critical section.

    ``_dispatch_lock`` only serialises one AdmissionController instance and the
    per-work lock only serialises one work id. Two independent controllers
    admitting *different* ids must still not both take the last slot.
    """

    def test_cap_one_two_controllers_admit_different_ids_concurrently(self):
        root = os.path.join(_tmpdir(self), "admission")
        alive = {os.getpid()}
        # Both threads reach active_count before either returns. With only
        # instance locks they both observe zero and both claim; with the
        # queue-level lock the second thread cannot even enter until the first
        # has claimed. The timeout keeps the locked path moving.
        gate = threading.Barrier(2, timeout=0.5)

        class CoordinatedStore(AdmissionStore):
            def active_count(self):
                value = super().active_count()
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass
                return value

        def make_store():
            return CoordinatedStore(
                root, heartbeat_seconds=30, lease_seconds=90,
                now=lambda: 1000.0,
                pid_alive=lambda pid: pid in alive)

        cfg = _cfg(os.path.dirname(root), max_jobs=1, reserve=False)
        first = AdmissionController(cfg, store=make_store(), detected_limit=8)
        second = AdmissionController(cfg, store=make_store(), detected_limit=8)
        self.assertEqual(first.capacity, 1)
        self.assertEqual(second.capacity, 1)

        started = []
        started_lock = threading.Lock()

        def spawn(record):
            with started_lock:
                started.append(record["work_id"])

        results = {}

        def admit(name, controller):
            results[name] = controller.submit(
                name, PRIORITY_BUILD, spawn=spawn)

        threads = [
            threading.Thread(target=admit, args=("a", first)),
            threading.Thread(target=admit, args=("b", second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(started), ["a"] if results["a"]["state"]
                         == "started" else ["b"])
        self.assertEqual(
            sorted(result["state"] for result in results.values()),
            ["queued", "started"])
        # Exactly one live lease: the cap was not exceeded by the loser.
        self.assertEqual(first.store.active_count(), 1)
        self.assertEqual(second.store.active_count(), 1)
        queued = [name for name, result in results.items()
                  if result["state"] == "queued"][0]
        self.assertEqual(first.store.position(queued), 1)


if __name__ == "__main__":
    unittest.main()
