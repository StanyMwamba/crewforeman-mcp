"""S2 regression tests: stream drops are network failures and retry boundedly.

The first classes exercise the shared failure vocabulary and ``RetryPolicy``.
``ProcessRetryTests`` then drives the exact markers through a real
``run_harness`` builder and reviewer call path, asserting the bounded attempt
counts and the backoff without any real sleeping.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, health
from deepseek_mcp.config import load_config
from deepseek_mcp.health import FailureClass, RetryPolicy
import tests
from tests.fakes import base_env, write_fake_dsh


STREAM_DROP_MARKERS = (
    "flush on a closed handle",
    "STREAM_CLOSED",
    "stream closed",
    "ECONNRESET",
    "write failed: flush on a closed handle",
)


class ClassifyStreamDropTests(unittest.TestCase):
    def test_classify_marks_stream_drops_as_network(self):
        for text in STREAM_DROP_MARKERS:
            with self.subTest(text=text):
                self.assertEqual(health.classify(text), FailureClass.NETWORK)

    def test_classify_execution_marks_stream_drops_as_network(self):
        for key in ("error", "stop_reason", "stderr_tail"):
            for text in ("flush on a closed handle", "STREAM_CLOSED"):
                with self.subTest(field=key, text=text):
                    execution = {"status": "error", "exit_code": 1, key: text}
                    self.assertEqual(health.classify_execution(execution),
                                     FailureClass.NETWORK)


class RetryPolicyStreamDropTests(unittest.TestCase):
    def test_reviewer_allows_attempt_one_then_stops(self):
        policy = RetryPolicy("reviewer", jitter=0.0)
        allowed, _reason = policy.should_retry(1, FailureClass.NETWORK)
        self.assertTrue(allowed)
        allowed, reason = policy.should_retry(2, FailureClass.NETWORK)
        self.assertFalse(allowed)
        self.assertIn("attempts exhausted", reason)

    def test_builder_allows_attempt_one_then_stops(self):
        policy = RetryPolicy("builder", jitter=0.0)
        allowed, _reason = policy.should_retry(1, FailureClass.NETWORK)
        self.assertTrue(allowed)
        allowed, reason = policy.should_retry(2, FailureClass.NETWORK)
        self.assertFalse(allowed)
        self.assertIn("attempts exhausted", reason)

    def test_auth_and_no_balance_stay_non_retryable(self):
        for phase in ("reviewer", "builder"):
            policy = RetryPolicy(phase)
            for failure_class in (FailureClass.AUTH, FailureClass.NO_BALANCE):
                with self.subTest(phase=phase, failure_class=failure_class):
                    allowed, reason = policy.should_retry(1, failure_class)
                    self.assertFalse(allowed)
                    self.assertIn("non-retryable", reason)

    def test_delay_backoff_increases_is_bounded_and_never_sleeps(self):
        slept = []
        policy = RetryPolicy("reviewer", base=1.0, cap=5.0, jitter=0.0,
                             sleeper=slept.append)
        delays = [policy.delay(attempt) for attempt in range(1, 7)]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 5.0, 5.0, 5.0])
        self.assertEqual(delays, sorted(delays))
        self.assertTrue(all(0.0 <= delay <= policy.cap for delay in delays))
        self.assertEqual(slept, [])  # delay() itself must not sleep
        policy.sleep(delays[0])
        self.assertEqual(slept, [1.0])


def _selective_wrapper(path, real, calls, counters, *,
                       builder_fails=0, builder_message="",
                       review_fails=0, review_message=""):
    """A process-level dsh wrapper that fails selected calls with a marker."""
    source = '''#!/usr/bin/env python3
import os
import sys

REAL = %(real)r
CALLS = %(calls)r
BUILDER_COUNTER = %(builder_counter)r
REVIEW_COUNTER = %(review_counter)r
BUILDER_FAILS = %(builder_fails)d
BUILDER_MESSAGE = %(builder_message)r
REVIEW_FAILS = %(review_fails)d
REVIEW_MESSAGE = %(review_message)r
task = sys.argv[-1] if len(sys.argv) else ""


def bump(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = int(handle.read().strip() or "0")
    except Exception:
        value = 0
    value += 1
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(value))
    return value


def record(kind):
    with open(CALLS, "a", encoding="utf-8") as handle:
        handle.write(kind + "\\n")


if "INDEPENDENT REVIEWER" in task:
    record("review")
    if bump(REVIEW_COUNTER) <= REVIEW_FAILS:
        os.environ["FAKE_DSH_FAIL_REVIEW"] = "1"
        os.environ["FAKE_DSH_REVIEW_ERROR_MESSAGE"] = REVIEW_MESSAGE
    os.execv(REAL, [REAL] + sys.argv[1:])
record("builder")
if bump(BUILDER_COUNTER) <= BUILDER_FAILS:
    os.environ["FAKE_DSH_MODE"] = "error"
    os.environ["FAKE_DSH_ERROR_MESSAGE"] = BUILDER_MESSAGE
os.execv(REAL, [REAL] + sys.argv[1:])
''' % {
        "real": str(real), "calls": str(calls),
        "builder_counter": str(counters[0]),
        "review_counter": str(counters[1]),
        "builder_fails": int(builder_fails),
        "builder_message": builder_message,
        "review_fails": int(review_fails),
        "review_message": review_message,
    }
    path = Path(path)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ProcessRetryTests(unittest.TestCase):
    """The markers must reach the real builder/reviewer retry loops."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.calls = self.root / "calls.txt"
        self.real = write_fake_dsh(self.root / "fake-dsh")
        self.env = base_env(self.root, fake_dsh=self.real,
                            sessions=self.root / "sessions")

    def _run(self, **wrapper_options):
        wrapper = _selective_wrapper(
            self.root / "selective-dsh", self.real, self.calls,
            (self.root / "builder-count", self.root / "review-count"),
            **wrapper_options)
        env = dict(self.env)
        env["DSH_BIN"] = str(wrapper)
        slept = []
        self.last_slept = slept

        def fake_sleep(policy, seconds):
            slept.append(seconds)

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            with mock.patch.object(health.RetryPolicy, "sleep", fake_sleep):
                result = harness.run_harness({
                    "prompt": "Do a small task", "subagents": 0,
                    "quality": False, "workspace": str(self.workspace),
                }, cfg)
        return result, slept

    def _review_run(self, **wrapper_options):
        wrapper = _selective_wrapper(
            self.root / "selective-dsh", self.real, self.calls,
            (self.root / "builder-count", self.root / "review-count"),
            **wrapper_options)
        env = dict(self.env)
        env["DSH_BIN"] = str(wrapper)
        slept = []
        self.last_slept = slept

        def fake_sleep(policy, seconds):
            slept.append(seconds)

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            with mock.patch.object(health.RetryPolicy, "sleep", fake_sleep):
                result = harness.run_harness({
                    "prompt": "Write a concise release note",
                    "subagents": 1, "quality": True,
                    "independent_review": True, "quality_rounds": 1,
                    "workspace": str(self.workspace),
                }, cfg)
        return result, slept

    def calls_for(self, label):
        if not self.calls.is_file():
            return []
        return [line.strip() for line in
                self.calls.read_text(encoding="utf-8").splitlines()
                if line.strip() == label]

    def test_builder_retries_stream_closed_once_then_stops(self):
        with self.assertRaises(RuntimeError):
            self._run(builder_fails=999, builder_message="STREAM_CLOSED")
        self.assertEqual(self.calls_for("builder"), ["builder", "builder"])
        self.assertEqual(len(self.last_slept), 1)
        self.assertGreaterEqual(self.last_slept[0], 1.0)
        self.assertLessEqual(self.last_slept[0], 1.25)

    def test_builder_retries_flush_on_closed_handle_and_succeeds(self):
        (_text, run, _path), slept = self._run(
            builder_fails=1, builder_message="flush on a closed handle")
        self.assertEqual(run.get("status"), "ok", run.get("error"))
        self.assertEqual(self.calls_for("builder"), ["builder", "builder"])
        self.assertEqual(run["builder_retry"]["status"], "ok")
        self.assertIn("flush on a closed handle",
                      run["builder_retry"]["reason"])
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 1.0)
        self.assertLessEqual(slept[0], 1.25)

    def test_reviewer_retries_stream_closed_once_then_stops(self):
        (_text, run, _path), slept = self._review_run(
            review_fails=999, review_message="STREAM_CLOSED")
        self.assertEqual(self.calls_for("review"), ["review", "review"])
        self.assertEqual(run.get("review_status"), "error")
        self.assertEqual(slept, [1.0])

    def test_reviewer_retries_flush_on_closed_handle_and_succeeds(self):
        (_text, run, _path), slept = self._review_run(
            review_fails=1, review_message="flush on a closed handle")
        self.assertEqual(run.get("review_status"), "accepted",
                         run.get("review_error"))
        self.assertEqual(run.get("quality_score"), 8.6)
        self.assertEqual(self.calls_for("review"), ["review", "review"])
        self.assertEqual(slept, [1.0])


if __name__ == "__main__":
    unittest.main()
