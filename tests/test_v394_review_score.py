"""3.9.4 regression tests: reviewer score re-prompt and ``score_missing``.

Run from the repo root::

    python3 -m unittest tests.test_v394_review_score -v

Deterministic fake-``dsh`` tests, no network.  Why each fails on 3.9.3:
3.9.3 has no re-prompt, no ``review_score_reprompts``/``score_missing`` and
fabricates ``0.0`` plus "Independent reviewer did not report a 0-10 score."
when the score line is missing; its parser also accepts unrelated ``3/10``
text and rejects the two-row table / negative case.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, quality, review_progress
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh
import tests

os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")

SYNTHETIC_DEFECT = "did not report a 0-10 score"


def _token_total(record):
    total = record.get("total_tokens")
    if total:
        return int(total)
    return sum(int(record.get(key) or 0) for key in
               ("tokens_in", "cache_read_tokens", "tokens_out"))


class ReviewScoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
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

    def _run(self, rounds=2, **fake_env):
        fake_env = dict(fake_env)
        # The fake writes a session log per process; this is what gives the
        # reviewer and the score re-prompt real (fake) token usage.
        fake_env.setdefault("FAKE_DSH_SESSIONS", str(self.sessions))
        with mock.patch.dict(os.environ, fake_env, clear=False):
            return harness.run_harness({
                "prompt": "Design a print poster with a QR code and five sections",
                "subagents": 2, "quality": True, "quality_rounds": rounds,
                "workspace": str(self.workspace),
            }, self.cfg())

    def _fake_state(self):
        candidates = []
        configured = os.environ.get("FAKE_DSH_STATE")
        if configured:
            candidates.append(Path(configured))
        candidates.append(self.workspace / ".fake-dsh-state.json")
        candidates.append(self.root / "fake-dsh-state.json")
        for path in candidates:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def test_missing_score_is_reprompted_once_and_accepted(self):
        _text, run, _path = self._run(
            rounds=2,
            FAKE_DSH_REVIEW_MISSING_SCORE="1",
            FAKE_DSH_REVIEW_REPROMPT="ok",
        )
        self.assertEqual(run["review_status"], "accepted")
        self.assertTrue(run["review_passed"])
        self.assertEqual(run["quality_score"], 9.2)
        self.assertEqual(run["quality_rounds_used"], 1)

        records = run.get("review_score_reprompts") or []
        self.assertEqual(len(records), 1, "exactly one re-prompt per report")
        record = records[0]
        self.assertEqual(record.get("status"), "ok")
        method = str(record.get("method") or "").lower()
        self.assertTrue("session" in method or "resume" in method,
                        "expected the same-session path, got %r" % (method,))
        self.assertGreater(_token_total(record), 0,
                           "re-prompt tokens must be recorded: %r" % (record,))

        entry = run["review_rounds"][-1]
        self.assertFalse(entry.get("score_missing"))
        self.assertTrue(entry.get("score_reprompt"))
        self.assertEqual(entry.get("score"), 9.2)

        state = self._fake_state()
        self.assertEqual(state.get("reprompt"), 1,
                         "the fake must see exactly one score re-prompt")
        self.assertFalse(state.get("revision"), "accepted run must not revise")

    def test_failed_reprompt_marks_score_missing_without_zero_or_revision(self):
        text, run, _path = self._run(
            rounds=2,
            FAKE_DSH_REVIEW_MISSING_SCORE="1",
            FAKE_DSH_REVIEW_REPROMPT="missing",
        )
        self.assertIsNone(run["quality_score"],
                          "a missing score is None, never a fabricated 0.0")
        self.assertFalse(run["review_passed"])
        self.assertEqual(run.get("status"), "ok_below_gate")
        self.assertEqual(run["review_status"], "stopped")
        self.assertTrue(run.get("review_score_missing"))
        self.assertEqual(run.get("review_unscored_rounds"), [1, 2])

        # No revision: the score-missing round goes straight to the next
        # review round, which re-reviews the same tree.
        self.assertEqual(run["quality_rounds_used"], 2)
        self.assertEqual(len(run["review_rounds"]), 2)
        self.assertFalse(self._fake_state().get("revision"),
                         "score_missing with no real blocking defect must not revise")

        last = run["review_rounds"][-1]
        self.assertTrue(last.get("score_missing"))
        self.assertEqual(last.get("score_status"), "score_missing")
        self.assertIsNone(last.get("score"))
        defects = " ".join(str(item) for item in (last.get("defects") or []))
        self.assertIn("fake non-blocking typo", defects,
                      "real reviewer defects must be preserved")
        self.assertNotIn(SYNTHETIC_DEFECT, defects)
        self.assertNotIn(SYNTHETIC_DEFECT, text)

        for name in ("REVIEW.partial.md", "REVIEW.partial.json"):
            path = self.workspace / name
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                self.assertNotIn(SYNTHETIC_DEFECT, content, name)
                self.assertNotIn("did not report", content, name)

        records = run.get("review_score_reprompts") or []
        self.assertEqual(len(records), 2, "each unscored round records its attempt")
        self.assertTrue(all(record.get("score") is None for record in records))

    def test_creative_visual_impact_caps_the_reprompted_score(self):
        _text, run, _path = self._run(
            rounds=1,
            FAKE_DSH_REVIEW_MISSING_SCORE="1",
            FAKE_DSH_REVIEW_VISUAL_IMPACT="6",
            FAKE_DSH_REVIEW_REPROMPT="ok",
        )
        self.assertEqual(run.get("task_type"), "creative")
        entry = run["review_rounds"][-1]
        # The re-prompt said 9.2/10, but the creative report's visual impact
        # 6/10 caps the score exactly as the normal scored-review path does.
        self.assertEqual(entry.get("score"), 7.5)
        self.assertTrue(entry.get("creative_cap"))
        self.assertEqual(entry.get("visual_impact"), 6.0)
        self.assertFalse(entry.get("accepted"))
        self.assertEqual(run["quality_score"], 7.5)
        self.assertFalse(run["review_passed"])

    def test_score_only_reprompt_keeps_report_blocking_count(self):
        _text, run, _path = self._run(
            rounds=1,
            FAKE_DSH_REVIEW_MISSING_SCORE="1",
            FAKE_DSH_REVIEW_MISSING_BLOCKING="1",
            FAKE_DSH_REVIEW_REPROMPT="score_only",
        )
        entry = run["review_rounds"][-1]
        self.assertEqual(entry.get("score"), 9.2)
        # The reply omitted "Blocking defects: N"; the report's real blocking
        # count survives instead of a fabricated zero.
        self.assertEqual(entry.get("blocking_defects"), 1)
        self.assertFalse(entry.get("accepted"))
        record = (run.get("review_score_reprompts") or [])[-1]
        self.assertIsNone(record.get("blocking"))
        self.assertFalse(record.get("contract_complete"))

    def test_extract_quality_score_variants_and_rejections(self):
        accepted = [
            ("Final quality score: 9/10", 9.0),
            ("**Final quality score:** 9 / 10", 9.0),
            ("The final quality score: 9/10", 9.0),
            ("## Score\n9.0/10", 9.0),
            ("## Score: 9/10", 9.0),
            ("## Score\nScore: 9/10", 9.0),
            ("| Final quality score | 8.8/10 |", 8.8),
            ("| Final quality score | 8.8/10 | Target: 9.0/10 |", 8.8),
            ("| Criterion | Score |\n| Quality | 8.5/10 |", 8.5),
            ("Overall score: 8.6/10", 8.6),
            ("## Summary\n9.0/10", 9.0),
            ("All checks done.\n\n9.0/10", 9.0),
        ]
        for value, expected in accepted:
            with self.subTest(value=value):
                self.assertEqual(
                    quality.extract_quality_score(None, value), expected)

        rejected = [
            "I checked 3/10 files and they all look fine.",
            "The body font is 9.0/10 pt.",
            "Target: 9.5/10",
            "Final quality score: -1/10",
            "Final quality score: 12/10",
            "Visual impact score: 6/10",
            "## Visual impact\n9.2/10",
            "## Target\n9.5/10",
            "## Threshold\n8.5/10",
            # Category sub-scores are not the official review score.
            "Test coverage score: 10/10",
            "Code quality score: 9/10",
            # Category/target/visual cells in a pipe row define the metric for
            # a score label that follows them.
            "| Visual impact | Score: 9/10 |",
            "| Target | Final quality score: 9.5/10 |",
            "| Test coverage | Score | 10/10 |",
            "| Code quality | Score: 9.0/10 |",
            "| Threshold | Score | 9.0/10 |",
            "| Score | Test coverage | 10/10 |",
            "## Test coverage score\n10/10",
            "## Code quality score: 9/10",
            "## Score\nPending\n\nTest coverage score: 10/10",
            "## Tests\n10/10",
            "## Fix list\n1. Fix it\n\n9.0/10",
            # Quoted/attributed or blockquoted scores are not the reviewer's
            # own closing statement.
            'The previous reviewer wrote "Final quality score: 9/10".',
            "> Final quality score: 9/10",
            "no score here",
        ]
        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(quality.extract_quality_score(None, value))

    def test_review_ending_in_visual_impact_is_not_an_official_score(self):
        report = (
            "# Independent review\n\n## Score\nPending.\n\n"
            "## Blocking defects\nNone\n\n"
            "## Visual impact\n9.2/10\n")
        parsed = quality.parse_review(report, "")
        self.assertIsNone(parsed["score"])
        self.assertIsNone(quality.extract_quality_score(None, report))

    def test_unrelated_category_score_does_not_score_the_review(self):
        report = (
            "# Independent review\n\n## Score\nPending.\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\n- Test coverage score: 10/10\n\n"
            "## Tests\n10/10\n")
        parsed = quality.parse_review(report, "")
        self.assertIsNone(parsed["score"])
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertIsNone(quality.extract_quality_score(None, report))

    def test_parenthesized_category_headings_and_labels_are_rejected(self):
        """A parenthetical category qualifier must not collapse to ``Score``.

        Regression for review round 2: stripping ``(...)`` unconditionally let
        ``## Score (test coverage)`` and ``| Quality (code quality) |`` be
        read as overall-score labels.
        """
        rejected = [
            "## Score (test coverage)\n10/10",
            "## Score (test coverage): 10/10",
            "## Quality (code quality)\n9/10",
            "## Final quality score (test coverage)\n9/10",
            "Final quality score (test coverage): 9/10",
            "| Score (test coverage) | 10/10 |",
            "| Quality (code quality) | 9/10 |",
            "| Final quality score (test coverage) | 9/10 |",
            "## Score\nPending\n\n| Score (test coverage) | 10/10 |",
        ]
        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(quality.extract_quality_score(None, value))
                self.assertIsNone(quality.parse_review(value, "")["score"])

    def test_explicit_overall_parenthetical_qualifiers_still_parse(self):
        accepted = [
            ("## Score (overall)\n9/10", 9.0),
            ("## Final quality score (out of 10)\n8.5/10", 8.5),
            ("| Final quality score (out of 10) | 9/10 |", 9.0),
            ("| Score (0-10) | 8/10 |", 8.0),
            ("Final quality score (out of 10): 9/10", 9.0),
            ("**Final quality score** (overall): 8.6/10", 8.6),
        ]
        for value, expected in accepted:
            with self.subTest(value=value):
                self.assertEqual(
                    quality.extract_quality_score(None, value), expected)

    def test_parenthesized_category_score_cannot_accept_unscored_report(self):
        """Complete report with only a category score stays unscored.

        Reproduces the round-2 evidence: ``## Score`` says ``Pending``, there
        are no blocking defects, and the report closes with
        ``## Score (test coverage)`` / ``10/10``.  The numeric/zero-blocking
        acceptance gate must not fire and the re-prompt path must remain.
        """
        report = (
            "# Independent review\n\n## Score\nPending\n\n"
            "## Blocking defects\nNone\n\n"
            "## Score (test coverage)\n10/10\n")
        parsed = quality.parse_review(report, "")
        self.assertIsNone(parsed["score"])
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertIsNone(quality.extract_quality_score(None, report))
        accepted = (parsed["score"] is not None
                    and parsed["score"] >= 8.5
                    and parsed["blocking_defects"] == 0)
        self.assertFalse(accepted, "a category score must not satisfy the gate")

    def test_parenthesized_category_report_is_reprompted_not_accepted(self):
        _text, run, _path = self._run(
            rounds=2,
            FAKE_DSH_REVIEW_CATEGORY_SCORE="1",
            FAKE_DSH_REVIEW_REPROMPT="ok",
        )
        self.assertEqual(self._fake_state().get("reprompt"), 1,
                         "the category score must not satisfy the accept gate")
        self.assertEqual(run["quality_score"], 9.2)
        self.assertTrue(run["review_passed"])

    def test_category_rows_in_pipe_tables_are_not_official_scores(self):
        """A category/target cell redefines the metric for a later score cell.

        Review round 1 evidence: ``| Visual impact | Score: 9/10 |`` parsed as
        9.0, ``| Target | Final quality score: 9.5/10 |`` as 9.5 and
        ``| Test coverage | Score | 10/10 |`` as 10.0; each could satisfy the
        numeric/zero-blocking acceptance gate.
        """
        rows = [
            "| Visual impact | Score: 9/10 |",
            "| Target | Final quality score: 9.5/10 |",
            "| Test coverage | Score | 10/10 |",
            "| Code quality | Score: 9.0/10 |",
            "| Threshold | Score | 9.0/10 |",
            "| Score | Test coverage | 10/10 |",
        ]
        for row in rows:
            with self.subTest(row=row):
                self.assertIsNone(quality.extract_quality_score(None, row))
                report = (
                    "# Independent review\n\n## Score\nPending\n\n"
                    "## Blocking defects\nNone\n\n## Score details\n\n"
                    + row + "\n")
                parsed = quality.parse_review(report, "")
                self.assertIsNone(parsed["score"])
                self.assertEqual(parsed["blocking_defects"], 0)
                accepted = (parsed["score"] is not None
                            and parsed["score"] >= 8.5
                            and parsed["blocking_defects"] == 0)
                self.assertFalse(
                    accepted, "category row satisfied the gate: %r" % (row,))

    def test_table_category_reports_are_reprompted_exactly_once(self):
        modes = ("visual", "target", "test_coverage", "code_quality")
        for mode in modes:
            with self.subTest(mode=mode):
                self.workspace = self.root / ("workspace-" + mode)
                self.workspace.mkdir()
                _text, run, _path = self._run(
                    rounds=2,
                    FAKE_DSH_REVIEW_TABLE_SCORE=mode,
                    FAKE_DSH_REVIEW_REPROMPT="ok",
                )
                self.assertEqual(
                    self._fake_state().get("reprompt"), 1,
                    "the %s row must not satisfy the accept gate" % mode)
                self.assertEqual(run["quality_score"], 9.2)
                self.assertTrue(run["review_passed"])
                self.assertEqual(run["quality_rounds_used"], 1)

    def test_reprompt_cost_is_merged_when_review_cost_is_known(self):
        entry = {
            "cost": 0.5, "cost_known": True,
            "tokens_in": 10, "cache_read_tokens": 0, "tokens_out": 2,
            "total_tokens": 12, "models": {"deepseek-flash": {
                "input": 10, "cache_read": 0, "output": 2}},
        }
        record = {
            "estimated_cost_usd": 0.1, "cost_known": True,
            "tokens_in": 3, "cache_read_tokens": 0, "tokens_out": 1,
            "total_tokens": 4, "models": {"deepseek-flash": {
                "input": 3, "cache_read": 0, "output": 1}},
        }
        merged = harness._merge_reprompt_usage(dict(entry), dict(record))
        self.assertAlmostEqual(merged["cost"], 0.6, places=6)
        self.assertTrue(merged["cost_known"])
        self.assertEqual(merged["tokens_in"], 13)

    def test_legacy_synthetic_defect_is_not_counted_or_carried(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
            "## Blocking defects\n"
            "- Independent reviewer did not report a 0-10 score.\n\n"
            "## Fix list\nNone\n")
        parsed = quality.parse_review(report, "")
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertEqual(parsed["score"], 9.0)
        self.assertFalse(any(SYNTHETIC_DEFECT in item
                             for item in parsed["defects"]))
        merged, carried = review_progress.merge_review(
            "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
            "## Blocking defects\nNone\n\n## Fix list\nNone\n",
            {"defects": [{"text": "Independent reviewer did not report a "
                                  "0-10 score.",
                          "severity": "blocking"}]})
        self.assertEqual(carried, [])
        self.assertNotIn("did not report a 0-10 score", merged)

    def test_revision_task_score_missing_uses_unscored_wording(self):
        task = quality.build_revision_task(
            "Design a poster", ["poster.pdf"], ["Move the footer up"],
            ["Footer overlaps at 2cm"],
            skill={"title": "Poster", "content": "- [ ] check"},
            score_missing=True,
        )
        lower = task.lower()
        self.assertIn("unscored review", lower)
        self.assertIn("not a 0/10", lower)
        self.assertIn("do not invent", lower)
        self.assertIn("Footer overlaps at 2cm", task)
        self.assertNotIn("Re-encode the QR", task)
        self.assertNotIn(SYNTHETIC_DEFECT, task)
        self.assertIn("REVISION agent", task)


class RepromptOrchestrationTests(unittest.TestCase):
    """The fallback counts are pure orchestration; no real DSH is launched."""

    def _call(self, review, executions):
        with mock.patch.object(harness, "_execute_once",
                               side_effect=executions) as execute:
            record = harness._reprompt_review_score(
                {}, "/tmp/unused-workspace", review, "report text",
                round_no=1, policy="read-only", transport_name="headless",
                budget=None, used_logs=set())
        return record, execute

    def test_no_session_makes_exactly_one_fresh_call(self):
        record, execute = self._call(
            {"status": "ok"},
            [{"status": "ok", "answer": "Final quality score: 8.6/10\n"
                                        "Blocking defects: 0",
              "seconds": 0.4, "tokens_in": 11, "tokens_out": 4,
              "models": {"deepseek-flash": {"input": 11, "cache_read": 0,
                                            "output": 4}},
              "estimated_cost_usd": 0.0001, "cost_known": True}])
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(record["method"], "fresh")
        self.assertEqual(record["score"], 8.6)
        self.assertEqual(record["blocking"], 0)
        self.assertEqual(record["tokens_in"], 11)

    def test_dead_session_falls_back_once_and_merges_usage(self):
        record, execute = self._call(
            {"status": "ok", "transport_session_id": "old-session"},
            [{"status": "error", "error": "session not found",
              "stop_reason": "error", "seconds": 0.2, "tokens_in": 3,
              "session_logs": []},
             {"status": "ok", "answer": "Final quality score: 9.1/10\n"
                                        "Blocking defects: 0",
              "seconds": 0.5, "tokens_in": 7, "tokens_out": 2,
              "models": {"deepseek-flash": {"input": 7, "cache_read": 0,
                                            "output": 2}},
              "estimated_cost_usd": 0.0002, "cost_known": True,
              "session_logs": []}])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(record["method"], "fresh_after_dead_session")
        self.assertTrue(record["fallback_used"])
        self.assertEqual(record["score"], 9.1)
        self.assertEqual(record["tokens_in"], 10)
        self.assertIn("session not found", record["session_error"])

    def test_fresh_followup_prompt_asks_for_a_consistent_score(self):
        report = ("# Independent review\n\n## Score\nPending.\n\n"
                  "## Blocking defects\nNone\n")
        task = quality.build_score_reprompt_task(report, fresh=True)
        lower = task.lower()
        self.assertIn("score its own findings support", lower)
        self.assertIn("consistent", lower)
        self.assertIn("derive", lower)
        # The fallback exists because no score parsed; telling it to answer
        # "unknown" would defeat the recovery path.
        self.assertNotIn("unknown", lower)
        self.assertNotIn("existing score", lower)

    def test_no_session_missing_score_stays_unscored(self):
        record, execute = self._call(
            {"status": "ok"},
            [{"status": "ok", "answer": "Score unavailable. No parsable score.",
              "seconds": 0.3, "tokens_in": 5, "tokens_out": 2,
              "models": {"deepseek-flash": {"input": 5, "cache_read": 0,
                                            "output": 2}},
              "estimated_cost_usd": 0.0001, "cost_known": True}])
        self.assertEqual(execute.call_count, 1)
        self.assertIsNone(record["score"])
        self.assertIsNone(record["blocking"])
        self.assertFalse(record["contract_complete"])
        task = execute.call_args[0][2]
        self.assertNotIn("unknown", task.lower())

    def test_score_only_reply_does_not_fabricate_zero(self):
        score, blocking = harness._reprompt_reply_fields(
            "Final quality score: 9.2/10")
        self.assertEqual(score, 9.2)
        self.assertIsNone(blocking)
        record = harness._reprompt_record(
            [{"status": "ok", "answer": "Final quality score: 9.2/10",
              "seconds": 0.1, "tokens_in": 1, "tokens_out": 1}],
            "fresh", None)
        self.assertIsNone(record["blocking"])
        self.assertFalse(record["contract_complete"])


class ResumePrefixTests(unittest.TestCase):
    def test_unscored_review_with_real_blocking_resumes_the_revision(self):
        prior = {1: {"kind": "review", "round": 1, "score": None,
                     "status": "ok", "score_missing": True,
                     "blocking_defects": 2}}
        self.assertEqual(harness._review_resume_start(2, prior, {}),
                         (1, 1, "disabled"))

    def test_unscored_review_without_blocking_is_a_consumed_round(self):
        prior = {1: {"kind": "review", "round": 1, "score": None,
                     "status": "ok", "score_missing": True,
                     "blocking_defects": 0}}
        self.assertEqual(harness._review_resume_start(3, prior, {}),
                         (2, None, "disabled"))

    def test_scored_review_without_revision_still_resumes_the_revision(self):
        prior = {1: {"kind": "review", "round": 1, "score": 7.0,
                     "status": "ok", "blocking_defects": 1}}
        self.assertEqual(harness._review_resume_start(3, prior, {}),
                         (1, 1, "disabled"))

    def test_all_rounds_consumed_stops(self):
        prior = {
            1: {"kind": "review", "round": 1, "score": 9.0, "status": "ok",
                "blocking_defects": 0},
            2: {"kind": "review", "round": 2, "score": 9.0, "status": "ok",
                "blocking_defects": 0},
        }
        self.assertEqual(harness._review_resume_start(2, prior, {1: {}, 2: {}}),
                         (3, None, "stopped"))


if __name__ == "__main__":
    unittest.main()
