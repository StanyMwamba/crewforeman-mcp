"""v3.5.0 release metadata, docs and configuration surface."""

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

V350_KEYS = (
    "sandbox",
    "reviewer_sandbox",
    "os_sandbox",
    "dsh_transport",
    "dsh_provider",
    "dsh_model",
    "checkpoint",
    "providers",
    "consult_models",
    "provider_fallback",
    "task_ttl",
    "browser_evidence",
    "add_dirs",
)

EXPECTED_TOOLS = {
    "ask_deepseek",
    "consult_deepseek",
    "swarm_deepseek",
    "list_deepseek_runs",
    "harness_deepseek",
    "resume_deepseek",
    "deepseek_job_status",
    "deepseek_job_cancel",
    "read_deepseek_run",
    "rate_deepseek_run",
    "list_skills",
    "read_skill",
    "approve_skill",
    "reject_skill",
    "retire_skill",
    "rollback_skill",
    "keep_awake",
    "take_screenshot",
    "restart_client_app",
    "deepseek_maintenance",
}


class ReleaseMetadataTests(unittest.TestCase):
    def cfg(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return load_config(config_path=Path(tmp.name) / "missing.json", env={})

    def test_version_and_instructions_are_360(self):
        self.assertEqual(__version__, "3.13.3")
        self.assertEqual(config.SERVER_VERSION, "3.13.3")
        response = server.dispatch("initialize", {"protocolVersion": "2025-06-18"},
                                   cfg=self.cfg())
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["serverInfo"]["version"], "3.13.3")
        instructions = response["result"]["instructions"]
        self.assertTrue(instructions.startswith(f"crewforeman-mcp {config.SERVER_VERSION}:"))
        for phrase in ("consult_deepseek", "resume", "checkpoints",
                       "progress notifications", "cancellation", "permission presets",
                       "ok_below_gate"):
            self.assertIn(phrase, instructions, phrase)

    def test_readme_is_the_short_generic_quick_start(self):
        lines = README.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "# CrewForeman")
        text = README.read_text(encoding="utf-8")
        self.assertLessEqual(len(lines), 110, "README must stay a short front door")
        for heading in ("## Method", "## Outcomes", "## Install",
                        "## Guarantees", "## Limits", "## Documentation"):
            self.assertIn(heading, text)
        self.assertIn("stdio MCP server", text)

    def test_readme_and_docs_document_the_shipped_surface(self):
        text = README.read_text(encoding="utf-8")
        docs = "\n".join(path.read_text(encoding="utf-8")
                          for path in (REPO_ROOT / "docs").glob("*.md"))
        self.assertEqual(len(server.TOOLS), 20)
        self.assertEqual({tool["name"] for tool in server.TOOLS}, EXPECTED_TOOLS)
        for phrase in ("DSH_PERMISSION_MODE", "sandbox-exec", "Seatbelt",
                       "dsh_transport", "--profile sdk", "resume_deepseek",
                       "checkpoint", "consult_deepseek", ".browser-evidence",
                       "workspace-write", "read-only", "TMPDIR",
                       "progress-gated"):
            self.assertIn(phrase, text + docs, phrase)

    def test_config_example_and_example_file_round_trip(self):
        example_data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        for key in V350_KEYS:
            self.assertIn(key, example_data, "config.example missing %s" % key)
            self.assertIn(key, DEFAULTS, "unknown config key %s" % key)
        cfg = load_config(config_path=CONFIG_EXAMPLE, env={})
        self.assertEqual(cfg.sandbox, "workspace-write")
        self.assertEqual(cfg.reviewer_sandbox, "read-only")
        self.assertEqual(cfg.dsh_transport, "headless")
        self.assertEqual(cfg.dsh_provider, "deepseek-official")
        self.assertTrue(cfg.checkpoint)
        self.assertEqual(cfg.task_ttl, 3600)
        self.assertTrue(cfg.browser_evidence)
        self.assertFalse(cfg.provider_fallback)
        self.assertEqual(example_data["stall_seconds"], 480)
        self.assertEqual(example_data["os_sandbox"], "auto")


class ChangelogTests(unittest.TestCase):
    def test_350_section_precedes_340_and_covers_the_gaps(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("## 3.5.0", text)
        self.assertLess(text.index("## 3.5.0"), text.index("## 3.4.0"))
        section = text[text.index("## 3.5.0"):text.index("## 3.4.0")].lower()
        for phrase in ("sandbox", "seatbelt", "sdk", "progress", "tasks/cancel",
                       "checkpoint", "resume", "provider", "consult",
                       "browser", "review.md", "3.5.0"):
            self.assertIn(phrase, section, phrase)

    def test_release_documents_exist(self):
        for name in ("README.md", "CHANGELOG.md", "config.example.json",
                     "LICENSE", "deepseek_mcp.py"):
            path = REPO_ROOT / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 0, name)


class RedactionAndPathsTests(unittest.TestCase):
    def test_release_docs_have_no_personal_absolute_paths(self):
        for name in ("README.md", "CHANGELOG.md", "wave-1-results.md",
                     "wave-1-summary.md"):
            path = REPO_ROOT / name
            if not path.is_file():
                # Wave records are internal build artifacts, not public items.
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text, name)
            self.assertNotIn("/home/", text, name)


if __name__ == "__main__":
    unittest.main()
