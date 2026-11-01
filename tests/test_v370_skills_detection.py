"""v3.7.0 skill detection, confidence and registry-aware lookup tests."""

import unittest

from deepseek_mcp import skills
from deepseek_mcp.skill_registry import SkillRegistry
from tests import v370_support as support

VECTORS = [
    ("Design a print poster with a QR code", "poster", 0.875),
    ("Write a cover letter for a job", "document", 0.713),
    ("Build a responsive HTML web page", "web", 0.792),
    ("make a poster", "poster", 0.625),
    ("what is data?", None, 0.562),
    ("poster or web?", None, 0.562),
    ("code and data analysis", None, 0.375),
    ("write a python cli with tests", "code", 0.688),
]


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-detect-")
        self.addCleanup(self.tmp.cleanup)
        self.registry = SkillRegistry(self.cfg)
        self.registry.load()

    def test_eight_worked_vectors_are_exact(self):
        for prompt, expected, confidence in VECTORS:
            result = skills.detect_skill_match(prompt, self.cfg)
            self.assertEqual(result["key"], expected, prompt)
            self.assertEqual(result["confidence"], confidence, prompt)
            self.assertEqual(result["unmatched"], expected is None, prompt)
            self.assertEqual(result["scores"],
                             self._expected_scores(prompt), prompt)

    def _expected_scores(self, prompt):
        # The eight pinned score maps from PLAN section 5.3.
        return {
            "Design a print poster with a QR code": {"poster": 8, "code": 2},
            "Write a cover letter for a job": {"poster": 1, "document": 5},
            "Build a responsive HTML web page": {"web": 6, "code": 1},
            "make a poster": {"poster": 2},
            "what is data?": {"data": 1},
            "poster or web?": {"poster": 1},
            "code and data analysis": {"data": 2, "code": 1},
            "write a python cli with tests": {"code": 3},
        }[prompt]

    def test_result_has_the_seventeen_documented_keys(self):
        result = skills.detect_skill_match("make a poster", self.cfg)
        for key in ("key", "status", "approved", "reason", "confidence",
                    "raw_score", "second_score", "margin", "runner_up",
                    "scores", "signals", "candidates", "matched_terms",
                    "threshold", "unmatched", "prompt_chars", "detail"):
            self.assertIn(key, result)
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["approved"])
        self.assertEqual(result["threshold"], 0.55)

    def test_legacy_detector_is_frozen_and_disagrees_on_purpose(self):
        cases = {
            "Design a print poster with a QR code": "poster",
            "Write a cover letter for a job": "document",
            "Build a responsive HTML web page for a cafe": "web",
            "Write a Python CLI with tests and a build step": "code",
            "Analyze a CSV dataset and chart the metrics": "data",
        }
        for prompt, expected in cases.items():
            self.assertEqual(skills.detect_skill(prompt), expected)
        self.assertIsNone(skills.detect_skill("What is 2+2?"))
        self.assertEqual(skills.detect_skill("Write a report about the data"),
                         "document")
        self.assertEqual(
            skills.detect_skill_match("Write a report about the data",
                                      self.cfg)["status"], "unmatched")

    def test_candidates_and_ties_are_deterministic(self):
        result = skills.detect_skill_match("code and data analysis", self.cfg)
        self.assertEqual([item["key"] for item in result["candidates"]],
                         ["data", "code"])
        self.assertLessEqual(len(result["candidates"]), 3)

    def test_approved_candidate_is_selectable_unapproved_is_not(self):
        data = self.registry.load()
        entry = dict(support.valid_draft(key="podcast-launch"))
        entry.pop("evidence", None)
        entry.update({"status": "candidate", "source": "generated",
                      "approved": True, "version": 1,
                      "created": "2026-01-01T00:00:00+00:00",
                      "updated": "2026-01-01T00:00:00+00:00",
                      "content_sha256": "", "stats": {}, "review": {},
                      "lessons": {}, "refresh": {}, "history": [],
                      "merged_into": None, "retired_reason": None})
        data["skills"]["podcast-launch"] = entry
        self.registry.save(data)
        prompt = "Plan a podcast launch with cover art and show notes"
        self.assertEqual(skills.detect_skill_match(prompt, self.cfg)["key"],
                         "podcast-launch")
        data = self.registry.load()
        data["skills"]["podcast-launch"]["approved"] = False
        self.registry.save(data)
        result = skills.detect_skill_match(prompt, self.cfg)
        self.assertIsNone(result["key"])
        self.assertTrue(result["unmatched"])

    def test_demoted_and_retired_are_never_injected(self):
        prompt = "Design a print poster with a QR code"
        for status in ("demoted", "retired"):
            data = self.registry.load()
            data["skills"]["poster"]["status"] = status
            self.registry.save(data)
            # Another skill may still match; the excluded poster must not.
            self.assertNotEqual(
                skills.detect_skill_match(prompt, self.cfg)["key"], "poster")
            self.assertNotIn("poster",
                             skills.detect_skill_match(prompt, self.cfg)["scores"])
            data = self.registry.load()
            data["skills"]["poster"]["status"] = "seed"
            self.registry.save(data)

    def test_cfg_none_never_touches_the_registry(self):
        before = self.registry.load()["revision"]
        self.assertEqual(skills.detect_skill("make a poster"), "poster")
        self.assertIsNone(skills.normalise_skill_name("podcast-launch"))
        self.assertEqual(self.registry.load()["revision"], before)

    def test_off_mode_uses_only_the_five_seeds(self):
        self.env["DSH_AUTO_SKILLS"] = "off"
        from deepseek_mcp.config import load_config
        cfg = load_config(config_path=self.root / "config.json", env=self.env)
        self.assertEqual(skills.detect_skill("make a poster", cfg), "poster")
        self.assertEqual(skills.list_skills(cfg)[0]["status"], "seed")

    def test_unknown_skill_name_is_none(self):
        self.assertIsNone(skills.normalise_skill_name("not-a-skill", self.cfg))
        self.assertIsNone(skills.skill_meta("not-a-skill", self.cfg))


if __name__ == "__main__":
    unittest.main()
