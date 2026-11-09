"""3.13.5 documentation contract: README rules, key docs and version.

The README checks use only the checked-in fixture and never invoke version
control; the release version checks cover the package constants, pyproject,
CHANGELOG and the documented configuration keys.
"""

import json
import re
import unittest
from pathlib import Path

from deepseek_mcp import __version__, config, observability

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
CHANGELOG = REPO / "CHANGELOG.md"
DOCS = REPO / "docs"
EXAMPLE = REPO / "config.example.json"
FIXTURE = REPO / "tests" / "fixtures" / "readme_invariants.json"

RUN_ID_RE = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b")
RUN_PREFIX_RE = re.compile(r"\brun_[0-9a-f]{8,}\b")
BOLD_VENDOR = ("**Using a reviewer from a different vendor means a model "
               "never grades its own work.**")
BUILDER_ROW = ("| Builder | Harness, any model (default DeepSeek) | "
               "Sizes the team, builds in waves, revises on notes. |")
REVIEW_GUARANTEE = ("- If the reviewer's service fails, a backup reviewer from "
                    "another vendor takes over, and a quota error names that "
                    "reviewer's vendor and model, never DeepSeek.")


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _fixture():
    return json.loads(_read(FIXTURE))


def _roles_table(text):
    match = re.search(r"## Roles\n((?:\|.*\|\n)+)", text)
    return match.group(1) if match else ""


def _roles_rows(text):
    return [line for line in _roles_table(text).splitlines()
            if line.startswith("|")]


def _bold_lines(text):
    return re.findall(r"^\*\*Using a reviewer from a different vendor.*\*\*$",
                      text, re.M)


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
        self.lines = self.text.splitlines()
        self.fixture = _fixture()

    def test_line_budget_version_and_builder_row(self):
        self.assertLessEqual(len(self.lines), 110)
        self.assertIn("CrewForeman 3.13.5", self.lines[2])
        rows = _roles_rows(self.text)
        self.assertIn(BUILDER_ROW, rows)
        self.assertNotIn("| Builder | DeepSeek Harness (default) |", self.text)

    def test_roles_table_matches_fixture(self):
        rows = _roles_rows(self.text)
        self.assertEqual(len(rows), 5, rows)
        self.assertEqual(rows, self.fixture["roles_table"])

    def test_protected_blocks_are_byte_identical(self):
        bold = _bold_lines(self.text)
        self.assertEqual(len(bold), 1, bold)
        self.assertEqual(bold[0], self.fixture["bold_vendor_line"])
        self.assertEqual(bold[0], BOLD_VENDOR)
        self.assertEqual(_mermaid(self.text), self.fixture["mermaid"])
        blocks = _mermaid(self.text)
        self.assertEqual(len(blocks), 2, blocks)
        for block, expected in zip(blocks, self.fixture["mermaid"]):
            self.assertEqual(block, expected)
        self.assertEqual(_convergence(self.text), self.fixture["convergence"])

    def test_guarantees_are_bounded_and_reviewer_line_merged(self):
        current = _bullets(self.text)
        self.assertIn(REVIEW_GUARANTEE, current)
        self.assertEqual(sum(1 for line in current
                             if "backup reviewer" in line.lower()), 1)
        text = " ".join(current).lower()
        for token in ("model", "cost", "reviewer"):
            self.assertIn(token, text)

    def test_no_shell_or_run_ids(self):
        self.assertNotIn("$", self.text)
        self.assertIsNone(RUN_ID_RE.search(self.text))
        self.assertIsNone(RUN_PREFIX_RE.search(self.text))


class DocsCoverage(unittest.TestCase):
    def test_release_version_everywhere(self):
        self.assertEqual(__version__, "3.13.5")
        self.assertEqual(config.SERVER_VERSION, "3.13.5")
        self.assertEqual(observability.SERVICE_VERSION, "3.13.5")
        pyproject = _read(REPO / "pyproject.toml")
        self.assertIn('version = "3.13.5"', pyproject)
        changelog = _read(CHANGELOG)
        self.assertEqual(re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.M).group(1),
                         "3.13.5")
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
