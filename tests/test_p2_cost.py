"""P2-3 cost reservation ledger and live guard (stdlib unittest, fakes/temp dirs only).

No real dsh process and no network are used.
"""

import inspect
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import budget, harness, jobs, safety
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh, write_fake_script


class FakeClock:
    """Epoch-seconds clock in the style of events.EventLog."""

    def __init__(self, start=1700000000.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class CostTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir()
        self.clock = FakeClock()
        self.ledger = budget.CostLedger(self.state_dir, clock=self.clock, ttl=900)

    def new_ledger(self):
        return budget.CostLedger(self.state_dir, clock=self.clock, ttl=900)

    def reserve(self, ledger=None, run_id="run-1", phase="builder",
                estimate=0.1, **kwargs):
        return (ledger or self.ledger).reserve(
            run_id, phase, estimate, **kwargs)

    def assertMoney(self, actual, expected):
        self.assertAlmostEqual(float(actual), float(expected), places=6)


class ConcurrentAdmissionTests(CostTestCase):
    def test_two_concurrent_reservations_cannot_jointly_exceed_job_ceiling(self):
        # Two ledger instances model two processes sharing one state dir.
        ledgers = [self.new_ledger(), self.new_ledger()]
        barrier = threading.Barrier(2)
        outcomes = []
        errors = []
        lock = threading.Lock()

        def worker(ledger):
            try:
                barrier.wait(timeout=5)
                ledger.reserve("run-shared", "builder", 0.6, job_limit=1.0)
                outcome = "reserved"
            except budget.BudgetExceeded:
                outcome = "blocked"
            except Exception as exc:  # surfaced below with details
                with lock:
                    errors.append(exc)
                return
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker, args=(item,))
                   for item in ledgers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(t.is_alive() for t in threads), "reservation hung")
        self.assertEqual(errors, [], "unexpected reservation errors: %r" % (errors,))
        self.assertEqual(sorted(outcomes), ["blocked", "reserved"])
        snap = self.ledger.snapshot("run-shared", limit=1.0)
        self.assertLessEqual(snap["reserved"], 1.0)
        self.assertMoney(snap["reserved"], 0.6)

    def test_second_outstanding_reservation_blocks(self):
        self.reserve(run_id="run-1", estimate=0.7, job_limit=1.0)
        with self.assertRaises(budget.BudgetExceeded):
            self.reserve(run_id="run-1", estimate=0.4, job_limit=1.0)

    def test_job_ceiling_counts_already_committed_cost(self):
        self.reserve(run_id="run-1", estimate=0.6, job_limit=1.0)
        self.ledger.settle("run-1", 0.6, phase="builder")
        self.reserve(run_id="run-1", estimate=0.4, phase="reviewer",
                     job_limit=1.0)
        with self.assertRaises(budget.BudgetExceeded):
            self.reserve(run_id="run-1", estimate=0.1, phase="revision",
                         job_limit=1.0)


class DailyAdmissionTests(CostTestCase):
    def test_daily_admission_counts_active_reservations(self):
        self.reserve(run_id="run-a", estimate=0.4, daily_limit=1.0,
                     spent_today=0.5)
        with self.assertRaises(budget.BudgetExceeded):
            self.reserve(run_id="run-b", estimate=0.2, daily_limit=1.0,
                         spent_today=0.5)
        self.assertMoney(self.ledger.daily_total(spent_today=0.5), 0.9)
        # Releasing the active reservation makes room again.
        released = self.ledger.release("run-a")
        self.assertEqual(released, 1)
        self.reserve(run_id="run-b", estimate=0.2, daily_limit=1.0,
                     spent_today=0.5)

    def test_daily_admission_accepts_callable_spent_today(self):
        with self.assertRaises(budget.BudgetExceeded):
            self.reserve(run_id="run-a", estimate=0.11, daily_limit=1.0,
                         spent_today=lambda: 0.95)

    def test_yesterdays_active_reservation_not_counted_today(self):
        self.reserve(run_id="run-old", estimate=0.6, ttl=3 * 86400,
                     daily_limit=1.0, spent_today=0.0)
        self.clock.advance(2 * 86400)
        # The old reservation is not yet expired, only dated yesterday.
        # 0.6 + 0.5 would exceed the daily limit if yesterday counted.
        self.reserve(run_id="run-new", estimate=0.5, daily_limit=1.0,
                     spent_today=0.0)
        snap = self.new_ledger().snapshot("run-new", limit=1.0)
        self.assertMoney(snap["reserved"], 0.5)


