"""S2: generic model_prices for reviewer and builder cost."""

import json
import unittest
from types import SimpleNamespace

from deepseek_mcp import api, harness, model_usage

MODEL = "claude-sonnet-5-5"
FULL = "anthropic/" + MODEL
ROW = {"input": 2.0, "cache_read": 0.2, "output": 6.0}
REVIEW = {"input": 0.5, "cache_read": 0.05, "output": 1.5}


def _usage(tokens_in=1000000, cache=0, out=0):
    return {"input_tokens": tokens_in, "cache_read_tokens": cache,
            "output_tokens": out}


class ReviewerPriceTests(unittest.TestCase):
    def test_reviewer_falls_back_to_model_prices(self):
        cfg = SimpleNamespace(dsh_model=MODEL, reviewer_prices={},
                              model_prices={MODEL: ROW})
        priced = harness._review_round_usage(
            {"tokens_in": 1000000, "tokens_out": 0,
             "models": {MODEL: {"input": 1000000, "cache_read": 0,
                                "output": 0}}}, cfg)
        self.assertTrue(priced["cost_known"])
        self.assertAlmostEqual(priced["cost"], 2.0, places=9)

    def test_reviewer_prices_override_model_prices(self):
        cfg = SimpleNamespace(dsh_model=MODEL,
                              reviewer_prices={MODEL: REVIEW},
                              model_prices={MODEL: ROW})
        priced = harness._review_round_usage(
            {"tokens_in": 1000000, "tokens_out": 0,
             "models": {MODEL: {"input": 1000000, "cache_read": 0,
                                "output": 0}}}, cfg)
        self.assertAlmostEqual(priced["cost"], 0.5, places=9)

    def test_reviewer_prices_override_across_key_forms(self):
        cfg = SimpleNamespace(dsh_model=MODEL,
                              reviewer_prices={FULL: REVIEW},
                              model_prices={MODEL: ROW})
        priced = harness._review_round_usage(
            {"tokens_in": 1000000, "tokens_out": 0,
             "models": {MODEL: {"input": 1000000, "cache_read": 0,
                                "output": 0}}}, cfg)
        self.assertAlmostEqual(priced["cost"], 0.5, places=9)

    def test_reviewer_does_not_price_builder(self):
        cfg = SimpleNamespace(dsh_model=MODEL,
                              reviewer_prices={MODEL: REVIEW},
                              model_prices={})
        _usage_out, cost = harness._sdk_usage_cost(
            cfg, _usage(), label="builder")
        self.assertIsNone(cost)

    def test_builder_sdk_uses_model_prices_for_non_deepseek(self):
        cfg = SimpleNamespace(dsh_model=MODEL, reviewer_prices={},
                              model_prices={FULL: ROW})
        _usage_out, cost = harness._sdk_usage_cost(
            cfg, _usage(), label="builder")
        self.assertAlmostEqual(cost, 2.0, places=9)

    def test_deepseek_builder_price_is_unchanged(self):
        cfg = SimpleNamespace(dsh_model="deepseek-flash",
                              reviewer_prices={MODEL: REVIEW},
                              model_prices={MODEL: ROW})
        _usage_out, cost = harness._sdk_usage_cost(
            cfg, _usage(), label="builder")
        expected = api.estimate_cost("deepseek-flash", {
            "prompt_tokens": 1000000, "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 1000000})
        self.assertAlmostEqual(cost, expected, places=9)

    def test_unknown_and_partial_rows_stay_unknown(self):
        cfg = SimpleNamespace(dsh_model="mystery-model",
                              reviewer_prices={},
                              model_prices={"mystery-model": {"input": 2.0}})
        unknown = harness._review_round_usage(
            {"tokens_in": 10, "tokens_out": 1,
             "models": {"mystery-model": {"input": 10, "cache_read": 0,
                                          "output": 1}}}, cfg)
        self.assertIsNone(unknown["cost"])
        self.assertFalse(unknown["cost_known"])
        never = harness._review_round_usage(
            {"tokens_in": 10, "tokens_out": 1,
             "models": {"mystery-model": {"input": 10, "cache_read": 0,
                                          "output": 1}}},
            SimpleNamespace(dsh_model="mystery-model", reviewer_prices={},
                            model_prices={}))
        self.assertIsNone(never["cost"])
        self.assertFalse(never["cost_known"])
        self.assertNotEqual(never["cost"], 0)

    def test_full_and_bare_keys_resolve_both_ways(self):
        for table_model, row_model in ((FULL, MODEL), (MODEL, FULL)):
            cfg = SimpleNamespace(dsh_model=MODEL,
                                  reviewer_prices={},
                                  model_prices={row_model: ROW})
            priced = harness._review_round_usage(
                {"tokens_in": 1000000, "tokens_out": 0,
                 "models": {table_model: {"input": 1000000,
                                          "cache_read": 0, "output": 0}}},
                cfg)
            self.assertTrue(priced["cost_known"])
            self.assertAlmostEqual(priced["cost"], 2.0, places=9)

    def test_headless_table_exposes_model_prices_fallback(self):
        cfg = SimpleNamespace(reviewer_prices={MODEL: REVIEW},
                              model_prices={MODEL: ROW})
        table = harness._reviewer_price_table(cfg)
        self.assertEqual(table[MODEL], REVIEW)
        partial = SimpleNamespace(
            reviewer_prices={MODEL: {"input": 0.5}},
            model_prices={MODEL: ROW})
        table = harness._reviewer_price_table(partial)
        self.assertEqual(table[MODEL], ROW)
        text = json.dumps({"type": "assistant/message", "data": {
            "message": {"id": "m1", "usage": {"inputTokens": 1000000,
                                              "cacheReadTokens": 0,
                                              "outputTokens": 0}}}})
        parsed = harness.parse_session_usage(
            text, default_model=MODEL,
            prices={MODEL: {"input": 2.0, "cache_read": 0.2,
                            "output": 6.0}})
        self.assertTrue(parsed["cost_known"])
        self.assertAlmostEqual(parsed["estimated_cost_usd"], 2.0, places=9)


class ModelUsagePriceTests(unittest.TestCase):
    def _run(self, prices, reviewer_prices, role="reviewer", model=MODEL):
        usage = model_usage.ModelUsage(prices=prices,
                                       reviewer_prices=reviewer_prices)
        usage.add_execution({
            "role": role, "model": model, "success": True,
            "tokens_in": 1000000, "cache_read_tokens": 0, "tokens_out": 0,
            "total_tokens": 1000000, "cost_source": "sdk_usage",
        })
        return usage.finish()

    def test_reviewer_row_uses_model_prices_fallback(self):
        out = self._run({MODEL: ROW}, {})
        row = out["models"][model_usage.label_for(MODEL)]
        self.assertTrue(row["cost_known"])
        self.assertAlmostEqual(row["estimated_cost_usd"], 2.0, places=9)

    def test_reviewer_row_prefers_reviewer_prices(self):
        out = self._run({MODEL: ROW}, {MODEL: REVIEW})
        row = out["models"][model_usage.label_for(MODEL)]
        self.assertAlmostEqual(row["estimated_cost_usd"], 0.5, places=9)

    def test_builder_row_never_uses_reviewer_prices(self):
        out = self._run({}, {MODEL: REVIEW}, role="builder")
        row = out["models"][model_usage.label_for(MODEL)]
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertFalse(row["cost_known"])

    def test_builder_unknown_model_is_not_zero(self):
        out = self._run({}, {}, role="builder", model="mystery-model")
        row = out["models"][model_usage.label_for("mystery-model")]
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertFalse(row["cost_known"])


if __name__ == "__main__":
    unittest.main()
