"""Tests for the 3.9.5 score report command (synthetic run JSON only)."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from deepseek_mcp import score_report


PRACTICES = {"enabled": True, "version": "3.9.5-practices"}


def make_runs():
    """Synthetic run records with known scores, groups and defects."""
    return {
        # Explicit first score wins over the review-round score; accepted.
        "run-a.json": {
            "first_review_score": 7.0,
            "quality_score": 9.0,
            "builder_practices": dict(PRACTICES),
            "review_rounds": [
                {"kind": "review", "score": 9.0, "blocking_defects": []}
            ],
        },
        # No explicit first score: first review round is the fallback; accepted.
        "run-b.json": {
            "quality_score": 8.5,
            "builder_practices": dict(PRACTICES),
            "review_rounds": [
                {"kind": "revision", "score": 8.0, "blocking_defects": []},
                {"kind": "review", "score": 8.0, "blocking_defects": []},
                {"kind": "review", "score": 8.5, "blocking_defects": []},
            ],
        },
        # Disabled practices; rejected on score and blocking defects.
        "run-c.json": {
            "quality_score": 7.0,
            "builder_practices": {"enabled": False, "version": "ignored"},
            "review_rounds": [
                {"kind": "review", "score": 6.0, "blocking_defects": ["fails"]}
            ],
        },
        # Pre-3.9.5 record: good score but the final review has a defect.
        "run-d.json": {
            "quality_score": 9.0,
            "review_rounds": [
                {"kind": "review", "score": 8.0, "blocking_defects": ["needs work"]}
            ],
        },
        "broken.json": "{this is not json",
        "list.json": [1, 2, 3],
        "notes.txt": "not a run record",
    }


class ScoreReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_dir = self._tmp.name
        for name, payload in make_runs().items():
            path = os.path.join(self.runs_dir, name)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

    # -- first_score -----------------------------------------------------

    def test_explicit_first_review_score_wins(self):
        run = make_runs()["run-a.json"]
        self.assertEqual(score_report.first_score(run), 7.0)

    def test_first_score_falls_back_to_first_review_round(self):
        run = make_runs()["run-b.json"]
        self.assertEqual(score_report.first_score(run), 8.0)

    def test_first_score_ignores_revision_and_missing_scores(self):
        run = {
            "review_rounds": [
                {"kind": "revision", "score": 9.5},
                {"kind": "review", "score": None},
                {"kind": "review", "score": "8.25"},
            ]
        }
        self.assertEqual(score_report.first_score(run), 8.25)
        self.assertIsNone(score_report.first_score({"review_rounds": []}))
        self.assertIsNone(score_report.first_score({}))

    # -- grouping --------------------------------------------------------

    def test_run_group_by_version_disabled_and_absent(self):
        self.assertEqual(score_report.run_group({"builder_practices": PRACTICES}),
                         "3.9.5-practices")
        self.assertEqual(
            score_report.run_group(
                {"builder_practices": {"enabled": False, "version": "x"}}
            ),
            "disabled",
        )
        self.assertEqual(score_report.run_group({}), "pre-3.9.5")

    # -- summarise -------------------------------------------------------

    def test_summarise_means_and_accepted(self):
        rows = score_report.summarise(self.runs_dir)
        by_group = {row["group"]: row for row in rows}

        # Sorted groups; malformed/non-dict JSON does not count as a run.
        self.assertEqual(list(by_group), ["3.9.5-practices", "disabled", "pre-3.9.5"])

        practices = by_group["3.9.5-practices"]
        self.assertEqual(practices["runs"], 2)
        self.assertAlmostEqual(practices["mean_first"], 7.5)
        self.assertAlmostEqual(practices["mean_final"], 8.75)
        self.assertAlmostEqual(practices["mean_delta"], 1.25)
        self.assertEqual(practices["accepted"], 2)

        disabled = by_group["disabled"]
        self.assertEqual(disabled["runs"], 1)
        self.assertAlmostEqual(disabled["mean_first"], 6.0)
        self.assertAlmostEqual(disabled["mean_final"], 7.0)
        self.assertAlmostEqual(disabled["mean_delta"], 1.0)
        self.assertEqual(disabled["accepted"], 0)

        legacy = by_group["pre-3.9.5"]
        self.assertEqual(legacy["runs"], 1)
        # Score is high enough, but the final review has a blocking defect.
        self.assertEqual(legacy["accepted"], 0)

    def test_accepted_uses_final_review_not_earlier_blocking_rounds(self):
        # A revision is supposed to clear earlier blocking findings. Acceptance
        # is the final gate: final score >= 8.5 and final review has no blocker.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "run.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "quality_score": 8.6,
                    "builder_practices": {"enabled": True, "version": "1.0"},
                    "review_rounds": [
                        {"kind": "review", "score": 6.0,
                         "blocking_defects": ["first pass failed"]},
                        {"kind": "revision", "score": 7.0,
                         "blocking_defects": ["fixed later"]},
                        {"kind": "review", "score": 8.6,
                         "blocking_defects": []},
                    ],
                }, handle)
            rows = score_report.summarise(tmp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runs"], 1)
        self.assertEqual(rows[0]["mean_first"], 6.0)
        self.assertEqual(rows[0]["accepted"], 1)

    def test_summarise_skips_malformed_and_non_object_json(self):
        rows = score_report.summarise(self.runs_dir)
        self.assertEqual(sum(row["runs"] for row in rows), 4)

    def test_summarise_missing_dir_returns_empty(self):
        missing = os.path.join(self.runs_dir, "missing")
        self.assertEqual(score_report.summarise(missing), [])

    # -- table -----------------------------------------------------------

    def test_format_table_includes_headers_and_all_groups(self):
        table = score_report.format_table(score_report.summarise(self.runs_dir))
        for header in score_report.HEADERS:
            self.assertIn(header, table)
        for group in ("3.9.5-practices", "disabled", "pre-3.9.5"):
            self.assertIn(group, table)
        self.assertIn("1.25", table)

    # -- CLI -------------------------------------------------------------

    def test_main_json_output_is_parseable(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = score_report.main(["--runs", self.runs_dir, "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(buffer.getvalue())
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["group"], "3.9.5-practices")

    def test_main_text_output_has_table(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = score_report.main(["--runs", self.runs_dir])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("mean_first", output)
        self.assertIn("pre-3.9.5", output)

    def test_main_empty_dir_reports_no_runs(self):
        empty = os.path.join(self.runs_dir, "empty")
        os.mkdir(empty)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = score_report.main(["--runs", empty])
        self.assertEqual(code, 0)
        self.assertIn("No completed runs found", buffer.getvalue())

    def test_main_missing_dir_returns_one(self):
        missing = os.path.join(self.runs_dir, "does-not-exist")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = score_report.main(["--runs", missing])
        self.assertEqual(code, 1)
        self.assertIn("not found", buffer.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
