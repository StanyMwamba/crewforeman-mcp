"""v3.4.0: one bounded retry for fast, output-less transient failures."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh


class RetryPolicyTests(unittest.TestCase):
    def execution(self, **overrides):
        value = {
            "status": "error", "stop_reason": "error", "stalled": False,
            "timed_out": False, "output_files": [], "seconds": 5.0,
            "error": "fetch failed: ECONNRESET", "stderr_tail": "",
        }
        value.update(overrides)
        return value

    def test_transient_network_failure_is_retryable(self):
        for message in ("ECONNRESET", "socket hang up", "HTTP 503",
                        "temporarily unavailable", "429 Too Many Requests"):
            with self.subTest(message=message):
                reason = harness._transient_failure_reason(
                    self.execution(error=message))
                self.assertTrue(reason)
                self.assertIn("transient", reason)

    def test_permanent_and_dangerous_failures_are_not_retried(self):
        cases = [
            {"status": "ok"},
            {"stop_reason": "stalled"},
            {"stop_reason": "time_limit"},
            {"stalled": True},
            {"timed_out": True},
            {"output_files": ["candidate.html"]},
            {"seconds": 31.0},
            {"error": "Insufficient Balance"},
            {"error": "HTTP 401 Unauthorized"},
            {"error": "invalid api key"},
            {"error": "plain model failure"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertIsNone(harness._transient_failure_reason(
                    self.execution(**overrides)))

    def test_retry_budget_is_bounded(self):
        self.assertEqual(harness._retry_budget(None, 0), 180.0)
        self.assertEqual(harness._retry_budget(600, 5), 180.0)
        self.assertEqual(harness._retry_budget(100, 10), 75.0)
        self.assertIsNone(harness._retry_budget(60, 45))
        self.assertIsNone(harness._retry_budget(60, 100))


class RetryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.env = base_env(self.root, fake_dsh=self.dsh, sessions=self.root / "sessions")
        self.env.update({
            "FAKE_DSH_MODE": "flaky",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
            "FAKE_DSH_SESSIONS": str(self.root / "sessions"),
        })

    def _run(self, job=None, **extra):
        env = dict(os.environ)
        env.update(self.env)
        env.update(extra)
        args = {
            "prompt": "Do a small task", "subagents": 0, "quality": False,
            "workspace": str(self.workspace),
        }
        args.update(job or {})
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            return harness.run_harness(args, cfg)

    def test_one_transient_retry_completes_the_run(self):
        _text, run, _path = self._run()
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["builder_retry"]["status"], "ok")
        self.assertIn("ECONNRESET", run["builder_retry"]["reason"])
        state = json.loads((self.root / "fake-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["flaky"], 2)

    def test_retry_can_be_disabled(self):
        with self.assertRaises(RuntimeError):
            self._run(DSH_TRANSIENT_RETRY="0")
        state = json.loads((self.root / "fake-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["flaky"], 1)

    def test_whole_job_cost_budget_blocks_the_transient_retry(self):
        """The retry is a paid phase: an exceeded ceiling must stop it."""
        with self.assertRaises(RuntimeError):
            self._run(job={"job_max_cost_usd": 1e-6})
        state = json.loads((self.root / "fake-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["flaky"], 1, "a paid retry started after the ceiling")
        run = json.loads(sorted((self.root / "runs").glob("*.json"))[-1]
                         .read_text(encoding="utf-8"))
        retry = run.get("builder_retry") or {}
        self.assertEqual(retry.get("status"), "skipped")
        self.assertIn("cost budget", retry.get("skip_reason") or "")
        self.assertGreater(run["job_budget"]["cost_usd"], 1e-6)
        self.assertTrue(run["job_budget"]["cost_known"])
        self.assertTrue(run["job_budget"]["cost_exceeded"])
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")


if __name__ == "__main__":
    unittest.main()
