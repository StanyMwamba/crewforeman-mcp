"""v3.4.0: creative work is reviewed for visual impact, not only correctness."""

import unittest

from deepseek_mcp import quality


class CreativeBuilderContractTests(unittest.TestCase):
    def test_creative_builder_gets_visual_contract(self):
        task = quality.build_harness_task(
            "Design a premium recruitment ad", 6, "chosen", quality=False,
            task_type="creative")
        for phrase in ("Visual-impact contract", "2-3 genuinely distinct directions",
                       "focal point", "default-template", "read_image at phone scale",
                       "would this stop a scroll", "Visual impact: X/10"):
            self.assertIn(phrase, task, phrase)

    def test_non_creative_builder_does_not_get_the_contract(self):
        task = quality.build_harness_task(
            "Build a Python CLI", 4, "chosen", quality=False, task_type="code")
        self.assertNotIn("Visual-impact contract", task)
        self.assertNotIn("would this stop a scroll", task)

    def test_render_hint_is_optional_and_names_the_probe(self):
        with_hint = quality.build_harness_task(
            "Design a poster", 6, "chosen", quality=False,
            render_hint="probe passed: use /Applications/fake-chrome")
        without = quality.build_harness_task("Design a poster", 6, "chosen",
                                             quality=False)
        self.assertIn("Render environment (connector probe)", with_hint)
        self.assertIn("/Applications/fake-chrome", with_hint)
        self.assertNotIn("Render environment (connector probe)", without)


class CreativeReviewContractTests(unittest.TestCase):
    def test_review_scores_visual_impact_and_caps_plain_results(self):
        task = quality.build_review_task(
            "Design a premium recruitment ad", ["ad.png"], task_type="creative")
        for phrase in ("visual impact is scored", "30%", "7.5/10 or lower",
                       "visually plain", "Visual impact: X/10", "Overall: X/10",
                       "Final quality score: X/10", "strongest and weakest visual"):
            self.assertIn(phrase, task, phrase)

    def test_code_review_is_not_forced_to_visual_impact(self):
        task = quality.build_review_task(
            "Build a Python CLI", ["cli.py"], task_type="code")
        self.assertNotIn("visually plain", task)
        self.assertNotIn("30% of the overall", task)

    def test_revision_changes_composition_not_only_copy(self):
        task = quality.build_revision_task(
            "Design a poster", ["poster.png"], ["plain layout"], ["plain layout"],
            task_type="creative")
        self.assertIn("Creative revision", task)
        self.assertIn("change composition", task)
        self.assertIn("not only rewrite copy", task)

    def test_plain_high_final_score_is_capped_in_code(self):
        report = (
            "# Independent review\n\n"
            "## Score\n"
            "Visual impact: 5.0/10\n"
            "Overall: 8.8/10\n"
            "Final quality score: 8.8/10\n\n"
            "## Blocking defects\nNone\n")
        parsed = quality.parse_review(report, "", task_type="creative")
        self.assertEqual(parsed["score"], 7.5)
        self.assertTrue(parsed["creative_cap"])
        self.assertEqual(parsed["visual_impact"], 5.0)
        self.assertTrue(any("visually plain" in item for item in parsed["defects"]))
        self.assertEqual(quality.parse_review(report, "", task_type="code")["score"], 8.8)

    def test_good_visual_score_keeps_the_final_score(self):
        report = ("Visual impact: 9.0/10\nOverall: 8.8/10\n"
                  "Final quality score: 8.8/10\n## Blocking defects\nNone\n")
        parsed = quality.parse_review(report, "", task_type="creative")
        self.assertEqual(parsed["score"], 8.8)
        self.assertFalse(parsed["creative_cap"])

    def test_parser_prefers_final_score_over_visual_score(self):
        report = (
            "# Independent review\n\n"
            "## Score\n"
            "Visual impact: 5.0/10\n"
            "Overall: 7.2/10\n"
            "Final quality score: 7.2/10\n\n"
            "## Blocking defects\nNone\n")
        parsed = quality.parse_review(report, "")
        self.assertEqual(parsed["score"], 7.2)
        self.assertEqual(parsed["blocking_defects"], 0)

    def test_negated_plain_sentence_does_not_trigger_the_cap(self):
        report = (
            "Visual impact: 9.0/10\nOverall: 8.8/10\n"
            "Final quality score: 8.8/10\n\n"
            "The result is not visually plain; it has a clear focal point.\n\n"
            "## Blocking defects\nNone\n")
        parsed = quality.parse_review(report, "", task_type="creative")
        self.assertEqual(parsed["score"], 8.8)
        self.assertFalse(parsed["creative_cap"])
        self.assertEqual(parsed["defects"], [])

    def test_positive_plain_defect_bullet_caps_even_with_high_visual_score(self):
        report = (
            "Visual impact: 9.0/10\nOverall: 8.8/10\n"
            "Final quality score: 8.8/10\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\n- Visually plain: default-template layout\n")
        parsed = quality.parse_review(report, "", task_type="creative")
        self.assertEqual(parsed["score"], 7.5)
        self.assertTrue(parsed["creative_cap"])

    def test_positive_plain_sentence_caps(self):
        report = (
            "Visual impact: 9.0/10\nOverall: 8.8/10\n"
            "Final quality score: 8.8/10\n\n"
            "The ad looks visually plain and generic.\n\n"
            "## Blocking defects\nNone\n")
        parsed = quality.parse_review(report, "", task_type="creative")
        self.assertEqual(parsed["score"], 7.5)
        self.assertTrue(parsed["creative_cap"])


if __name__ == "__main__":
    unittest.main()
