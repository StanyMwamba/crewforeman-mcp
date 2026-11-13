"""3.15.0 MiMo cost accounting: prices, reasoning, and the by_vendor split.

Stdlib unittest only; no network.  Every case is isolation-friendly (the
per-test hook wraps ``TestCase.run``) and only temp state is touched.
"""

import json
import unittest
from types import SimpleNamespace

from deepseek_mcp import api, spend, status, waste_report
from deepseek_mcp.model_usage import (
    ModelUsage, build_model_usage, by_vendor_of, label_for)


def _cfg(**overrides):
    values = {
        "model_usage_max_models": 8,
        "model_prices": {},
        "reviewer_prices": {},
        "mimo_prices": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _exec(model, tokens_in=0, cache=0, out=0, reasoning=0, total=None,
          **extra):
    if total is None:
        total = tokens_in + cache + out + reasoning
    return {
        "role": "builder", "models": [model], "success": True,
        "tokens_in": tokens_in, "cache_read_tokens": cache,
        "tokens_out": out, "reasoning_tokens": reasoning,
        "total_tokens": total, "cost_source": "mimo_events",
    }


def _run(execs, cfg=None):
    return build_model_usage(execs, cfg=cfg or _cfg())


class MimoPriceMathTests(unittest.TestCase):
    """Exact USD math against the builtin MIMO_PRICES table."""

    def test_mimo_v26_pro_price_math(self):
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=1000, cache=500,
                          out=200)])
        label = label_for("xiaomi/mimo-v2.6-pro")
        row = out["models"][label]
        self.assertTrue(row["cost_known"])
        expected = round((1000 * 0.435 + 500 * 0.0036 + 200 * 0.87) / 1e6, 6)
        self.assertAlmostEqual(row["estimated_cost_usd"], expected, places=9)
        self.assertAlmostEqual(out["totals"]["estimated_cost_usd"], expected,
                               places=9)

    def test_mimo_v26_flash_price_math(self):
        out = _run([_exec("xiaomi/mimo-v2.6-flash", tokens_in=1000, cache=500,
                          out=200)])
        row = out["models"][label_for("xiaomi/mimo-v2.6-flash")]
        expected = round((1000 * 0.14 + 500 * 0.0036 + 200 * 0.28) / 1e6, 6)
        self.assertTrue(row["cost_known"])
        self.assertAlmostEqual(row["estimated_cost_usd"], expected, places=9)

    def test_loose_name_matching_finds_builtin(self):
        # Bare and provider-qualified spellings both resolve to MIMO_PRICES.
        for name in ("mimo-v2.6-pro", "xiaomi/mimo-v2.6-pro"):
            out = _run([_exec(name, tokens_in=1000)])
            row = out["models"][label_for(name)]
            self.assertAlmostEqual(row["estimated_cost_usd"],
                                   0.435 / 1e3, places=9)

    def test_mimo_events_cost_source_recorded(self):
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=10)])
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertEqual(row["cost_source"], "mimo_events")


