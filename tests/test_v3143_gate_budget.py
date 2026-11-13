"""Gate budget regression: the connector's own suite must fit the default.

Revision round 2: the reviewer's deterministic gate killed the 2205-test
connector suite at the 300 s default (exit -15, tests=0).  A budget that sits
inside the suite's normal run time makes the gate non-deterministic: the next
run under load fails again.  These tests pin the default headroom and prove
``run_gate`` hands that whole budget to the test check.
"""

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import config, gate

# The suite has taken 230-300 s on this host.  900 s keeps >3x headroom while
# still bounding a hung test run, and every explicit config value still wins.
MINIMUM_DEFAULT_SECONDS = 900.0


def _ok_check(name="tests"):
    return {
        "name": name, "status": "ok", "command": ["python"], "exit_code": 0,
        "seconds": 0.01, "tests": 1, "failures": 0, "errors": 0,
        "output_digest": "", "output_tail": "", "tree_changed": False,
        "timed_out": False, "findings": [],
    }


class GateBudgetDefaultsTests(unittest.TestCase):
    def test_default_gate_and_reviewer_budgets_have_headroom(self):
        self.assertGreaterEqual(
            config.DEFAULTS["gate_timeout_seconds"],
            MINIMUM_DEFAULT_SECONDS)
        self.assertGreaterEqual(
            config.DEFAULTS["review_test_timeout_seconds"],
            MINIMUM_DEFAULT_SECONDS)
        self.assertEqual(gate.DEFAULT_TOTAL_SECONDS,
                         config.DEFAULTS["gate_timeout_seconds"])

    def test_env_override_still_wins(self):
        cfg = config.load_config(
            config_path=None, include_file=False,
            env={"DSH_GATE_TIMEOUT_SECONDS": "45"})
        self.assertEqual(cfg.gate_timeout_seconds, 45.0)


class GateBudgetWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = self.tmp.name
        self.state = os.path.join(self.ws, "state")

    def _run_gate(self, **cfg_overrides):
        path = os.path.join(self.ws, "pkg", "mod.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        before = gate._snapshot(self.ws)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 2\n")
        values = {
            "gate_enabled": True,
            "gate_max_output_chars": 4000,
            "state_dir": self.state,
        }
        values.update(cfg_overrides)
        cfg = SimpleNamespace(**values)
        captured = {}

        def fake_tests(_cfg, _workspace, _run, deadline):
            captured["remaining"] = deadline - time.monotonic()
            return _ok_check()

        with mock.patch.object(gate, "_secret_check",
                               return_value=_ok_check("secrets")), \
                mock.patch.object(gate, "_lint_check",
                                  return_value=_ok_check("lint")), \
                mock.patch.object(gate, "_tests_check",
                                  side_effect=fake_tests):
            result = gate.run_gate(cfg, self.ws, {}, before=before)
        self.assertEqual(result["verdict"], "passed", result)
        return captured["remaining"]

    def test_gate_tests_check_gets_the_whole_default_budget(self):
        remaining = self._run_gate()
        self.assertGreaterEqual(remaining, MINIMUM_DEFAULT_SECONDS - 10.0)

    def test_explicit_gate_budget_is_not_enlarged(self):
        remaining = self._run_gate(gate_timeout_seconds=45.0)
        self.assertGreater(remaining, 30.0)
        self.assertLessEqual(remaining, 45.0)


class RetrySleepBudgetTests(unittest.TestCase):
    """The suite caps real retry backoff so it fits the gate on slow hosts."""

    def test_real_retry_backoff_is_capped_but_policy_and_injected_sleeper_are_not(self):
        from deepseek_mcp import health

        policy = health.RetryPolicy("reviewer")
        started = time.monotonic()
        policy.sleep(5.0)
        self.assertLess(time.monotonic() - started, 0.5,
                        "the test-only retry sleep cap is not active")
        # The backoff *policy* still reports the real delay; only the waiting
        # in tests is compressed.
        self.assertGreaterEqual(policy.delay(1), 1.0)

        slept = []
        injected = health.RetryPolicy("reviewer", sleeper=slept.append)
        injected.sleep(5.0)
        self.assertEqual(slept, [5.0],
                         "an explicitly injected sleeper must keep its value")


if __name__ == "__main__":
    unittest.main()
