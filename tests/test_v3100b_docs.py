"""Documentation invariants for 3.10.0 Part B (item 6d).

Focused checks only: README's public shape is fixed, the three product docs
describe the Part B efficiency work, and the release stays 3.10.0.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
TEAMS = REPO / "docs" / "TEAMS.md"
CHANGELOG = REPO / "CHANGELOG.md"


def _read(path):
    return path.read_text(encoding="utf-8")


def _head_readme():
    """The committed README (pre-Part A), used as the protected-byte baseline."""
    try:
        proc = subprocess.run(
            ["git", "show", "HEAD:README.md"],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def _roles_table(text):
    match = re.search(r"## Roles\n((?:\|.*\|\n)+)", text)
    return match.group(1) if match else None


def _roles_bold_line(text):
    match = re.search(r"^\*\*Using a reviewer from a different vendor.*\*\*$", text, re.M)
    return match.group(0) if match else None


def _convergence_table(text):
    match = re.search(r"## Convergence\n.*?\n\n((?:\|[^\n]*\|\n)+)", text)
    return match.group(1) if match else None


def _mermaid_blocks(text):
    return re.findall(r"```mermaid\n.*?```", text, re.S)


def _guarantees_bullets(text):
    match = re.search(r"## Guarantees\n(.*?)(?=\n## )", text, re.S)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines() if line.startswith("- ")]


class ReadmeInvariants(unittest.TestCase):
    def setUp(self):
        self.text = _read(README)
        self.head = _head_readme()
        if self.head is None:
            self.skipTest("git HEAD:README.md is unavailable")

    def test_line_budget_and_no_dollar(self):
        self.assertLessEqual(len(self.text.splitlines()), 110)
        self.assertNotIn("$", self.text)

    def test_exactly_one_new_guarantees_bullet(self):
        current = _guarantees_bullets(self.text)
        head = _guarantees_bullets(self.head)
        self.assertGreaterEqual(len(current), len(head))
        added = [bullet for bullet in current if bullet not in head]
        # Part A already added the backup-reviewer bullet before this build.
        mine = [bullet for bullet in added if "backup reviewer" not in bullet.lower()]
        self.assertEqual(len(mine), 1, added)
        self.assertIn("overlap", mine[0].lower())
        self.assertIn("waste", mine[0].lower())

    def test_roles_table_and_bold_line_unchanged(self):
        self.assertIsNotNone(_roles_table(self.head))
        self.assertEqual(_roles_table(self.text), _roles_table(self.head))
        self.assertIsNotNone(_roles_bold_line(self.head))
        self.assertEqual(_roles_bold_line(self.text), _roles_bold_line(self.head))

    def test_convergence_table_and_mermaid_unchanged(self):
        self.assertIsNotNone(_convergence_table(self.head))
        self.assertEqual(_convergence_table(self.text), _convergence_table(self.head))
        self.assertEqual(_mermaid_blocks(self.text), _mermaid_blocks(self.head))
        self.assertEqual(len(_mermaid_blocks(self.text)), 2)

    def test_version_line_stays_3100(self):
        self.assertIn("3.10.0", self.text.splitlines()[2])


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

    def test_version_stays_3100(self):
        self.assertEqual(re.search(r"^## (\d+\.\d+\.\d+)", self.text, re.M).group(1), "3.10.0")
        self.assertNotIn("## 3.10.1", self.text)


if __name__ == "__main__":
    unittest.main()
