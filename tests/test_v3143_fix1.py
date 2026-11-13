"""Regression tests for FIX 1: closed-handle faults in the gate/revision path.

Run from the repo root with:  python -m pytest test_candidate.py -q
"""
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE, _HERE.parents[0] / "repo",
              _HERE.parents[2] / "repo", _HERE.parent):
    if (_cand / "deepseek_mcp" / "harness.py").is_file():
        sys.path.insert(0, str(_cand))
        break

from deepseek_mcp import harness, jobs, safety, status  # noqa: E402
from deepseek_mcp.config import load_config  # noqa: E402
from tests.fakes import base_env, write_fake_dsh  # noqa: E402


class ClosedHandleRetryTests(unittest.TestCase):
    def test_flush_on_closed_handle_does_not_escape_phase(self):
        calls = []

        def flaky_gate():
            calls.append(1)
            handle = io.StringIO()
            handle.close()
            handle.write("gate evidence")
            handle.flush()  # ValueError: I/O operation on closed file
            return {"verdict": "passed"}

        result, error = harness._call_closed_handle_safe(flaky_gate)
        self.assertIsNone(result)
        self.assertTrue(harness._is_closed_handle_error(error))
        self.assertEqual(len(calls), 2)  # one fresh-session retry

    def test_closed_handle_is_retried_once_on_fresh_call(self):
        calls = []

        def flaky_session():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("flush on a closed handle")
            return {"verdict": "passed"}

        result, error = harness._call_closed_handle_safe(flaky_session)
        self.assertIsNone(error)
        self.assertEqual(result, {"verdict": "passed"})
        self.assertEqual(len(calls), 2)

    def test_non_infrastructure_error_is_not_retried(self):
        calls = []

        def broken_gate():
            calls.append(1)
            raise RuntimeError("tests failed")

        result, error = harness._call_closed_handle_safe(broken_gate)
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(calls), 1)

    def test_closed_handle_detection(self):
        self.assertTrue(harness._is_closed_handle_error(
            'session "session-1": flush on a closed handle'))
        self.assertTrue(harness._is_closed_handle_error(
            ValueError("I/O operation on closed file")))
        self.assertFalse(harness._is_closed_handle_error("tests failed"))


class GateDegradedTerminalStatusTests(unittest.TestCase):
    def _degraded_run(self):
        run = {"id": "20261112-143641-471f46", "status": "error",
               "review_status": "pending", "review_in_flight": True}
        gate = {"verdict": "error", "gate_error":
                'session "session-060fd9bf": flush on a closed handle',
                "gate_error_class": "closed_handle"}
        harness._apply_gate_degraded(run, gate)
        return run, gate

    def test_degraded_run_is_not_an_error(self):
        run, _gate = self._degraded_run()
        self.assertEqual(run["status"], "ok_unreviewed")
        self.assertEqual(run["stop_reason"], "gate_error")
        self.assertFalse(run["review_in_flight"])
        self.assertIn("flush on a closed handle", run["gate_warning"])
        self.assertIn("not independently verified", run["gate_warning"])

    def test_final_status_maps_degraded_gate_to_ok_unreviewed(self):
        final, detail, passed = status.final_status(
            builder_status="ok", review_status="skipped",
            gate_status="gate_error")
        self.assertEqual(final, "ok_unreviewed")
        self.assertFalse(passed)
        self.assertIn("gate could not run", detail)

    def test_jobs_gate_accepts_degraded_gate_run(self):
        run, _gate = self._degraded_run()
        gate_tuple = jobs._review_gate_state_from_run(run)
        self.assertIsNone(jobs._review_gate_error(gate_tuple))
        # Backward compatibility: the historical 5-tuple still refuses a
        # genuine gate_failed run.
        self.assertIsNotNone(jobs._review_gate_error(
            (False, True, False, "gate_failed", "tests failed")))


if __name__ == "__main__":
    unittest.main()


class GateClosedHandleIntegrationTest(unittest.TestCase):
    """A closed-handle gate revision degrades through the real run record."""

    def test_run_harness_degrades_instead_of_erroring(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        error = ValueError('session "s": flush on a closed handle')
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("deepseek_mcp.gate.run_gate", side_effect=error):
            _text, run, _path = harness.run_harness({
                "prompt": "small task", "subagents": 0, "quality": True,
                "independent_review": True, "quality_rounds": 1,
                "workspace": str(workspace),
            }, cfg)
        self.assertEqual(run["status"], "ok_unreviewed", run.get("error"))
        self.assertEqual(run["stop_reason"], "gate_error")
        self.assertEqual(run["review_status"], "skipped")
        self.assertIn("flush on a closed handle", run["gate_error"])
        self.assertTrue(run["gate_warning"])


class StopDrainIntegrationTest(unittest.TestCase):
    """The stop path must drain a child that is still flushing."""

    def test_stop_keeps_draining_until_the_child_exits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        child_env = dict(env)
        child_env.update({
            "FAKE_DSH_MODE": "drain",
            "FAKE_DSH_DRAIN_SECONDS": "1.2",
            "FAKE_DSH_CLOSED_HANDLE_STDERR": "1",
        })
        with mock.patch.dict(os.environ, child_env, clear=False):
            result = harness._execute_once_impl(
                cfg, workspace, "drain after stop", limit=0.4,
                policy=safety.resolve({}, cfg), dsh_bin=str(dsh))
        self.assertEqual(result["stop_reason"], "time_limit")
        if os.name == "nt":
            # Windows has no SIGTERM: the stop path uses taskkill /F, so the
            # fake cannot flush a final event after the stop. The bounded-kill
            # test below covers that path; here we only require the available
            # stream to have been read and the teardown noise stripped.
            self.assertGreaterEqual(result["events"], 1)
        else:
            self.assertEqual(result["answer"], "drain complete")
            self.assertGreaterEqual(result["events"], 3,
                                    "events written after the stop were lost")
        self.assertNotIn("flush on a closed handle", result["error"] or "")
        self.assertNotIn("flush on a closed handle",
                         result.get("stderr_tail") or "")

    def test_a_child_that_outlives_the_drain_window_is_killed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        child_env = dict(env)
        child_env.update({
            "FAKE_DSH_MODE": "drain",
            "FAKE_DSH_DRAIN_SECONDS": "10",
        })
        started = time.monotonic()
        with mock.patch.dict(os.environ, child_env, clear=False):
            result = harness._execute_once_impl(
                cfg, workspace, "drain forever", limit=0.3,
                policy=safety.resolve({}, cfg), dsh_bin=str(dsh))
        elapsed = time.monotonic() - started
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertLess(elapsed, 6.0,
                        "a child that ignores SIGTERM must be killed")
        self.assertNotIn("flush on a closed handle", result["error"] or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
