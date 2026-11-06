"""Fail-first regressions for the Kimi review of the P2 implementation.

Each test targets one blocking defect (or one listed non-blocking defect)
from the earlier P2 review.  They use only local fakes and temp dirs.
"""

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import (authority, budget, checkpoints, harness, health,
                          skill_author, spend, transport)
from deepseek_mcp.config import Config, load_config
from tests.fakes import base_env, write_fake_dsh
from tests.test_v320_rescue import FAKE_DSH_SOURCE


FAKE_SDK_DSH_SOURCE = r'''
import json
import os
import sys
import time

log_path = os.environ.get("FAKE_SDK_LOG")


def log(message):
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def respond(rid, result):
    emit({"jsonrpc": "2.0", "id": rid, "result": result})


log("start")
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        frame = json.loads(line)
    except ValueError:
        continue
    rid = frame.get("id")
    method = frame.get("method")
    if method == "initialize":
        respond(rid, {"serverInfo": {"name": "fake-sdk", "version": "0.0.1"}})
    elif method == "session/prompt":
        respond(rid, {"messageId": "msg-budget"})
        emit({"jsonrpc": "2.0", "method": "session.event", "params": {
            "sessionId": "sess-budget",
            "event": {"type": "assistant/message", "data": {
                "message": {"role": "assistant", "content": "over budget"},
                "usage": {"inputTokens": 9000000, "cacheReadTokens": 0,
                          "outputTokens": 9000000}}}}})
        log("usage-emitted")
        # Deliberately keep the turn open: the connector must stop at the
        # usage event instead of waiting for a final answer.
    elif method == "session/wait":
        time.sleep(30)
    elif method == "shutdown":
        respond(rid, {})
        break
    else:
        respond(rid, {})
'''


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.cfg = Config({
            "harness_workspace": str(self.ws),
            "sandbox": "workspace-write",
            "reviewer_sandbox": "read-only",
            "job_max_seconds": 7200.0,
            "job_max_cost_usd": None,
            "default_subagents": 4,
            "subagent_soft_cap": 8,
            "max_explicit_subagents": 8,
            "allowed_workspace_roots": [str(self.ws)],
            "allow_writing_reviewer": False,
            "state_dir": str(self.root / "state"),
        })

    def envelope(self, **overrides):
        args = {"workspace": str(self.ws), "sandbox": "workspace-write"}
        args.update(overrides)
        return authority.direct(args, self.cfg)

    def error(self, envelope, **changes):
        data = envelope.to_dict()
        data.update(changes)
        with self.assertRaises(authority.InvalidAuthority):
            authority.AuthorityEnvelope.from_dict(data)


class AuthorityFailClosedTests(Base):
    def test_from_dict_rejects_bad_versions(self):
        env = self.envelope()
        for broken in (1.5, "1.5", True, 0, -1, 2, "x", None):
            with self.subTest(version=broken):
                self.error(env, version=broken)
        clone = authority.AuthorityEnvelope.from_dict(env.to_dict())
        self.assertEqual(clone.version, authority.AUTHORITY_VERSION)

    def test_from_dict_rejects_malformed_budgets(self):
        env = self.envelope()
        for field in ("job_max_seconds", "job_max_cost_usd"):
            for broken in ("not-a-number", float("nan"), float("inf"), 0,
                           -1, True, [], {}):
                with self.subTest(field=field, value=broken):
                    self.error(env, **{field: broken})
        clone = authority.AuthorityEnvelope.from_dict(env.to_dict())
        self.assertIsNone(clone.job_max_cost_usd)
        for broken in ("nan", "inf", "-inf"):
            with self.subTest(direct_budget=broken):
                with self.assertRaises(authority.InvalidAuthority):
                    self.envelope(job_max_seconds=broken)

    def test_from_dict_rejects_malformed_flags_agents_and_root(self):
        env = self.envelope()
        self.error(env, reviewer_override="false")
        self.error(env, legacy_default=1)
        for broken in (-1, 1.5, "many", True):
            with self.subTest(max_agents=broken):
                self.error(env, max_agents=broken)
        self.error(env, workspace_root="relative/ws")
        self.error(env, workspace_root="")
        self.error(env, workspace_root=123)

    def test_from_dict_rejects_bad_timestamps(self):
        env = self.envelope()
        self.error(env, created="not-a-date")
        self.error(env, created=None)
        self.error(env, expires="not-a-date")
        self.error(env, created="2026-01-01T00:00:00+00:00",
                   expires="2025-01-01T00:00:00+00:00")

    def test_continuation_danger_and_reviewer_danger_are_rejected(self):
        env = self.envelope()
        data = env.to_dict()
        data["basis"] = "continuation"
        data["sandbox"] = "danger-full-access"
        with self.assertRaises(authority.InvalidAuthority):
            authority.AuthorityEnvelope.from_dict(data)
        data = env.to_dict()
        data["reviewer_policy"] = "danger-full-access"
        data["reviewer_override"] = True
        with self.assertRaises(authority.InvalidAuthority):
            authority.AuthorityEnvelope.from_dict(data)
        with self.assertRaises(authority.ReviewerPolicyError):
            self.envelope(reviewer_sandbox="danger")

    def test_expiry_is_preserved_then_reported_as_drift(self):
        env = self.envelope()
        env.created = "2020-01-01T00:00:00+00:00"
        env.expires = "2020-01-02T00:00:00+00:00"
        cont = env.continuation("resume", self.cfg)
        self.assertEqual(cont.created, env.created)
        self.assertEqual(cont.expires, env.expires)
        run = {"workspace": str(self.ws), "authority": env.to_dict(),
               "authority_version": 1}
        effective, drift = authority.revalidate_run(run, self.cfg)
        self.assertTrue(drift["changed"].get("expired"))
        self.assertIn("2020-01-01",
                          drift["changed"]["expired"][0])
        self.assertNotEqual(effective.expires, env.expires)


