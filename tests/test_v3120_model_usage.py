"""Focused 3.12.0 S2 tests: model usage, history hints, failure notes v2."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from deepseek_mcp import failure_notes, quality
from deepseek_mcp.model_usage import ModelUsage, build_model_usage


def _cfg(tmp=None, **overrides):
    values = {
        "failure_notes": "on", "failure_notes_models": "on",
        "failure_notes_max": 3, "failure_notes_half_life_days": 30,
        "failure_notes_min_occurrences": 1, "failure_notes_max_store": 200,
        "failure_notes_max_models": 6, "failure_notes_model_min_runs": 2,
        "failure_notes_store": "",
        "model_prices": {}, "reviewer_prices": {},
    }
    if tmp:
        values["failure_notes_store"] = os.path.join(tmp, "notes.json")
    values.update(overrides)
    return SimpleNamespace(**values)


class ModelUsageTests(unittest.TestCase):
    def test_priced_and_unpriced_rows_and_totals(self):
        execs = [
            {"role": "builder", "models": ["deepseek-flash"],
             "tokens_in": 1000000, "cache_read_tokens": 0,
             "tokens_out": 0, "total_tokens": 1000000},
            {"role": "builder", "provider": "ac" "me", "models": ["ac" "me/widget"],
             "tokens_in": 1000, "cache_read_tokens": 0, "tokens_out": 0,
             "total_tokens": 1000},
        ]
        result = build_model_usage(execs, cfg=_cfg(model_usage_max_models=1))
        self.assertEqual(result["version"], 1)
        priced = "deepseek-official/deepseek-flash"
        self.assertIn(priced, result["models"])
        self.assertAlmostEqual(result["models"][priced]["estimated_cost_usd"],
                               0.15, places=6)
        self.assertEqual(result["models"][priced]["cost_known"], True)
        # Unpriced row was folded into __other__, which must stay null.
        self.assertEqual(result["__other__"]["models_folded"], 1)
        self.assertIsNone(result["__other__"]["estimated_cost_usd"])
        self.assertFalse(result["__other__"]["cost_known"])
        self.assertIsNone(result["totals"]["estimated_cost_usd"])
        self.assertFalse(result["totals"]["cost_known"])
        self.assertIn("ac" "me/widget", result["unpriced_models"])

    def test_reviewer_prices_not_applied_to_builder(self):
        cfg = _cfg()
        cfg.model_prices = {"deepseek-flash": {"input": 1.0, "cache_read": 0.0,
                                              "output": 0.0}}
        cfg.reviewer_prices = {"deepseek-flash": {"input": 1000.0,
                                                 "cache_read": 0.0,
                                                 "output": 0.0}}
        builder = build_model_usage([{
            "role": "builder", "models": ["deepseek-flash"],
            "tokens_in": 1000000, "total_tokens": 1000000,
        }], cfg=cfg)
        reviewer = build_model_usage([{
            "role": "reviewer", "models": ["deepseek-flash"],
            "tokens_in": 1000000, "total_tokens": 1000000,
        }], cfg=cfg)
        label = "deepseek-official/deepseek-flash"
        self.assertAlmostEqual(
            builder["models"][label]["estimated_cost_usd"], 1.0)
        self.assertAlmostEqual(
            reviewer["models"][label]["estimated_cost_usd"], 1000.0)

    def test_score_attribution_and_duplicate_finish(self):
        usage = ModelUsage(prices={"deepseek-flash": {
            "input": 0.15, "cache_read": 0.003, "output": 0.6}})
        usage.add_execution({
            "role": "reviewer", "models": ["deepseek-flash"],
            "score": 8.5, "tokens_in": 10, "total_tokens": 10})
        usage.add_execution({
            "role": "builder", "models": ["deepseek-flash", "other/x"],
            "score": 9.0, "tokens_in": 20, "total_tokens": 20})
        first = usage.finish()
        second = usage.finish()
        self.assertEqual(first, second)
        label = "deepseek-official/deepseek-flash"
        self.assertEqual(first["models"][label]["scores"]["count"], 1)
        self.assertEqual(first["models"][label]["scores"]["source"],
                         "reviewer")
        self.assertEqual(first["unattributed_scores"], 1)
        usage.add_score(label, 9.5, source="candidate")
        self.assertEqual(usage.finish()["models"][label]["scores"]["source"],
                         "candidate")


class HistoryModelTests(unittest.TestCase):
    def _run(self, model, score=9.0, cost=0.10, subagents=4, status="ok",
             **extra):
        run = {
            "kind": "harness", "task_type": "code", "subagents": subagents,
            "status": status, "quality_score": score,
            "estimated_cost_usd": cost,
            "model_usage": {
                "models": {model: {"roles": ["builder"], "scores": None}},
                "unpriced_models": [], "totals": {
                    "estimated_cost_usd": cost, "cost_known": True}},
        }
        run.update(extra)
        return run

    def test_mixed_runs_and_slice_scores(self):
        mixed = self._run("a/one", 9.0, 0.1)
        mixed["builder_models"] = ["a/one", "b/two"]
        mixed["slice_scores"] = [
            {"model": "a/one", "score": 8.5, "verified": True},
            {"model": "b/two", "score": 9.0, "verified": True}]
        stats = quality.history_stats([self._run("a/one"), mixed], "code")
        self.assertEqual(stats["mixed_model_runs"], 1)
        self.assertEqual(len(stats["models"]), 1)
        self.assertEqual(stats["models"][0]["runs"], 1)
        self.assertEqual(stats["slice_scores"][0]["model"], "a/one")

    def test_hint_does_not_change_sizing(self):
        prompt = "Build a Python CLI tool and tests"
        files = ["app.py", "tests/test_app.py"]
        base = quality.choose_agents(prompt, files=files)
        runs = [self._run("a/one", 9.0, 0.10, subagents=base["subagents"])
                for _ in range(2)]
        choice = quality.choose_agents(prompt, files=files, runs=runs)
        self.assertEqual(choice["subagents"], base["subagents"])
        self.assertEqual(choice["waves"], base["waves"])
        self.assertEqual(choice["concurrency"], base["concurrency"])
        self.assertEqual(choice["model_hint"]["model"], "a/one")
        self.assertIn("Model hint:", choice["agent_choice_reason"])


class FailureNotesV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.path = self.cfg.failure_notes_store

    def tearDown(self):
        self.tmp.cleanup()

    def _write_v1(self):
        v1 = {"version": 1, "task_types": {}, "seen": {}, "updated": ""}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(v1, handle)

    def test_v1_reads_as_v2_without_rewrite_and_record_idempotent(self):
        self._write_v1()
        loaded = failure_notes.load(self.cfg)
        self.assertEqual(loaded["version"], 2)
        self.assertEqual(loaded["model_stats"], {})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["version"], 1)
        first = failure_notes.record_model_run(
            "code", "a/one", 9.0, 9.0, 0.2, True, "run-1", self.cfg)
        second = failure_notes.record_model_run(
            "code", "a/one", 9.0, 9.0, 0.2, True, "run-1", self.cfg)
        self.assertTrue(first["written"])
        self.assertTrue(second.get("duplicate"))
        loaded = failure_notes.load(self.cfg)
        self.assertEqual(loaded["model_stats"]["code"]["a/one"]["runs"], 1)

    def test_caps_and_note_limit(self):
        for index in range(10):
            failure_notes.record_model_run(
                "code", "provider/model-%d" % index, 9.0, 9.0, 0.1, True,
                "model-run-%d" % index, self.cfg)
        loaded = failure_notes.load(self.cfg)
        self.assertLessEqual(len(loaded["model_stats"]["code"]), 6)
        failure_notes.record("code", ["overlap"], "defect-run", 1, self.cfg)
        for index in range(2):
            failure_notes.record_model_run(
                "code", "a/one", 9.0, 9.0, 0.2, True,
                "good-run-%d" % index, self.cfg)
        note = failure_notes.note_for("code", self.cfg)
        self.assertLessEqual(len(note), 400)
        self.assertIn("Model:", note)
        meta = failure_notes.note_meta("code", self.cfg)
        self.assertLessEqual(len(meta["models"]), 6)

    def test_unsafe_model_labels_rejected(self):
        for label in ("https://example.com/x", "/tmp/x/model", "a@b"):
            result = failure_notes.record_model_run(
                "code", label, 9.0, 9.0, 0.1, True, "bad", self.cfg)
            self.assertFalse(result["written"])
        self.assertEqual(failure_notes.load(self.cfg)["model_stats"], {})


if __name__ == "__main__":
    unittest.main()