class MimoConfigPriceTests(unittest.TestCase):
    """cfg mimo_prices overrides the builtin table per model."""

    OVERRIDE = {"input": 1.0, "cache_hit": 0.1, "output": 2.0}

    def test_mimo_prices_dict_override_wins(self):
        cfg = _cfg(mimo_prices={"mimo-v2.6-pro": dict(self.OVERRIDE)})
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=1000, cache=500,
                          out=200)], cfg=cfg)
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        expected = (1000 * 1.0 + 500 * 0.1 + 200 * 2.0) / 1e6
        self.assertAlmostEqual(row["estimated_cost_usd"], expected, places=9)

    def test_mimo_prices_json_string_override_wins(self):
        cfg = _cfg(mimo_prices=json.dumps(
            {"mimo-v2.6-pro": dict(self.OVERRIDE)}))
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=1000)], cfg=cfg)
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertAlmostEqual(row["estimated_cost_usd"], 1.0 / 1e3,
                               places=9)

    def test_mimo_prices_override_is_per_model_only(self):
        cfg = _cfg(mimo_prices={"mimo-v2.6-pro": dict(self.OVERRIDE)})
        out = _run([_exec("xiaomi/mimo-v2.6-flash", tokens_in=1000)], cfg=cfg)
        row = out["models"][label_for("xiaomi/mimo-v2.6-flash")]
        self.assertAlmostEqual(row["estimated_cost_usd"], 0.14 / 1e3,
                               places=9)

    def test_cache_hit_falls_back_to_input_when_absent(self):
        cfg = _cfg(mimo_prices={"mimo-v2.6-pro": {"input": 1.0, "output": 2.0}})
        out = _run([_exec("xiaomi/mimo-v2.6-pro", cache=1000)], cfg=cfg)
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertAlmostEqual(row["estimated_cost_usd"], 1.0 / 1e3,
                               places=9)

    def test_cache_hit_spelling_accepted(self):
        cfg = _cfg(mimo_prices={"mimo-v2.6-pro": {
            "input": 1.0, "cache_hit": 0.1, "output": 2.0}})
        out = _run([_exec("xiaomi/mimo-v2.6-pro", cache=1000)], cfg=cfg)
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertAlmostEqual(row["estimated_cost_usd"], 0.1 / 1e3,
                               places=9)

    def test_model_usage_constructor_accepts_mimo_prices(self):
        usage = ModelUsage(mimo_prices={"mimo-v2.6-pro": dict(self.OVERRIDE)})
        usage.add_execution(_exec("xiaomi/mimo-v2.6-pro", tokens_in=1000))
        row = usage.finish()["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertAlmostEqual(row["estimated_cost_usd"], 1.0 / 1e3,
                               places=9)


class UnknownModelTests(unittest.TestCase):
    """Unknown model: cost stays unknown — never zero, tokens still counted."""

    def test_unknown_model_is_never_zero(self):
        out = _run([_exec("acme/widget-9000", tokens_in=1000, cache=500,
                          out=200)])
        row = out["models"][label_for("acme/widget-9000")]
        self.assertFalse(row["cost_known"])
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertNotEqual(row["estimated_cost_usd"], 0)
        self.assertEqual(row["tokens_in"], 1000)
        self.assertEqual(row["cache_read_tokens"], 500)
        self.assertEqual(row["tokens_out"], 200)
        self.assertEqual(row["total_tokens"], 1700)
        self.assertIsNone(out["totals"]["estimated_cost_usd"])
        self.assertFalse(out["totals"]["cost_known"])
        self.assertIn("acme/widget-9000", out["unpriced_models"])

    def test_supplied_cost_is_not_a_proxy_for_unpriced_mimo_model(self):
        execution = _exec("mimo-v9-unknown", tokens_in=10)
        execution["estimated_cost_usd"] = 42.0
        execution["cost_known"] = True
        out = _run([execution])
        row = out["models"][label_for("mimo-v9-unknown")]
        self.assertFalse(row["cost_known"])
        self.assertIsNone(row["estimated_cost_usd"])

    def test_partial_price_row_stays_unknown(self):
        # A partial row is ignored (it falls through), so a model priced only
        # by a broken row stays unknown rather than becoming zero.
        cfg = _cfg(mimo_prices={"mimo-v9-custom": {"input": 1.0}})
        out = _run([_exec("mimo-v9-custom", tokens_in=1000)], cfg=cfg)
        row = out["models"][label_for("mimo-v9-custom")]
        self.assertFalse(row["cost_known"])
        self.assertIsNone(row["estimated_cost_usd"])


class ReasoningTokenTests(unittest.TestCase):
    """Reasoning tokens bill at the output rate unless a price says so."""

    def test_reasoning_counts_into_output_cost(self):
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=1000, out=200,
                          reasoning=300)])
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertEqual(row["reasoning_tokens"], 300)
        expected = (1000 * 0.435 + 200 * 0.87 + 300 * 0.87) / 1e6
        self.assertAlmostEqual(row["estimated_cost_usd"], expected, places=9)

    def test_explicit_reasoning_price_is_used(self):
        cfg = _cfg(mimo_prices={"mimo-v2.6-pro": {
            "input": 1.0, "cache_hit": 0.1, "output": 2.0, "reasoning": 0.5}})
        out = _run([_exec("xiaomi/mimo-v2.6-pro", out=100, reasoning=100)],
                   cfg=cfg)
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        expected = (100 * 2.0 + 100 * 0.5) / 1e6
        self.assertAlmostEqual(row["estimated_cost_usd"], expected, places=9)

    def test_reasoning_flows_into_by_vendor(self):
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=10, out=20,
                          reasoning=30)])
        bucket = out["by_vendor"]["Xiaomi (MiMo)"]
        self.assertEqual(bucket["reasoning_tokens"], 30)
        self.assertEqual(bucket["output_tokens"], 20)
        self.assertEqual(bucket["total_tokens"], 60)


