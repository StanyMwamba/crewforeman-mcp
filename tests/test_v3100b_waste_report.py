"""Tests for item 6c: WASTE_REPORT parsing and waste-aware sizing."""

import os
import tempfile
import unittest

from deepseek_mcp import quality, waste_report


CODE_PROMPT = (
    "Implement a Python 3.9 CLI package named forge that builds and tests a "
    "small HTTP API server. Add modules for parsing, storage, and routing, "
    "write unit tests for every function, fix the existing bug in the retry "
    "logic, refactor the config loader, and document the public API. "
    "Deliverables:\n"
    "- forge/__init__.py\n"
    "- forge/cli.py\n"
    "- forge/server.py\n"
    "- forge/store.py\n"
    "- forge/config.py\n"
    "- tests/test_forge.py\n"
    "- tests/test_server.py\n"
    "- tests/test_store.py\n"
    "Each module must expose a clear function-level interface, use only the "
    "standard library, validate inputs, return typed results, and include "
    "docstrings. The CLI must support subcommands for init, run, check, and "
    "export, with stable exit codes and helpful error messages. The server "
    "must handle concurrent requests, timeouts, and graceful shutdown. The "
    "storage layer must persist JSON atomically and recover from partial "
    "writes. The config loader must merge defaults, environment variables, "
    "and a config file, with the file winning. Unit tests must cover happy "
    "paths, error paths, edge cases, and concurrency. The repository must "
    "keep Python 3.9 compatibility, avoid third-party dependencies, and pass "
    "the existing test suite without regressions. Keep functions small, "
    "names explicit, and behavior deterministic so the tests can assert "
    "exact output. Add package metadata, changelog entries, and type hints, "
    "and make sure the whole test suite runs quickly and cleanly on every "
    "supported platform."
)

CODE_FILES = [
    "forge/__init__.py",
    "forge/cli.py",
    "forge/server.py",
    "forge/store.py",
    "forge/config.py",
    "tests/test_forge.py",
    "tests/test_server.py",
    "tests/test_store.py",
]


def _waste_run(ratio, subagents=8):
    return {
        "task_type": "code",
        "subagents": subagents,
        "status": "ok",
        "quality_score": 9.2,
        "estimated_cost_usd": 0.10,
        "seconds": 60,
        "waste_report": {
            "agents_launched": 6,
            "agents_used": 3,
            "agents_discarded": 3,
            "waste_ratio": ratio,
        },
    }


def _clean_run():
    return {
        "task_type": "code",
        "subagents": 8,
        "status": "ok",
        "quality_score": 9.2,
        "estimated_cost_usd": 0.10,
        "seconds": 60,
    }


class ParseWasteTests(unittest.TestCase):
    def test_explicit_labels_and_ratio(self):
        parsed = waste_report.parse_waste(
            "Agents launched: 6\nAgents used: 5\nDiscarded: 1\n"
            "Waste ratio: 0.5\n")
        self.assertEqual(parsed["agents_launched"], 6)
        self.assertEqual(parsed["agents_used"], 5)
        self.assertEqual(parsed["agents_discarded"], 1)
        self.assertAlmostEqual(parsed["waste_ratio"], 0.5)
        self.assertEqual(parsed["source"], "explicit")

    def test_derived_ratio_and_discarded(self):
        parsed = waste_report.parse_waste("Launched: 6\nUsed: 5\n")
        self.assertEqual(parsed["agents_launched"], 6)
        self.assertEqual(parsed["agents_used"], 5)
        self.assertEqual(parsed["agents_discarded"], 1)
        self.assertAlmostEqual(parsed["waste_ratio"], 1.0 / 6.0)
        self.assertEqual(parsed["source"], "derived")

    def test_compact_line(self):
        parsed = waste_report.parse_waste("6 launched / 5 used / 1 discarded\n")
        self.assertEqual(parsed["agents_launched"], 6)
        self.assertEqual(parsed["agents_used"], 5)
        self.assertEqual(parsed["agents_discarded"], 1)
        self.assertAlmostEqual(parsed["waste_ratio"], 1.0 / 6.0)
        self.assertEqual(parsed["source"], "derived")

    def test_explicit_ratio_is_clamped(self):
        high = waste_report.parse_waste(
            "Agents launched: 4\nWaste ratio: 1.7\n")
        self.assertEqual(high["waste_ratio"], 1.0)
        self.assertEqual(high["source"], "explicit")
        percent = waste_report.parse_waste(
            "Agents launched: 4\nWaste ratio: 50%\n")
        self.assertAlmostEqual(percent["waste_ratio"], 0.5)

    def test_missing_or_zero_launched_ratio_none(self):
        zero = waste_report.parse_waste(
            "Agents launched: 0\nAgents used: 0\nDiscarded: 0\n")
        self.assertIsNone(zero["waste_ratio"])
        missing = waste_report.parse_waste("Used: 5\nDiscarded: 1\n")
        self.assertIsNone(missing["waste_ratio"])
        self.assertIsNone(missing["source"])

    def test_explicit_ratio_requires_launched_count(self):
        parsed = waste_report.parse_waste("Waste ratio: 0.5\n")
        self.assertIsNone(parsed["agents_launched"])
        self.assertIsNone(parsed["waste_ratio"])
        self.assertIsNone(parsed["source"])
        with_launched = waste_report.parse_waste(
            "Agents launched: 4\nWaste ratio: 0.5\n")
        self.assertAlmostEqual(with_launched["waste_ratio"], 0.5)
        self.assertEqual(with_launched["source"], "explicit")

    def test_non_text_is_tolerated(self):
        parsed = waste_report.parse_waste(None)
        self.assertIsNone(parsed["agents_launched"])
        self.assertIsNone(parsed["waste_ratio"])

    def test_inspect_missing_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            result = waste_report.inspect(workspace)
        self.assertFalse(result["present"])
        self.assertIsNone(result["agents_launched"])
        self.assertIsNone(result["agents_used"])
        self.assertIsNone(result["agents_discarded"])
        self.assertIsNone(result["waste_ratio"])
        self.assertIsNone(result["source"])

    def test_inspect_present_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = os.path.join(workspace, waste_report.NAME)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("6 launched / 5 used / 1 discarded\n")
            result = waste_report.inspect(workspace)
        self.assertTrue(result["present"])
        self.assertEqual(result["agents_launched"], 6)
        self.assertAlmostEqual(result["waste_ratio"], 1.0 / 6.0)


