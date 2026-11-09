"""3.12.1 documentation contract: README rules, key docs and version."""

import json
import re
import subprocess
import unittest
from pathlib import Path

from deepseek_mcp import __version__, config, observability

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
CHANGELOG = REPO / "CHANGELOG.md"
DOCS = REPO / "docs"
EXAMPLE = REPO / "config.example.json"

BUILDER_ROW = ("| Builder | Harness, any model (default DeepSeek) | "
               "Sizes the team, builds in waves, revises on notes. |")
REVIEW_GUARANTEE = ("- If the reviewer's service fails, a backup reviewer from "
                    "another vendor takes over, and a quota error names that "
                    "reviewer's vendor and model, never DeepSeek.")


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _head_readme():
    try:
        proc = subprocess.run(
            ["git", "show", "HEAD:README.md"], cwd=str(REPO),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def _roles(text):
    match = re.search(r"## Roles\n((?:\|.*\|\n)+)", text)
    return match.group(1) if match else ""


def _bullets(text):
    match = re.search(r"## Guarantees\n(.*?)(?=\n## )", text, re.S)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines()
            if line.startswith("- ")]


def _mermaid(text):
    return re.findall(r"```mermaid\n.*?```", text, re.S)


def _convergence(text):
    match = re.search(r"## Convergence\n.*?\n\n((?:\|[^\n]*\|\n)+)", text, re.S)
    return match.group(1) if match else ""


class ReadmeRules(unittest.TestCase):
    def setUp(self):
        self.text = _read(README)
        self.head = _head_readme()
        if self.head is None:
            self.skipTest("git HEAD:README.md is unavailable")

    def test_line_budget_version_and_builder_row(self):
        self.assertLessEqual(len(self.text.splitlines()), 110)
        self.assertIn("CrewForeman 3.12.1", self.text.splitlines()[2])
        self.assertIn(BUILDER_ROW, self.text.splitlines())
        self.assertNotIn("| Builder | DeepSeek Harness (default) |", self.text)

    def test_protected_blocks_are_byte_identical(self):
        rows = [line for line in _roles(self.text).splitlines()
                if line.startswith("|")]
        head_rows = [line for line in _roles(self.head).splitlines()
                     if line.startswith("|")]
        self.assertEqual(rows[0], head_rows[0])
        self.assertEqual(rows[1], head_rows[1])
        self.assertEqual(rows[3:], head_rows[3:])
        self.assertIn(BUILDER_ROW, rows)
        self.assertEqual(_mermaid(self.text), _mermaid(self.head))
        self.assertEqual(_convergence(self.text), _convergence(self.head))
        bold = [line for line in self.text.splitlines()
                if line.startswith("**Using a reviewer")]
        self.assertEqual(len(bold), 1)
        self.assertIn(bold[0], self.head)
        # Reviewer and client rows are unchanged; only Builder differs.

    def test_guarantees_are_bounded_and_reviewer_line_merged(self):
        current = _bullets(self.text)
        head = _bullets(self.head)
        added = [line for line in current if line not in head]
        self.assertLessEqual(len(added), 3)
        self.assertGreaterEqual(len(current), len(head))
        self.assertEqual(sum(1 for line in current
                             if "backup reviewer" in line.lower()), 1)
        self.assertIn(REVIEW_GUARANTEE, current)

    def test_no_shell_or_run_ids(self):
        self.assertNotIn("$", self.text)
        self.assertIsNone(re.search(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b",
                                    self.text))
        self.assertIsNone(re.search(r"\brun_[0-9a-f]{8,}\b", self.text))


class DocsCoverage(unittest.TestCase):
    def test_release_version_everywhere(self):
        self.assertEqual(__version__, "3.12.1")
        self.assertEqual(config.SERVER_VERSION, "3.12.1")
        self.assertEqual(observability.SERVICE_VERSION, "3.12.1")
        pyproject = _read(REPO / "pyproject.toml")
        self.assertIn('version = "3.12.1"', pyproject)
        changelog = _read(CHANGELOG)
        self.assertEqual(re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.M).group(1),
                         "3.12.1")
        self.assertIn("## 3.11.0", changelog)  # history is kept

    def test_docs_document_new_keys_and_status(self):
        text = "\n".join(_read(path) for path in (
            DOCS / "TEAMS.md", DOCS / "REVIEWER.md",
            DOCS / "OPERATIONS.md", DOCS / "CLIENTS.md",
            DOCS / "STATUS.md", DOCS / "LIMITATIONS.md"))
        for key in ("builder_model", "builder_provider", "slice_models",
                    "model_candidates", "model_usage", "backup_builder_model",
                    "backup_builder_max_cost_usd", "reviewers",
                    "reviewer_model", "reviewer_provider",
                    "reviewer_same_vendor_policy", "gate_no_product_mode",
                    "plan_optional_max_files", "MODEL_AUDIT.json",
                    "same-vendor review (chosen by user)", "needs_input"):
            self.assertIn(key, text)
        self.assertIn("| `needs_input` |", _read(DOCS / "STATUS.md"))

    def test_example_has_new_keys_and_no_key_values(self):
        data = json.loads(_read(EXAMPLE))
        for key in ("builder_model", "builder_provider", "slice_models",
                    "model_candidates_max", "model_prices", "reviewers",
                    "reviewer_model", "backup_builder_model",
                    "backup_builder_max_cost_usd", "gate_no_product_mode",
                    "plan_optional_max_files"):
            self.assertIn(key, data)
        text = _read(EXAMPLE)
        self.assertIsNone(re.search(r"\b(sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|"
                                    r"gh[pousr]_[A-Za-z0-9]{20,})\b", text))


if __name__ == "__main__":
    unittest.main()
