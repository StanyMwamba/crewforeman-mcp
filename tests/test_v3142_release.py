"""3.14.2 release contract: version, schema, docs and README constraints."""

import json
import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import deepseek_mcp
from deepseek_mcp import config, observability, server


def _read(relative):
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


class VersionTests(unittest.TestCase):
    def test_package_versions(self):
        self.assertEqual(deepseek_mcp.__version__, "3.14.2")
        self.assertEqual(config.SERVER_VERSION, "3.14.2")
        self.assertEqual(observability.SERVICE_VERSION, "3.14.2")

    def test_pyproject_version(self):
        text = _read("pyproject.toml")
        match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
        self.assertEqual(match.group(1), "3.14.2")

    def test_changelog_top_section(self):
        text = _read("CHANGELOG.md")
        self.assertEqual(re.search(r"^## (\d+\.\d+\.\d+)", text, re.MULTILINE).group(1),
                         "3.14.2")
        self.assertIn("crosscheck", text.lower())
        self.assertIn("audit", text.lower())


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.text = _read("README.md")
        self.lines = self.text.splitlines()

    def test_line_cap_and_no_dollar(self):
        self.assertLessEqual(len(self.lines), 110)
        self.assertNotIn("$", self.text)

    def test_protected_blocks_stay(self):
        self.assertEqual(len(re.findall(r"^```mermaid$", self.text, re.MULTILINE)), 2)
        self.assertIn("## Convergence", self.text)
        self.assertIn("| Round | What the reviewer checked | Review length | Score | Blocking defects |",
                      self.text)
        vendor = [line for line in self.lines
                  if line.startswith("**Using a reviewer from a different vendor")]
        self.assertEqual(len(vendor), 1, vendor)
        self.assertEqual(vendor[0], "**Using a reviewer from a different vendor "
                                   "means a model never grades its own work.**")
        self.assertIn("| Builder |", self.text)

    def test_links_new_docs(self):
        self.assertIn("docs/CROSSCHECK.md", self.text)
        self.assertIn("docs/PARALLEL.md", self.text)


class DocsTests(unittest.TestCase):
    def test_crosscheck_doc_covers_the_contract(self):
        text = _read("docs/CROSSCHECK.md")
        for term in ("crosscheck_threshold", "10.0", "crosscheck_max_rounds",
                     "crosscheck_below_threshold", "2.0", "BLOCKING",
                     "breaker", "spec checker", "rescue", "PLAN.json",
                     "CROSSCHECK.json", "audit_sample_rate"):
            self.assertIn(term, text, term)
        self.assertNotIn("$", text)

    def test_parallel_doc_covers_the_contract(self):
        text = _read("docs/PARALLEL.md")
        for term in ("auto_parallel", "0.85", "disjoint", "serial",
                     "worktree", "git apply", "rebase", "confidence",
                     "stray"):
            self.assertIn(term, text, term)
        self.assertNotIn("$", text)


class SchemaTests(unittest.TestCase):
    def test_harness_schema_exposes_new_arguments(self):
        tool = next(item for item in server.TOOLS
                    if item.get("name") == "harness_deepseek")
        props = tool["inputSchema"]["properties"]
        for key in ("mode", "crosscheck_threshold", "crosscheck_crews",
                    "crosscheck_crew_size", "crosscheck_max_rounds",
                    "audit_review", "audit_sample_rate",
                    "audit_spot_requirements", "audit_spot_fixes",
                    "auto_parallel", "auto_parallel_min_confidence"):
            self.assertIn(key, props, key)
        self.assertEqual(set(props["mode"]["enum"]), {"standard", "crosscheck"})
        self.assertEqual(props["crosscheck_threshold"]["maximum"], 10)
        self.assertEqual(props["crosscheck_threshold"]["minimum"], 0)

    def test_config_defaults(self):
        self.assertEqual(config.DEFAULTS["crosscheck_threshold"], 10.0)
        self.assertEqual(config.DEFAULTS["crosscheck_max_rounds"], 3)
        self.assertEqual(config.DEFAULTS["crosscheck_crews"], 2)
        self.assertEqual(config.DEFAULTS["crosscheck_crew_size"], 4)
        self.assertEqual(config.DEFAULTS["audit_sample_rate"], 1.0)
        self.assertEqual(config.DEFAULTS["audit_spot_requirements"], 3)
        self.assertEqual(config.DEFAULTS["audit_spot_fixes"], 2)
        self.assertEqual(config.DEFAULTS["default_mode"], "")
        self.assertTrue(config.DEFAULTS["auto_parallel"])
        self.assertEqual(config.DEFAULTS["auto_parallel_min_confidence"], 0.85)

    def test_example_config_parses_and_has_keys(self):
        data = json.loads(_read("config.example.json"))
        for key in ("default_mode", "crosscheck_threshold", "crosscheck_crews",
                    "crosscheck_crew_size", "crosscheck_max_rounds",
                    "audit_review", "audit_sample_rate",
                    "audit_spot_requirements", "audit_spot_fixes",
                    "auto_parallel", "auto_parallel_min_confidence"):
            self.assertIn(key, data, key)


if __name__ == "__main__":
    unittest.main()