class DailyCommittedTests(CostTestCase):
    def test_daily_admission_counts_unflushed_committed_rows(self):
        self.ledger.reserve("run-a", "builder", 0.6, daily_limit=1.0,
                            spent_today=0.0)
        self.ledger.settle("run-a", 0.6, phase="builder")
        with self.assertRaises(budget.BudgetExceeded):
            self.ledger.reserve("run-b", "builder", 0.5, daily_limit=1.0,
                                spent_today=0.0)
        # Once the run is recorded in the spend ledger its committed row is
        # reflected and no longer counted twice; a later phase still would be.
        self.assertEqual(self.ledger.mark_reflected("run-a"), 1)
        self.ledger.reserve("run-b", "builder", 0.4, daily_limit=1.0,
                            spent_today=0.6)
        self.ledger.settle("run-b", 0.4, phase="builder")
        self.ledger.mark_reflected("run-b")
        # A later committed row for the same run is still counted.
        self.ledger.settle("run-b", 0.2, phase="revision")
        with self.assertRaises(budget.BudgetExceeded):
            self.ledger.reserve("run-b", "reviewer", 0.1, daily_limit=1.0,
                                spent_today=1.0)

    def test_daily_unknown_committed_row_fails_closed(self):
        real = budget.CostLedger(self.state_dir)
        real.settle("run-unknown", None, phase="builder", cost_known=False)
        with self.assertRaises(budget.CostUnknown):
            real.reserve("run-next", "builder", 0.1, daily_limit=1.0,
                         spent_today=0.0)
        from deepseek_mcp import spend
        from deepseek_mcp.config import load_config
        from tests.fakes import base_env
        root = self.state_dir.parent
        cfg = load_config(config_path=root / "config.json",
                          env=dict(base_env(root), DSH_DAILY_LIMIT_USD="1.0"))
        state = spend.budget_state(cfg)
        self.assertFalse(state["reserved_cost_known"])
        self.assertTrue(state["limit_reached"])
        self.assertIn("cannot be verified", state["warning"])
        with self.assertRaises(RuntimeError):
            spend.preflight(cfg)

    def test_mark_reflected_never_hides_an_unknown_row(self):
        self.ledger.settle("run-unknown", None, phase="builder",
                           cost_known=False)
        self.assertEqual(self.ledger.mark_reflected("run-unknown", total=0.0),
                         0)
        self.assertFalse(self.ledger.snapshot(
            "run-unknown")["cost_known"])

    def test_unmatched_settle_does_not_lose_or_double_count(self):
        self.ledger.settle("run-lost", 0.7, phase="builder")
        self.ledger.settle("run-lost", 0.7, phase="builder")
        self.assertMoney(self.ledger.snapshot("run-lost")["committed"], 0.7)

    def test_check_live_subtracts_reserved(self):
        self.ledger.reserve("run-live", "builder", 0.4, job_limit=1.0)
        allowed, remaining = self.ledger.check_live(
            "run-live", "builder", 0.2, 1.0)
        self.assertTrue(allowed)
        self.assertMoney(remaining, 0.4)

    def test_live_guard_fails_closed_when_usage_cannot_be_priced(self):
        guard = budget.LiveCostGuard(limit=1.0, model="unpriced-model")
        self.assertTrue(guard.observe({"inputTokens": 100,
                                       "outputTokens": 10}))
        self.assertFalse(guard.cost_known)
        self.assertTrue(guard.exceeded)

    def test_global_snapshot_reports_unknown_cost(self):
        self.ledger.settle("run-unknown", None, phase="builder",
                           cost_known=False)
        self.assertFalse(self.ledger.snapshot()["cost_known"])


class UnknownCostTests(CostTestCase):
    def test_unknown_cost_blocks_when_job_ceiling_exists(self):
        with self.assertRaises(budget.CostUnknown):
            self.reserve(run_id="run-u", estimate=None, cost_known=False,
                         job_limit=1.0)
        # Nothing was admitted by the failed attempt.
        self.assertEqual(self.ledger.snapshot("run-u", limit=1.0)["reserved"], 0.0)

    def test_unknown_cost_allowed_without_any_ceiling(self):
        reservation = self.reserve(run_id="run-u", estimate=None,
                                   cost_known=False)
        self.assertIsNone(reservation["estimate"])
        self.assertFalse(reservation["cost_known"])
        snap = self.ledger.snapshot("run-u")
        self.assertFalse(snap["cost_known"])
        self.assertIsNone(snap["available"])