class HistoryWasteTests(unittest.TestCase):
    def test_history_stats_mean_waste_and_waste_runs(self):
        runs = [_waste_run(0.5), _waste_run(0.5),
                _waste_run(0.2, subagents=4), _clean_run()]
        stats = quality.history_stats(runs, "code", concurrency=8)
        rows = {row["count"]: row for row in stats["groups"]}
        self.assertEqual(sorted(rows), [4, 8])
        self.assertEqual(rows[8]["runs"], 3)
        self.assertEqual(rows[8].get("waste_runs"), 2)
        self.assertAlmostEqual(rows[8].get("mean_waste"), 0.5)
        self.assertEqual(rows[4].get("waste_runs"), 1)
        self.assertAlmostEqual(rows[4].get("mean_waste"), 0.2)
        self.assertEqual(stats.get("waste_runs"), 3)
        self.assertAlmostEqual(stats.get("mean_waste"), 0.4)

    def test_history_stats_without_waste_reports(self):
        stats = quality.history_stats([_clean_run()], "code", concurrency=8)
        row = stats["groups"][0]
        self.assertEqual(row.get("waste_runs"), 0)
        self.assertIsNone(row.get("mean_waste"))
        self.assertEqual(stats.get("waste_runs"), 0)
        self.assertIsNone(stats.get("mean_waste"))


class ChooseAgentsWasteTests(unittest.TestCase):
    def test_high_waste_nudges_start_down_by_two(self):
        result = quality.choose_agents(
            CODE_PROMPT, files=CODE_FILES, soft_cap=16,
            default_subagents=16, concurrency=8,
            runs=[_waste_run(0.5), _waste_run(0.5)])
        self.assertEqual(result["start_subagents"], 8)
        self.assertEqual(result["subagents"], 6)
        self.assertIn("Waste ratio 0.50 nudged the start -2 to 6.",
                      result["reason"])

    def test_low_waste_does_not_change_start(self):
        result = quality.choose_agents(
            CODE_PROMPT, files=CODE_FILES, soft_cap=16,
            default_subagents=16, concurrency=8,
            runs=[_waste_run(0.1), _waste_run(0.1)])
        self.assertEqual(result["start_subagents"], 8)
        self.assertEqual(result["subagents"], 8)
        self.assertNotIn("Waste ratio", result["reason"])

    def test_no_waste_data_does_not_change_start(self):
        result = quality.choose_agents(
            CODE_PROMPT, files=CODE_FILES, soft_cap=16,
            default_subagents=16, concurrency=8, runs=[])
        self.assertEqual(result["start_subagents"], 8)
        self.assertEqual(result["subagents"], 8)
        self.assertNotIn("Waste ratio", result["reason"])

    def test_explicit_count_is_never_nudged(self):
        result = quality.choose_agents(
            CODE_PROMPT, files=CODE_FILES, explicit=10, soft_cap=16,
            default_subagents=16, concurrency=8,
            runs=[_waste_run(0.5), _waste_run(0.5)])
        self.assertEqual(result["subagents"], 10)
        self.assertNotIn("Waste ratio", result["reason"])


if __name__ == "__main__":
    unittest.main()
