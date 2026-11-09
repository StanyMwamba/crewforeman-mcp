"""v3.4.0 release metadata, docs and deliverable presence."""

import json
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import __version__, config, server
from deepseek_mcp.config import DEFAULTS, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.json"


class ReleaseMetadataTests(unittest.TestCase):
    def cfg(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return load_config(config_path=Path(tmp.name) / "missing.json", env={})

    def test_version_is_current_everywhere(self):
        self.assertEqual(__version__, "3.13.6")
        self.assertEqual(config.SERVER_VERSION, "3.13.6")
        response = server.dispatch("initialize", {"protocolVersion": "2025-06-18"},
                                   cfg=self.cfg())
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["serverInfo"]["version"], "3.13.6")
        instructions = response["result"]["instructions"]
        self.assertTrue(instructions.startswith(f"crewforeman-mcp {config.SERVER_VERSION}:"))
        self.assertIn("progress", instructions)
        self.assertIn("0-10", instructions)
        self.assertIn("generic MCP", instructions)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("# CrewForeman", readme.splitlines()[0])
        self.assertIn("CrewForeman 3.13.6", readme)

    def test_server_tool_text_describes_v34(self):
        tools = server.dispatch("tools/list", cfg=self.cfg())["result"]["tools"]
        harness_tool = {tool["name"]: tool for tool in tools}["harness_deepseek"]
        description = harness_tool["description"]
        self.assertIn("child-session aware watchdog", description)
        self.assertIn("render probe", description)
        self.assertIn("one safe retry", description)
        schema = harness_tool["inputSchema"]["properties"]["subagents"]["description"]
        self.assertIn("strong smaller history bucket", schema)
        self.assertNotIn("rounded up", schema)
        self.assertNotIn("16+", description)

    def test_changelog_has_a_3_4_0_section_before_3_3_0(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("## 3.4.0", text)
        self.assertLess(text.index("## 3.4.0"), text.index("## 3.3.0"))
        section = text[text.index("## 3.4.0"):text.index("## 3.3.0")].lower()
        for phrase in ("child-aware", "tmpdir", "visual-impact", "render",
                       "retry", "deliverable", "3.4.0"):
            self.assertIn(phrase, section, phrase)

    def test_readme_documents_the_3_4_contract_today(self):
        text = README.read_text(encoding="utf-8")
        text += "\n" + "\n".join(path.read_text(encoding="utf-8")
                                 for path in (REPO_ROOT / "docs").glob("*.md"))
        # The restructured README keeps the operational contract, while the
        # 3.4.0 release notes live in the changelog.
        for phrase in ("progress-gated", "workspace file",
                       "TMPDIR", "extra waves", "docs/OPERATIONS.md"):
            self.assertIn(phrase, text, phrase)
        changelog = CHANGELOG.read_text(encoding="utf-8")
        section = changelog[changelog.index("## 3.4.0"):changelog.index("## 3.3.0")].lower()
        for phrase in ("child-aware", "visual-impact", "render", "retry",
                       "deliverable", "3.4.0"):
            self.assertIn(phrase, section, phrase)

    def test_config_example_has_the_new_keys(self):
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(data["default_subagents"], 16)
        self.assertEqual(data["subagent_soft_cap"], 32)
        self.assertTrue(data["child_progress"])
        self.assertTrue(data["render_probe"])
        self.assertTrue(data["transient_retry"])
        self.assertIn("chrome_bin", data)
        cfg = load_config(config_path=CONFIG_EXAMPLE, env={})
        self.assertTrue(cfg.child_progress)
        self.assertTrue(cfg.render_probe)
        self.assertEqual(cfg.render_probe_timeout, 12.0)
        self.assertEqual(cfg.stall_max_seconds, 1800.0)

    def test_config_example_round_trips_to_defaults(self):
        # The restructured README links to docs/INSTALL.md instead of embedding
        # a second copy of the whole config; config.example.json is the source
        # of truth for operators.
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        for key in ("stall_max_seconds", "child_progress", "render_probe",
                    "render_probe_timeout", "chrome_bin", "transient_retry"):
            self.assertIn(key, data)
            self.assertIn(key, DEFAULTS)

    def test_release_documents_exist(self):
        # A public checkout must pass.  Builder artefacts (review notes,
        # improvement plans, quality reports, wave files, raw test logs) are
        # deliberately not required and must never be shipped.
        for name in ("LICENSE", "README.md", "CHANGELOG.md",
                     "config.example.json"):
            path = REPO_ROOT / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 0, name)


if __name__ == "__main__":
    unittest.main()