class DailyPreflightTests(CostTestCase):
    def test_daily_preflight_counts_active_reservations(self):
        from deepseek_mcp import spend
        from deepseek_mcp.config import load_config
        from tests.fakes import base_env

        root = self.state_dir.parent
        env = base_env(root)
        env.update({"DSH_DAILY_LIMIT_USD": "0.50",
                    "DSH_COST_RESERVATION_TTL_SECONDS": "900"})
        cfg = load_config(config_path=root / "config.json", env=env)
        # Use the real clock: spend.budget_state reads the same file with the
        # wall clock, and the reservation date must be today.
        real = budget.CostLedger(self.state_dir)
        real.reserve("run-preflight", "builder", 0.60,
                     daily_limit=10.0, spent_today=0.0)
        state = spend.budget_state(cfg)
        self.assertAlmostEqual(state["reserved_usd"], 0.60, places=6)
        self.assertGreaterEqual(state["effective_usd"], 0.60)
        self.assertTrue(state["limit_reached"])
        with self.assertRaises(RuntimeError) as caught:
            spend.preflight(cfg)
        self.assertIn("Daily limit reached", str(caught.exception))


class SettleTests(CostTestCase):
    def test_settle_reconciles_reserved_to_committed(self):
        self.reserve(run_id="run-s", phase="builder", estimate=0.3,
                     job_limit=1.0)
        self.reserve(run_id="run-s", phase="reviewer", estimate=0.1,
                     job_limit=1.0)
        self.assertMoney(
            self.ledger.snapshot("run-s", limit=1.0)["reserved"], 0.4)

        result = self.ledger.settle("run-s", 0.25, phase="builder")
        self.assertEqual(result["actual"], 0.25)
        self.assertTrue(result["cost_known"])
        self.assertIn("released", result)

        snap = self.ledger.snapshot("run-s", limit=1.0)
        self.assertMoney(snap["reserved"], 0.1)
        self.assertMoney(snap["committed"], 0.25)
        self.assertMoney(snap["available"], 0.65)

        self.ledger.settle("run-s", 0.05, phase="reviewer")
        snap = self.ledger.snapshot("run-s", limit=1.0)
        self.assertEqual(snap["reserved"], 0.0)
        self.assertMoney(snap["committed"], 0.30)
        # Committed state survives a fresh ledger on the same directory.
        self.assertMoney(
            self.new_ledger().snapshot("run-s", limit=1.0)["committed"], 0.30)


class ReclaimTests(CostTestCase):
    def test_expired_reservation_is_reclaimed(self):
        self.reserve(run_id="run-exp", estimate=0.6, ttl=60, job_limit=1.0)
        self.clock.advance(61)
        # Admission reclaims first: a 0.9 reservation fits only if 0.6 is gone.
        self.reserve(run_id="run-exp", estimate=0.9, ttl=60, job_limit=1.0)
        snap = self.ledger.snapshot("run-exp", limit=1.0)
        self.assertMoney(snap["reserved"], 0.9)

        self.clock.advance(61)
        self.assertEqual(self.ledger.reclaim(), 1)
        self.assertEqual(self.ledger.snapshot("run-exp", limit=1.0)["reserved"], 0.0)


class SnapshotTests(CostTestCase):
    def test_snapshot_arithmetic(self):
        self.reserve(run_id="run-a", phase="builder", estimate=0.3,
                     job_limit=2.0)
        self.reserve(run_id="run-a", phase="reviewer", estimate=0.1,
                     job_limit=2.0)
        self.ledger.settle("run-a", 0.05, phase="reviewer")
        self.reserve(run_id="run-b", phase="builder", estimate=0.4)

        snap = self.ledger.snapshot("run-a", limit=1.0, estimated=0.1)
        self.assertEqual(snap["limit"], 1.0)
        self.assertMoney(snap["estimated"], 0.1)
        self.assertMoney(snap["reserved"], 0.3)
        self.assertMoney(snap["committed"], 0.05)
        self.assertMoney(snap["available"], 0.55)
        self.assertTrue(snap["cost_known"])

        unbounded = self.ledger.snapshot("run-a")
        self.assertIsNone(unbounded["limit"])
        self.assertIsNone(unbounded["available"])
        self.assertMoney(unbounded["reserved"], 0.3)

        other = self.ledger.snapshot("run-b", limit=1.0)
        self.assertMoney(other["reserved"], 0.4)
        self.assertEqual(other["committed"], 0.0)
        self.assertMoney(other["available"], 0.6)


