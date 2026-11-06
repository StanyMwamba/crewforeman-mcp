"""P2-2 retry policy and component health (stdlib unittest, fakes/temp dirs only).

No real dsh process and no network are used.
"""

import email.utils
import json
import multiprocessing
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _base in (_HERE, _HERE.parent, _HERE.parent.parent,
              _HERE.parent.parent / "repo"):
    if ((_base / "deepseek_mcp" / "health.py").exists()
            or (_base / "deepseek_mcp" / "__init__.py").exists()):
        if str(_base) not in sys.path:
            sys.path.insert(0, str(_base))
        break

from deepseek_mcp import harness  # noqa: E402
from deepseek_mcp import health  # noqa: E402
from deepseek_mcp import safety  # noqa: E402
from deepseek_mcp.config import load_config  # noqa: E402
from tests.fakes import base_env, write_fake_dsh  # noqa: E402

FC = health.FailureClass


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()

    def board(self):
        return health.HealthBoard(self.state_dir)


class ClassificationTests(unittest.TestCase):
    def test_429_with_retry_after_is_rate_limit(self):
        for text in ("HTTP 429 Too Many Requests\nRetry-After: 2",
                     "429 rate limit exceeded",
                     "retry-after 3"):
            with self.subTest(text=text):
                self.assertEqual(health.classify(text), FC.RATE_LIMIT)

    def test_retry_after_parses_seconds_and_clamps(self):
        self.assertEqual(health.retry_after("Retry-After: 2"), 2.0)
        self.assertEqual(health.retry_after("retry after 5 seconds"), 5.0)
        self.assertEqual(health.retry_after("Retry-After: 99999"), 60.0)
        self.assertIsNone(health.retry_after("no header here"))

    def test_retry_after_http_date(self):
        header = "Retry-After: %s" % email.utils.formatdate(
            time.time() + 30, usegmt=True)
        value = health.retry_after(header)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 5.0)
        self.assertLessEqual(value, 60.0)

    def test_retryable_classes(self):
        self.assertEqual(health.classify("ECONNRESET"), FC.NETWORK)
        self.assertEqual(health.classify("HTTP 503 Service Unavailable"),
                         FC.PROVIDER_5XX)


class RetryPolicyTests(unittest.TestCase):
    def test_rate_limit_is_retryable_and_retry_after_sets_delay_floor(self):
        policy = health.RetryPolicy("builder", base=1.0, cap=60.0, jitter=0.25,
                                    rng=random.Random(7))
        ok, _ = policy.should_retry(1, FC.RATE_LIMIT)
        self.assertTrue(ok)
        delay = policy.delay(1, retry_after=12.0)
        self.assertGreaterEqual(delay, 12.0)
        self.assertLessEqual(delay, 60.25)

    def test_exponential_backoff_jitter_cap_and_seed_determinism(self):
        a = health.RetryPolicy("builder", base=1.0, cap=60.0, jitter=0.25,
                               rng=random.Random(1234))
        b = health.RetryPolicy("builder", base=1.0, cap=60.0, jitter=0.25,
                               rng=random.Random(1234))
        self.assertEqual(a.delay(1), b.delay(1))
        self.assertGreaterEqual(a.delay(1), 1.0)
        self.assertLessEqual(a.delay(1), 1.25)
        self.assertGreaterEqual(a.delay(3), 4.0)
        self.assertLessEqual(a.delay(3), 4.25)
        capped = health.RetryPolicy("builder", base=1.0, cap=60.0,
                                    jitter=0.25, rng=random.Random(5))
        self.assertLessEqual(capped.delay(10), 60.0)
        self.assertGreaterEqual(capped.delay(10), 60.0)

    def test_sleep_uses_injected_sleeper(self):
        slept = []
        policy = health.RetryPolicy("reviewer", sleeper=slept.append)
        policy.sleep(1.5)
        self.assertEqual(slept, [1.5])

    def test_hard_stop_classes_never_retry(self):
        policy = health.RetryPolicy("builder")
        for failure_class in (FC.AUTH, FC.NO_BALANCE, FC.PREPARE, FC.DEADLINE,
                              FC.BUDGET):
            with self.subTest(failure_class=failure_class):
                ok, reason = policy.should_retry(1, failure_class)
                self.assertFalse(ok)
                self.assertIn("non-retryable", reason)
                self.assertIn(failure_class, reason)

    def test_attempts_exhausted_and_unknown(self):
        policy = health.RetryPolicy("revision")
        self.assertEqual(policy.attempts, 1)
        ok, reason = policy.should_retry(1, FC.NETWORK)
        self.assertFalse(ok)
        self.assertIn("attempts exhausted", reason)
        ok, reason = health.RetryPolicy("builder").should_retry(
            1, FC.UNKNOWN)
        self.assertFalse(ok)
        self.assertIn("unclassified", reason)

    def test_time_and_budget_gates(self):
        policy = health.RetryPolicy("builder")
        self.assertFalse(policy.should_retry(1, FC.NETWORK,
                                             remaining_seconds=0)[0])
        self.assertFalse(policy.should_retry(1, FC.NETWORK, spent=5.0,
                                             ceiling=5.0)[0])
        self.assertFalse(policy.should_retry(1, FC.NETWORK, spent=0.0,
                                             ceiling=5.0,
                                             cost_known=False)[0])
        self.assertTrue(policy.should_retry(1, FC.NETWORK, spent=1.0,
                                            ceiling=5.0,
                                            cost_known=True)[0])