class ByVendorRollupTests(unittest.TestCase):
    """by_vendor rollup in the ledger/usage output."""

    def test_by_vendor_present_and_correct(self):
        out = _run([
            _exec("xiaomi/mimo-v2.6-pro", tokens_in=1000, cache=500, out=200),
            _exec("xiaomi/mimo-v2.6-flash", tokens_in=10, cache=5, out=2),
        ])
        by_vendor = out["by_vendor"]
        self.assertIn("Xiaomi (MiMo)", by_vendor)
        bucket = by_vendor["Xiaomi (MiMo)"]
        self.assertEqual(bucket["input_tokens"], 1010)
        self.assertEqual(bucket["cache_read_tokens"], 505)
        self.assertEqual(bucket["output_tokens"], 202)
        self.assertEqual(bucket["reasoning_tokens"], 0)
        self.assertEqual(bucket["total_tokens"], 1717)
        self.assertTrue(bucket["cost_known"])
        expected = round(((1000 * 0.435 + 500 * 0.0036 + 200 * 0.87)
                          + (10 * 0.14 + 5 * 0.0036 + 2 * 0.28)) / 1e6, 6)
        self.assertAlmostEqual(bucket["cost"], expected, places=9)
        self.assertEqual(sorted(bucket["models"]),
                         ["xiaomi/mimo-v2.6-flash", "xiaomi/mimo-v2.6-pro"])

    def test_by_vendor_separates_vendors_and_unknown_price(self):
        out = _run([
            _exec("xiaomi/mimo-v2.6-pro", tokens_in=100, out=50),
            _exec("acme/widget-9000", tokens_in=7, out=3),
        ])
        by_vendor = out["by_vendor"]
        self.assertIn("Xiaomi (MiMo)", by_vendor)
        self.assertIn("unknown", by_vendor)
        mimo = by_vendor["Xiaomi (MiMo)"]
        self.assertTrue(mimo["cost_known"])
        self.assertAlmostEqual(mimo["cost"], (100 * 0.435 + 50 * 0.87) / 1e6,
                               places=9)
        unknown = by_vendor["unknown"]
        self.assertFalse(unknown["cost_known"])
        self.assertIsNone(unknown["cost"])
        self.assertEqual(unknown["input_tokens"], 7)

    def test_by_vendor_of_helpers(self):
        out = _run([_exec("xiaomi/mimo-v2.6-flash", tokens_in=5)])
        self.assertEqual(by_vendor_of(out), out["by_vendor"])
        self.assertEqual(by_vendor_of(None), {})
        self.assertEqual(by_vendor_of("junk"), {})

    def test_mimo_row_attribution(self):
        out = _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=1)])
        row = out["models"][label_for("xiaomi/mimo-v2.6-pro")]
        self.assertEqual(row["provider"], "mimo")
        self.assertEqual(row["vendor"], "Xiaomi (MiMo)")


class MixedVendorLedgerTests(unittest.TestCase):
    """One mimo execution + one deepseek-like execution with unknown price."""

    def test_mixed_ledger_sums_correctly(self):
        out = _run([
            _exec("xiaomi/mimo-v2.6-pro", tokens_in=1000, cache=500, out=200),
            _exec("deepseek-official/deepseek-mystery", tokens_in=100,
                  cache=50, out=25, total=175),
        ])
        by_vendor = out["by_vendor"]
        self.assertIn("Xiaomi (MiMo)", by_vendor)
        self.assertIn("DeepSeek", by_vendor)
        mimo = by_vendor["Xiaomi (MiMo)"]
        deepseek = by_vendor["DeepSeek"]
        self.assertTrue(mimo["cost_known"])
        self.assertFalse(deepseek["cost_known"])
        self.assertIsNone(deepseek["cost"])
        self.assertEqual(mimo["total_tokens"] + deepseek["total_tokens"],
                         1700 + 175)
        self.assertEqual(mimo["input_tokens"], 1000)
        self.assertEqual(deepseek["input_tokens"], 100)
        # Any unpriced row makes the ledger total unknown — never a split.
        self.assertIsNone(out["totals"]["estimated_cost_usd"])
        self.assertFalse(out["totals"]["cost_known"])
        self.assertEqual(mimo["cost"],
                         out["models"][label_for(
                             "xiaomi/mimo-v2.6-pro")]["estimated_cost_usd"])

    def test_priced_deepseek_rows_keep_working(self):
        out = _run([_exec("deepseek-flash", tokens_in=1000000, out=0,
                          total=1000000, cost_source="session_logs")])
        row = out["models"][label_for("deepseek-flash")]
        self.assertTrue(row["cost_known"])
        self.assertAlmostEqual(row["estimated_cost_usd"], 0.15, places=9)
        self.assertAlmostEqual(out["by_vendor"]["DeepSeek"]["cost"], 0.15,
                               places=9)