class LiveGuardTests(CostTestCase):
    def make_guard(self, limit, reserved=0.0, model="deepseek-flash"):
        """Tolerate the small constructor variations left open by the spec."""
        params = inspect.signature(budget.LiveCostGuard).parameters
        kwargs = {}
        reserved_name = next((name for name in params if "reserv" in name), None)
        if reserved_name:
            kwargs[reserved_name] = reserved
        model_name = next((name for name in params if "model" in name), None)
        if model_name:
            kwargs[model_name] = model
        return budget.LiveCostGuard(limit, **kwargs)

    TINY = {"prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 100,
            "completion_tokens": 10}
    HUGE = {"prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 10_000_000,
            "completion_tokens": 1_000_000}

    def test_live_guard_is_not_observable_without_usage(self):
        guard = self.make_guard(limit=0.5)
        self.assertFalse(guard.observable)
        self.assertFalse(guard.exceeded)
        self.assertFalse(guard.events)

    def test_live_guard_stops_only_at_a_usage_event_boundary(self):
        # Reserved already exceeds the limit, but there is no priced usage
        # event yet: the guard must not claim a live stop until one arrives.
        guard = self.make_guard(limit=0.5, reserved=0.6)
        self.assertFalse(guard.observable)
        self.assertFalse(guard.exceeded)
        self.assertTrue(guard.observe(self.TINY, model="deepseek-flash"))
        self.assertTrue(guard.exceeded)
        self.assertTrue(guard.reason)
        self.assertTrue(guard.events)

    def test_live_guard_crosses_on_the_event_that_exceeds(self):
        guard = self.make_guard(limit=0.5)
        self.assertFalse(guard.observe(self.TINY, model="deepseek-flash"))
        self.assertFalse(guard.exceeded)
        self.assertTrue(guard.observe(self.HUGE, model="deepseek-flash"))
        self.assertTrue(guard.exceeded)
        self.assertTrue(guard.events)




