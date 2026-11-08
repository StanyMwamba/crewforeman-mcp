"""3.9.5 slice C: builder-practices run record + first review score."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh, write_session_log


CODE_PROMPT = ("Implement a Python module that parses CSV files, fix the "
               "existing unit tests and add new tests for the parser.")


class RunRecordTests(unittest.TestCase):
    """Mirrors tests.test_harness.HarnessRunTests fake-DSH setup.

    Slices B and C are integrated, so these tests exercise the real prompt
    builders; the earlier local shim was removed because it re-enabled the
    block after harness passed ``builder_practices=False``.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.sessions = self.root / "sessions"
        self.env = base_env(self.root, fake_dsh=self.dsh, sessions=self.sessions)


    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def test_first_review_score_and_builder_practices_record(self):
        write_session_log(self.sessions, self.workspace)
        env = dict(os.environ, **{
            "FAKE_DSH_SCORE": "7.0",
            "FAKE_DSH_REVIEW_SCORES": "6.0,9.0",
            "FAKE_DSH_REVIEW_BLOCKING": "1,0",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })
        with mock.patch.dict(os.environ, env, clear=False):
            text, run, path = harness.run_harness({
                "prompt": CODE_PROMPT,
                "subagents": 2,
                "quality": True,
                "quality_rounds": 2,
                "workspace": str(self.workspace),
            }, self.cfg())

        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["first_review_score"], 6.0)
        self.assertEqual(run["quality_score"], 9.0)
        self.assertEqual(run["review_scores"], [6.0, 9.0])
        kinds = [item["kind"] for item in run["review_rounds"]]
        self.assertEqual(kinds, ["review", "revision", "review"])

        practices = run["builder_practices"]
        self.assertEqual(practices["set"], "builder-practices")
        self.assertEqual(practices["version"], "1.0")
        self.assertEqual(practices["variant"], "code")
        self.assertTrue(practices["enabled"])
        self.assertTrue(0 < practices["words"] <= 350)
        self.assertEqual(practices["budget_words"], 350)

        self.assertIn("## Builder practices", run["harness_task"])

        md = store._md(run)
        self.assertIn("Builder practices", md)
        self.assertIn("builder-practices v1.0", md)
        self.assertIn("code,", md)
        self.assertTrue(Path(path).is_file())

    def test_disabled_builder_practices_record(self):
        write_session_log(self.sessions, self.workspace)
        with mock.patch.dict(os.environ,
                             {"FAKE_DSH_SCORE": "8.0"}, clear=False):
            text, run, _path = harness.run_harness({
                "prompt": CODE_PROMPT,
                "subagents": 0,
                "quality": False,
                "builder_practices": False,
                "workspace": str(self.workspace),
            }, self.cfg())

        practices = run["builder_practices"]
        self.assertFalse(practices["enabled"])
        self.assertEqual(practices["words"], 0)
        self.assertEqual(practices["variant"], "none")
        self.assertNotIn("## Builder practices", run["harness_task"])

        md = store._md(run)
        self.assertIn("Builder practices", md)
        self.assertIn("disabled", md)


if __name__ == "__main__":
    unittest.main()
