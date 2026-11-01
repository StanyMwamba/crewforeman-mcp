"""Boundary tests for ``swarm_deepseek`` limits.

The documented contract is: validate every option before the first HTTP call;
``max_agents`` is a hard whole-swarm cap and the first round is reduced to it;
``confidence_target=0`` is honoured; ``max_cost_usd`` and ``max_seconds`` are
whole-swarm limits checked before the first call and before every later agent,
judge and round. These tests use the offline fake DeepSeek server.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import api, server
from deepseek_mcp.config import load_config
from tests.fakes import FakeDeepSeekServer, base_env


class SwarmLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = FakeDeepSeekServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.env = base_env(self.root, base_url=self.server.base_url)

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    # ------------------------------------------------------------- helpers

    def _run(self, args):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
            return server.tool_swarm(args, cfg=self.cfg())

    def _run_expecting_error(self, args):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                server.tool_swarm(args, cfg=self.cfg())
        return str(caught.exception)

    @staticmethod
    def _is_judge(request):
        return any(message.get("role") == "system"
                   and message.get("content") == api.JUDGE_SYSTEM
                   for message in request.get("messages") or [])

    def _requests(self):
        return list(self.server.requests)

    def _agent_requests(self):
        return [request for request in self._requests() if not self._is_judge(request)]

    def _judge_requests(self):
        return [request for request in self._requests() if self._is_judge(request)]

    def _saved_run_list(self):
        return sorted((self.root / "runs").glob("*.json"),
                      key=lambda path: path.stat().st_mtime_ns)

    def _saved_run(self):
        runs = self._saved_run_list()
        self.assertTrue(runs, "tool_swarm did not save a run JSON")
        return json.loads(runs[-1].read_text(encoding="utf-8"))

    # --------------------------------------------------------------- tests

    def test_start_six_cap_two_launches_two_agents_and_one_judge(self):
        text = self._run({"prompt": "compare options", "start_agents": 6,
                          "max_agents": 2})
        self.assertIn("DeepSeek swarm", text)
        self.assertIn("2 agents", text)
        self.assertEqual(len(self._agent_requests()), 2)
        self.assertEqual(len(self._judge_requests()), 1)
        self.assertEqual(len(self._requests()), 3)
        run = self._saved_run()
        self.assertEqual(run["agents_used"], 2)
        self.assertEqual(run["start_agents"], 6)
        self.assertEqual(run["start_agents_effective"], 2)
        self.assertEqual(run["max_agents"], 2)
        self.assertEqual(run["stop_reason"], "max agents reached (2)")

    def test_confidence_target_zero_stops_after_one_round(self):
        text = self._run({"prompt": "quick answer", "start_agents": 2,
                          "max_agents": 5, "confidence_target": 0})
        self.assertIn("judge confident", text)
        self.assertEqual(len(self._agent_requests()), 2)
        self.assertEqual(len(self._judge_requests()), 1)
        run = self._saved_run()
        self.assertEqual(run["confidence_target"], 0.0)
        self.assertEqual(run["rounds"], 1)
        self.assertEqual(run["agents_used"], 2)
        self.assertEqual(run["stop_reason"], "judge confident (0.00 >= 0.0)")

    def test_zero_cost_budget_makes_no_request_and_saves_error_run(self):
        text = self._run_expecting_error({"prompt": "do work", "max_cost_usd": 0})
        self.assertIn("budget reached before launch", text)
        self.assertIn("Run saved", text)
        self.assertEqual(len(self._requests()), 0)
        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["agents_used"], 0)
        self.assertEqual(run["rounds"], 0)
        self.assertEqual(run["estimated_cost_usd"], 0.0)
        self.assertEqual(run["stop_reason"], "budget reached before launch")

    def test_zero_time_limit_makes_no_request_and_saves_error_run(self):
        text = self._run_expecting_error({"prompt": "do work", "max_seconds": 0})
        self.assertIn("time limit reached before launch", text)
        self.assertIn("Run saved", text)
        self.assertEqual(len(self._requests()), 0)
        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["agents_used"], 0)
        self.assertEqual(run["estimated_cost_usd"], 0.0)
        self.assertEqual(run["stop_reason"], "time limit reached before launch")

    def test_tiny_cost_budget_stops_before_second_agent_and_skips_judge(self):
        text = self._run({"prompt": "do work", "start_agents": 2,
                          "max_agents": 5, "max_cost_usd": 1e-9})
        self.assertIn("confidence n/a", text)
        self.assertEqual(len(self._agent_requests()), 1)
        self.assertEqual(len(self._judge_requests()), 0)
        run = self._saved_run()
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["agents_used"], 1)
        self.assertEqual(run["rounds"], 1)
        self.assertEqual(run["stop_reason"], "budget reached")
        self.assertTrue(run["judge_skipped"])
        self.assertTrue(run["answer"])

    def test_unpriced_model_with_cost_budget_stops_after_one_call(self):
        text = self._run({"prompt": "unpriced", "model": "deepseek-chat",
                          "start_agents": 2, "max_agents": 2,
                          "max_cost_usd": 0.01})
        self.assertIn("unpriced model", text)
        self.assertEqual(len(self._agent_requests()), 1)
        self.assertEqual(len(self._judge_requests()), 0)
        run = self._saved_run()
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["agents_used"], 1)
        self.assertIsNone(run["estimated_cost_usd"])
        self.assertFalse(run["cost_known"])
        self.assertEqual(run["stop_reason"],
                         "unpriced model: cost budget cannot be enforced")

    def test_cost_budget_keeps_only_one_call_in_flight(self):
        original = self.server.responder
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def slow_responder(request):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(0.05)
                return original(request)
            finally:
                with lock:
                    active[0] -= 1

        self.server.responder = slow_responder
        self.server.httpd.responder = slow_responder
        self._run({"prompt": "serial", "start_agents": 3, "max_agents": 5,
                   "max_cost_usd": 10})
        self.assertEqual(peak[0], 1, "cost-budgeted calls must be serialised")

    def test_max_agents_one_stops_after_one_agent_and_judge(self):
        self._run({"prompt": "one", "start_agents": 3, "max_agents": 1})
        self.assertEqual(len(self._agent_requests()), 1)
        self.assertEqual(len(self._judge_requests()), 1)
        run = self._saved_run()
        self.assertEqual(run["agents_used"], 1)
        self.assertEqual(run["stop_reason"], "max agents reached (1)")

    def test_invalid_options_are_rejected_before_request(self):
        cases = [
            {"start_agents": 0},
            {"start_agents": 1.5},
            {"max_agents": 0},
            {"max_agents": -1},
            {"confidence_target": 1.5},
            {"confidence_target": -0.1},
            {"confidence_target": float("nan")},
            {"confidence_target": float("inf")},
            {"max_cost_usd": -0.01},
            {"max_cost_usd": float("inf")},
            {"max_seconds": -1},
            {"max_seconds": float("nan")},
            {"max_tokens": 0},
        ]
        for options in cases:
            with self.subTest(options=options):
                before = len(self._saved_run_list())
                text = self._run_expecting_error(
                    dict({"prompt": "bad", "start_agents": 1, "max_agents": 1},
                         **options))
                self.assertIn("Invalid swarm option", text)
                self.assertIn("Run saved", text)
                self.assertEqual(len(self._requests()), 0)
                self.assertEqual(len(self._saved_run_list()), before + 1)
                run = self._saved_run()
                self.assertEqual(run["status"], "error")
                self.assertIn("Invalid swarm option", run["error"])
                self.assertEqual(run["agents_used"], 0)

    def test_schema_documents_the_enforced_contract(self):
        tools = server.dispatch("tools/list", cfg=self.cfg())["result"]["tools"]
        swarm = {tool["name"]: tool for tool in tools}["swarm_deepseek"]
        props = swarm["inputSchema"]["properties"]
        self.assertEqual(props["start_agents"]["minimum"], 1)
        self.assertEqual(props["max_agents"]["minimum"], 1)
        self.assertEqual(props["confidence_target"]["minimum"], 0)
        self.assertEqual(props["confidence_target"]["maximum"], 1)
        self.assertEqual(props["max_cost_usd"]["minimum"], 0)
        self.assertEqual(props["max_seconds"]["minimum"], 0)
        self.assertIn("Never raised", props["max_agents"]["description"])
        self.assertIn("0 is honoured", props["confidence_target"]["description"])
        self.assertIn("0 starts no call", props["max_cost_usd"]["description"])
        self.assertIn("0 starts no call", props["max_seconds"]["description"])
        self.assertIn("in-flight", props["max_cost_usd"]["description"])


if __name__ == "__main__":
    unittest.main()
