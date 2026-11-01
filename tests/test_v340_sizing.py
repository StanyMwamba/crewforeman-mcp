"""v3.4.0: declared-deliverable counting and history-matched team sizing."""

import unittest

from deepseek_mcp import quality


VIP = """Create a launch ad for the fictional Trailhead 2 tent from trailhead-ad.jpg
into an advanced, professional, clean social-media ad ready to publish, in French.
The current one is cluttered. Keep it premium, calm and trustworthy: one strong
headline, clear hierarchy, generous spacing, max 2-3 brand colours (moss + warm
accent), excellent readability on a phone.

Deliverables: ad.html (self-contained, fonts from Google Fonts or system, no
external images except files in this folder) and ad.png rendered at exactly
1080x1350 px (Facebook/Instagram portrait). Also ad-story.png at 1080x1920 if
time allows. Render with headless Chrome using a hard timeout and a fresh
--user-data-dir.

Use EXACTLY these fictional facts, with the same French wording and numbers (do
not invent anything, no other claims):
- Brand: Northwind Outdoors (fictional demo)
- Headline idea: "Made for the long way round" ; region: Rocky Ridge (fictional)
- Product: Trailhead 2 tent; two-person, three-season
- Weight: 2.4 kg packed
- Fabric: 40D ripstop nylon
- Colourways: moss green, slate grey
- Price: 349 dollars (fictional)
- Feature: two doors and two vestibules
- Warranty: five years
- Call to action: "Plan your first night out" ; demo-address-placeholder

Footnote (required, small but legible): this is a fictional demo brief; no real
product, price or offer is represented. Check before finishing: every number and
the demo address match exactly, all French accents correct, no text cut off,
contrast readable, file sizes reasonable. List the files in your final answer."""


def run(task_type, subagents, score=9.0, status="ok", stalled=False,
        errors=0, rescued=False, run_id=None):
    return {
        "kind": "harness", "id": run_id or ("run-%s-%s" % (task_type, subagents)),
        "task_type": task_type, "subagents": subagents, "quality_score": score,
        "status": status, "estimated_cost_usd": 0.2, "seconds": 1000,
        "stalled": stalled, "stop_reason": "stalled" if stalled else None,
        "rescued": rescued, "errors": errors,
    }


class DeclaredDeliverableTests(unittest.TestCase):
    def test_fact_bullets_are_not_deliverables(self):
        result = quality.classify_task(VIP)
        self.assertEqual(result["signals"]["bullets"], 10)
        self.assertLessEqual(result["signals"]["deliverable_count"], 3)
        self.assertGreaterEqual(result["signals"]["deliverable_count"], 2)
        self.assertEqual(result["task_type"], "creative")

    def test_a_single_help_line_mentioning_files_does_not_count(self):
        text = ("Create a poster. Deliverables: poster.html and poster.png.\n"
                "- one fact\n- another fact\n"
                "Check before finishing: file sizes reasonable and no text cut off.")
        result = quality.classify_task(text)
        self.assertEqual(result["signals"]["deliverable_count"], 2)

    def test_numbered_pages_and_sections_still_make_a_large_task(self):
        result = quality.classify_task("Design a 12-page brochure with 8 sections")
        self.assertEqual(result["size"]["size"], "large")
        self.assertGreaterEqual(result["signals"]["deliverable_count"], 6)

    def test_explicit_bullet_deliverables_still_count(self):
        prompt = "Build a Python CLI with tests.\n" + "\n".join(
            "- deliverable %d: section %d with file output" % (index, index)
            for index in range(1, 16))
        result = quality.classify_task(prompt)
        self.assertEqual(result["signals"]["deliverable_count"], 15)
        self.assertEqual(result["size"]["size"], "large")


class SimpleQuestionGuardTests(unittest.TestCase):
    def test_question_with_a_numbered_deliverable_is_not_simple(self):
        result = quality.classify_task("What is an API? 3 pages")
        self.assertNotEqual(result["task_type"], "simple")
        self.assertGreater(result["signals"]["deliverable_count"], 1)
        self.assertGreater(quality.choose_agents("What is an API? 3 pages")["subagents"], 0)

    def test_bare_definitional_question_stays_simple(self):
        result = quality.classify_task("What is an API?")
        self.assertEqual(result["task_type"], "simple")
        self.assertEqual(quality.choose_agents("What is an API?")["subagents"], 0)


class HistoryMatchTests(unittest.TestCase):
    def test_one_strong_smaller_creative_run_matches_six_agents(self):
        choice = quality.choose_agents(VIP, runs=[run("creative", 6)])
        self.assertEqual(choice["start_subagents"], 12)
        self.assertEqual(choice["subagents"], 6)
        self.assertEqual(choice["waves"], [6])
        self.assertIn("matched the smaller best count 6", choice["reason"])

    def test_no_history_keeps_the_baseline(self):
        choice = quality.choose_agents(VIP)
        self.assertEqual(choice["start_subagents"], 12)
        self.assertEqual(choice["subagents"], 12)

    def test_one_smaller_weak_run_does_not_shrink(self):
        choice = quality.choose_agents(VIP, runs=[run("creative", 6, score=7.0)])
        self.assertEqual(choice["subagents"], 12)

    def test_one_larger_run_does_not_raise_even_when_strong(self):
        choice = quality.choose_agents(
            "Build a Python CLI with tests", runs=[run("code", 6, run_id="one")])
        self.assertEqual(choice["subagents"], 4)

    def test_two_larger_runs_raise_by_two(self):
        runs = [run("code", 6, run_id="a"), run("code", 6, run_id="b")]
        choice = quality.choose_agents("Build a Python CLI with tests", runs=runs)
        self.assertEqual(choice["subagents"], 6)
        self.assertIn("nudged the start +2", choice["reason"])

    def test_smaller_bucket_never_goes_below_the_floor(self):
        choice = quality.choose_agents(VIP, runs=[run("creative", 2)])
        self.assertEqual(choice["subagents"], 4)


if __name__ == "__main__":
    unittest.main()
