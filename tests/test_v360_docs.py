"""Public release-document consistency checks for v3.6.0.

These are deliberately static assertions over the shipped documents: they
guard the claims that an independent review found contradicting the code
(Linux data paths, JSONL traces, a nonexistent ``STAMP`` instruction and the
smoke-test denominator).  No network, clock or paid API is involved.
"""

import os
import unittest
from pathlib import Path

from deepseek_mcp import config


REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"
STATUS = REPO_ROOT / "docs" / "STATUS.md"
LIMITATIONS = REPO_ROOT / "docs" / "LIMITATIONS.md"
SMOKE = REPO_ROOT / "scripts" / "smoke_mcp.py"
SERVER = REPO_ROOT / "deepseek_mcp" / "server.py"


def _flat(path):
    """Return the document with every whitespace run collapsed to one space."""
    return " ".join(path.read_text(encoding="utf-8").split())


class OperationsPathTests(unittest.TestCase):
    def test_linux_data_paths_match_the_code_default(self):
        rows = [line for line in OPERATIONS.read_text(encoding="utf-8").splitlines()
                if line.startswith("| Linux |")]
        self.assertEqual(len(rows), 1, "OPERATIONS.md must have one Linux row")
        self.assertIn("~/DeepSeek-MCP/{runs,jobs,state}", rows[0])
        self.assertNotIn("$XDG_DATA_HOME/DeepSeek-MCP", rows[0])
        self.assertNotIn("$XDG_STATE_HOME/DeepSeek-MCP", rows[0])
        if os.name != "nt":
            # config._base_dir intentionally keeps the historical location.
            self.assertEqual(config.BASE_DIR, "~/DeepSeek-MCP")
            self.assertTrue(str(config.DEFAULTS["log_dir"]).startswith(
                "~/DeepSeek-MCP/"))


class ReadmeClaimTests(unittest.TestCase):
    def test_trace_claim_is_scoped_to_harness_runs(self):
        text = _flat(README)
        self.assertIn(
            "Every Harness run is saved as Markdown and JSON with token usage, "
            "estimated cost and the review trail",
            text)
        self.assertNotIn("every paid call gets a run record", text)


class ChangelogClaimTests(unittest.TestCase):
    def test_no_nonexistent_stamp_instruction(self):
        # No STAMP constant or command exists in the package or docs; the
        # install docs never printed one.  Do not promise it again.
        self.assertNotIn("STAMP", _flat(CHANGELOG))


class StatusWordingTests(unittest.TestCase):
    def test_review_passed_documents_the_stopped_run_none(self):
        text = _flat(STATUS)
        self.assertIn(
            "`None` when no independent review ran or when the run stopped "
            "before the review gate",
            text)


class RetentionAndBudgetDocsTests(unittest.TestCase):
    """The operations page must match retention.py and budget.py."""

    def test_retention_defaults_and_keys_match_the_code(self):
        text = _flat(OPERATIONS)
        for key in ("retention_days", "keep_runs", "keep_jobs"):
            self.assertIn("| `%s` | `%s` |" % (key, config.DEFAULTS[key]), text)
        for key in ("log_dir", "jobs_dir", "state_dir"):
            self.assertIn("`%s`" % key, text)
        for stale in ("runs_dir", "traces_dir", "base_dir"):
            self.assertNotIn(stale, text)
        self.assertIn("from deepseek_mcp.retention import cleanup", text)
        self.assertIn("from deepseek_mcp.retention import secure_tree", text)
        self.assertIn("ValueError", text)
        self.assertNotIn("UnsafePathError", text)
        self.assertIn("`(directories, files)`", text)

    def test_retention_is_documented_as_off_by_default(self):
        self.assertIn("retention_days=0", _flat(LIMITATIONS))
        self.assertIn("`retention_days` | `0`", _flat(OPERATIONS))

    def test_budget_snapshot_fields_match_the_code(self):
        text = _flat(OPERATIONS)
        for field in ("cost_usd", "cost_known", "cost_exceeded",
                      "time_exceeded", "stop_reason"):
            self.assertIn("`%s`" % field, text)
        for stale in ("last_spent_usd", "spent_known"):
            self.assertNotIn(stale, text)
        self.assertIn("yields `cost_exceeded: true`", text)
        self.assertIn("Unknown cost is never treated as free", text)


class ScalingDefaultTests(unittest.TestCase):
    def test_live_scaling_max_extra_waves_description_matches_default(self):
        advertised = "Maximum extra waves (default %d)." % (
            config.DEFAULTS["live_scaling_max_extra_waves"])
        self.assertIn(advertised, SERVER.read_text(encoding="utf-8"))


class SmokeCounterTests(unittest.TestCase):
    def test_denominator_matches_the_counted_checks(self):
        source = SMOKE.read_text(encoding="utf-8")
        self.assertIn("CHECK_COUNT = 5", source)
        self.assertIn('"PASS stdio smoke test (%d/%d checks)" % (passed, CHECK_COUNT)',
                      source)


if __name__ == "__main__":
    unittest.main()
