"""P31 reviewer usage: private DSH_HOME sessions and configurable pricing."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepseek_mcp import api, config, harness

MODEL = "kimi-k3.1-pro"
INPUT = 1000
CACHE = 200
OUTPUT = 50


class _SdkCfg(dict):
    """Minimal cfg with mapping access plus attribute access like the real one."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _line(obj):
    return json.dumps(obj) + "\n"


def _session_text(workspace, model=MODEL):
    lines = [
        _line({"type": "session", "cwd": str(workspace)}),
        _line({
            "type": "assistant",
            "data": {"message": {
                "id": "msg-1", "role": "assistant", "model": model,
                "usage": {"inputTokens": INPUT, "cacheReadTokens": CACHE,
                          "outputTokens": OUTPUT},
            }},
        }),
    ]
    return "".join(lines)


class ReviewerUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        # An alternate DSH_HOME-like sessions root, not cfg.dsh_sessions_dir.
        self.alt_sessions = root / "alt-home" / "sessions"
        encoded = self.alt_sessions / harness.encode_workspace(self.workspace)
        encoded.mkdir(parents=True)
        self.session = encoded / "session.v4.jsonl"
        self.session.write_text(_session_text(self.workspace), encoding="utf-8")
        self.empty_base = root / "default-sessions"
        self.empty_base.mkdir()
        self.cfg = SimpleNamespace(dsh_sessions_dir=str(self.empty_base),
                                   zstd_bin="zstd", reviewer_prices={})
        self.since = time.time() - 60

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_session_usage_scans_session_dirs_before_home_deletion(self):
        usage, paths, reason = harness.read_session_usage(
            self.cfg, self.workspace, self.since,
            session_dirs=[self.alt_sessions], prices={})
        self.assertIn("no configured reviewer price", reason or "")
        self.assertTrue(paths)
        self.assertEqual(usage["input_tokens"], INPUT)
        self.assertEqual(usage["cache_read_tokens"], CACHE)
        self.assertEqual(usage["output_tokens"], OUTPUT)
        self.assertEqual(usage["total_tokens"], INPUT + CACHE + OUTPUT)
        self.assertEqual(usage["messages"], 1)

    def test_old_single_root_signature_still_works(self):
        self.assertEqual(harness.find_session_logs(
            self.cfg, self.workspace, self.since), [])

    def test_empty_reviewer_prices_records_tokens_without_cost(self):
        parsed = harness.parse_session_usage(_session_text(self.workspace), prices={})
        self.assertEqual(parsed["total_tokens"], INPUT + CACHE + OUTPUT)
        self.assertFalse(parsed["cost_known"])
        self.assertIsNone(parsed["estimated_cost_usd"])
        self.assertIn(MODEL, parsed["unknown_models"])

        usage, _paths, reason = harness.read_session_usage(
            self.cfg, self.workspace, self.since,
            session_dirs=[self.alt_sessions], prices={})
        self.assertEqual(usage["total_tokens"], INPUT + CACHE + OUTPUT)
        self.assertFalse(usage["cost_known"])
        self.assertIsNone(usage["estimated_cost_usd"])
        self.assertEqual(reason, "no configured reviewer price for %s" % MODEL)

    def test_configured_reviewer_prices_yield_known_cost(self):
        prices = {MODEL: {"input": 0.5, "cache_read": 0.05, "output": 1.5}}
        parsed = harness.parse_session_usage(
            _session_text(self.workspace), prices=prices)
        expected = (INPUT * 0.5 + CACHE * 0.05 + OUTPUT * 1.5) / 1000000.0
        self.assertTrue(parsed["cost_known"])
        self.assertAlmostEqual(parsed["estimated_cost_usd"], expected, places=9)

        usage, _paths, reason = harness.read_session_usage(
            self.cfg, self.workspace, self.since,
            session_dirs=[self.alt_sessions], prices=prices)
        self.assertIsNone(reason)
        self.assertTrue(usage["cost_known"])
        self.assertAlmostEqual(usage["estimated_cost_usd"], expected, places=9)

    def test_miss_hit_out_aliases_are_accepted(self):
        resolved, _name = harness._prices_for(
            MODEL, {MODEL: {"miss": 1.0, "hit": 2.0, "out": 3.0}})
        self.assertEqual(resolved, {"miss": 1.0, "hit": 2.0, "out": 3.0})

    def test_deepseek_builtin_prices_remain(self):
        parsed = harness.parse_session_usage(
            _session_text(self.workspace, model="deepseek-flash"), prices={})
        self.assertTrue(parsed["cost_known"])
        self.assertAlmostEqual(parsed["estimated_cost_usd"], 0.000181, places=9)

    def test_reviewer_usage_aggregate_over_review_entries(self):
        entries = [
            {"kind": "review", "tokens_in": 100, "cache_read_tokens": 10,
             "tokens_out": 20, "total_tokens": 130,
             "models": {MODEL: {"input": 100, "cache_read": 10, "output": 20}},
             "cost_known": True, "cost": 0.001},
            {"kind": "review", "tokens_in": 5, "cache_read_tokens": 0,
             "tokens_out": 1, "total_tokens": 6, "models": {},
             "cost_known": True, "cost": 0.002},
            {"kind": "revision", "tokens_in": 999, "cost_known": True, "cost": 9.0},
        ]
        aggregate = harness._reviewer_usage_aggregate(entries)
        self.assertEqual(aggregate["input"], 105)
        self.assertEqual(aggregate["cached"], 10)
        self.assertEqual(aggregate["output"], 21)
        self.assertEqual(aggregate["total"], 136)
        self.assertTrue(aggregate["cost_known"])
        self.assertAlmostEqual(aggregate["estimated_cost_usd"], 0.003, places=9)

    def test_reviewer_usage_aggregate_unknown_cost(self):
        aggregate = harness._reviewer_usage_aggregate([
            {"kind": "review", "tokens_in": 10, "cost_known": False, "cost": None},
        ])
        self.assertEqual(aggregate["input"], 10)
        self.assertFalse(aggregate["cost_known"])
        self.assertIsNone(aggregate["estimated_cost_usd"])

    def test_config_ships_empty_price_table(self):
        self.assertEqual(config.DEFAULTS.get("reviewer_prices"), {})
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        data = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(data.get("reviewer_prices"), {})


