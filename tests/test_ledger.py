import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import ledger, spend, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fallback = self.root / "fallback-runs"
        patcher = mock.patch.object(store, "FALLBACK_LOG_DIR", self.fallback)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=base_env(self.root))

    def _save(self, run_id, cost, created=None):
        created = created or dt.datetime.now().astimezone().isoformat(timespec="seconds")
        store.save_run({"id": run_id, "kind": "single", "created": created,
                        "title": run_id, "model": "deepseek-flash", "status": "ok",
                        "prompt": "p", "estimated_cost_usd": cost, "files": []}, self.cfg)

    def test_save_appends_and_spend_reads_today(self):
        self._save("ledger-a", 0.25)
        self._save("ledger-b", 0.75)
        lines = ledger.ledger_path(self.cfg).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(set(entry), {"date", "run_id", "cost"})
        self.assertAlmostEqual(spend.spend_today(self.cfg), 1.0)

    def test_duplicate_saves_are_deduped_by_run_id(self):
        self._save("same-run", 0.40)
        self._save("same-run", 0.40)
        self._save("same-run", 0.50)
        self.assertAlmostEqual(spend.spend_today(self.cfg), 0.5)

    def test_old_and_unknown_cost_runs_are_ignored(self):
        self._save("today", 0.20)
        self._save("old", 9.0, created="2020-01-01T00:00:00+00:00")
        self._save("unknown", None)
        self.assertAlmostEqual(spend.spend_today(self.cfg), 0.2)

    def test_missing_ledger_rebuilds_from_run_records(self):
        self._save("run-a", 0.30)
        self._save("run-b", 0.70)
        ledger.ledger_path(self.cfg).unlink()
        self.assertAlmostEqual(spend.spend_today(self.cfg), 1.0)
        self.assertTrue(ledger.ledger_path(self.cfg).is_file())

    def test_malformed_ledger_lines_are_skipped(self):
        self._save("good", 0.10)
        with ledger.ledger_path(self.cfg).open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write('{"date": "today", "run_id": "bad", "cost": "nope"}\n')
        self.assertAlmostEqual(spend.spend_today(self.cfg), 0.1)

    def test_budget_state_uses_ledger(self):
        self._save("run-a", 0.60)
        cfg = load_config(config_path=self.root / "config.json",
                          env=dict(base_env(self.root), DSH_DAILY_ALERT_USD="0.50"))
        state = spend.budget_state(cfg)
        self.assertTrue(state["alert_crossed"])
        self.assertIn("Daily spend alert", state["warning"])


if __name__ == "__main__":
    unittest.main()
