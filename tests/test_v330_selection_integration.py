"""v3.3.0 PART C integration with a fake Harness CLI (no paid run)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh


class SelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.sessions = self.root / "sessions"
        self.env = base_env(self.root, fake_dsh=self.dsh, sessions=self.sessions)
        self.env["FAKE_DSH_STATE"] = str(self.root / "fake-state.json")
        self.task_log = self.root / "tasks.log"
        self.env["FAKE_DSH_LOG"] = str(self.task_log)
        # History must come only from this test's temporary run directory.
        # Without this, a developer's installed checkout can leak its real
        # runs/ fallback into the selection assertions.
        fallback = mock.patch.object(
            store, "FALLBACK_LOG_DIR", self.root / "fallback-runs")
        fallback.start()
        self.addCleanup(fallback.stop)

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def _seed(self, name, **overrides):
        run = {
            "id": name,
            "kind": "harness",
            "created": "2026-01-01T00:00:00-05:00",
            "prompt": "Build a Python CLI with tests",
            "task_type": "code",
            "subagents": 6,
            "quality_score": 9.1,
            "status": "ok",
            "estimated_cost_usd": 0.01,
            "seconds": 80,
        }
        run.update(overrides)
        directory = self.root / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (name + ".json")).write_text(json.dumps(run), encoding="utf-8")

    def _run(self, args, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        with mock.patch.dict(os.environ, env, clear=False):
            return harness.run_harness(args, self.cfg())

    def _saved_run(self):
        paths = sorted((self.root / "runs").glob("*.json"))
        current = [path for path in paths
                   if not path.stem.startswith("2026-01-01")]
        self.assertTrue(current, "the current run must be saved")
        return json.loads(current[-1].read_text(encoding="utf-8"))

    def _task_lines(self):
        if not self.task_log.is_file():
            return []
        return [line for line in self.task_log.read_text(encoding="utf-8").splitlines()
                if line]

    def test_automatic_choice_is_recorded_in_the_run(self):
        _text, run, _path = self._run({
            "prompt": "Design a print poster with five sections and a QR code",
            "quality": False, "workspace": str(self.workspace),
        })
        self.assertEqual(run["task_type"], "creative")
        self.assertEqual(run["subagents"], 8)
        self.assertEqual(run["subagent_waves"], [8])
        self.assertIn("Task type creative/design", run["agent_choice_reason"])
        self.assertIn("No comparable history", run["agent_choice_reason"])
        self.assertEqual(run["size_estimate"]["size"], "medium")
        self.assertIn("Execution safety (hard requirement)", run["harness_task"])
        self.assertIn("6 minutes", run["harness_task"])
        self.assertFalse(run["agent_choice"]["explicit"])

    def test_history_nudges_the_start_and_is_explained(self):
        self._seed("2026-01-01-000000-aaaaaa")
        self._seed("2026-01-01-000000-bbbbbb")
        _text, run, _path = self._run({
            "prompt": "Build a Python CLI with tests",
            "quality": False, "workspace": str(self.workspace),
        })
        self.assertEqual(run["task_type"], "code")
        self.assertEqual(run["subagents"], 6)
        self.assertEqual(run["agent_history"]["runs"], 2)
        self.assertEqual(run["agent_history"]["best_count"], 6)
        self.assertIn("History nudged the start +2 to 6", run["agent_choice_reason"])
        self.assertIn("Agent choice", (self.root / "runs" / (
            run["id"] + ".md")).read_text(encoding="utf-8"))

    def test_early_accept_does_not_start_a_revision(self):
        _text, run, _path = self._run({
            "prompt": "Build a Python CLI with tests and an API",
            "quality": True, "quality_rounds": 3, "workspace": str(self.workspace),
        }, {"FAKE_DSH_REVIEW_SCORES": "9.2", "FAKE_DSH_REVIEW_BLOCKING": "0"})
        self.assertEqual(run["quality_rounds_used"], 1)
        self.assertEqual(run["quality_score"], 9.2)
        self.assertEqual(run["agent_escalations"], [])
        self.assertNotIn("REVISION agent", "\n".join(self._task_lines()))

    def test_failed_review_escalates_the_revision_count(self):
        _text, run, _path = self._run({
            "prompt": "Build a Python CLI with tests and an API",
            "quality": True, "quality_rounds": 2, "workspace": str(self.workspace),
        }, {"FAKE_DSH_REVIEW_SCORES": "6.5,9.2",
            "FAKE_DSH_REVIEW_BLOCKING": "1,0"})
        self.assertEqual(run["quality_rounds_used"], 2)
        self.assertEqual(len(run["agent_escalations"]), 1)
        escalation = run["agent_escalations"][0]
        self.assertEqual(escalation["from"], 4)
        self.assertEqual(escalation["to"], 6)
        self.assertEqual(escalation["approach"], "more_agents")
        revision = [item for item in run["review_rounds"]
                    if item["kind"] == "revision"][0]
        self.assertEqual(revision["escalation"]["to"], 6)
        self.assertIn("Review round 1 scored 6.50/10", revision["escalation"]["reason"])
        self.assertIn("escalate from 4 to at most 6", revision["escalation"]["reason"])

    def test_recurring_defect_appends_one_skill_lesson(self):
        self._seed(
            "2026-01-01-000000-cccccc",
            task_type="creative", skill="poster", subagents=4,
            prompt="Design a poster",
            review_rounds=[{
                "kind": "review", "round": 1, "score": 7.0,
                "blocking_defects": 1,
                "defects": ["Reviewer found a missing fact in poster.html"],
            }],
        )
        _text, run, _path = self._run({
            "prompt": "Design a print poster with five sections and a QR code",
            "subagents": 2, "quality": True, "quality_rounds": 2,
            "workspace": str(self.workspace),
        }, {"FAKE_DSH_REVIEW_SCORES": "6.5,9.2",
            "FAKE_DSH_REVIEW_BLOCKING": "1,0"})
        self.assertTrue(run["lessons_added"], "a recurring lesson should be recorded")
        lesson = run["lessons_added"][0]
        self.assertEqual(lesson["kind"], "missing required fact")
        self.assertEqual(lesson["runs"], 2)
        self.assertEqual(lesson["skill"], "poster")
        text = (self.root / "skills" / "poster.md").read_text(encoding="utf-8")
        self.assertIn("Recurring creative defect (missing required fact)", text)


if __name__ == "__main__":
    unittest.main()
