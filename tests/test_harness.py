import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import concurrency, harness, ledger
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh, write_session_log


class SessionParsingTests(unittest.TestCase):
    def test_parse_session_usage_deduplicates_messages_and_prices(self):
        lines = [
            {"type": "session", "cwd": "/tmp/work"},
            {"type": "assistant/message", "seq": 1,
             "data": {"message": {"id": "m1", "role": "assistant",
                                  "source": {"model": "deepseek-flash"}},
                      "usage": {"inputTokens": 1000, "cacheReadTokens": 2000, "outputTokens": 500}}},
            # Duplicate copy of the same message must not be billed twice.
            {"type": "assistant/message", "seq": 1,
             "data": {"message": {"id": "m1", "role": "assistant",
                                  "source": {"model": "deepseek-flash"}},
                      "usage": {"inputTokens": 1000, "cacheReadTokens": 2000, "outputTokens": 500}}},
            {"type": "assistant/message", "seq": 2,
             "data": {"message": {"id": "m2", "role": "assistant",
                                  "source": {"model": "deepseek-v4-pro"}},
                      "usage": {"inputTokens": 100, "cacheReadTokens": 50, "outputTokens": 10}}},
        ]
        text = "\n".join(json.dumps(line) for line in lines)
        usage = harness.parse_session_usage(text)
        self.assertEqual(usage["input_tokens"], 1100)
        self.assertEqual(usage["cache_read_tokens"], 2050)
        self.assertEqual(usage["output_tokens"], 510)
        self.assertEqual(usage["messages"], 2)
        expected = (1000 * 0.15 + 2000 * 0.003 + 500 * 0.60) / 1e6
        expected += (100 * 0.66 + 50 * 0.022 + 10 * 1.98) / 1e6
        self.assertAlmostEqual(usage["estimated_cost_usd"], round(expected, 6), places=6)
        self.assertTrue(usage["cost_known"])

    def test_parse_usage_unknown_model_reports_unknown_cost(self):
        line = {"type": "assistant/message", "seq": 1,
                "data": {"message": {"id": "m1", "source": {"model": "mystery-model"}},
                         "usage": {"inputTokens": 10, "outputTokens": 2}}}
        usage = harness.parse_session_usage(json.dumps(line))
        self.assertFalse(usage["cost_known"])
        self.assertIsNone(usage["estimated_cost_usd"])

    def test_parse_events(self):
        lines = [
            json.dumps({"type": "tool_call", "name": "bash"}),
            {"type": "tool/call", "data": {"name": "read"}},
            json.dumps({"type": "assistant/message",
                        "data": {"message": {"role": "assistant",
                                             "content": [{"type": "text", "text": "partial"}]}}}),
            json.dumps({"type": "final", "text": "final answer"}),
            json.dumps({"type": "error", "message": "minor warning"}),
        ]
        parsed = harness.parse_events(lines)
        self.assertEqual(parsed["tools"], {"bash": 1, "read": 1})
        self.assertEqual(parsed["final"], "final answer")
        self.assertEqual(parsed["errors"], ["minor warning"])
        self.assertEqual(parsed["events"], 5)

    def test_decompress_without_zstd_returns_none(self):
        self.assertIsNone(harness.decompress_log(Path("missing.zstd"), type("C", (), {"zstd_bin": "definitely-not-zstd"})()))


class HarnessRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.sessions = self.root / "sessions"
        self.env = base_env(self.root, fake_dsh=self.dsh, sessions=self.sessions)

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def test_run_harness_records_files_events_and_session_cost(self):
        write_session_log(self.sessions, self.workspace)
        text, run, path = harness.run_harness({
            "prompt": "Do a small task", "subagents": 0, "quality": False,
            "workspace": str(self.workspace),
        }, self.cfg())
        self.assertIn("DeepSeek Harness run", text)
        self.assertIn("deliverable.md", text)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["subagents"], 0)
        self.assertEqual(run["tools_used"], {"bash": 1, "read": 1})
        self.assertEqual(run["tokens_in"], 1000)
        self.assertEqual(run["cache_read_tokens"], 2000)
        self.assertEqual(run["tokens_out"], 500)
        self.assertAlmostEqual(run["estimated_cost_usd"], 0.000456, places=6)
        self.assertEqual(run["cost_source"], "session_logs")
        self.assertIn("tokens: in 1000 uncached + cache 2000 / out 500", text)
        self.assertTrue(Path(path).is_file())
        self.assertIn("deliverable.md", run["output_files"])

    def test_quality_loop_and_score_are_recorded(self):
        write_session_log(self.sessions, self.workspace)
        with mock.patch.dict(os.environ, {"FAKE_DSH_SCORE": "9.1",
                                          "FAKE_DSH_REVIEW_SCORES": "9.1"}, clear=False):
            text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with a QR code and five sections",
                "subagents": 2, "quality": True, "quality_rounds": 2,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertTrue(run["quality_used"])
        self.assertTrue(run["independent_review"])
        self.assertEqual(run["quality_rounds"], 2)
        self.assertEqual(run["quality_rounds_used"], 1)
        self.assertEqual(run["quality_score"], 9.1)
        self.assertEqual(run["self_score"], 9.1)
        self.assertEqual(run["review_status"], "accepted")
        self.assertIn("at most 8 children", run["harness_task"])
        self.assertIn("SUB-LEADER", run["harness_task"])
        self.assertIn("read_image", run["harness_task"])
        self.assertIn("QUALITY_REPORT.md", run["harness_task"])
        self.assertIn("quality 9.10/10", text)
        self.assertIn("independent", text)
        self.assertTrue((self.workspace / "QUALITY_REPORT.md").is_file())
        self.assertFalse((self.root / "skills" / "poster.md").exists())

    def test_quality_defects_ignore_passed_checklist_items(self):
        (self.workspace / "QUALITY_REPORT.md").write_text(
            "# Report\n- [x] No overflow, clipping or overlap\n"
            "- Defect: headline overlaps the footer\n",
            encoding="utf-8")
        defects = harness._quality_defects(self.workspace)
        self.assertIn("headline overlaps", defects)
        self.assertNotIn("No overflow", defects)

    def test_zero_quality_rounds_is_clamped_consistently(self):
        write_session_log(self.sessions, self.workspace)
        with mock.patch.dict(os.environ, {"FAKE_DSH_SCORE": "8.6",
                                          "FAKE_DSH_REVIEW_SCORES": "8.6"}, clear=False):
            text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with five sections",
                "subagents": 2, "quality": True, "quality_rounds": 0,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertEqual(run["quality_rounds"], 1)
        self.assertEqual(run["quality_rounds_used"], 1)
        self.assertIn("1 revision round", run["harness_task"])
        self.assertIn("quality 8.60/10", text)

    def test_non_executable_dsh_is_saved_as_an_error(self):
        bad = self.root / "dsh-noexec"
        bad.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bad.chmod(0o644)
        cfg = self.cfg(DSH_BIN=str(bad))
        with self.assertRaises(RuntimeError) as caught:
            harness.run_harness({"prompt": "Do a small task", "subagents": 0,
                                 "quality": False, "workspace": str(self.workspace)}, cfg)
        self.assertIn("Run saved", str(caught.exception))
        run = json.loads(list((self.root / "runs").glob("*.json"))[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "error")
        self.assertIn("not executable", run["error"])

    def test_max_seconds_kills_a_silent_child(self):
        env = dict(self.env, FAKE_DSH_MODE="sleep", FAKE_DSH_SLEEP="3")
        with mock.patch.dict(os.environ, env, clear=False):
            started = time.time()
            with self.assertRaises(RuntimeError) as caught:
                harness.run_harness({"prompt": "Do a small task", "subagents": 0,
                                     "quality": False, "max_seconds": 1,
                                     "workspace": str(self.workspace)}, self.cfg())
            elapsed = time.time() - started
        self.assertLess(elapsed, 6)
        self.assertIn("Run saved", str(caught.exception))
        run = json.loads(list((self.root / "runs").glob("*.json"))[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "error")
        self.assertIn("time limit reached", run["error"])

    def test_stale_quality_report_is_not_reused(self):
        (self.workspace / "QUALITY_REPORT.md").write_text(
            "# Old\nFinal quality score: 9.9/10\n", encoding="utf-8")
        write_session_log(self.sessions, self.workspace)
        with mock.patch.dict(os.environ, {"FAKE_DSH_SCORE": "8.6"}, clear=False):
            _text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with five sections",
                "subagents": 2, "quality": True, "workspace": str(self.workspace),
            }, self.cfg())
        self.assertEqual(run["quality_score"], 8.6)
        self.assertTrue(list(self.workspace.glob("QUALITY_REPORT.previous-*.md")))

    def test_missing_final_event_is_an_error_but_run_is_saved(self):
        env = dict(self.env, FAKE_DSH_MODE="error")
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                harness.run_harness({"prompt": "Do a small task", "subagents": 0,
                                     "quality": False,
                                     "workspace": str(self.workspace)}, self.cfg())
        self.assertIn("Run saved", str(caught.exception))
        runs = list((self.root / "runs").glob("*.json"))
        self.assertEqual(len(runs), 1)
        run = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "error")
        self.assertIn("fake dsh failure", run["error"])

    def test_find_session_logs_falls_back_to_header_cwd(self):
        other = self.root / "other-workspace"
        other.mkdir()
        wrong = write_session_log(self.sessions, other,
                                  entries=[{"input": 1, "cache": 0, "output": 1}],
                                  filename="session-wrong.jsonl")
        target = write_session_log(self.sessions, other,
                                   entries=[{"input": 7, "cache": 0, "output": 3}],
                                   filename="session-target.jsonl")
        lines = target.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        header["cwd"] = str(self.workspace.resolve())
        lines[0] = json.dumps(header)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse((self.sessions / harness.encode_workspace(self.workspace)).exists())
        found = harness.find_session_logs(self.cfg(), self.workspace)
        self.assertEqual([Path(path) for path in found], [target])
        self.assertNotIn(wrong, found)

    @unittest.skipUnless(shutil.which("zstd"), "zstd binary not installed")
    def test_zstd_session_log_is_decompressed(self):
        log = write_session_log(self.sessions, self.workspace)
        compressed = log.with_suffix(".jsonl.zstd")
        subprocess.run(["zstd", "-q", "-f", str(log), "-o", str(compressed)], check=True)
        log.unlink()
        text = harness.decompress_log(compressed, self.cfg())
        self.assertIsNotNone(text)
        self.assertIn("assistant/message", text)
        usage, paths, reason = harness.read_session_usage(self.cfg(), self.workspace, 0)
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(len(paths), 1)
        self.assertIsNone(reason)

    def test_old_session_logs_are_not_billed(self):
        old = write_session_log(self.sessions, self.workspace, filename="old.v4.jsonl")
        old_time = time.time() - 3600
        os.utime(str(old), (old_time, old_time))
        write_session_log(self.sessions, self.workspace,
                          entries=[{"input": 5, "cache": 0, "output": 1}],
                          filename="new.v4.jsonl")
        usage, paths, _reason = harness.read_session_usage(
            self.cfg(), self.workspace, time.time())
        self.assertEqual(usage["input_tokens"], 5)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("new.v4.jsonl"))

    def test_multiple_session_logs_are_summed(self):
        write_session_log(self.sessions, self.workspace)
        write_session_log(self.sessions, self.workspace,
                          entries=[{"input": 10, "cache": 20, "output": 30}],
                          filename="second.v4.jsonl")
        usage, paths, _reason = harness.read_session_usage(self.cfg(), self.workspace, 0)
        self.assertEqual(usage["input_tokens"], 1010)
        self.assertEqual(usage["cache_read_tokens"], 2020)
        self.assertEqual(usage["output_tokens"], 530)
        self.assertEqual(len(paths), 2)

    def test_partial_unreadable_logs_make_cost_unknown(self):
        write_session_log(self.sessions, self.workspace)
        encoded = harness.encode_workspace(self.workspace)
        bad = self.sessions / encoded / "session-bad" / "session.v4.jsonl.zstd"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"corrupt")
        usage, _paths, reason = harness.read_session_usage(self.cfg(), self.workspace, 0)
        self.assertIsNotNone(usage)
        self.assertFalse(usage["cost_known"])
        self.assertIsNone(usage["estimated_cost_usd"])
        self.assertIn("could not be read", reason)

    def test_usage_without_message_id_or_seq_is_not_collapsed(self):
        events = [
            {"type": "assistant/message", "data": {"message": {"role": "assistant"},
                                                   "usage": {"inputTokens": 10, "outputTokens": 1}}},
            {"type": "assistant/message", "data": {"message": {"role": "assistant"},
                                                   "usage": {"inputTokens": 20, "outputTokens": 2}}},
        ]
        usage = harness.parse_session_usage("\n".join(json.dumps(event) for event in events))
        self.assertEqual(usage["messages"], 2)
        self.assertEqual(usage["input_tokens"], 30)

    def test_cost_unknown_when_zstd_missing(self):
        encoded = harness.encode_workspace(self.workspace)
        directory = self.sessions / encoded / "session-test"
        directory.mkdir(parents=True)
        (directory / "session.v4.jsonl.zstd").write_bytes(b"not really zstd")
        cfg = self.cfg(DSH_ZSTD_BIN="definitely-not-zstd")
        text, run, _path = harness.run_harness({
            "prompt": "Do a small task", "subagents": 0, "quality": False,
            "workspace": str(self.workspace),
        }, cfg)
        self.assertEqual(run["cost_source"], "unknown")
        self.assertIsNone(run["estimated_cost_usd"])
        self.assertIn("cost: unknown", text)

    def test_independent_review_revises_until_accepted(self):
        env = dict(os.environ, **{
            "FAKE_DSH_SCORE": "7.0",
            "FAKE_DSH_REVIEW_SCORES": "6.5,9.2",
            "FAKE_DSH_REVIEW_BLOCKING": "1,0",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with a QR code and five sections",
                "subagents": 2, "quality": True, "quality_rounds": 2,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertEqual(run["status"], "ok")
        self.assertTrue(run["independent_review"])
        self.assertEqual(run["quality_score"], 9.2)
        self.assertEqual(run["self_score"], 7.0)
        self.assertEqual(run["review_status"], "accepted")
        self.assertEqual(run["quality_rounds_used"], 2)
        kinds = [item["kind"] for item in run["review_rounds"]]
        self.assertEqual(kinds, ["review", "revision", "review"])
        self.assertEqual(run["review_rounds"][0]["blocking_defects"], 1)
        self.assertEqual(run["review_rounds"][2]["blocking_defects"], 0)
        self.assertNotIn("QUALITY_REPORT.md", run["deliverable_files"])
        self.assertNotIn("REVIEW.md", run["deliverable_files"])
        self.assertIn("Review 1", text)
        self.assertIn("Revision 1", text)
        self.assertIn("self_score 7.00/10", text)
        skill_text = (self.root / "skills" / "poster.md").read_text(encoding="utf-8")
        self.assertIn("independent review found", skill_text)
        self.assertIn("fixed", skill_text)

    def test_independent_review_stops_after_max_rounds(self):
        env = dict(os.environ, **{
            "FAKE_DSH_SCORE": "7.5",
            "FAKE_DSH_REVIEW_SCORES": "6.0,7.0",
            "FAKE_DSH_REVIEW_BLOCKING": "1,1",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with five sections",
                "subagents": 2, "quality": True, "quality_rounds": 2,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertEqual(run["review_status"], "stopped")
        self.assertEqual(run["quality_score"], 7.0)
        self.assertEqual(run["quality_rounds_used"], 2)
        self.assertEqual([item["kind"] for item in run["review_rounds"]],
                         ["review", "revision", "review"])
        self.assertIn("blocking defect", text)
        self.assertGreaterEqual(int(run["review_rounds"][0]["blocking_defects"]), 1)

    def test_independent_review_can_be_disabled(self):
        env = dict(os.environ, **{
            "FAKE_DSH_SCORE": "9.1",
            "FAKE_DSH_REVIEW_SCORES": "4.0",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with five sections",
                "subagents": 2, "quality": True, "independent_review": False,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertEqual(run["quality_score"], 9.1)
        self.assertEqual(run["self_score"], 9.1)
        self.assertEqual(run["review_status"], "disabled")
        self.assertEqual(run["review_rounds"], [])
        self.assertFalse((self.workspace / "REVIEW.md").exists())

    def test_reviewer_failure_keeps_the_builder_work_unreviewed(self):
        env = dict(os.environ, **{
            "FAKE_DSH_SCORE": "8.6",
            "FAKE_DSH_FAIL_REVIEW": "1",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            text, _run, _path = harness.run_harness({
                "prompt": "Design a print poster with five sections",
                "subagents": 2, "quality": True, "quality_rounds": 1,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertIn("Independent review failed", text)
        self.assertIn("not independently verified", text)
        run = json.loads(list((self.root / "runs").glob("*.json"))[0].read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "ok_unreviewed")
        self.assertIsNone(run["quality_score"])
        self.assertEqual(run["review_status"], "error")
        self.assertIn("fake reviewer failure", run["review_error"])

    def test_agent_limit_error_invalidates_the_concurrency_cache(self):
        cfg = self.cfg()
        concurrency.write_cache(cfg, 8, "test", "test-version")
        self.assertTrue(concurrency.cache_path(cfg).is_file())
        env = dict(os.environ, **{
            "FAKE_DSH_MODE": "error",
            "FAKE_DSH_ERROR_MESSAGE": "ACTIVATION_LIMIT_REACHED: active child limit: 8",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                harness.run_harness({
                    "prompt": "Do a small task", "subagents": 0, "quality": False,
                    "workspace": str(self.workspace),
                }, cfg)
        self.assertFalse(concurrency.cache_path(cfg).exists())

    def test_deliverable_filter_drops_quality_and_review_artifacts(self):
        files = harness._deliverable_files(self.workspace, [
            "QUALITY_REPORT.md", "REVIEW.md", "QUALITY_REPORT.previous-x.md",
            "REVIEW.previous-x-r1.md", "deliverable.md", ".fake-state.json",
        ])
        self.assertEqual(files, ["deliverable.md"])

    def test_combined_cost_is_unknown_if_any_round_is_unknown(self):
        total = harness._sum_executions([
            {"estimated_cost_usd": 0.1, "tokens_in": 10, "tokens_out": 1},
            {"estimated_cost_usd": None, "tokens_in": 5, "tokens_out": 1},
        ])
        self.assertIsNone(total["estimated_cost_usd"])
        self.assertFalse(total["cost_known"])
        self.assertEqual(total["tokens_in"], 15)

    def test_detected_concurrency_override_reaches_task_text(self):
        cfg = self.cfg(DSH_MAX_CONCURRENT_CHILDREN="4")
        _text, run, _path = harness.run_harness({
            "prompt": "Do a small task", "subagents": 2, "quality": False,
            "workspace": str(self.workspace),
        }, cfg)
        self.assertEqual(run["concurrency"], 4)
        self.assertEqual(run["concurrency_source"], "config")
        self.assertIn("at most 4 children alive at once", run["harness_task"])
        self.assertIn("waves of AT MOST 4 (2)", run["harness_task"])

    def test_harness_run_appends_the_spend_ledger(self):
        _text, run, _path = harness.run_harness({
            "prompt": "Do a small task", "subagents": 0, "quality": False,
            "workspace": str(self.workspace),
        }, self.cfg())
        entries = ledger.read_entries(self.cfg())
        self.assertIsNotNone(entries)
        self.assertIn(run["id"], [item["run_id"] for item in entries])

    def test_costs_are_summed_across_builder_reviewer_and_revision(self):
        env = dict(os.environ, **{
            "FAKE_DSH_SCORE": "7.0",
            "FAKE_DSH_REVIEW_SCORES": "6.5,9.2",
            "FAKE_DSH_REVIEW_BLOCKING": "1,0",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
            "FAKE_DSH_SESSIONS": str(self.sessions),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            _text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with a QR code and five sections",
                "subagents": 2, "quality": True, "quality_rounds": 2,
                "live_scaling": False,
                "workspace": str(self.workspace),
            }, self.cfg(DSH_MAX_CONCURRENT_CHILDREN="8"))
        self.assertEqual(len(run["review_rounds"]), 3)
        self.assertAlmostEqual(run["estimated_cost_usd"], 4 * 0.000456, places=6)
        for entry in run["review_rounds"]:
            self.assertAlmostEqual(entry["cost"], 0.000456, places=6)


if __name__ == "__main__":
    unittest.main()