class PhaseContinuationTests(Base):
    def test_phase_policy_downgrades_danger_everywhere(self):
        danger = self.envelope(sandbox="danger")
        self.assertEqual(danger.sandbox, "danger-full-access")
        for origin in ("rescue", "live-scaling", "revision"):
            with self.subTest(origin=origin):
                policy, phase = harness._phase_policy(
                    danger, self.cfg, origin)
                self.assertEqual(policy["sandbox"], "workspace-write")
                self.assertEqual(phase.basis, "continuation")
                self.assertEqual(phase.sandbox, "workspace-write")
        reviewer = danger.continuation("reviewer", self.cfg)
        self.assertEqual(
            authority.reviewer_policy_for(reviewer, self.cfg), "read-only")
        auto = authority.auto_skill_continuation(self.cfg, envelope=danger)
        self.assertEqual(auto.basis, "continuation")
        self.assertEqual(auto.sandbox, "read-only")

    def test_auto_skill_run_agent_gets_a_read_only_continuation(self):
        danger = self.envelope(sandbox="danger")
        captured = {}

        def fake_runner(cfg, task, workspace, **kwargs):
            captured.update(kwargs)
            return {"status": "ok", "answer": "{}", "seconds": 0}

        with mock.patch.object(harness, "run_agent_once", fake_runner):
            with authority.activate(danger):
                result = skill_author._run_agent(
                    self.cfg, "task", str(self.ws), "label", 60, None)
        self.assertEqual(captured.get("sandbox"), "read-only")
        self.assertEqual(result["authority"]["basis"], "continuation")
        self.assertEqual(result["authority"]["sandbox"], "read-only")

    def test_author_draft_scratch_workspace_stays_inside_the_recorded_root(self):
        """Scratch dirs live under the grant, not under an outside state_dir."""
        env = self.envelope()
        self.assertFalse(
            authority.inside_root(self.cfg.state_dir, str(self.ws)))
        captured = {}

        def fake_runner(cfg, task, workspace, **kwargs):
            captured["workspace"] = str(workspace)
            captured["kwargs"] = kwargs
            return {"status": "ok", "answer": "{}", "seconds": 0}

        with mock.patch.object(harness, "run_agent_once", fake_runner):
            skill_author.author_draft(
                "create", {"cluster_id": "c"}, cfg=self.cfg,
                envelope=env.to_dict())
        self.assertIn("workspace", captured)
        self.assertTrue(
            authority.inside_root(captured["workspace"], str(self.ws)),
            captured["workspace"])
        continuation = authority.auto_skill_continuation(
            self.cfg, envelope=env)
        self.assertEqual(continuation.workspace_root, str(self.ws))
        self.assertEqual(captured["kwargs"].get("sandbox"), "read-only")

    def test_run_agent_refuses_an_outside_root_workspace(self):
        """Fail closed when the actual execution workspace is outside the grant."""
        env = self.envelope()
        outside = self.root / "outside-state" / "skill-build" / "c" / "t"
        outside.mkdir(parents=True)
        runner = mock.Mock()
        with mock.patch.object(harness, "run_agent_once", runner):
            result = skill_author._run_agent(
                self.cfg, "task", str(outside), "label", 60, None,
                envelope=env.to_dict())
        self.assertEqual(result.get("status"), "error")
        self.assertIn("outside", result.get("error") or "")
        runner.assert_not_called()

    def test_author_draft_forwards_the_explicit_envelope(self):
        danger = self.envelope(sandbox="danger").to_dict()
        captured = {}

        def fake_agent(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "ok", "answer": "{}", "seconds": 0}

        with mock.patch.object(skill_author, "_run_agent", fake_agent):
            skill_author.author_draft(
                "create", {"cluster_id": "c"}, cfg=self.cfg,
                envelope=danger)
        self.assertEqual(captured.get("envelope"), danger)


class CostAdmissionRegressionTests(Base):
    def _run(self):
        return {"id": "run-x", "cost_ledger_events": []}

    def test_daily_only_ledger_error_fails_closed(self):
        class BrokenLedger:
            def snapshot(self, *args, **kwargs):
                raise TimeoutError("lock timeout")

            def reserve(self, *args, **kwargs):
                raise TimeoutError("lock timeout")

        cfg = SimpleNamespace(dsh_model="deepseek-flash", model=None,
                              daily_limit_usd=1.0)
        job = budget.JobBudget()
        result = harness._cost_admit(
            self._run(), cfg, BrokenLedger(), "builder", 1, 60, job)
        self.assertIsNotNone(result)
        self.assertIn("ledger unavailable", result["error"])
        # A missing ledger is equally unsafe when only a daily cap exists.
        result = harness._cost_admit(
            self._run(), cfg, None, "builder", 1, 60, job)
        self.assertIsNotNone(result)
        self.assertIn("ledger unavailable", result["error"])

    def test_unknown_active_reservation_blocks_both_ceilings(self):
        state = self.root / "state"
        ledger = budget.CostLedger(state)
        ledger.reserve("run-u", "builder", None, cost_known=False)
        with self.assertRaises(budget.CostUnknown):
            ledger.reserve("run-u", "reviewer", 0.1, job_limit=1.0)
        with self.assertRaises(budget.CostUnknown):
            ledger.reserve("run-other", "builder", 0.1, daily_limit=1.0,
                           spent_today=0.0)

    def test_check_live_counts_all_active_phases_for_the_run(self):
        ledger = budget.CostLedger(self.root / "state")
        ledger.reserve("run-live", "builder", 0.4, job_limit=1.0)
        ledger.reserve("run-live", "reviewer", 0.2, job_limit=1.0)
        allowed, remaining = ledger.check_live(
            "run-live", "builder", 0.0, 1.0)
        self.assertTrue(allowed)
        self.assertAlmostEqual(remaining, 0.4, places=6)

    def test_check_live_unknown_active_reservation_fails_closed(self):
        ledger = budget.CostLedger(self.root / "state")
        ledger.reserve("run-live-u", "builder", None, cost_known=False)
        allowed, remaining = ledger.check_live(
            "run-live-u", "builder", 0.0, 1.0)
        self.assertFalse(allowed)
        self.assertIsNotNone(remaining)

    def test_guard_prior_uses_ledger_committed_and_unknown_state(self):
        prior, known = harness._guard_prior(
            {"prior_committed": 0.8, "prior_known": True}, 0.3, True)
        self.assertAlmostEqual(prior, 0.8, places=6)
        self.assertTrue(known)
        prior, known = harness._guard_prior(
            {"prior_committed": 0.8, "prior_known": False}, 0.3, True)
        self.assertAlmostEqual(prior, 0.8, places=6)
        self.assertFalse(known)

    def test_spend_preflight_sees_unknown_active_reservation(self):
        env = base_env(self.root)
        env["DSH_DAILY_LIMIT_USD"] = "1.0"
        cfg = load_config(config_path=self.root / "config.json", env=env)
        ledger = budget.CostLedger(cfg.state_dir)
        ledger.reserve("run-u", "builder", None, cost_known=False)
        state = spend.budget_state(cfg)
        self.assertFalse(state["reserved_cost_known"])
        self.assertTrue(state["limit_reached"])