class PartialPriceTests(unittest.TestCase):
    def test_partial_price_row_is_not_treated_as_zero(self):
        resolved, _name = harness._prices_for(
            MODEL, {MODEL: {"input": 1.0}})
        self.assertIsNone(resolved)
        parsed = harness.parse_session_usage(
            _session_text(Path(tempfile.gettempdir())),
            prices={MODEL: {"input": 1.0}})
        self.assertFalse(parsed["cost_known"])
        self.assertIsNone(parsed["estimated_cost_usd"])


class SdkUsagePriceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _SdkCfg(
            dsh_model="deepseek-flash",
            reviewer_prices={"deepseek-flash": {
                "input": 5.0, "cache_read": 1.0, "output": 2.0}})
        self.raw = {"input_tokens": 1_000_000,
                    "cache_read_tokens": 200_000,
                    "output_tokens": 100_000}

    def test_reviewer_label_uses_configured_prices(self):
        usage, cost = harness._sdk_usage_cost(
            self.cfg, self.raw, label="reviewer round 1")
        self.assertEqual(usage["input_tokens"], 1_000_000)
        self.assertAlmostEqual(cost, 4.4, places=9)

    def test_builder_label_keeps_builtin_prices(self):
        _usage, cost = harness._sdk_usage_cost(
            self.cfg, self.raw, label="builder")
        expected = api.estimate_cost("deepseek-flash", {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
            "prompt_cache_hit_tokens": 200_000,
            "prompt_cache_miss_tokens": 800_000,
        })
        self.assertAlmostEqual(cost, expected, places=12)
        self.assertNotAlmostEqual(cost, 4.4, places=6)

    def test_reviewer_partial_row_stays_unknown(self):
        cfg = _SdkCfg(dsh_model=MODEL,
                      reviewer_prices={MODEL: {"input": 1.0}})
        _usage, cost = harness._sdk_usage_cost(
            cfg, self.raw, label="reviewer round 1")
        self.assertIsNone(cost)

    def test_budget_execution_threads_reviewer_label(self):
        execution = harness._sdk_budget_execution(
            self.cfg, {}, {}, time.time(), self.raw, 3, "sess-1",
            label="reviewer round 1")
        self.assertTrue(execution["cost_known"])
        self.assertAlmostEqual(execution["estimated_cost_usd"], 4.4, places=9)


if __name__ == "__main__":
    unittest.main()