class HarnessCostIntegrationTests(unittest.TestCase):
    """Run the real loop against the fake dsh; no real provider is contacted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = write_fake_dsh(self.root / "fake-dsh")
        self.env = base_env(self.root, fake_dsh=self.fake,
                            sessions=self.root / "sessions")
        self.env.update({
            "DSH_LIVE_SCALING": "0",
            "DSH_MAX_CONCURRENT_CHILDREN": "4",
            "DSH_AUTO_SKILLS": "off",
            "DSH_RETRY_BACKOFF_BASE_SECONDS": "0",
            "DSH_RETRY_BACKOFF_CAP_SECONDS": "0",
            "FAKE_DSH_SESSIONS": str(self.root / "sessions"),
        })

    def _cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        with mock.patch.dict(os.environ, env, clear=False):
            return load_config(config_path=self.root / "config.json", env=env)

    def test_run_record_and_structured_status_carry_cost_snapshot(self):
        cfg = self._cfg()
        args = {"prompt": "Write a deliverable", "subagents": 0,
                "quality": False, "workspace": str(self.workspace),
                "job_max_cost_usd": 1.0}
        with mock.patch.dict(os.environ, self.env, clear=False):
            _text, run, _path = harness.run_harness(args, cfg)
        snapshot = run.get("cost_ledger") or {}
        for key in ("limit", "estimated", "reserved", "committed",
                    "available", "cost_known"):
            self.assertIn(key, snapshot)
        self.assertEqual(float(snapshot["limit"]), 1.0)
        self.assertEqual(float(snapshot["reserved"]), 0.0)
        self.assertGreater(float(snapshot["committed"]), 0.0)
        self.assertTrue(snapshot["cost_known"])
        self.assertEqual(run.get("status"), "ok")

        status = self.root / "jobs" / (run["id"] + ".status.json")
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({
            "status": "done", "result": _text, "run_id": run["id"],
            "updated": "2025-01-01T00:00:00+00:00",
        }), encoding="utf-8")
        payload = jobs.tool_status(
            {"run_id": run["id"], "events_since": 0}, cfg)
        self.assertIsInstance(payload, dict)
        self.assertEqual(float(payload["cost"]["limit"]), 1.0)
        self.assertIn("health", payload)

    def test_first_phase_overrun_is_refused_by_default(self):
        """Strict admission is the default: even the first phase must fit."""
        cfg = self._cfg()
        args = {"prompt": "Write a deliverable", "subagents": 0,
                "quality": False, "workspace": str(self.workspace),
                "job_max_cost_usd": 1e-6}
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                harness.run_harness(args, cfg)
        self.assertIn("cost admission refused the builder",
                      str(caught.exception))
        self.assertFalse((self.root / "fake-dsh-state.json").exists(),
                         "no builder may start above the job ceiling")
        run = json.loads(sorted((self.root / "runs").glob("*.json"))[-1]
                         .read_text(encoding="utf-8"))
        self.assertNotIn("admission_overrun", run)
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")

    def test_first_phase_overrun_is_opt_in_and_recorded(self):
        """The 3.8.0 compatibility carve-out is operator opt-in only."""
        cfg = self._cfg(DSH_ALLOW_FIRST_PHASE_OVERRUN="1")
        args = {"prompt": "Write a deliverable", "subagents": 0,
                "quality": False, "workspace": str(self.workspace),
                "job_max_cost_usd": 1e-6}
        with mock.patch.dict(os.environ, self.env, clear=False):
            _text, run, _path = harness.run_harness(args, cfg)
        overrun = run.get("admission_overrun") or {}
        self.assertEqual(overrun.get("phase"), "builder")
        self.assertEqual(float(overrun.get("limit")), 1e-6)
        snapshot = run.get("cost_ledger") or {}
        self.assertGreater(float(snapshot.get("committed") or 0.0), 1e-6)
        status = self.root / "jobs" / (run["id"] + ".status.json")
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({
            "status": "error", "run_id": run["id"],
            "updated": "2025-01-01T00:00:00+00:00",
        }), encoding="utf-8")
        payload = jobs.tool_status(
            {"run_id": run["id"], "events_since": 0}, cfg)
        self.assertEqual(payload.get("admission_overrun", {}).get("phase"),
                         "builder")

    def test_unknown_cost_blocks_before_the_first_paid_phase(self):
        cfg = self._cfg(DSH_MODEL="a-model-with-no-local-price")
        args = {"prompt": "Write a deliverable", "subagents": 0,
                "quality": False, "workspace": str(self.workspace),
                "job_max_cost_usd": 1.0}
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError):
                harness.run_harness(args, cfg)
        self.assertFalse((self.root / "fake-dsh-state.json").exists(),
                         "no builder may start when the cost is unknown")
        paths = sorted((self.root / "runs").glob("*.json"))
        run = json.loads(paths[-1].read_text(encoding="utf-8"))
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertIn("cost admission", run.get("error") or "")


class LiveBoundaryIntegrationTests(unittest.TestCase):
    """A usage event stops the process at that boundary, before its sleep ends."""

    def test_event_boundary_stop_uses_the_usage_event(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = root / "usage-dsh"
        dsh = write_fake_script(dsh, (
            "#!/usr/bin/env python3\n"
            "import json, time\n"
            "print(json.dumps({'type': 'assistant/message', 'data': {"
            "'message': {'id': 'm1', 'role': 'assistant', 'source': "
            "{'model': 'deepseek-flash'}}, 'usage': {'inputTokens': 9000000, "
            "'outputTokens': 9000000}}}), flush=True)\n"
            "time.sleep(20)\n"))
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        policy = safety.resolve({}, cfg, reviewer=False)
        started = time.time()
        result = harness._execute_once(
            cfg, workspace, "task", limit=10, policy=policy,
            live_usage_cb=lambda event, parsed: "budget")
        elapsed = time.time() - started
        self.assertEqual(result["stop_reason"], "budget")
        self.assertLess(elapsed, 10.0, "the process must not reach its sleep end")
        self.assertGreater(result["events"], 0)
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