class SettleIdempotencyTests(Base):
    def setUp(self):
        super().setUp()
        self.ledger = budget.CostLedger(self.root / "state")

    def test_repeated_settle_with_operation_id_is_idempotent(self):
        reservation = self.ledger.reserve("run-s", "builder", 0.3,
                                          job_limit=1.0)
        first = self.ledger.settle("run-s", 0.4, phase="builder",
                                   operation_id=reservation["id"])
        second = self.ledger.settle("run-s", 0.4, phase="builder",
                                    operation_id=reservation["id"])
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(first["committed"]["id"], second["committed"]["id"])
        self.assertEqual(self.ledger.snapshot("run-s")["committed"], 0.4)

    def test_repeated_unmatched_settle_does_not_double_count(self):
        self.ledger.settle("run-lost", 0.4, phase="builder")
        self.ledger.settle("run-lost", 0.4, phase="builder")
        snap = self.ledger.snapshot("run-lost")
        self.assertAlmostEqual(snap["committed"], 0.4, places=6)
        data = self.ledger.load()
        rows = [item for item in data["committed"].values()
                if item.get("run_id") == "run-lost"]
        self.assertEqual(len(rows), 1, rows)

    def test_distinct_unmatched_operations_both_accumulate_and_replay(self):
        """Two named operations with no live reservation are two actuals.

        The old fallback returned operation 1's committed row for operation 2,
        so the second actual was silently lost.  Replaying either operation
        must stay idempotent and keep the $0.90 total.
        """
        first = self.ledger.settle("run-ops", 0.4, phase="builder",
                                   operation_id="op-one")
        second = self.ledger.settle("run-ops", 0.5, phase="builder",
                                    operation_id="op-two")
        self.assertEqual(first["released"], 0)
        self.assertEqual(second["released"], 0)
        self.assertNotEqual(first["committed"]["id"],
                            second["committed"]["id"])
        self.assertAlmostEqual(self.ledger.snapshot("run-ops")["committed"],
                               0.9, places=6)
        again = self.ledger.settle("run-ops", 0.5, phase="builder",
                                   operation_id="op-two")
        self.assertTrue(again["idempotent"])
        self.assertAlmostEqual(self.ledger.snapshot("run-ops")["committed"],
                               0.9, places=6)
        again = self.ledger.settle("run-ops", 0.4, phase="builder",
                                   operation_id="op-one")
        self.assertTrue(again["idempotent"])
        self.assertAlmostEqual(self.ledger.snapshot("run-ops")["committed"],
                               0.9, places=6)

    def test_second_same_phase_reservation_settles_after_release(self):
        """TTL/release recovery must not borrow the first operation's row."""
        first = self.ledger.reserve("run-ttl", "builder", 0.3,
                                    job_limit=10.0)
        self.ledger.settle("run-ttl", 0.4, phase="builder",
                           operation_id=first["id"])
        second = self.ledger.reserve("run-ttl", "builder", 0.3,
                                     job_limit=10.0)
        self.assertEqual(self.ledger.release("run-ttl", "builder"), 1)
        result = self.ledger.settle("run-ttl", 0.5, phase="builder",
                                    operation_id=second["id"])
        self.assertEqual(result["released"], 0)
        self.assertNotEqual(result["committed"]["operation_id"],
                            first["id"])
        self.assertAlmostEqual(
            self.ledger.snapshot("run-ttl")["committed"], 0.9, places=6)
        replay = self.ledger.settle("run-ttl", 0.5, phase="builder",
                                    operation_id=second["id"])
        self.assertTrue(replay["idempotent"])
        self.assertAlmostEqual(
            self.ledger.snapshot("run-ttl")["committed"], 0.9, places=6)

    def test_released_settlement_keeps_an_unrelated_same_phase_row(self):
        """Stale-row cleanup must be scoped to the settling operation."""
        self.ledger.settle("run-keep", 0.7, phase="builder",
                           operation_id="op-other")
        reservation = self.ledger.reserve("run-keep", "builder", 0.1,
                                          job_limit=10.0)
        self.ledger.release("run-keep", "builder")
        self.ledger.settle("run-keep", 0.5, phase="builder",
                           operation_id=reservation["id"])
        data = self.ledger.load()
        rows = [item for item in data["committed"].values()
                if item.get("run_id") == "run-keep"]
        self.assertEqual(len(rows), 2, rows)
        self.assertAlmostEqual(
            self.ledger.snapshot("run-keep")["committed"], 1.2, places=6)