class ReviewerClassificationFallbackTests(unittest.TestCase):
    def test_generic_reviewer_failure_is_retryable_reviewer(self):
        execution = {"label": "reviewer round 1", "status": "error",
                     "error": "boom", "exit_code": 1}
        self.assertEqual(health.classify_execution(execution),
                         FC.REVIEWER)
        allowed, reason = health.RetryPolicy("reviewer").should_retry(
            1, health.classify_execution(execution))
        self.assertTrue(allowed, reason)


class HealthBoardTests(TempDirTestCase):
    def test_three_failures_quarantine_then_success_resets(self):
        board = self.board()
        entry = None
        for _ in range(2):
            entry = board.record_failure("builder", FC.NETWORK,
                                         error="ECONNRESET")
            self.assertFalse(entry.get("quarantined"))
        entry = board.record_failure("builder", FC.NETWORK,
                                     error="ECONNRESET")
        self.assertTrue(entry.get("quarantined"))
        self.assertIn(entry.get("state"), ("quarantined", "error"))
        payload = entry.get("quarantine_event") or entry.get("attention")
        self.assertTrue(payload)
        self.assertEqual(payload.get("kind"), "attention")
        self.assertTrue(board.is_quarantined("builder"))
        self.assertTrue(board.quarantine_block("builder"))
        self.assertIn("builder", board.quarantine_block("builder").lower())

        reset = board.record_success("builder")
        self.assertFalse(reset.get("quarantined"))
        self.assertEqual(reset.get("state"), "ok")
        self.assertEqual(reset.get("consecutive_failures"), 0)
        self.assertFalse(board.is_quarantined("builder"))
        self.assertIsNone(board.quarantine_block("builder"))
        self.assertTrue(reset.get("last_success_at"))

    def test_failure_object_persists_and_is_cleared(self):
        board = self.board()
        for _ in range(3):
            board.record_failure("reviewer", FC.AUTH,
                                 error="401 unauthorized")
        entry = board.snapshot()["components"]["reviewer"]
        self.assertEqual(entry["last_class"], FC.AUTH)
        self.assertIn(entry["state"], ("error", "quarantined"))
        self.assertEqual(board.clear("reviewer"), 1)
        self.assertEqual(board.clear(), 0)
        self.assertEqual(
            board.snapshot()["components"]["reviewer"]["consecutive_failures"],
            0)

    def test_record_retry_does_not_add_a_failure_strike(self):
        board = self.board()
        board.record_failure("builder", FC.NETWORK, error="reset")
        board.record_retry("builder", FC.NETWORK, error="reset")
        entry = board.snapshot()["components"]["builder"]
        self.assertEqual(entry["consecutive_failures"], 1)
        self.assertEqual(len(entry["retries"]), 1)
        self.assertFalse(entry["quarantined"])

    def test_status_lines_contain_component(self):
        board = self.board()
        board.record_failure("builder", FC.RATE_LIMIT, error="429")
        lines = board.status_lines()
        text = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines)
        self.assertIn("builder", text)

    def test_structured_attention_payload(self):
        board = self.board()
        entry = board.record_failure("builder", FC.NETWORK, error="reset")
        payload = health.structured_attention("builder", entry)
        self.assertEqual(payload.get("kind"), "attention")
        self.assertEqual(payload.get("attention"), "health_quarantine")
        self.assertEqual(payload.get("component"), "builder")


class SideEffectGuardTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        (self.workspace / "main.py").write_text("print('v1')\n", "utf-8")

    def test_read_only_reviewer_ignores_workspace_and_is_not_blocked(self):
        before = safety.snapshot_workspace(str(self.workspace))
        (self.workspace / "main.py").write_text("print('v2')\n", "utf-8")
        with mock.patch.object(safety, "snapshot_workspace",
                               side_effect=AssertionError("must not re-scan")):
            ok, changes = health.side_effect_guard(
                str(self.workspace), before, phase="reviewer", writing=False)
        self.assertTrue(ok)
        self.assertEqual(changes, [])

    def test_non_empty_diff_blocks_writing_retry(self):
        before = safety.snapshot_workspace(str(self.workspace))
        (self.workspace / "generated.html").write_text("<html></html>\n",
                                                       "utf-8")
        ok, changes = health.side_effect_guard(
            str(self.workspace), before, phase="builder", writing=True)
        self.assertFalse(ok)
        self.assertTrue(any(c["path"] == "generated.html" for c in changes))

    def test_writing_reviewer_with_override_and_diff_refuses(self):
        before = safety.snapshot_workspace(str(self.workspace))
        (self.workspace / "main.py").write_text("print('changed')\n", "utf-8")
        ok, changes = health.side_effect_guard(
            str(self.workspace), before, phase="reviewer", writing=True)
        self.assertFalse(ok)
        self.assertEqual([c["path"] for c in changes], ["main.py"])

    def test_writing_reviewer_allowed_artifact_passes(self):
        before = safety.snapshot_workspace(str(self.workspace))
        (self.workspace / "REVIEW.md").write_text("review notes\n", "utf-8")
        ok, changes = health.side_effect_guard(
            str(self.workspace), before, phase="reviewer", writing=True)
        self.assertTrue(ok)
        self.assertEqual(changes, [])


class ReviewerRetryTests(TempDirTestCase):
    def test_read_only_reviewer_retries_do_not_touch_builder(self):
        calls = {"builder": 0, "reviewer": 0}
        policy = health.RetryPolicy("reviewer", sleeper=lambda _s: None)

        def run_builder():
            calls["builder"] += 1
            return "builder-output"

        def run_reviewer(attempt):
            calls["reviewer"] += 1
            ok, _ = policy.should_retry(attempt, FC.NETWORK)
            return ok

        run_builder()
        retried = run_reviewer(1)
        again = run_reviewer(2)
        self.assertTrue(retried)
        self.assertFalse(again)
        self.assertEqual(calls["builder"], 1)
        self.assertEqual(calls["reviewer"], 2)


def _process_failures(state_dir, count):
    from deepseek_mcp import health as child_health
    board = child_health.HealthBoard(state_dir)
    for _ in range(count):
        board.record_failure("builder", child_health.FailureClass.NETWORK,
                             error="process reset")