class PayloadSurfaceTests(unittest.TestCase):
    """spend/waste/status payloads include by_vendor, old keys unchanged."""

    def _usage(self):
        return _run([_exec("xiaomi/mimo-v2.6-pro", tokens_in=100, out=50)])

    def test_spend_payload_includes_by_vendor(self):
        import os
        import tempfile
        usage = self._usage()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(daily_alert_usd=None, daily_limit_usd=None,
                       state_dir=os.path.join(tmp, "state"),
                       cost_reservation_ttl_seconds=900)
            state = spend.budget_state(cfg, model_usage=usage)
            self.assertEqual(state["by_vendor"], usage["by_vendor"])
            for key in ("spent_usd", "reserved_usd", "reserved_cost_known",
                        "effective_usd", "alert_usd", "limit_usd",
                        "alert_crossed", "limit_reached", "warning"):
                self.assertIn(key, state)
            plain = spend.budget_state(cfg)
            self.assertEqual(plain["by_vendor"], {})
            for key in ("spent_usd", "warning", "limit_reached"):
                self.assertIn(key, plain)

    def test_waste_payload_includes_by_vendor(self):
        usage = self._usage()
        parsed = waste_report.parse_waste(
            "Agents launched: 6\nUsed: 5\n", model_usage=usage)
        self.assertEqual(parsed["by_vendor"], usage["by_vendor"])
        self.assertEqual(parsed["agents_launched"], 6)
        self.assertEqual(parsed["agents_used"], 5)
        self.assertEqual(parsed["agents_discarded"], 1)
        self.assertAlmostEqual(parsed["waste_ratio"], 1.0 / 6.0)
        self.assertEqual(parsed["source"], "derived")
        self.assertEqual(parsed["present"], False)
        plain = waste_report.parse_waste("Agents launched: 6\n")
        self.assertEqual(plain["by_vendor"], {})

    def test_status_payload_includes_by_vendor(self):
        usage = self._usage()
        payload = status.usage_status(usage)
        self.assertEqual(payload["by_vendor"], usage["by_vendor"])
        self.assertEqual(payload["models"], usage["models"])
        self.assertEqual(payload["totals"], usage["totals"])
        self.assertIn("unpriced_models", payload)
        plain = status.usage_status(None)
        self.assertEqual(plain["by_vendor"], {})
        self.assertEqual(plain["models"], {})

    def test_run_record_shape_is_backward_compatible(self):
        # Non-MiMo paths keep every pre-existing key; by_vendor is additive.
        out = _run([_exec("deepseek-flash", tokens_in=10, total=10,
                          cost_source="session_logs")])
        for key in ("version", "max_models", "models", "__other__",
                    "unpriced_models", "unattributed_scores", "totals",
                    "by_vendor"):
            self.assertIn(key, out)
        row = out["models"][label_for("deepseek-flash")]
        for key in ("provider", "model", "vendor", "roles", "executions",
                    "tokens_in", "cache_read_tokens", "tokens_out",
                    "reasoning_tokens", "total_tokens", "estimated_cost_usd",
                    "cost_known", "cost_source", "scores", "wins", "losses"):
            self.assertIn(key, row)
        self.assertEqual(row["provider"], "deepseek-official")
        self.assertEqual(row["vendor"], "deepseek-official")
        self.assertIn("models", out["by_vendor"]["DeepSeek"])


if __name__ == "__main__":
    unittest.main()
