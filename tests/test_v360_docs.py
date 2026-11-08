"""Public release-document consistency checks for v3.6.0.

These are deliberately static assertions over the shipped documents: they
guard the claims that an independent review found contradicting the code
(Linux data paths, JSONL traces, a nonexistent ``STAMP`` instruction and the
smoke-test denominator).  No network, clock or paid API is involved.
"""

import json
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


class FeatureConfigDocTests(unittest.TestCase):
    KEYS = (
        "gate_enabled", "gate_tests", "gate_test_command", "gate_lint",
        "gate_lint_command", "gate_secret_scan", "gate_secret_patterns",
        "gate_timeout_seconds", "gate_max_revisions", "gate_max_output_chars",
        "plan_contract", "plan_file", "plan_max_files", "plan_max_risks",
        "failure_notes", "failure_notes_max", "failure_notes_half_life_days",
        "failure_notes_min_occurrences", "failure_notes_max_store",
        "failure_notes_store", "pipeline_enabled", "pipeline_max_extra_jobs",
        "pipeline_require_disjoint", "pipeline_isolation",
        "pipeline_worktree_root", "pipeline_patch_dir", "pipeline_install",
        "pipeline_min_remaining_usd", "pipeline_max_pending_patches",
        "pipeline_queue_scan", "pipeline_patch_retention_days",
    )

    def test_every_3110_key_is_example_and_operations(self):
        example = json.loads(
            (REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
        text = _flat(OPERATIONS)
        self.assertEqual(len(self.KEYS), 31)
        for key in self.KEYS:
            self.assertIn(key, example, key)
            self.assertEqual(example[key], config.DEFAULTS[key], key)
            self.assertIn("`%s`" % key, text, key)

    def test_gate_failed_and_pipeline_install_are_documented(self):
        status_text = _flat(STATUS)
        self.assertIn("`gate_failed`", status_text)
        self.assertIn("install_conflict", status_text)
        ops = _flat(OPERATIONS)
        self.assertIn("git apply --check", ops)
        self.assertIn("gate_failed", ops)


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