class WholeJobLiveGuardTests(Base):
    TINY = {"inputTokens": 10, "completion_tokens": 1}

    def test_prior_committed_spend_is_enforced(self):
        guard = budget.LiveCostGuard(limit=1.0, reserved=0.3,
                                     prior_committed=0.8,
                                     model="deepseek-flash")
        self.assertAlmostEqual(guard.projected_usd(), 1.1, places=6)
        self.assertTrue(guard.observe(self.TINY))
        self.assertTrue(guard.exceeded)
        self.assertIn("prior committed", guard.reason)

    def test_remaining_reserve_shrinks_with_observed_usage(self):
        guard = budget.LiveCostGuard(limit=1.0, reserved=0.4,
                                     prior_committed=0.5,
                                     model="deepseek-flash")
        # $0.45 of cache-miss input consumes the reserve instead of being
        # added on top of it: 0.5 + max(0.45, 0.4) = 0.95, still under 1.0.
        self.assertFalse(guard.observe({"inputTokens": 3000000}))
        self.assertAlmostEqual(guard.projected_usd(), 0.95, places=6)
        self.assertFalse(guard.exceeded)

    def test_unknown_prior_spend_fails_closed_on_the_first_event(self):
        guard = budget.LiveCostGuard(limit=1.0, prior_known=False,
                                     model="deepseek-flash")
        self.assertTrue(guard.observe(self.TINY))
        self.assertFalse(guard.cost_known)
        self.assertTrue(guard.exceeded)


class HealthRegressionTests(Base):
    def test_clear_health_resets_a_corrupt_board(self):
        board = health.HealthBoard(self.root / "state")
        board.path.parent.mkdir(parents=True, exist_ok=True)
        board.path.write_text("{not json", encoding="utf-8")
        self.assertTrue(board.quarantine_block("builder"))
        self.assertGreaterEqual(board.clear(), 1)
        self.assertIsNone(board.quarantine_block("builder"))
        self.assertFalse(board.load().get("corrupt"))

    def test_retry_after_is_capped_and_jittered(self):
        self.assertEqual(health.retry_after("Retry-After: 120"), 60.0)
        policy = health.RetryPolicy("builder", base=1.0, cap=60.0,
                                    jitter=0.25)
        for _ in range(20):
            delay = policy.delay(1, retry_after=120)
            self.assertGreaterEqual(delay, 60.0)
            self.assertLessEqual(delay, 60.0)

    def test_incomplete_price_mapping_is_cost_unknown(self):
        estimate = budget.estimate_phase_cost(
            "priced", "builder", prices={"priced": {"miss": 1.0}})
        self.assertFalse(estimate["cost_known"])
        self.assertIsNone(estimate["cost_usd"])
        guard = budget.LiveCostGuard(
            limit=1.0, model="priced",
            prices={"priced": {"miss": 1.0}})
        self.assertTrue(guard.observe({"inputTokens": 1000}))
        self.assertTrue(guard.exceeded)


class TempDirTests(unittest.TestCase):
    def test_tempdir_is_usable(self):
        handle, name = tempfile.mkstemp(prefix="p2-tmp-")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(name) and os.unlink(name))
        self.assertTrue(os.path.isdir(tempfile.gettempdir()))

    def test_usable_tmpdir_is_honoured(self):
        import tests
        with tempfile.TemporaryDirectory() as usable:
            with mock.patch.dict(os.environ, {
                    "TMPDIR": usable, "DSH_TEST_TMPDIR": "",
                    "DSH_TMPDIR": ""}, clear=False):
                selected = tests._select_tempdir()
            self.assertEqual(selected, Path(usable).resolve())

    def test_explicit_dsh_test_tmpdir_override_is_honoured(self):
        import tests
        with tempfile.TemporaryDirectory() as usable:
            with mock.patch.dict(os.environ, {
                    "DSH_TEST_TMPDIR": usable,
                    "TMPDIR": "/no/such/tmpdir-for-review"}, clear=False):
                selected = tests._select_tempdir()
            self.assertEqual(selected, Path(usable).resolve())

    def test_unusable_tmpdir_falls_back_into_the_workspace(self):
        import tests
        repo = Path(__file__).resolve().parent.parent
        with mock.patch.dict(os.environ, {
                "TMPDIR": "/no/such/tmpdir-for-review",
                "TMP": "/no/such/tmp-for-review",
                "TEMP": "/no/such/temp-for-review",
                "DSH_TEST_TMPDIR": "", "DSH_TMPDIR": ""}, clear=False):
            selected = tests._select_tempdir()
        if selected is None:  # fully read-only sandbox: nothing can be probed
            self.skipTest("no writable temp candidate is available")
        self.assertTrue(tests._usable(selected))
        workspace_fallback = (tests._usable(repo / ".tmp")
                              or tests._usable(repo.parent / ".tmp"))
        if workspace_fallback:
            # The normal fallback is inside the repository/workspace.
            self.assertTrue(
                str(selected).startswith(str(repo)) or
                str(selected).startswith(str(repo.parent)), selected)
        else:
            # A stricter review sandbox may only expose a tmpfs/runtime dir.
            self.assertTrue(os.path.isdir(str(selected)), selected)


