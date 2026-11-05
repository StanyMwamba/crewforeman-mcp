"""Release-level acceptance tests for the v3.2.0 fixes (still valid in 3.3.0).

Covers section 6 of ``wave2/TASKS.md``: the ``stall_seconds`` config key and
its precedence (R5), version/release metadata, and the finisher task contract
(R2).  Everything is read-only or uses a temporary config path, so no test
touches the real ``~/.config`` or an actual Harness installation.
"""

import json
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import __version__, config, quality, server
from deepseek_mcp.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE = REPO_ROOT / "config.example.json"
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


class StallConfigTests(unittest.TestCase):
    """R5: stall_seconds loading, precedence and config.example.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _load(self, file_data=None, env=None, name="config.json"):
        path = self.root / name
        if file_data is not None:
            path.write_text(json.dumps(file_data), encoding="utf-8")
        return load_config(config_path=path, env=env or {})

    def test_stall_seconds_default_is_480(self):
        cfg = self._load(env={})
        self.assertEqual(cfg.stall_seconds, 480.0)
        self.assertIsInstance(cfg.stall_seconds, float)

    def test_stall_seconds_file_and_env_override(self):
        from_file = self._load({"stall_seconds": 60}, name="file.json")
        self.assertEqual(from_file.stall_seconds, 60.0)

        dsh_env = self._load(env={"DSH_STALL_SECONDS": "90"}, name="missing-1.json")
        self.assertEqual(dsh_env.stall_seconds, 90.0)

        alias_only = self._load(
            env={"DEEPSEEK_STALL_SECONDS": "120"}, name="missing-2.json")
        self.assertEqual(alias_only.stall_seconds, 120.0)

        both = self._load(
            env={"DEEPSEEK_STALL_SECONDS": "120", "DSH_STALL_SECONDS": "90"},
            name="missing-3.json")
        self.assertEqual(both.stall_seconds, 90.0)

    def test_stall_seconds_null_disables(self):
        cfg = self._load({"stall_seconds": None}, name="null.json")
        self.assertIsNone(cfg.stall_seconds)

        zero = self._load({"stall_seconds": "0"}, name="zero.json")
        self.assertEqual(zero.stall_seconds, 0.0)
        self.assertFalse(zero.stall_seconds > 0)

    def test_config_example_includes_stall_seconds(self):
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        self.assertIn("stall_seconds", data)
        self.assertEqual(data["stall_seconds"], 480)

        cfg = load_config(config_path=CONFIG_EXAMPLE, env={})
        self.assertEqual(cfg.stall_seconds, 480.0)


class ReleaseMetadataTests(unittest.TestCase):
    """R5: version bump and the user-visible release markers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_version_is_current(self):
        self.assertEqual(__version__, "3.8.0")
        self.assertEqual(config.SERVER_VERSION, "3.8.0")

        cfg = load_config(config_path=self.root / "missing.json", env={})
        response = server.dispatch(
            "initialize", {"protocolVersion": "2025-06-18"}, cfg=cfg)
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["serverInfo"]["version"], "3.8.0")

        readme = README.read_text(encoding="utf-8")
        self.assertIn("# CrewForeman", readme.splitlines()[0])
        self.assertIn("CrewForeman 3.8.0", readme)

        changelog = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn("## 3.5.0", changelog)
        self.assertIn("## 3.2.0", changelog)
        self.assertLess(
            changelog.index("## 3.5.0"), changelog.index("## 3.4.0"),
            "the 3.5.0 changelog entry must come before 3.4.0")

    def test_changelog_mentions_all_four_fixes(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        start = text.index("## 3.2.0")
        end = text.index("## 3.1.1", start)
        section = text[start:end].lower()

        self.assertIn("stall", section)
        self.assertTrue(
            "rescue" in section or "finisher" in section,
            "the 3.2.0 changelog must mention rescue/finisher")
        self.assertIn("workflow", section)
        self.assertIn("wave", section)
        self.assertTrue(
            "log_fallback_reason" in section or "fallback" in section,
            "the 3.2.0 changelog must mention the log_dir fallback reason")


class FinisherTaskTests(unittest.TestCase):
    """R2: the task text handed to the single rescue (finisher) agent."""

    def test_finisher_task_contract(self):
        task = quality.build_finisher_task(
            "BRIEF", ["a.md"], stop_reason="stalled", stopped_after=10,
            quality=True)

        required = (
            "FINISHER",
            "previous builder attempt stopped early",
            "Use the existing files",
            "single best existing candidate",
            "Final quality score",
            "0 subagents",
            "no `workflow`",
            "list the file names",
        )
        for phrase in required:
            self.assertIn(phrase, task)
        self.assertIn("`a.md`", task)

        review_lines = [line for line in task.splitlines() if "REVIEW.md" in line]
        self.assertEqual(
            len(review_lines), 1,
            "REVIEW.md must appear exactly once, in the prohibition context")
        self.assertEqual(task.count("REVIEW.md"), 1)
        self.assertIn("Do not write", review_lines[0])

    def test_finisher_task_time_limit_and_quality_off(self):
        task = quality.build_finisher_task(
            "BRIEF", [], stop_reason="time_limit", stopped_after=2,
            quality=False)

        self.assertIn("hit its time limit after 2 s", task)
        self.assertIn("None were listed", task)
        self.assertIn("0 subagents", task)
        self.assertIn("Do not write", task)
        self.assertNotIn("QUALITY_REPORT.md", task)
        self.assertNotIn("Final quality score", task)


if __name__ == "__main__":
    unittest.main()
