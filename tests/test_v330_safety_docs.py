"""Public README and CHANGELOG safety contract for v3.4.0.

Only shipped documentation is read.  The assertions cover the safety rules the
shipped code actually implements, and are intentionally tolerant of prose
edits: they match section headings and stable tokens, not whole sentences.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"

# Public docs must never contain personal home paths or e-mail addresses.
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PERSONAL_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")


def _flat(text):
    return re.sub(r"\s+", " ", text.lower())


def _stall_section(text):
    """Return the current operations watchdog section, lowercased."""
    match = re.search(
        r"(?ms)^## Watchdog and (?:rescue|budgets).*?\n(.*?)(?=^## )", text)
    if match is None:
        raise AssertionError("docs/OPERATIONS.md has no watchdog section")
    return _flat(match.group(1))


class PublicSafetyDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = OPERATIONS.read_text(encoding="utf-8")
        cls.low = cls.text.lower()
        cls.flat = _flat(cls.text)

    def test_readme_has_a_public_safety_section(self):
        self.assertRegex(self.low, r"(?m)^## safety and sandboxing\s*$")
        for token in ("seatbelt", "permission", "api key", "credential",
                      "read-only", "workspace-write", "untrusted"):
            self.assertIn(token, self.flat, token)

    def test_builders_use_a_hard_shell_timeout(self):
        self.assertRegex(self.low, r"hard (?:shell )?timeout")
        self.assertIn("timeout 60", self.low)
        self.assertIn("gtimeout 60", self.low)
        self.assertIn("perl", self.low)

    def test_chrome_state_is_workspace_local(self):
        self.assertRegex(self.low, r"workspace[- ]local")
        for token in ("user-data-dir", "home", "tmpdir"):
            self.assertIn(token, self.low, token)

    def test_at_most_two_chrome_attempts_and_render_fallback(self):
        self.assertIn("at most two", self.flat)
        self.assertIn("render unavailable", self.low)

    def test_builder_and_wave_time_budgets(self):
        section = _stall_section(self.text)
        for token in ("builder", "stall_seconds", "480", "stall_max_seconds",
                      "1800", "job_max_seconds", "job_max_cost_usd"):
            self.assertIn(token, section, token)

    def test_rescue_is_capped_at_eight_minutes(self):
        section = _stall_section(self.text)
        self.assertIn("rescue", section)
        self.assertIn("8 minutes", section)
        self.assertIn("finisher", section)

    def test_watchdog_is_child_aware_and_progress_gated(self):
        section = _stall_section(self.text)
        for token in ("stall_seconds", "480", "stall_max_seconds", "1800",
                      "child-session", "process group", "progress", "extend"):
            self.assertIn(token, section, token)

    def test_no_personal_paths_or_emails_in_public_docs(self):
        for path in (README, CHANGELOG):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(PERSONAL_PATH.search(text), path.name)
            self.assertIsNone(EMAIL.search(text), path.name)

    def test_changelog_records_the_v34_safety_contract(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("## 3.4.0", text)
        section = text[text.index("## 3.4.0"):text.index("## 3.3.0")].lower()
        flat = re.sub(r"\s+", " ", section)
        for token in ("minimal environment", "no connector api keys",
                      "process group", "redacts the home path", "hard timeout"):
            self.assertIn(token, flat, token)


if __name__ == "__main__":
    unittest.main()
