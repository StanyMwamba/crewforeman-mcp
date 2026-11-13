"""Regression tests for FIX 1: closed-handle faults in the gate/revision path.

Run from the repo root with:  python -m pytest test_candidate.py -q
"""
import io
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE, _HERE.parents[0] / "repo",
              _HERE.parents[2] / "repo", _HERE.parent):
    if (_cand / "deepseek_mcp" / "harness.py").is_file():
        sys.path.insert(0, str(_cand))
        break

from deepseek_mcp import harness, jobs, status  # noqa: E402


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
