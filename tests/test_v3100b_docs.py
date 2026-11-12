"""Documentation invariants for the current release (3.14.2) plus 3.10.0 Part B.

Focused checks only: README's public shape is fixed by a checked-in fixture,
the product docs describe the Part B efficiency work, and the current release
is 3.14.2.  Nothing here shells out to version control, so the checks hold in
a full checkout, an exported tarball and CI alike.
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
TEAMS = REPO / "docs" / "TEAMS.md"
CHANGELOG = REPO / "CHANGELOG.md"
FIXTURE = REPO / "tests" / "fixtures" / "readme_invariants.json"

RUN_ID_RE = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b")
RUN_PREFIX_RE = re.compile(r"\brun_[0-9a-f]{8,}\b")
BOLD_VENDOR = ("**Using a reviewer from a different vendor means a model "
               "never grades its own work.**")
BUILDER_ROW = ("| Builder | Harness, any model (default DeepSeek) | "
               "Sizes the team, builds in waves, revises on notes. |")


def _read(path):
    return path.read_text(encoding="utf-8")


def _fixture():
    return json.loads(_read(FIXTURE))


def _roles_table(text):
    match = re.search(r"## Roles\n((?:\|.*\|\n)+)", text)
    return match.group(1) if match else None


def _roles_rows(text):
    table = _roles_table(text)
    if table is None:
        return []
    return [line for line in table.splitlines() if line.startswith("|")]


def _bold_lines(text):
    return re.findall(r"^\*\*Using a reviewer from a different vendor.*\*\*$",
                      text, re.M)


def _convergence_table(text):
    match = re.search(r"## Convergence\n.*?\n\n((?:\|[^\n]*\|\n)+)", text, re.S)
    return match.group(1) if match else None


def _mermaid_blocks(text):
    return re.findall(r"```mermaid\n.*?```", text, re.S)


def _guarantees_bullets(text):
    match = re.search(r"## Guarantees\n(.*?)(?=\n## )", text, re.S)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines()
            if line.startswith("- ")]


class ReadmeInvariants(unittest.TestCase):
    def setUp(self):
        self.text = _read(README)
        self.lines = self.text.splitlines()
        self.fixture = _fixture()

    def test_line_budget_and_no_dollar(self):
        self.assertLessEqual(len(self.lines), 110)
        self.assertNotIn("$", self.text)

    def test_no_run_ids(self):
        self.assertIsNone(RUN_ID_RE.search(self.text))
        self.assertIsNone(RUN_PREFIX_RE.search(self.text))

    def test_roles_table_is_exact(self):
        rows = _roles_rows(self.text)
        self.assertEqual(len(rows), 5, rows)
        self.assertEqual(rows, self.fixture["roles_table"])
        self.assertIn(BUILDER_ROW, rows)

    def test_bold_vendor_line_is_exact_once(self):
        bold = _bold_lines(self.text)
        self.assertEqual(len(bold), 1, bold)
        self.assertEqual(bold[0], self.fixture["bold_vendor_line"])
        self.assertEqual(bold[0], BOLD_VENDOR)

    def test_convergence_and_mermaid_are_byte_identical(self):
        self.assertEqual(_convergence_table(self.text),
                         self.fixture["convergence"])
        blocks = _mermaid_blocks(self.text)
        self.assertEqual(len(blocks), 2, blocks)
        for block, expected in zip(blocks, self.fixture["mermaid"]):
            self.assertEqual(block, expected)
        self.assertEqual(blocks, self.fixture["mermaid"])

    def test_version_line_is_current(self):
        self.assertGreaterEqual(len(self.lines), 3)
        self.assertIn("CrewForeman 3.14.2", self.lines[2])

    def test_guarantees_keep_reviewer_backup_line(self):
        bullets = _guarantees_bullets(self.text)
        backups = [line for line in bullets if "backup reviewer" in line.lower()]
        self.assertEqual(len(backups), 1, backups)
        text = " ".join(bullets).lower()
        for token in ("model", "cost", "reviewer"):
            self.assertIn(token, text)


class TeamsDoc(unittest.TestCase):
    def setUp(self):
        self.text = _read(TEAMS)
        self.lower = " ".join(self.text.lower().split())

    def test_project_map_documented(self):
        self.assertIn("PROJECT_MAP.md", self.text)
        self.assertIn("project map", self.lower)
        self.assertIn("fingerprint", self.lower)
        for state in ("reuse", "stale"):
            self.assertIn(state, self.lower)

    def test_slices_overlap_documented(self):
        self.assertIn("SLICES.md", self.text)
        self.assertIn("overlap", self.lower)
        self.assertIn("warn", self.lower)
        self.assertIn("not block", self.lower)
        for surface in ("run record", "result header", "metrics"):
            self.assertIn(surface, self.lower)

    def test_waste_report_and_nudge_documented(self):
        self.assertIn("WASTE_REPORT.md", self.text)
        self.assertIn("waste ratio", self.lower)
        self.assertIn("0.35", self.text)
        self.assertIn("WASTE_NUDGE_STEP", self.text)
        self.assertIn("floor", self.lower)
        self.assertIn("explicit", self.lower)

    def test_no_private_paths_or_usernames(self):
        for marker in ("/Users/", "/home/", "C:\\Users\\"):
            self.assertNotIn(marker, self.text)


class ChangelogDoc(unittest.TestCase):
    def setUp(self):
        self.text = _read(CHANGELOG)

    def test_part_b_under_3100(self):
        match = re.search(r"^## 3\.10\.0\n(.*?)(?=^## 3\.9\.5)", self.text, re.M | re.S)
        self.assertIsNotNone(match)
        section = match.group(1)
        self.assertIn("### Part B", section)
        self.assertEqual(self.text.count("### Part B"), 1)

    def test_part_b_covers_items_2_and_6a_to_6d(self):
        match = re.search(r"^### Part B\n(.*?)(?=^### |^## )", self.text, re.M | re.S)
        self.assertIsNotNone(match)
        body = match.group(1).lower()
        for token in (
            "review scope",
            "follow-up",
            "project map",
            "slices",
            "overlap",
            "waste",
            "teams.md",
        ):
            self.assertIn(token, body)

    def test_version_is_current(self):
        self.assertEqual(re.search(r"^## (\d+\.\d+\.\d+)", self.text, re.M).group(1), "3.14.2")
        self.assertNotIn("## 3.11.1", self.text)


if __name__ == "__main__":
    unittest.main()
