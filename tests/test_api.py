import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import api, server
from deepseek_mcp.config import load_config
from tests.fakes import FakeDeepSeekServer, base_env


class ApiTests(unittest.TestCase):
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

    def test_one_agent_parses_answer_usage_and_cost(self):
        result = api.one_agent([{"role": "user", "content": "hello"}], "deepseek-flash", "max",
                               timeout=5, base_url=self.server.base_url, api_key="test")
        self.assertIn("Fake DeepSeek answer", result["answer"])
        self.assertEqual(result["usage"]["prompt_tokens"], 120)
        self.assertGreater(result["cost"], 0)
        self.assertEqual(result["reasoning"], "fake reasoning")

    def test_unknown_model_cost_is_unknown_not_zero(self):
        result = api.one_agent([{"role": "user", "content": "hi"}], "deepseek-chat", "max",
                               timeout=5, base_url=self.server.base_url, api_key="test")
        self.assertIsNone(result["cost"])

    def test_call_requires_key(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                api.call_deepseek([{"role": "user", "content": "hi"}], "deepseek-flash",
                                  base_url=self.server.base_url, api_key="")

    def test_tool_ask_saves_run_and_returns_cost(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
            text = server.tool_ask({"prompt": "Summarize this", "title": "T"}, cfg=self.cfg())
        self.assertIn("DeepSeek run", text)
        self.assertIn("est. $", text)
        runs = list((self.root / "runs").glob("*.json"))
        self.assertEqual(len(runs), 1)
        run = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["kind"], "single")
        self.assertGreater(run["estimated_cost_usd"], 0)
        self.assertTrue((self.root / "runs" / (run["id"] + ".md")).is_file())

    def test_swarm_stops_before_judge_when_budget_already_spent(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                server.tool_swarm({"prompt": "do a thing", "start_agents": 1,
                                   "max_agents": 1, "max_cost_usd": 0},
                                  cfg=self.cfg())
        self.assertIn("budget reached before launch", str(caught.exception))
        self.assertEqual(len(self.server.requests), 0)
        run = json.loads(list((self.root / "runs").glob("*.json"))[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["estimated_cost_usd"], 0.0)

    def test_invalid_swarm_option_saves_error_without_spending(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                server.tool_swarm({"prompt": "do a thing", "max_cost_usd": "not-a-number"},
                                  cfg=self.cfg())
        self.assertIn("Run saved", str(caught.exception))
        run = json.loads(list((self.root / "runs").glob("*.json"))[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "error")
        self.assertIn("Invalid swarm option", run["error"])
        self.assertEqual(len(self.server.requests), 0)

    def test_tool_ask_saves_failed_run(self):
        def broken(_request):
            return 500, {"error": {"message": "boom"}}

        broken_server = FakeDeepSeekServer(responder=broken)
        broken_server.__enter__()
        self.addCleanup(broken_server.__exit__, None, None, None)
        env = dict(self.env, DEEPSEEK_BASE_URL=broken_server.base_url)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                server.tool_ask({"prompt": "will fail"}, cfg=cfg)
        self.assertIn("Run saved", str(caught.exception))
        runs = list((self.root / "runs").glob("*.json"))
        self.assertEqual(len(runs), 1)
        run = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "error")


if __name__ == "__main__":
    unittest.main()
