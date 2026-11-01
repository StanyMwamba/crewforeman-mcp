import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import server, spend, store
from deepseek_mcp.config import load_config
from deepseek_mcp.store import save_run
from tests.fakes import FakeDeepSeekServer, base_env


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fallback = self.root / "fallback-runs"
        patcher = mock.patch.object(store, "FALLBACK_LOG_DIR", self.fallback)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, **extra):
        env = base_env(self.root)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def add_run(self, cfg, run_id, cost, created=None):
        created = created or dt.datetime.now().astimezone().isoformat(timespec="seconds")
        save_run({"id": run_id, "kind": "single", "created": created, "title": run_id,
                  "model": "deepseek-flash", "status": "ok", "prompt": "p",
                  "estimated_cost_usd": cost, "files": []}, cfg)

    def test_today_spend_ignores_old_and_unknown(self):
        cfg = self.cfg()
        self.add_run(cfg, "today-a", 0.25)
        self.add_run(cfg, "today-b", 0.75)
        self.add_run(cfg, "old", 5.0, created="2020-01-01T00:00:00+00:00")
        self.add_run(cfg, "unknown", None)
        self.assertAlmostEqual(spend.spend_today(cfg), 1.0)
        self.assertEqual(spend.budget_state(cfg)["spent_usd"], 1.0)

    def test_alert_without_hard_block(self):
        cfg = self.cfg(DSH_DAILY_ALERT_USD="0.50")
        self.add_run(cfg, "run-a", 0.60)
        state = spend.budget_state(cfg)
        self.assertTrue(state["alert_crossed"])
        self.assertFalse(state["limit_reached"])
        self.assertIn("Daily spend alert", state["warning"])
        spend.preflight(cfg)  # must not raise without a limit

    def test_limit_blocks_preflight(self):
        cfg = self.cfg(DSH_DAILY_LIMIT_USD="1.00")
        self.add_run(cfg, "run-a", 1.00)
        state = spend.budget_state(cfg)
        self.assertTrue(state["limit_reached"])
        with self.assertRaises(RuntimeError) as caught:
            spend.preflight(cfg)
        self.assertIn("Daily limit reached", str(caught.exception))

    def test_alert_appears_in_ask_result(self):
        with FakeDeepSeekServer() as fake:
            cfg = self.cfg(DSH_DAILY_ALERT_USD="0.000001", DEEPSEEK_BASE_URL=fake.base_url)
            self.add_run(cfg, "prior", 0.05)
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}, clear=False):
                text = server.tool_ask({"prompt": "hello"}, cfg=cfg)
            self.assertIn("Daily spend alert", text)
            self.assertEqual(len(fake.requests), 1)

    def test_limit_blocks_before_http(self):
        with FakeDeepSeekServer() as fake:
            cfg = self.cfg(DSH_DAILY_LIMIT_USD="0.01", DEEPSEEK_BASE_URL=fake.base_url)
            self.add_run(cfg, "prior", 0.02)
            with self.assertRaises(RuntimeError):
                server.tool_ask({"prompt": "hello"}, cfg=cfg)
            self.assertEqual(len(fake.requests), 0)

    def test_with_warning_appends_only_when_crossed(self):
        cfg = self.cfg()
        self.assertEqual(spend.with_warning("body", cfg), "body")
        self.add_run(cfg, "run-a", 0.1)
        cfg2 = self.cfg(DSH_DAILY_ALERT_USD="0.05")
        self.assertIn("Daily spend alert", spend.with_warning("body", cfg2))


if __name__ == "__main__":
    unittest.main()
