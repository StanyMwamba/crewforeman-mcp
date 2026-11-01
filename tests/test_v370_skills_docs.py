"""v3.7.0 documentation and example-config tests."""

import json
import re
import unittest
from pathlib import Path

from deepseek_mcp.config import DEFAULTS

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs" / "skills.md"
CHANGELOG = ROOT / "CHANGELOG.md"
CONFIG_EXAMPLE = ROOT / "config.example.json"


class DocumentationTests(unittest.TestCase):
    def test_readme_has_the_one_line_skills_paragraph(self):
        lines = README.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 110)
        paragraph = [line for line in lines if line.startswith("**Skills that learn:**")]
        self.assertEqual(len(paragraph), 1)
        self.assertGreater(len(paragraph[0].split()), 20)
        self.assertIn("auto_skills", paragraph[0])
        self.assertIn("[docs/skills.md](docs/skills.md)", paragraph[0])
        self.assertIn("`auto` | `propose` | `off`", paragraph[0])

    def test_docs_page_has_the_required_sections(self):
        text = DOCS.read_text(encoding="utf-8")
        for heading in ("What is a skill", "The registry",
                        "Detection and unmatched", "Lifecycle",
                        "Automatic creation", "Configuration",
                        "MCP tools", "Privacy", "Rollback, merge and refresh",
                        "Migration from 3.6.0", "Troubleshooting",
                        "Limitations"):
            self.assertIn(heading, text, heading)
        self.assertIn("3.7.0", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("/home/", text)

    def test_no_public_doc_contains_a_home_path_or_email(self):
        for path in [README, DOCS, CHANGELOG]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text, str(path))
            self.assertNotIn("/home/", text, str(path))
            self.assertIsNone(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text),
                              str(path))

    def test_changelog_has_the_3_7_0_entry(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("## 3.7.0", text)
        section = text[text.index("## 3.7.0"):
                       text.index("## 3.6.0") if "## 3.6.0" in text else len(text)]
        for phrase in ("registry", "unmatched", "reviewer", "candidate",
                       "rollback", "auto_skills"):
            self.assertIn(phrase, section.lower(), phrase)

    def test_config_example_has_the_short_auto_skills_block(self):
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        for key in ("auto_skills", "auto_skills_min_confidence",
                    "auto_skills_cluster_min", "auto_skills_cluster_min_severe",
                    "auto_skills_quality_gate", "auto_skills_review_rounds",
                    "auto_skills_promote_after", "auto_skills_demote_after",
                    "auto_skills_store_evidence_text"):
            self.assertIn(key, data, key)
            self.assertIn(key, DEFAULTS, key)
        self.assertEqual(data["auto_skills"], "auto")


if __name__ == "__main__":
    unittest.main()