class LockSafetyTests(TempDirTestCase):
    def test_two_threads_and_two_boards_do_not_corrupt_health_json(self):
        boards = [self.board(), self.board()]
        errors = []
        barrier = threading.Barrier(6)

        def worker(index):
            try:
                barrier.wait(timeout=10)
                board = boards[index % len(boards)]
                for _ in range(10):
                    board.record_failure("builder", FC.NETWORK,
                                         error="thread reset")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads))

        data = json.loads((self.state_dir / "health.json").read_text("utf-8"))
        self.assertEqual(data["version"], 1)
        entry = data["components"]["builder"]
        self.assertEqual(entry["consecutive_failures"], 60)
        self.assertTrue(entry["quarantined"])
        self.assertFalse((self.state_dir / "health.lock").exists())

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_two_processes_do_not_corrupt_health_json(self):
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_process_failures,
                             args=(str(self.state_dir), 5))
                 for _ in range(2)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=30)
        self.assertEqual([p.exitcode for p in procs], [0, 0])

        data = json.loads((self.state_dir / "health.json").read_text("utf-8"))
        entry = data["components"]["builder"]
        self.assertEqual(entry["consecutive_failures"], 10)
        self.assertTrue(entry["quarantined"])




class HarnessReviewerRetryIntegrationTests(unittest.TestCase):
    """A failed reviewer is retried without re-running the paid builder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = write_fake_dsh(self.root / "fake-dsh")
        self.counter = self.root / "calls.txt"
        self.wrapper = self.root / "count-dsh"
        self.wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "task = sys.argv[-1] if len(sys.argv) > 1 else ''\n"
            "kind = 'review' if 'INDEPENDENT REVIEWER' in task else 'builder'\n"
            "with open(os.environ['P2_COUNT_OUT'], 'a') as handle:\n"
            "    handle.write(kind + '\\n')\n"
            "fake = os.environ['P2_FAKE_DSH']\n"
            "os.execv(fake, [fake] + sys.argv[1:])\n",
            encoding="utf-8")
        self.wrapper.chmod(0o755)
        self.env = base_env(self.root, fake_dsh=self.wrapper,
                            sessions=self.root / "sessions")
        self.env.update({
            "P2_COUNT_OUT": str(self.counter),
            "P2_FAKE_DSH": str(self.fake),
            "FAKE_DSH_FAIL_REVIEW": "1",
            "DSH_RETRY_BACKOFF_BASE_SECONDS": "0",
            "DSH_RETRY_BACKOFF_CAP_SECONDS": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_MAX_CONCURRENT_CHILDREN": "4",
            "DSH_AUTO_SKILLS": "off",
            "DSH_QUALITY": "1",
        })

    def _run(self):
        args = {
            "prompt": "Build a small deliverable",
            "subagents": 1,
            "quality": True,
            "quality_rounds": 1,
            "independent_review": True,
            "workspace": str(self.workspace),
        }
        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = load_config(config_path=self.root / "config.json",
                              env=self.env)
            _text, run, _path = harness.run_harness(args, cfg)
        return cfg, run

    def test_failed_reviewer_retries_twice_and_builder_runs_once(self):
        cfg, run = self._run()
        calls = [line.strip() for line in
                 self.counter.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(calls.count("builder"), 1,
                         "the reviewer failure must not restart the builder")
        self.assertEqual(calls.count("review"), 2,
                         "reviewer retry policy allows exactly two attempts")
        self.assertEqual(run.get("review_status"), "error")
        self.assertEqual(run.get("status"), "ok_unreviewed")
        entry = health.HealthBoard(cfg.state_dir).snapshot()[
            "components"].get("reviewer") or {}
        self.assertEqual(entry.get("consecutive_failures"), 2)
        self.assertFalse(entry.get("quarantined"))




class QuarantineGateTests(unittest.TestCase):
    """Quarantine blocks new automatic phases until clear_health."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = write_fake_dsh(self.root / "fake-dsh")
        self.env = base_env(self.root, fake_dsh=self.fake,
                            sessions=self.root / "sessions")
        self.env.update({"DSH_AUTO_SKILLS": "off",
                         "DSH_LIVE_SCALING": "0",
                         "DSH_MAX_CONCURRENT_CHILDREN": "4"})
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.cfg = load_config(config_path=self.root / "config.json",
                                   env=self.env)
        self.args = {"prompt": "Build one deliverable", "subagents": 0,
                     "quality": False, "workspace": str(self.workspace)}

    def test_three_failures_block_the_next_run_until_cleared(self):
        board = health.HealthBoard(self.cfg.state_dir)
        for _ in range(3):
            board.record_failure("builder", health.FailureClass.NETWORK,
                                 error="connection reset")
        self.assertTrue(board.is_quarantined("builder"))
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                harness.run_harness(dict(self.args), self.cfg)
        self.assertIn("quarantined", str(caught.exception))
        self.assertFalse((self.root / "fake-dsh-state.json").exists(),
                         "a quarantined component must not spawn")

        from deepseek_mcp import server
        cleared = server.tool_maintenance(
            {"action": "clear_health", "component": "builder"}, self.cfg)
        self.assertIn("Health cleared", cleared)
        self.assertFalse(health.HealthBoard(
            self.cfg.state_dir).is_quarantined("builder"))
        with mock.patch.dict(os.environ, self.env, clear=False):
            _text, run, _path = harness.run_harness(dict(self.args), self.cfg)
        self.assertEqual(run.get("status"), "ok")

    def test_health_action_reports_components(self):
        board = health.HealthBoard(self.cfg.state_dir)
        for _ in range(3):
            board.record_failure("reviewer", health.FailureClass.RATE_LIMIT,
                                 retry_after=5)
        from deepseek_mcp import server
        text = server.tool_maintenance({"action": "health"}, self.cfg)
        self.assertIn("reviewer=quarantined", text)
        self.assertIn("retry_after=5", text)
        status = server.tool_maintenance({"action": "status"}, self.cfg)
        self.assertIn("Health (", status)


