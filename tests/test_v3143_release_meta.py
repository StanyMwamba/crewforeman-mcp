"""3.14.3 release metadata: version, README constraints, changelog, docs.

Windows-compatible: pure file reads plus ``re``, no shell, no git.
"""

import json
import re
import unittest
from pathlib import Path

import deepseek_mcp
from deepseek_mcp import config, observability

ROOT = Path(__file__).resolve().parent.parent
VERSION = "3.14.3"


class VersionTests(unittest.TestCase):
    def test_package_versions_match(self):
        self.assertEqual(deepseek_mcp.__version__, VERSION)
        self.assertEqual(config.SERVER_VERSION, VERSION)
        self.assertEqual(observability.SERVICE_VERSION, VERSION)

    def test_pyproject_version(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "%s"' % VERSION, text)

    def test_readme_states_release(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("CrewForeman %s" % VERSION, text)

    def test_changelog_has_current_release(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## %s" % VERSION, text)
        self.assertLess(text.index("## %s" % VERSION),
                        text.index("## 3.14.2"))


class ReadmeContractTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def test_length_and_no_dollar(self):
        self.assertLessEqual(len(self.lines), 110)
        self.assertNotIn("$", self.text)

    def test_mermaid_blocks_and_sections_preserved(self):
        self.assertEqual(self.text.count("```mermaid"), 2)
        for needle in ("## Roles", "## Convergence"):
            self.assertIn(needle, self.text)

    def test_convergence_table_present(self):
        start = self.text.index("## Convergence")
        table = [line for line in self.text[start:].splitlines()
                 if line.startswith("|")]
        self.assertGreaterEqual(len(table), 5)

    def test_reviewer_vendor_line_is_bold(self):
        self.assertRegex(self.text, r"\*\*[^\n]*[Rr]eviewer[^\n]*\*\*")

    def test_roles_builder_row_unchanged(self):
        self.assertIn("| Builder |", self.text)


class DocsTests(unittest.TestCase):
    def test_new_options_documented(self):
        ops = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        for needle in ("depends_on_policy", "git_identity_exceptions",
                       "job_max_seconds", "os_sandbox=off",
                       "job_max_seconds_display"):
            self.assertIn(needle, ops)
        cross = (ROOT / "docs" / "CROSSCHECK.md").read_text(encoding="utf-8")
        self.assertIn("false_alarm_filtered", cross)
        self.assertIn("repro", cross)

    def test_config_example_has_identity_and_policy(self):
        data = json.loads((ROOT / "config.example.json").read_text(
            encoding="utf-8"))
        self.assertEqual(data["git_identity"]["name"], "crewforeman-mcp")
        self.assertEqual(data["git_identity"]["email"], "noreply@example.com")
        self.assertEqual(data["depends_on_policy"], "success")
        self.assertIn("git_identity_exceptions", data)

    def test_config_example_does_not_pin_a_job_deadline(self):
        # An explicit 7200 in the shipped example would be an operator
        # ceiling and would turn a per-job job_max_seconds=0 back into 7200.
        data = json.loads((ROOT / "config.example.json").read_text(
            encoding="utf-8"))
        self.assertNotIn("job_max_seconds", data)

    def test_no_personal_email_in_tree_docs(self):
        pattern = re.compile(r"[\w.+-]+@(?!example\.com|example\.org|"
                             r"users\.noreply\.github\.com)[\w-]+\.[\w.]+")
        offenders = []
        for path in list(ROOT.glob("docs/*.md")) + [ROOT / "README.md",
                                                    ROOT / "CHANGELOG.md"]:
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                if "noreply@example.com" in match.group(0):
                    continue
                offenders.append("%s: %s" % (path.name, match.group(0)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
