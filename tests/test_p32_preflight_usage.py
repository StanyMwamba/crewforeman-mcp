"""P3.2 pre-flight and reviewer token-usage visibility."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import harness, jobs, store
from deepseek_mcp.config import load_config
import tests
from tests.fakes import base_env, write_fake_dsh, write_fake_script

MODEL = "kimi-k3.1-pro"


def _root(case):
    tmp = tempfile.TemporaryDirectory(prefix="p32-", dir=tests.tempdir())
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name).resolve()


def _wrapper(root, fake, preflight="ok"):
    """Fake dsh that answers the pre-flight itself and delegates the rest."""
    path = root / "dsh-wrapper"
    body = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "task = sys.argv[-1] if len(sys.argv) else ''\n"
        "if 'Reply with exactly READY' in task:\n"
        "    if os.environ.get('WRAP_PREFLIGHT') == 'fail':\n"
        "        sys.stderr.write('Kimi (Moonshot) quota is exhausted: "
        "Insufficient Balance; HTTP 402\\n')\n"
        "        sys.exit(1)\n"
        "    print(json.dumps({'type': 'assistant/message', 'data': "
        "{'message': {'id': 'pf-1', 'role': 'assistant', 'usage': "
        "{'inputTokens': 10, 'cacheReadTokens': 0, 'outputTokens': 1}}}}), "
        "flush=True)\n"
        "    print(json.dumps({'type': 'final', 'text': 'READY'}), flush=True)\n"
        "    sys.exit(0)\n"
        "real = os.environ['WRAP_REAL_DSH']\n"
        "os.execv(real, [real] + sys.argv[1:])\n")
    return write_fake_script(path, body)


class PreflightTests(unittest.TestCase):
    def _run(self, env_extra):
        root = _root(self)
        ws = root / "ws"
        ws.mkdir()
        fake = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=_wrapper(root, fake))
        env.update({"WRAP_REAL_DSH": str(fake),
                    "DSH_QUALITY_CONVERGENCE": "observe",
                    "FAKE_DSH_REVIEW_SCORE": "9.0"})
        env.update(env_extra)
        args = {"prompt": "Build a tiny note.", "workspace": str(ws),
                "sandbox": "danger", "quality": True,
                "independent_review": True, "quality_rounds": 1,
                "subagents": 1, "max_seconds": 30, "job_max_seconds": 120}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            _text, run, _path = harness.run_harness(args, cfg)
        return run, cfg, root

    def test_preflight_failure_blocks_paid_review_with_kimi_message(self):
        run, _cfg, _root_dir = self._run({
            "DSH_REVIEW_PREFLIGHT": "1", "WRAP_PREFLIGHT": "fail"})
        pre = run.get("reviewer_preflight") or {}
        self.assertEqual(pre.get("status"), "error")
        self.assertIn("platform.moonshot.cn", run.get("review_error") or "")
        self.assertIn("moonshot", (pre.get("vendor") or "").lower())
        # 3.12.0: no cross-vendor reviewer passed, so the job pauses with ONE
        # validated question instead of starting a paid review or failing open.
        self.assertEqual(run.get("review_status"), "paused")
        self.assertEqual(run.get("status"), "needs_input")
        self.assertIsNotNone(run.get("reviewer_attention"))
        self.assertFalse([item for item in run.get("review_rounds") or []
                          if item.get("kind") == "review"])
        self.assertFalse((_root_dir / "ws" / "REVIEW.md").exists(),
                         "the paid review was started despite a failed pre-flight")

    def test_preflight_success_allows_review(self):
        run, _cfg, _root_dir = self._run({
            "DSH_REVIEW_PREFLIGHT": "1", "WRAP_PREFLIGHT": "ok"})
        pre = run.get("reviewer_preflight") or {}
        self.assertEqual(pre.get("status"), "ok")
        self.assertTrue(pre.get("model"))
        reviews = [item for item in run.get("review_rounds") or []
                   if item.get("kind") == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(run.get("review_status"), "accepted")

    def test_success_records_preflight_usage_in_round_and_aggregate(self):
        run, _cfg, _root_dir = self._run({
            "DSH_REVIEW_PREFLIGHT": "1", "WRAP_PREFLIGHT": "ok"})
        reviews = [item for item in run.get("review_rounds") or []
                   if item.get("kind") == "review"]
        self.assertEqual(len(reviews), 1)
        entry = reviews[0]
        self.assertIn("preflight_usage", entry)
        self.assertIn("preflight", entry)
        self.assertIsNotNone(entry["preflight_usage"])
        self.assertGreaterEqual(
            int(entry["tokens_in"]),
            int(entry["preflight_usage"]["tokens_in"]))
        usage = run.get("reviewer_usage") or {}
        self.assertGreaterEqual(int(usage.get("input") or 0), 0)
        markdown = store._md(run)
        self.assertIn("(incl. pre-flight)", markdown)

    def test_preflight_default_is_on_but_suite_env_opts_out(self):
        from deepseek_mcp.config import DEFAULTS
        self.assertIs(DEFAULTS.get("review_preflight"), True)
        self.assertIn(os.environ.get("DSH_REVIEW_PREFLIGHT", "0"), ("0", "1"))

    def test_preflight_can_be_disabled(self):
        run, _cfg, _root_dir = self._run({
            "DSH_REVIEW_PREFLIGHT": "0", "WRAP_PREFLIGHT": "fail"})
        self.assertNotIn("reviewer_preflight", run)
        self.assertEqual(run.get("review_status"), "accepted")


class UsageVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "id": "run-usage", "created": "2026-01-01T00:00:00",
            "prompt": "Build a tiny note.",
            "status": "ok", "kind": "harness", "model": MODEL,
            "independent_review": True, "review_status": "accepted",
            "quality_score": 9.0, "self_score": 8.0,
            "quality_rounds_used": 1, "tokens_in": 1000,
            "cache_read_tokens": 200, "tokens_out": 50,
            "review_rounds": [{
                "round": 1, "kind": "review", "seconds": 1.2, "score": 9.0,
                "blocking_defects": 0, "tokens_in": 1000,
                "cache_read_tokens": 200, "tokens_out": 50,
                "total_tokens": 1250, "cost": 0.00125, "cost_known": True,
                "usage": {"input_tokens": 1000, "cache_read_tokens": 200,
                          "output_tokens": 50, "total_tokens": 1250},
                "models": {MODEL: {"input": 1000, "cache_read": 200,
                                   "output": 50}},
                "status": "ok", "accepted": True,
            }],
        }

    def test_md_and_result_text_show_per_round_usage_and_cost(self):
        self.run["reviewer_usage"] = harness._reviewer_usage_aggregate(
            self.run["review_rounds"])
        md = store._md(self.run)
        text = harness._result_text(self.run, "/tmp/run-usage.md")
        for blob in (md, text):
            self.assertIn("input 1000 (uncached)", blob)
            self.assertIn("cache read 200", blob)
            self.assertIn("output 50", blob)
            self.assertIn("total 1250", blob)
            self.assertIn("$0.00125".rstrip("0"), blob)
        self.assertIn("Review/revision rounds:", text)

    def test_saved_run_files_include_per_round_usage(self):
        from deepseek_mcp.config import Config
        root = _root(self)
        cfg = Config({"log_dir": str(root / "runs"),
                      "jobs_dir": str(root / "jobs"),
                      "state_dir": str(root / "state")})
        run = dict(self.run, id="run-usage-saved",
                   reviewer_usage=harness._reviewer_usage_aggregate(
                       self.run["review_rounds"]))
        path = store.save_run(run, cfg, ledger=False)
        markdown = Path(path).read_text(encoding="utf-8")
        payload = json.loads(Path(path).with_suffix(".json").read_text(
            encoding="utf-8"))
        self.assertIn("input 1000 (uncached)", markdown)
        self.assertIn("cache read 200", markdown)
        self.assertIn("output 50", markdown)
        self.assertIn("$0.00125".rstrip("0"), markdown)
        entry = payload["review_rounds"][0]
        self.assertEqual(entry["tokens_in"], 1000)
        self.assertEqual(entry["cache_read_tokens"], 200)
        self.assertEqual(entry["tokens_out"], 50)
        self.assertEqual(entry["total_tokens"], 1250)
        self.assertEqual(entry["cost"], 0.00125)

    def test_unknown_cost_reason_is_explicit(self):
        entry = dict(self.run["review_rounds"][0], cost=None,
                     cost_known=False)
        run = dict(self.run, review_rounds=[entry])
        md = store._md(run)
        text = harness._result_text(run, "/tmp/run-usage.md")
        self.assertIn("cost unknown (no configured model price)", md)
        self.assertIn("cost unknown (no configured model price)", text)

    def test_run_json_keeps_usage_fields(self):
        blob = json.loads(json.dumps(self.run))
        entry = blob["review_rounds"][0]
        self.assertEqual(entry["tokens_in"], 1000)
        self.assertEqual(entry["cache_read_tokens"], 200)
        self.assertEqual(entry["tokens_out"], 50)
        self.assertEqual(entry["total_tokens"], 1250)
        self.assertTrue(entry["cost_known"])

    def test_structured_job_status_exposes_reviewer_usage(self):
        run = dict(self.run)
        run["reviewer_usage"] = harness._reviewer_usage_aggregate(
            run["review_rounds"])
        root = _root(self)
        cfg = SimpleNamespace(jobs_dir=str(root), state_dir=str(root / "state"))
        blank = {"stage": {}, "attention": [], "question": None,
                 "options": [], "next_action": None, "terminal": False,
                 "result": None}
        with mock.patch.object(store, "find_run", return_value=(run, None)), \
                mock.patch.object(jobs, "_admission_status_fields",
                                  return_value={}), \
                mock.patch("deepseek_mcp.events.project",
                           return_value=blank):
            payload = jobs._structured_status(
                {"events_since": 0}, {"run_id": "run-usage"}, "run-usage", cfg)
        usage = payload["reviewer_usage"]
        self.assertEqual(usage.get("input", usage.get("input_tokens")), 1000)
        self.assertEqual(usage.get("output", usage.get("output_tokens")), 50)
        self.assertEqual(payload["review_rounds"][0]["tokens_in"], 1000)
        self.assertEqual(payload["review_rounds"][0]["cost"], 0.00125)

    def test_preflight_usage_is_folded_into_round_usage(self):
        from deepseek_mcp.config import Config
        cfg = Config({"reviewer_prices": {
            MODEL: {"input": 0.5, "cache_read": 0.05, "output": 1.5}}})
        entry = {
            "round": 1, "kind": "review",
            "tokens_in": 1000, "cache_read_tokens": 200, "tokens_out": 50,
            "total_tokens": 1250, "cost": 0.00125, "cost_known": True,
            "models": {MODEL: {"input": 1000, "cache_read": 200,
                               "output": 50}},
            "usage": {"input_tokens": 1000, "cache_read_tokens": 200,
                      "output_tokens": 50, "total_tokens": 1250},
        }
        preflight = {
            "tokens_in": 100, "cache_read_tokens": 10, "tokens_out": 5,
            "total_tokens": 115, "cost": 0.00008, "cost_known": True,
            "models": {MODEL: {"input": 100, "cache_read": 10,
                               "output": 5}},
            "usage": {"input_tokens": 100, "cache_read_tokens": 10,
                      "output_tokens": 5, "total_tokens": 115},
        }
        merged = harness._merge_preflight_usage(entry, preflight, cfg)
        self.assertEqual(merged["tokens_in"], 1100)
        self.assertEqual(merged["cache_read_tokens"], 210)
        self.assertEqual(merged["tokens_out"], 55)
        self.assertEqual(merged["total_tokens"], 1365)
        self.assertAlmostEqual(merged["cost"], 0.00133, places=9)
        aggregate = harness._reviewer_usage_aggregate([merged])
        self.assertEqual(aggregate["input"], 1100)
        self.assertEqual(aggregate["cached"], 210)
        self.assertEqual(aggregate["output"], 55)
        self.assertAlmostEqual(aggregate["estimated_cost_usd"], 0.00133,
                               places=9)
        repriced = harness._review_round_usage(
            dict(merged, cost=None, cost_known=False), cfg)
        expected = (1100 * 0.5 + 210 * 0.05 + 55 * 1.5) / 1000000.0
        self.assertTrue(repriced["cost_known"])
        self.assertAlmostEqual(repriced["cost"], expected, places=9)

    def test_configured_reviewer_prices_compute_cost(self):
        record = {"tokens_in": 1000, "cache_read_tokens": 200,
                  "tokens_out": 50, "models": {MODEL: {"input": 1000,
                  "cache_read": 200, "output": 50}}}
        priced = harness._review_round_usage(
            record, SimpleNamespace(dsh_model=MODEL, reviewer_prices={
                MODEL: {"input": 0.5, "cache_read": 0.05, "output": 1.5}}))
        expected = (1000 * 0.5 + 200 * 0.05 + 50 * 1.5) / 1000000.0
        self.assertTrue(priced["cost_known"])
        self.assertAlmostEqual(priced["cost"], expected, places=9)
        unknown = harness._review_round_usage(
            record, SimpleNamespace(dsh_model=MODEL, reviewer_prices={}))
        if MODEL in __import__("deepseek_mcp.api", fromlist=["x"]).HARNESS_PRICES:
            self.skipTest("builtin price exists for %s" % MODEL)
        self.assertIsNone(unknown["cost"])
        self.assertFalse(unknown["cost_known"])


if __name__ == "__main__":
    unittest.main()