class HarnessPhaseIntegrationTests(Base):
    def _fake(self):
        dsh = write_fake_dsh(self.root / "fake-dsh")
        env = base_env(self.root, fake_dsh=dsh, sessions=self.root / "sessions")
        env.update({"DSH_LIVE_SCALING": "0", "DSH_AUTO_SKILLS": "off",
                    "DSH_MAX_CONCURRENT_CHILDREN": "4",
                    "FAKE_DSH_SESSIONS": str(self.root / "sessions")})
        return env

    def test_rescue_execution_uses_a_continuation_policy(self):
        dsh = self.root / "rescue-dsh"
        dsh.write_text(FAKE_DSH_SOURCE, encoding="utf-8")
        dsh.chmod(0o755)
        env = base_env(self.root, fake_dsh=dsh)
        env.update({"DSH_STALL_SECONDS": "1", "DSH_LIVE_SCALING": "0",
                    "DSH_AUTO_SKILLS": "off",
                    "DSH_MAX_CONCURRENT_CHILDREN": "8"})
        seen = []
        callbacks = []
        attention = []
        original = harness._execute_once
        original_health = harness._health_record
        original_emit = harness._emit_run_event

        def spy(*args, **kwargs):
            if kwargs.get("label") == "finisher":
                seen.append((kwargs.get("policy") or {}).get("sandbox"))
                callbacks.append(kwargs.get("live_usage_cb") is not None)
            return original(*args, **kwargs)

        def health_spy(board, component, execution, label=None):
            if component == "rescue":
                return {"component": "rescue", "state": "error",
                        "quarantined": True, "consecutive_failures": 3,
                        "last_class": "network"}
            return original_health(board, component, execution, label=label)

        def emit_spy(run, cfg, event_type, payload=None):
            if isinstance(payload, dict) and payload.get("attention"):
                attention.append(payload)
            return original_emit(run, cfg, event_type, payload)

        args = {"prompt": "Write a report about otters", "subagents": 2,
                "quality": True, "quality_rounds": 1,
                "independent_review": True, "max_seconds": 20,
                "job_max_cost_usd": 100.0,
                "workspace": str(self.ws), "sandbox": "danger"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            with mock.patch.object(harness, "_execute_once", side_effect=spy):
                with mock.patch.object(harness, "_health_record",
                                       side_effect=health_spy):
                    with mock.patch.object(harness, "_emit_run_event",
                                           side_effect=emit_spy):
                        with mock.patch.object(harness, "_running_cost",
                                               return_value=(0.0, True)):
                            with mock.patch.object(harness, "_cost_settle",
                                                   return_value=None):
                                _text, run, _path = harness.run_harness(
                                    args, cfg)
        self.assertTrue(run.get("rescued"))
        self.assertEqual(seen, ["workspace-write"])
        self.assertEqual(run["phase_authority"]["rescue"]["basis"],
                         "continuation")
        self.assertEqual(callbacks, [True])
        self.assertTrue(any(item.get("component") == "rescue"
                            for item in attention), attention)

    def test_live_wave_execution_uses_a_continuation_policy(self):
        env = self._fake()
        env.update({"FAKE_DSH_REVIEW_SCORE": "8.7"})
        seen = []
        callbacks = []
        original = harness._execute_once

        def spy(*args, **kwargs):
            label = kwargs.get("label") or ""
            if label.startswith("wave "):
                seen.append((kwargs.get("policy") or {}).get("sandbox"))
                callbacks.append(kwargs.get("live_usage_cb") is not None)
            return original(*args, **kwargs)

        args = {"prompt": "Build a status page with a purple header",
                "subagents": 2, "quality": True, "quality_rounds": 1,
                "live_scaling": True, "live_scaling_threshold": 7.0,
                "live_scaling_max_extra_waves": 1,
                "job_max_cost_usd": 100.0,
                "workspace": str(self.ws), "sandbox": "danger"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            with mock.patch.object(harness, "_execute_once", side_effect=spy):
                _text, run, _path = harness.run_harness(args, cfg)
        self.assertEqual(run["live_scaling"]["extra_waves_used"], 1)
        self.assertEqual(seen, ["workspace-write"])
        self.assertEqual(callbacks, [True])
        self.assertEqual(run["phase_authority"]["live-scaling"]["basis"],
                         "continuation")
        events = [item for item in (run.get("cost_ledger_events") or [])
                  if item.get("phase") == "live-wave"]
        self.assertEqual({item.get("event") for item in events},
                         {"reserved", "settled"})
        reserved = [item for item in events if item.get("event") == "reserved"]
        settled = [item for item in events if item.get("event") == "settled"]
        self.assertTrue(reserved and reserved[0].get("reservation"))
        self.assertTrue(settled and settled[0].get("released", 0) >= 1)

    def test_revision_execution_uses_a_continuation_policy(self):
        env = self._fake()
        env.update({"FAKE_DSH_REVIEW_SCORES": "7.0,8.7",
                    "FAKE_DSH_REVIEW_BLOCKING": "1,0"})
        seen = []
        callbacks = []
        attention = []
        original = harness._execute_once
        original_health = harness._health_record
        original_emit = harness._emit_run_event

        def spy(*args, **kwargs):
            if (kwargs.get("label") or "").startswith("revision round"):
                seen.append((kwargs.get("policy") or {}).get("sandbox"))
                callbacks.append(kwargs.get("live_usage_cb") is not None)
            return original(*args, **kwargs)

        def health_spy(board, component, execution, label=None):
            if component == "revision":
                return {"component": "revision", "state": "error",
                        "quarantined": True, "consecutive_failures": 3,
                        "last_class": "network"}
            return original_health(board, component, execution, label=label)

        def emit_spy(run, cfg, event_type, payload=None):
            if isinstance(payload, dict) and payload.get("attention"):
                attention.append(payload)
            return original_emit(run, cfg, event_type, payload)

        args = {"prompt": "Build a status page with a purple header",
                "subagents": 2, "quality": True, "quality_rounds": 2,
                "independent_review": True, "live_scaling": False,
                "job_max_cost_usd": 100.0,
                "workspace": str(self.ws), "sandbox": "danger"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            with mock.patch.object(harness, "_execute_once", side_effect=spy):
                with mock.patch.object(harness, "_health_record",
                                       side_effect=health_spy):
                    with mock.patch.object(harness, "_emit_run_event",
                                           side_effect=emit_spy):
                        _text, run, _path = harness.run_harness(args, cfg)
        self.assertEqual(seen, ["workspace-write"])
        self.assertEqual(callbacks, [True])
        self.assertEqual(run["phase_authority"]["revision"]["basis"],
                         "continuation")
        self.assertTrue(any(item.get("component") == "revision"
                            for item in attention), attention)


class BudgetCheckpointPhaseTests(Base):
    """Every budget stop must leave the *phase that was running* interrupted."""

    def _env(self, **extra):
        dsh = write_fake_dsh(self.root / "fake-dsh")
        env = base_env(self.root, fake_dsh=dsh, sessions=self.root / "sessions")
        env.update({"DSH_LIVE_SCALING": "0", "DSH_AUTO_SKILLS": "off"})
        env.update(extra)
        return env

    def _cfg(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            return load_config(config_path=self.root / "config.json", env=env)

    def _run(self, cfg, env, args, patches=()):
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                return harness.run_harness(args, cfg)

    def _latest_run(self):
        return json.loads(sorted((self.root / "runs").glob("*.json"))[-1]
                          .read_text(encoding="utf-8"))

    def _phase(self, run_id, name):
        manifest = checkpoints.Checkpoint.find(
            self.root / "state" / "checkpoints", run_id)
        self.assertIsNotNone(manifest)
        entry = (manifest.get("phases") or {}).get(name)
        self.assertIsNotNone(entry, manifest.get("phases"))
        return entry

    @staticmethod
    def _budget_result(error="guarded"):
        return {"status": "error", "stop_reason": "budget", "error": error,
                "seconds": 1, "estimated_cost_usd": 0.01,
                "cost_known": True, "output_files": [], "session_logs": [],
                "answer": None}

    def test_builder_live_stop_interrupts_the_builder_phase(self):
        env = self._env()
        cfg = self._cfg(env)
        args = {"prompt": "Write a report", "subagents": 0,
                "quality": False, "workspace": str(self.ws),
                "job_max_cost_usd": 100.0}
        with self.assertRaises(RuntimeError):
            self._run(
                cfg, env, args,
                [mock.patch.object(harness, "_execute_once",
                                   side_effect=lambda *a, **k:
                                   self._budget_result())])
        run = self._latest_run()
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(self._phase(run["id"], "builder")["status"],
                         "interrupted")

    def test_reviewer_admission_refusal_interrupts_the_review_phase(self):
        env = self._env(FAKE_DSH_REVIEW_SCORE="8.6")
        cfg = self._cfg(env)
        original = harness._cost_admit

        def admit(run, cfg, ledger, phase, agents, limit, job):
            if str(phase) == "reviewer":
                return {"error": "daily limit reached"}
            return original(run, cfg, ledger, phase, agents, limit, job)

        args = {"prompt": "Write a report", "subagents": 0,
                "quality": True, "quality_rounds": 1,
                "independent_review": True, "workspace": str(self.ws)}
        run = self._run(
            cfg, env, args,
            [mock.patch.object(harness, "_cost_admit", side_effect=admit)])[1]
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(self._phase(run["id"], "review-1")["status"],
                         "interrupted")

    def test_reviewer_live_stop_interrupts_the_review_phase(self):
        env = self._env(FAKE_DSH_REVIEW_SCORE="8.6")
        cfg = self._cfg(env)
        original = harness._execute_once

        def execute(cfg, workspace, task, **kwargs):
            if str(kwargs.get("label") or "").startswith("reviewer"):
                return self._budget_result("reviewer live ceiling")
            return original(cfg, workspace, task, **kwargs)

        args = {"prompt": "Write a report", "subagents": 0,
                "quality": True, "quality_rounds": 1,
                "independent_review": True, "workspace": str(self.ws)}
        run = self._run(
            cfg, env, args,
            [mock.patch.object(harness, "_execute_once",
                               side_effect=execute)])[1]
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(self._phase(run["id"], "review-1")["status"],
                         "interrupted")

    def test_rescue_live_stop_interrupts_the_rescue_phase(self):
        env = self._env()
        cfg = self._cfg(env)

        def execute(cfg, workspace, task, **kwargs):
            if kwargs.get("label") == "finisher":
                return self._budget_result("finisher live ceiling")
            Path(workspace, "deliverable.md").write_text(
                "# partial\n", encoding="utf-8")
            return {"status": "error", "stop_reason": "stalled",
                    "stalled": True, "error": "stalled", "seconds": 1,
                    "estimated_cost_usd": 0.01, "cost_known": True,
                    "output_files": ["deliverable.md"], "session_logs": [],
                    "answer": None}

        args = {"prompt": "Write a report", "subagents": 0,
                "quality": False, "workspace": str(self.ws)}
        with self.assertRaises(RuntimeError):
            self._run(
                cfg, env, args,
                [mock.patch.object(harness, "_execute_once",
                                   side_effect=execute)])
        run = self._latest_run()
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(self._phase(run["id"], "rescue")["status"],
                         "interrupted")

    def test_revision_live_stop_interrupts_the_revision_phase(self):
        env = self._env(FAKE_DSH_REVIEW_SCORES="7.0",
                        FAKE_DSH_REVIEW_BLOCKING="1")
        cfg = self._cfg(env)
        original = harness._execute_once

        def execute(cfg, workspace, task, **kwargs):
            if str(kwargs.get("label") or "").startswith("revision"):
                return self._budget_result("revision live ceiling")
            return original(cfg, workspace, task, **kwargs)

        args = {"prompt": "Write a report", "subagents": 0,
                "quality": True, "quality_rounds": 2,
                "independent_review": True, "workspace": str(self.ws)}
        run = self._run(
            cfg, env, args,
            [mock.patch.object(harness, "_execute_once",
                               side_effect=execute)])[1]
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(self._phase(run["id"], "revision-1")["status"],
                         "interrupted")

    def test_live_wave_stop_interrupts_the_wave_phase(self):
        env = self._env(FAKE_DSH_SCORE="6.0", DSH_LIVE_SCALING="1")
        cfg = self._cfg(env)
        original = harness._execute_once
        labels = []

        def execute(cfg, workspace, task, **kwargs):
            labels.append(kwargs.get("label"))
            if str(kwargs.get("label") or "").startswith("wave "):
                return self._budget_result("wave live ceiling")
            return original(cfg, workspace, task, **kwargs)

        args = {"prompt": "Build a status page. Required facts: purple "
                          "header, striped table.", "subagents": 2,
                "quality": True, "quality_rounds": 1,
                "live_scaling": True, "live_scaling_threshold": 7.0,
                "live_scaling_max_extra_waves": 1,
                "workspace": str(self.ws)}
        with self.assertRaises(RuntimeError):
            self._run(
                cfg, env, args,
                [mock.patch.object(harness, "_execute_once",
                                   side_effect=execute)])
        run = self._latest_run()
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertEqual(run["live_scaling"]["extra_waves_used"], 1)
        phases = checkpoints.Checkpoint.find(
            self.root / "state" / "checkpoints", run["id"])["phases"]
        wave = [name for name in phases if name.startswith("live-wave-")]
        self.assertTrue(wave, phases)
        self.assertEqual(phases[wave[0]]["status"], "interrupted")
        self.assertFalse(
            any(str(label).startswith("reviewer") for label in labels), labels)


    def test_live_wave_admission_refusal_interrupts_and_skips_paid_phases(self):
        """A refused wave is interrupted and must not fall into review."""
        env = self._env(FAKE_DSH_SCORE="6.0", DSH_LIVE_SCALING="1")
        cfg = self._cfg(env)
        original_admit = harness._cost_admit
        original_execute = harness._execute_once
        labels = []

        def admit(*args, **kwargs):
            phase = args[3] if len(args) > 3 else kwargs.get("phase")
            if str(phase) == "live-wave":
                return {"error": "daily limit reached", "estimate": {}}
            return original_admit(*args, **kwargs)

        def execute(*args, **kwargs):
            labels.append(kwargs.get("label"))
            return original_execute(*args, **kwargs)

        args = {"prompt": "Build a status page. Required facts: purple "
                          "header, striped table.", "subagents": 2,
                "quality": True, "quality_rounds": 1,
                "independent_review": True,
                "live_scaling": True, "live_scaling_threshold": 7.0,
                "live_scaling_max_extra_waves": 1,
                "workspace": str(self.ws)}
        with self.assertRaises(RuntimeError):
            self._run(
                cfg, env, args,
                [mock.patch.object(harness, "_cost_admit", side_effect=admit),
                 mock.patch.object(harness, "_execute_once",
                                   side_effect=execute)])
        run = self._latest_run()
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertFalse(run.get("review_rounds"),
                         run.get("review_rounds"))
        phases = checkpoints.Checkpoint.find(
            self.root / "state" / "checkpoints", run["id"])["phases"]
        wave = [name for name in phases if name.startswith("live-wave-")]
        self.assertTrue(wave, phases)
        self.assertEqual(phases[wave[0]]["status"], "interrupted")
        self.assertFalse(
            any(str(label).startswith("reviewer") for label in labels), labels)


class SdkLiveBudgetTests(Base):
    """The SDK/auto transport must enforce the ceiling at a usage event."""

    def _fake_sdk(self, name="fake-sdk"):
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + FAKE_SDK_DSH_SOURCE,
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def _cfg(self, fake):
        return Config({
            "harness_workspace": str(self.ws),
            "sandbox": "workspace-write",
            "reviewer_sandbox": "read-only",
            "state_dir": str(self.root / "state"),
            "job_max_seconds": 7200.0,
            "job_max_cost_usd": None,
            "dsh_bin": str(fake),
            "dsh_transport": "sdk",
            "dsh_model": "deepseek-flash",
            "os_sandbox": False,
            "auto_skills": "off",
            "live_scaling": False,
            "timeout": 15.0,
        })

    @staticmethod
    def _policy():
        return {"preset": "workspace-write", "sandbox": "workspace-write",
                "approval": "ask", "os_sandbox": False}

    def test_sdk_client_observer_stops_the_child_before_completion(self):
        fake = self._fake_sdk()
        seen = []
        client = transport.SdkClient(str(fake), str(self.ws),
                                     env=dict(os.environ), timeout=15.0)
        started = time.monotonic()
        with self.assertRaises(transport.BudgetStopped):
            client.run(
                "over budget", timeout=15.0,
                usage_observer=lambda event, usage: (
                    seen.append((event, usage)) or "budget"))
        elapsed = time.monotonic() - started
        self.assertTrue(seen, "the usage observer never fired")
        self.assertLess(elapsed, 5.0)
        self.assertEqual(client.usage.get("input_tokens"), 9000000)
        self.assertEqual(client.usage.get("output_tokens"), 9000000)
        self.assertIsNotNone(client._proc.poll())

    def test_sdk_execution_returns_a_budget_stop_with_observed_usage(self):
        fake = self._fake_sdk()
        cfg = self._cfg(fake)
        seen = []
        started = time.monotonic()
        result = harness._sdk_execution(
            cfg, self.ws, "over budget", 15.0, None, "builder",
            self._policy(), None, time.time(), 1.0,
            live_usage_cb=lambda event, parsed: (
                seen.append(event) or "budget"))
        elapsed = time.monotonic() - started
        self.assertTrue(seen, "the harness callback never fired")
        self.assertLess(elapsed, 5.0)
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("stop_reason"), "budget")
        self.assertTrue(result.get("cost_known"))
        self.assertGreater(result.get("estimated_cost_usd") or 0.0, 1.0)
        self.assertEqual(result.get("tokens_in"), 9000000)
        self.assertEqual(result.get("tokens_out"), 9000000)

    def test_auto_transport_enforces_the_same_live_budget_stop(self):
        fake = self._fake_sdk()
        cfg = self._cfg(fake)
        seen = []
        result = harness._execute_once(
            cfg, self.ws, "over budget", limit=15.0, label="builder",
            policy=self._policy(), transport_name="auto",
            live_usage_cb=lambda event, parsed: (
                seen.append(event) or "budget"))
        self.assertTrue(seen, "the auto transport discarded the callback")
        self.assertEqual(result.get("transport"), "sdk")
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("stop_reason"), "budget")
        self.assertTrue(result.get("cost_known"))

    def test_sdk_result_usage_without_notifications_is_still_checked(self):
        """A client that only returns final usage must not bypass the ceiling."""
        fake = self._fake_sdk()
        cfg = self._cfg(fake)
        seen = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self._session_id = "fake-session"
                self._events = []
                self._usage = {}

            def run(self, *args, **kwargs):
                return {"answer": "completed without an event",
                        "events": [],
                        "session_id": "fake-session",
                        "usage": {"input_tokens": 9000000,
                                  "cache_read_tokens": 0,
                                  "output_tokens": 9000000}}

            def close(self, timeout=None):
                pass

        with mock.patch.object(harness.transport_mod, "SdkClient",
                               FakeClient):
            result = harness._sdk_execution(
                cfg, self.ws, "over budget", 15.0, None, "builder",
                self._policy(), None, time.time(), 1.0,
                live_usage_cb=lambda event, parsed: (
                    seen.append(event) or "budget"))
        self.assertTrue(seen)
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("stop_reason"), "budget")
        self.assertTrue(result.get("cost_known"))
        self.assertGreater(result.get("estimated_cost_usd") or 0.0, 1.0)

    def test_quality_off_sdk_run_ends_budget_exceeded_never_ok(self):
        fake = self._fake_sdk()
        env = base_env(self.root, fake_dsh=fake,
                       sessions=self.root / "sessions")
        env.update({"DSH_TRANSPORT": "sdk", "DSH_OS_SANDBOX": "0",
                    "DSH_LIVE_SCALING": "0", "DSH_AUTO_SKILLS": "off",
                    "DSH_MAX_CONCURRENT_CHILDREN": "4"})
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
        args = {"prompt": "Write a report", "subagents": 0,
                "quality": False, "independent_review": False,
                "workspace": str(self.ws), "max_seconds": 6,
                "job_max_cost_usd": 1.0}
        started = time.monotonic()
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                harness.run_harness(args, cfg)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "the SDK run waited for completion")
        run = json.loads(sorted((self.root / "runs").glob("*.json"))[-1]
                         .read_text(encoding="utf-8"))
        self.assertNotEqual(run.get("status"), "ok")
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")
        manifest = checkpoints.Checkpoint.find(
            self.root / "state" / "checkpoints", run["id"])
        self.assertIsNotNone(manifest)
        self.assertEqual((manifest.get("phases") or {}).get(
            "builder", {}).get("status"), "interrupted")


