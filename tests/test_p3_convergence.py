"""P3-2 review convergence and one-question handoff tests."""

import os
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import config, quality
from deepseek_mcp.checkpoints import Checkpoint


def review(round_no, defects, score=7.0, blocking=1, kind="review", **extra):
    entry = {"kind": kind, "round": round_no, "score": score,
             "blocking_defects": blocking, "defects": list(defects)}
    entry.update(extra)
    return entry


def revision(round_no):
    return {"kind": "revision", "round": round_no, "status": "done"}


class ConvergenceTest(unittest.TestCase):
    def test_identical_signature_stops_at_round_two(self):
        rounds = [review(1, ["Missing required fact in report.md"]),
                  review(2, ["Missing required fact in report.md"])]
        result = quality.convergence_decision(rounds, mode="auto")
        self.assertEqual(result["action"], "stop")
        self.assertEqual(result["options"],
                         ["resume_with_scope", "accept_below_gate", "stop"])
        self.assertEqual(result["question"].count("?"), 1)
        self.assertIn("report.md", result["question"])
        self.assertIn("missing", result["question"].lower())
        self.assertEqual(len(result["signatures"]), 1)
        self.assertEqual(len(result["signatures"][0]), 64)

    def test_distinct_signatures_continue(self):
        rounds = [review(1, ["Missing required fact in report.md"]),
                  review(2, ["Broken QR code in poster.png"])]
        result = quality.convergence_decision(rounds, mode="auto")
        self.assertEqual(result["action"], "continue")
        self.assertEqual(result["options"], [])
        self.assertEqual(result["question"], "")

    def test_cosmetic_only_stops_after_one_revision(self):
        rounds = [review(1, ["Missing required fact in report.md"]),
                  revision(1),
                  review(2, ["Spacing and alignment polish in report.md"],
                         score=8.6)]
        result = quality.convergence_decision(rounds, mode="auto",
                                              target=8.5, tolerance=0.5)
        self.assertEqual(result["action"], "stop")
        self.assertTrue(result["cosmetic_only"])
        self.assertEqual(len(result["options"]), 3)
        self.assertEqual(result["question"].count("?"), 1)

    def test_substantive_stays(self):
        rounds = [review(1, ["Broken QR code in poster.png"]),
                  revision(1),
                  review(2, ["Font size too small and contrast too low"],
                         score=8.6)]
        result = quality.convergence_decision(rounds, mode="auto")
        self.assertEqual(result["action"], "continue")
        self.assertFalse(result["cosmetic_only"])

    def test_question_is_pure_and_not_duplicated(self):
        rounds = [review(1, ["Missing required fact in report.md"]),
                  review(2, ["Missing required fact in report.md"])]
        first = quality.convergence_decision(rounds, mode="auto")
        second = quality.convergence_decision(rounds, mode="auto")
        self.assertEqual(first, second)
        self.assertEqual(first["question"].count("?"), 1)

    def test_observe_records_observe_stop_without_enforcing(self):
        rounds = [review(1, ["Missing required fact in report.md"]),
                  review(2, ["Missing required fact in report.md"])]
        result = quality.convergence_decision(rounds, mode="observe")
        self.assertEqual(result["action"], "observe_stop")
        self.assertEqual(len(result["options"]), 3)

    def test_old_records_without_signatures_still_compute(self):
        rounds = [{"kind": "review", "round": 1, "score": 7.0,
                   "blocking_defects": 1,
                   "defects": ["Missing required fact in report.md"]},
                  {"kind": "review", "round": 2, "score": 7.0,
                   "blocking_defects": 1,
                   "defects": ["Missing required fact in report.md"]}]
        repeated = quality.repeated_signatures(rounds)
        self.assertEqual(len(repeated), 1)
        decision = quality.convergence_decision(rounds, mode="auto")
        self.assertEqual(decision["action"], "stop")
        self.assertIsNotNone(decision["first_seen_round"])

    def test_persist_signatures_and_checkpoint_round_trip(self):
        rounds = [review(1, ["Missing required fact in report.md"]),
                  review(2, ["Missing required fact in report.md"])]
        quality.persist_signatures(rounds, rounds[-1])
        self.assertTrue(rounds[-1]["signatures"])
        self.assertEqual(rounds[-1]["first_seen_round"],
                         {rounds[-1]["signatures"][0]: 1})
        base = os.environ.get("TMPDIR") or os.getcwd()
        try:
            tmpdir = tempfile.TemporaryDirectory(prefix="p3-conv-", dir=base)
        except OSError:
            tmpdir = tempfile.TemporaryDirectory(prefix="p3-conv-", dir=os.getcwd())
        with tmpdir as tmp:
            checkpoint = Checkpoint(str(Path(tmp) / "checkpoints"), "run-p3")
            checkpoint.create(["build"])
            checkpoint.update(review_rounds=rounds)
            loaded = checkpoint.load()
            self.assertIsNotNone(loaded)
            saved = loaded["review_rounds"][-1]
            self.assertEqual(saved["signatures"], rounds[-1]["signatures"])
            self.assertEqual(saved["first_seen_round"],
                             rounds[-1]["first_seen_round"])

    def test_normalise_and_classification(self):
        self.assertEqual(quality.normalise_defect("-  Spacing   POLISH!!"),
                         "spacing polish")
        self.assertEqual(quality.defect_class("cosmetic spacing issue"),
                         "cosmetic")
        self.assertEqual(quality.defect_class("font size too small"),
                         "substantive")
        self.assertEqual(quality.defect_class("missing required fact"),
                         "substantive")

    def test_revision_prompt_suppresses_unrelated_non_blocking_fixes(self):
        task = quality.build_revision_task(
            "brief", ["report.md"],
            ["fix the missing fact in report.md",
             "polish the logo.png colours"],
            ["Missing required fact in report.md"])
        self.assertIn("report.md", task)
        self.assertNotIn("logo.png", task)

    def test_config_default_and_enum(self):
        self.assertEqual(config.DEFAULTS["quality_convergence"], "observe")
        self.assertEqual(config.ENUM_KEYS["quality_convergence"],
                         ("observe", "auto"))
        self.assertIn("DSH_QUALITY_CONVERGENCE", config.ENV_MAP)
        self.assertIn("DEEPSEEK_QUALITY_CONVERGENCE", config.ENV_MAP)
        self.assertEqual(config._coerce("quality_convergence", "AUTO"), "auto")
        with self.assertRaises(ValueError):
            config._coerce("quality_convergence", "bogus")


if __name__ == "__main__":
    unittest.main()
