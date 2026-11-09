"""Release acceptance checks for crewforeman-mcp 3.12.0 (P3.5).

Static, fast checks over the shipped version, config, docs and status
vocabulary. The full stdlib suite is the release gate; these assertions keep
the four feature contracts from drifting out of the release artifacts.
"""

import json
import re
import unittest
from pathlib import Path

import deepseek_mcp
from deepseek_mcp import config, events, observability, status


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
TEAMS = ROOT / "docs" / "TEAMS.md"
REVIEWER = ROOT / "docs" / "REVIEWER.md"
STATUS_DOC = ROOT / "docs" / "STATUS.md"
SMOKE = ROOT / "scripts" / "smoke_mcp.py"

FEATURE_KEYS = (
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


class VersionTests(unittest.TestCase):
    def test_version_is_3110_everywhere(self):
        self.assertEqual(deepseek_mcp.__version__, "3.13.1")
        self.assertEqual(config.SERVER_VERSION, "3.13.1")
        self.assertEqual(observability.SERVICE_VERSION, "3.13.1")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
        self.assertEqual(match.group(1), "3.13.1")
        self.assertIn('"version": "3.13.1"', SMOKE.read_text(encoding="utf-8"))


class ConfigContractTests(unittest.TestCase):
    def test_all_31_feature_keys_have_defaults_aliases_and_example(self):
        example = json.loads((ROOT / "config.example.json").read_text(
            encoding="utf-8"))
        self.assertEqual(len(FEATURE_KEYS), 31)
        for key in FEATURE_KEYS:
            self.assertIn(key, config.DEFAULTS, key)
            self.assertEqual(example.get(key), config.DEFAULTS[key], key)
            aliases = [env for env, mapped in config.ENV_MAP.items()
                       if mapped == key]
            self.assertGreaterEqual(len(aliases), 2, key)

    def test_enum_and_type_sets_cover_every_feature_key(self):
        typed = (config.BOOL_KEYS | config.INT_KEYS | set(config.ENUM_KEYS)
                 | config.LIST_KEYS | config.FLOAT_KEYS | config.PATH_KEYS)
        # Free-form command/path strings are intentionally uncoerced: they
        # accept "auto"/"off"/a command or a workspace-relative plan path.
        untyped = {"gate_tests", "gate_test_command", "gate_lint",
                   "gate_lint_command", "plan_file", "pipeline_isolation"}
        for key in FEATURE_KEYS:
            if key in untyped:
                continue
            self.assertIn(key, typed, key)


class StatusContractTests(unittest.TestCase):
    def test_gate_failed_is_failure_shaped(self):
        self.assertIn("gate_failed", status.STATUSES)
        self.assertIn("gate_failed", status.FAILURE_STATUSES)
        self.assertIn("gate_failed", status.STATUS_MEANING)
        self.assertIn("gate_failed", events._FAILURE_ATTENTION)
        value = status.final_status(
            builder_status="ok", review_status="skipped", score=None,
            gate_status="gate_failed")
        self.assertEqual(value[0], "gate_failed")
        self.assertIsNone(value[2])


class DocsContractTests(unittest.TestCase):
    def test_readme_budget_and_feature_lines(self):
        text = README.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 110)
        self.assertNotIn("$", text)
        self.assertIn("CrewForeman 3.13.1", text)
        for token in ("deterministic gate", "PLAN.json", "failure notes",
                      "write-set"):
            self.assertIn(token, text)

    def test_changelog_has_3110_with_all_features(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        match = re.search(r"^## 3\.11\.0\n(.*?)(?=^## 3\.10\.0)",
                          text, re.M | re.S)
        self.assertIsNotNone(match)
        body = match.group(1).lower()
        for token in ("deterministic gate", "plan.json", "failure notes",
                      "pipelined jobs", "vendor", "gate_failed"):
            self.assertIn(token, body)

    def test_operations_teams_reviewer_are_updated(self):
        self.assertIn("Gates, plans, failure notes and pipelining",
                      OPERATIONS.read_text(encoding="utf-8"))
        self.assertIn("Pipelined builds", TEAMS.read_text(encoding="utf-8"))
        self.assertIn("Vendor attribution", REVIEWER.read_text(encoding="utf-8"))
        self.assertIn("gate_failed", STATUS_DOC.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
