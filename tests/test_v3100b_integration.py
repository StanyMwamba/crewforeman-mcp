"""B6 integration contract tests for 3.10.0 Part B items 2 and 6."""

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, quality, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh

FOLLOW = "## Follow-ups (pre-existing, non-blocking)"


class ReviewFollowUpRunRecordTest(unittest.TestCase):
    """The real review entry, not a hand-built one, must carry follow-ups."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.sessions = self.root / "sessions"
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.sessions)

    def cfg(self):
        return load_config(config_path=self.root / "config.json", env=self.env)

    def _run(self, follow_ups, readonly=False):
        fake_env = {
            "FAKE_DSH_SCORE": "9.0",
            "FAKE_DSH_REVIEW_SCORES": "9.0",
            "FAKE_DSH_REVIEW_BLOCKING": "0",
            "FAKE_DSH_REVIEW_FOLLOW_UPS": follow_ups,
        }
        if readonly:
            fake_env["FAKE_DSH_REVIEW_READONLY"] = "1"
        with mock.patch.dict(os.environ, fake_env, clear=False):
            return harness.run_harness({
                "prompt": "Build a tiny python service with tests",
                "subagents": 2, "quality": True, "quality_rounds": 1,
                "workspace": str(self.workspace),
            }, self.cfg())

    def test_non_blocking_follow_ups_reach_record_and_result(self):
        text, run, _path = self._run(
            "Legacy cache files are left behind|Old README has a typo")
        self.assertEqual(run["quality_score"], 9.0)
        self.assertEqual(run["review_status"], "accepted")
        self.assertEqual(len(run["follow_ups"]), 2)
        self.assertEqual(run["metrics"]["follow_ups"], 2)
        self.assertEqual(run["metrics"]["blocking_follow_ups"], 0)
        review = [item for item in run["review_rounds"]
                  if item.get("kind") == "review"][-1]
        self.assertEqual(len(review["follow_ups"]), 2)
        self.assertEqual(review["blocking_defects"], 0)
        self.assertIn("Follow-ups (pre-existing", text)
        self.assertIn("[non-blocking] Legacy cache files", text)

    def test_readonly_full_report_is_not_double_counted(self):
        text, run, _path = self._run(
            "Legacy cache files are left behind|"
            "[security] Legacy session cookie is not HttpOnly",
            readonly=True)
        review = [item for item in run["review_rounds"]
                  if item.get("kind") == "review"][-1]
        # The connector persisted the reviewer's complete final answer as
        # REVIEW.md and then passed report + answer to parse_review.  The same
        # finding must be counted once in the review entry, run record, result
        # header and security count.
        self.assertEqual(len(review["follow_ups"]), 2)
        self.assertEqual(review["blocking_defects"], 1)
        self.assertEqual(len(review["blocking_follow_ups"]), 1)
        self.assertEqual(len(review["non_blocking_follow_ups"]), 1)
        self.assertEqual(len(run["follow_ups"]), 2)
        self.assertEqual(run["metrics"]["follow_ups"], 2)
        self.assertEqual(run["metrics"]["blocking_follow_ups"], 1)
        self.assertEqual(text.count("[non-blocking] Legacy cache files"), 1)
        self.assertEqual(text.count("[blocking: security]"), 1)

    def test_security_follow_up_blocks_and_is_marked(self):
        text, run, _path = self._run(
            "[security] Legacy session cookie is not HttpOnly")
        review = [item for item in run["review_rounds"]
                  if item.get("kind") == "review"][-1]
        self.assertGreaterEqual(review["blocking_defects"], 1)
        self.assertEqual(len(review["blocking_follow_ups"]), 1)
        self.assertTrue(run["follow_ups"][0]["blocking"])
        self.assertNotEqual(run["review_status"], "accepted")
        self.assertIn("[blocking: security]", text)


class ParseReviewFollowUpsTest(unittest.TestCase):
    def test_security_follow_up_is_blocking_and_counted(self):
        report = (
            "# Independent review\n\n## Score\n\nFinal quality score: 8.0/10\n\n"
            "## Blocking defects\n\nNone\n\n"
            "## Non-blocking defects\n\n- typo in README\n\n"
            + FOLLOW + "\n"
            "- [security] legacy auth token is put in the URL\n"
            "- old naming style in an untouched module\n\n"
            "## Fix list\n\n- fix typo\n\n"
            "Final quality score: 8.0/10\nBlocking defects: 0\n"
        )
        parsed = quality.parse_review(report)
        self.assertEqual(len(parsed["follow_ups"]), 2)
        self.assertEqual(len(parsed["blocking_follow_ups"]), 1)
        self.assertEqual(len(parsed["non_blocking_follow_ups"]), 1)
        self.assertGreaterEqual(parsed["blocking_defects"], 1)
        defects = " ".join(parsed["defects"])
        fixes = " ".join(parsed["fixes"])
        self.assertIn("token", defects)
        self.assertIn("token", fixes)
        self.assertNotIn("old naming style", defects)
        self.assertEqual(parsed["score"], 8.0)

    def test_follow_ups_only_do_not_block_or_invent_score(self):
        report = (FOLLOW + "\n"
                  "- old style in an untouched file\n\n"
                  "## Blocking defects\n\nNone\n\n"
                  "## Non-blocking defects\n\nNone\n")
        parsed = quality.parse_review(report)
        self.assertIsNone(parsed["score"])
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertEqual(parsed["blocking_follow_ups"], [])
        self.assertEqual(len(parsed["non_blocking_follow_ups"]), 1)
        self.assertFalse(any("score" in str(item).lower()
                             and "missing" in str(item).lower()
                             for item in parsed["defects"]))

    def test_report_without_follow_up_section_is_unchanged(self):
        report = ("## Blocking defects\n\n- broken build in app.py\n\n"
                  "## Non-blocking defects\n\n- typo\n\n"
                  "## Fix list\n\n- fix build\n")
        parsed = quality.parse_review(report)
        self.assertEqual(parsed["follow_ups"], [])
        self.assertEqual(parsed["blocking_follow_ups"], [])
        self.assertEqual(parsed["blocking_defects"], 1)
        self.assertEqual(parsed["defects"], ["broken build in app.py"])

    def test_review_prompt_has_heading_and_scope_rule(self):
        task = quality.build_review_task("build a thing", ["a.py"])
        self.assertIn("Follow-ups (pre-existing, non-blocking)", task)
        self.assertIn("security", task.lower())
        reprompt = quality.build_score_reprompt_task("## Blocking defects\nNone\n")
        self.assertIn("non-blocking follow-ups", reprompt.lower())
        self.assertIn("security", reprompt.lower())


class HistoryWasteNudgeTest(unittest.TestCase):
    def _run(self, ratio, subagents=6):
        run = {"kind": "harness", "task_type": "code", "subagents": subagents,
               "status": "ok", "quality_score": 9.0,
               "estimated_cost_usd": 0.5, "seconds": 100}
        if ratio is not None:
            run["waste_report"] = {"waste_ratio": ratio}
        return run

    def test_history_stats_waste_fields(self):
        stats = quality.history_stats(
            [self._run(0.5), self._run(0.25),
             {"kind": "harness", "task_type": "code", "subagents": 6,
              "metrics": {"waste_ratio": 0.0}}], "code")
        self.assertEqual(stats["waste_runs"], 3)
        self.assertAlmostEqual(stats["mean_waste"], 0.25, places=4)
        row = stats["groups"][0]
        self.assertEqual(row["waste_runs"], 3)
        self.assertAlmostEqual(row["mean_waste"], 0.25, places=4)

    def test_high_waste_nudges_down_by_two(self):
        self.assertEqual(quality.WASTE_NUDGE_THRESHOLD, 0.35)
        self.assertEqual(quality.WASTE_NUDGE_STEP, 2)
        prompt = ("Build a production python API server: implement app.py, "
                  "database migrations, authentication, tests and documentation.")
        base = quality.choose_agents(prompt)
        self.assertGreater(base["subagents"], 0)
        history = {"runs": 0, "best_count": None, "best": None, "groups": [],
                   "target": 9.0, "mean_waste": 0.5, "waste_runs": 3}
        with mock.patch.object(quality, "history_stats", return_value=history):
            chosen = quality.choose_agents(prompt)
            explicit = quality.choose_agents(prompt, explicit=6)
        if base["subagents"] > 2:
            self.assertEqual(chosen["subagents"], base["subagents"] - 2)
            self.assertIn(
                "Waste ratio 0.50 nudged the start -2 to %d."
                % chosen["subagents"], chosen["reason"])
        else:
            self.assertEqual(chosen["subagents"], base["subagents"])
        self.assertEqual(explicit["subagents"], 6)
        self.assertNotIn("Waste ratio", explicit["reason"])

    def test_low_or_absent_waste_does_not_nudge(self):
        prompt = "Build a python API server: implement app.py and tests."
        low = {"runs": 0, "best_count": None, "best": None, "groups": [],
               "target": 9.0, "mean_waste": 0.1, "waste_runs": 2}
        with mock.patch.object(quality, "history_stats", return_value=low):
            chosen = quality.choose_agents(prompt)
        self.assertNotIn("Waste ratio", chosen["reason"])


class BuildHarnessTaskTest(unittest.TestCase):
    def test_project_map_hint_and_team_artifacts(self):
        task = quality.build_harness_task(
            "do the work", 4, "reason", project_map_hint="MAP HINT BODY")
        self.assertIn("MAP HINT BODY", task)
        self.assertIn("SLICES.md", task)
        self.assertIn("Writes:", task)
        self.assertIn("WASTE_REPORT.md", task)
        self.assertIn("Agents launched", task)
        single = quality.build_harness_task("do the work", 0, "reason")
        self.assertNotIn("SLICES.md", single)
        self.assertNotIn("WASTE_REPORT.md", single)

    def _fake_pm(self, state):
        class FakePM(object):
            def __init__(self):
                self.calls = []

            def offer(self, workspace, state_dir):
                self.calls.append(("offer", workspace, state_dir))
                return {"state": state, "present": True,
                        "reused": state == "reuse",
                        "stale": state == "stale", "bytes": 12,
                        "prompt": "MAP PROMPT",
                        "summary": {"state": state, "present": True,
                                    "reused": state == "reuse",
                                    "stale": state == "stale",
                                    "bytes": 12, "fingerprint": "abc123",
                                    "version": 1}}

            def capture(self, workspace, state_dir):
                self.calls.append(("capture", workspace, state_dir))
                return {"present": True, "stored": True}

        return FakePM()

    def test_project_map_helper_stores_summary_only(self):
        for state in ("new", "reuse", "stale"):
            fake = self._fake_pm(state)
            run = {}
            with mock.patch.object(harness, "_project_map_module",
                                   return_value=fake):
                hint = harness._project_map_hint("/tmp/ws", "/tmp/state", run)
            self.assertEqual(hint, "MAP PROMPT")
            self.assertEqual(run["project_map"]["state"], state)
            self.assertNotIn("prompt", run["project_map"])
            expected = ["offer", "capture"] if state == "new" else ["offer"]
            self.assertEqual([item[0] for item in fake.calls], expected)


class CollectTeamArtifactsTest(unittest.TestCase):
    def test_metrics_and_follow_ups_from_last_review(self):
        fake_slices = types.SimpleNamespace(inspect=lambda workspace: {
            "present": True, "waves": 1, "slices": 2,
            "overlaps": [{"wave": 1, "file": "a.py",
                          "slices": ["B1", "B2"]}],
            "warnings": 1})
        fake_waste = types.SimpleNamespace(inspect=lambda workspace: {
            "present": True, "agents_launched": 4, "agents_used": 2,
            "agents_discarded": 2, "waste_ratio": 0.5, "source": "derived"})
        run = {"review_rounds": [
            {"kind": "revision", "round": 1},
            {"kind": "review", "round": 2, "blocking_defects": 0,
             "follow_ups": [{"text": "legacy thing", "blocking": False,
                             "kind": None}]},
        ]}
        with mock.patch.object(harness, "_team_modules",
                               return_value=(fake_slices, fake_waste)), \
                mock.patch.object(harness, "_project_map_module",
                                  return_value=None):
            metrics = harness._collect_team_artifacts(run, "/tmp/ws")
        self.assertEqual(run["slices"]["warnings"], 1)
        self.assertIn("a.py shared by B1, B2",
                      run["slices_overlap_warning"])
        self.assertIn("not blocking", run["slices_overlap_warning"])
        self.assertEqual(run["waste_report"]["waste_ratio"], 0.5)
        self.assertEqual(metrics["slices_present"], True)
        self.assertEqual(metrics["slices_overlap_warnings"], 1)
        self.assertEqual(metrics["waste_agents_launched"], 4)
        self.assertEqual(metrics["waste_agents_discarded"], 2)
        self.assertEqual(metrics["waste_ratio"], 0.5)
        self.assertEqual(metrics["follow_ups"], 1)
        self.assertEqual(metrics["blocking_follow_ups"], 0)
        self.assertEqual(run["follow_ups"][0]["text"], "legacy thing")

    def _fake_pm(self, digest, final_state="reuse"):
        class FakePM(object):
            def __init__(self):
                self.calls = []

            def map_digest(self, workspace):
                return digest

            def capture(self, workspace, state_dir):
                self.calls.append("capture")
                return {"stored": True}

            def offer(self, workspace, state_dir):
                self.calls.append("offer")
                return {"summary": {"state": final_state, "present": True,
                                    "reused": final_state == "reuse",
                                    "stale": final_state == "stale",
                                    "bytes": 10, "fingerprint": "abc",
                                    "map_sha256": digest, "version": 1}}

        return FakePM()

    def test_project_map_final_summary_is_recorded_after_refresh(self):
        fake_pm = self._fake_pm("newhash")
        run = {"project_map": {"state": "new", "present": True,
                               "map_sha256": "oldhash"}}
        with mock.patch.object(harness, "_team_modules",
                               return_value=(None, None)), \
                mock.patch.object(harness, "_project_map_module",
                                  return_value=fake_pm):
            harness._collect_team_artifacts(run, "/tmp/ws", "/tmp/state")
        self.assertEqual(fake_pm.calls, ["capture", "offer"])
        self.assertEqual(run["project_map"]["state"], "reuse")
        self.assertTrue(run["project_map"]["captured"])

    def test_unchanged_stale_map_is_not_reblessed(self):
        fake_pm = self._fake_pm("samehash", final_state="stale")
        run = {"project_map": {"state": "stale", "present": True,
                               "map_sha256": "samehash"}}
        with mock.patch.object(harness, "_team_modules",
                               return_value=(None, None)), \
                mock.patch.object(harness, "_project_map_module",
                                  return_value=fake_pm):
            harness._collect_team_artifacts(run, "/tmp/ws", "/tmp/state")
        self.assertEqual(fake_pm.calls, ["offer"])
        self.assertFalse(run["project_map"]["captured"])
        self.assertEqual(run["project_map"]["state"], "stale")


class ResultAndStoreTest(unittest.TestCase):
    def _run(self):
        return {
            "id": "run1", "created": "2026-01-01", "model": "m",
            "reasoning_effort": "high", "status": "ok", "kind": "harness",
            "seconds": 12, "subagents": 2, "output_files": ["a.py"],
            "prompt": "do the work",
            "answer": "done",
            "metrics": {"slices_present": True,
                        "slices_overlap_warnings": 1,
                        "waste_ratio": 0.5,
                        "waste_agents_launched": 4,
                        "waste_agents_discarded": 2,
                        "follow_ups": 7, "blocking_follow_ups": 1},
            "slices": {"overlaps": [{"wave": 1, "file": "a.py",
                                     "slices": ["B1", "B2"]}]},
            "follow_ups": [{"text": "old thing %d" % index,
                            "blocking": index == 0, "kind": None}
                           for index in range(7)],
        }

    def test_result_text_overlap_and_follow_up_list(self):
        text = harness._result_text(self._run(), "/tmp/run.md")
        self.assertIn("overlap", text.lower())
        self.assertIn("waste 0.50", text)
        self.assertIn("old thing 0", text)
        self.assertIn("old thing 4", text)
        self.assertNotIn("old thing 5", text)
        self.assertIn("+2 more", text)

    def test_store_md_prints_metrics_and_follow_up_counts(self):
        md = store._md(self._run())
        self.assertIn("- **Metrics:**", md)
        self.assertIn("overlap warning", md)
        self.assertIn("waste ratio 0.50", md)
        self.assertIn("- **Follow-ups:** 7 (1 blocking)", md)


if __name__ == "__main__":
    unittest.main()