class BuilderSideEffectGuardIntegrationTests(unittest.TestCase):
    """A writing builder that changed files is not retried."""

    def test_partial_write_blocks_the_transient_retry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        counter = root / "calls.txt"
        dsh = root / "partial-dsh"
        dsh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "open(os.environ['P2_COUNT_OUT'], 'a').write('call\\n')\n"
            "pathlib.Path('.fake-partial.json').write_text('partial')\n"
            "print(json.dumps({'type': 'error', 'message': "
            "'fetch failed: ECONNRESET'}), flush=True)\n"
            "sys.exit(1)\n", encoding="utf-8")
        dsh.chmod(0o755)
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        env.update({"P2_COUNT_OUT": str(counter), "DSH_TRANSIENT_RETRY": "1",
                    "DSH_AUTO_SKILLS": "off", "DSH_LIVE_SCALING": "0",
                    "DSH_MAX_CONCURRENT_CHILDREN": "4",
                    "DSH_RETRY_BACKOFF_BASE_SECONDS": "0"})
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            with self.assertRaises(RuntimeError):
                harness.run_harness(
                    {"prompt": "write", "subagents": 0, "quality": False,
                     "workspace": str(workspace)}, cfg)
        self.assertEqual(len(counter.read_text().splitlines()), 1,
                         "the partial write must block the retry")
        runs = sorted((root / "runs").glob("*.json"))
        run = json.loads(runs[-1].read_text(encoding="utf-8"))
        retry = run.get("builder_retry") or {}
        self.assertEqual(retry.get("status"), "skipped")
        self.assertIn("workspace changed", retry.get("skip_reason") or "")
        self.assertTrue((workspace / ".fake-partial.json").is_file())


if __name__ == "__main__":
    unittest.main()
