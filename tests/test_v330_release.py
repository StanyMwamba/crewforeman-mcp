"""v3.3.0 regression metadata: MCP surface, docs and public config defaults.

Only public shipped artefacts are asserted.  Internal builder reports and raw
test logs are intentionally not required.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import __version__, config, harness, quality, server
from deepseek_mcp.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
LICENSE = REPO_ROOT / "LICENSE"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.json"
COPYRIGHT_LINE = re.compile(r"(?im)^[ \t]*(?:copyright\b|\(c\)\s|©)")
CONTRIBUTORS_LABEL = re.compile(
    r"(?i)copyright\s*\(c\)[^\n]{0,80}\bcontributors?\b")


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_is_current_everywhere(self):
        self.assertEqual(__version__, "3.13.7")
        self.assertEqual(config.SERVER_VERSION, "3.13.7")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(config_path=Path(tmp) / "missing.json", env={})
            response = server.dispatch(
                "initialize", {"protocolVersion": "2025-06-18"}, cfg=cfg)
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["serverInfo"]["version"], "3.13.7")
        instructions = response["result"]["instructions"]
        self.assertTrue(instructions.startswith(f"crewforeman-mcp {config.SERVER_VERSION}:"))
        self.assertNotIn("v3.1", instructions)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(config_path=Path(tmp) / "missing.json", env={})
            tools = server.dispatch("tools/list", cfg=cfg)["result"]["tools"]
        harness_tool = {tool["name"]: tool for tool in tools}["harness_deepseek"]
        description = harness_tool["description"]
        self.assertIn("0 agents for a simple question", description)
        self.assertIn("4 for code", description)
        self.assertNotIn("16+", description)
        self.assertNotIn("soft-capped at 32", description)
        schema = harness_tool["inputSchema"]["properties"]["subagents"]["description"]
        self.assertIn("6/8/12", schema)
        self.assertIn("Explicit values are kept exactly", schema)
        self.assertNotIn("rounded up", schema)
        max_seconds = harness_tool["inputSchema"]["properties"]["max_seconds"]["description"]
        self.assertIn("480 s", max_seconds)
        self.assertNotIn("600", max_seconds)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("# CrewForeman", readme.splitlines()[0])
        self.assertIn("CrewForeman 3.13.7", readme)

    def test_changelog_has_a_3_3_0_section_before_3_2_0(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("## 3.3.0", text)
        self.assertIn("## 3.2.0", text)
        self.assertLess(text.index("## 3.3.0"), text.index("## 3.2.0"))
        section = text[text.index("## 3.3.0"):text.index("## 3.2.0")].lower()
        for phrase in ("timeout", "finisher", "dynamic", "history", "escalat",
                       "recurring", "3.3.0"):
            self.assertIn(phrase, section, phrase)

    def test_readme_documents_the_current_sections(self):
        text = README.read_text(encoding="utf-8")
        for heading in ("## Method", "## Outcomes", "## Install",
                        "## Guarantees", "## Limits", "## Documentation", "## License"):
            self.assertIn(heading, text)
        self.assertIn("docs/LIMITATIONS.md", text)
        self.assertIn("docs/CLIENTS.md", text)
        # Historical detail lives in the changelog, not the ~150-line README.
        changelog = CHANGELOG.read_text(encoding="utf-8")
        section = changelog[changelog.index("## 3.4.0"):changelog.index("## 3.3.0")]
        for phrase in ("hard timeout", "retry", "deliverable"):
            self.assertIn(phrase, section.lower())

    def test_config_example_keeps_the_automatic_ceiling(self):
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(data["default_subagents"], 16)
        self.assertEqual(data["subagent_soft_cap"], 32)
        cfg = load_config(config_path=CONFIG_EXAMPLE, env={})
        self.assertEqual(cfg.default_subagents, 16)

    def test_finisher_process_cap_matches_the_8_minute_task_text(self):
        self.assertEqual(harness.FINISHER_MAX_SECONDS, 480.0)
        task = quality.build_finisher_task(
            "Create flyer.png", ["candidate.html"], quality=False)
        self.assertIn("8 minutes", task)

    def test_license_and_readme_have_no_copyright_holder(self):
        license_text = LICENSE.read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn("The above copyright notice and this permission notice",
                      license_text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)
        self.assertIsNone(COPYRIGHT_LINE.search(license_text))
        self.assertIsNone(CONTRIBUTORS_LABEL.search(license_text))

        readme = README.read_text(encoding="utf-8")
        license_section = readme.split("## License", 1)[1]
        self.assertIsNone(COPYRIGHT_LINE.search(license_section))
        self.assertIsNone(CONTRIBUTORS_LABEL.search(license_section))


class DeliverablePresenceTests(unittest.TestCase):
    def test_public_release_documents_exist(self):
        for name in ("LICENSE", "README.md", "CHANGELOG.md",
                     "config.example.json"):
            path = REPO_ROOT / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 0, name)


if __name__ == "__main__":
    unittest.main()