class HealthBlockedEntryTests(Base):
    """Already-quarantined rescue/revision must emit structured attention."""

    def _env(self, **extra):
        dsh = write_fake_dsh(self.root / "fake-dsh")
        env = base_env(self.root, fake_dsh=dsh, sessions=self.root / "sessions")
        env.update({"DSH_LIVE_SCALING": "0", "DSH_AUTO_SKILLS": "off"})
        env.update(extra)
        return env

    def _cfg(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            return load_config(config_path=self.root / "config.json", env=env)

    def test_blocked_rescue_emits_attention_before_returning(self):
        env = self._env()
        cfg = self._cfg(env)
        attention = []
        original_emit = harness._emit_run_event
        original_block = health.HealthBoard.quarantine_block

        def block(board, component):
            if component == "rescue":
                return "rescue is quarantined"
            return original_block(board, component)

        def emit(run, cfg, event_type, payload=None):
            if isinstance(payload, dict) and payload.get("attention"):
                attention.append(payload)
            return original_emit(run, cfg, event_type, payload)

        def execute(cfg, workspace, task, **kwargs):
            Path(workspace, "deliverable.md").write_text(
                "# partial\n", encoding="utf-8")
            return {"status": "error", "stop_reason": "stalled",
                    "stalled": True, "error": "stalled", "seconds": 1,
                    "estimated_cost_usd": 0.01, "cost_known": True,
                    "output_files": ["deliverable.md"], "session_logs": [],
                    "answer": None}

        args = {"prompt": "Write a report", "subagents": 0,
                "quality": False, "workspace": str(self.ws)}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(health.HealthBoard, "quarantine_block",
                                   block):
                with mock.patch.object(harness, "_execute_once",
                                       side_effect=execute):
                    with mock.patch.object(harness, "_emit_run_event",
                                           side_effect=emit):
                        with self.assertRaises(RuntimeError):
                            harness.run_harness(args, cfg)
        self.assertTrue(any(item.get("component") == "rescue"
                            for item in attention), attention)

    def test_blocked_revision_emits_attention_before_returning(self):
        env = self._env(FAKE_DSH_REVIEW_SCORES="7.0",
                        FAKE_DSH_REVIEW_BLOCKING="1")
        cfg = self._cfg(env)
        attention = []
        original_emit = harness._emit_run_event
        original_block = health.HealthBoard.quarantine_block

        def block(board, component):
            if component == "revision":
                return "revision is quarantined"
            return original_block(board, component)

        def emit(run, cfg, event_type, payload=None):
            if isinstance(payload, dict) and payload.get("attention"):
                attention.append(payload)
            return original_emit(run, cfg, event_type, payload)

        args = {"prompt": "Write a report", "subagents": 0,
                "quality": True, "quality_rounds": 2,
                "independent_review": True, "workspace": str(self.ws)}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(health.HealthBoard, "quarantine_block",
                                   block):
                with mock.patch.object(harness, "_emit_run_event",
                                       side_effect=emit):
                    harness.run_harness(args, cfg)
        self.assertTrue(any(item.get("component") == "revision"
                            for item in attention), attention)


if __name__ == "__main__":
    unittest.main()
