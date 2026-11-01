import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import quality
from deepseek_mcp.config import load_config


class SizingTests(unittest.TestCase):
    def test_simple_questions_use_zero_subagents(self):
        for prompt in ("What is 2+2?", "Who wrote Hamlet?", "Explain photosynthesis."):
            choice = quality.choose_subagents(prompt)
            self.assertEqual(choice["subagents"], 0, prompt)
            self.assertEqual(choice["complexity"], "simple")

    def test_real_work_starts_per_type_and_uses_waves(self):
        code = quality.choose_agents("Build a Python CLI with tests.", concurrency=8)
        self.assertEqual(code["task_type"], "code")
        self.assertEqual(code["subagents"], 4)
        self.assertEqual(code["waves"], [4])

        creative = quality.choose_agents("Design a poster.", concurrency=8)
        self.assertEqual(creative["task_type"], "creative")
        self.assertEqual(creative["subagents"], 6)
        self.assertIn("Waves at concurrency 8", creative["reason"])

        medium = quality.choose_agents(
            "Design a print poster with five sections and a QR code.", concurrency=8)
        self.assertEqual(medium["subagents"], 8)

    def test_workload_scales_up_and_respects_soft_cap(self):
        prompt = "Build a Python CLI with tests.\n" + "\n".join(
            "- deliverable %d: section %d with file output" % (index, index)
            for index in range(1, 16))
        choice = quality.choose_agents(prompt, soft_cap=32, default_subagents=16,
                                       concurrency=8)
        self.assertEqual(choice["task_type"], "code")
        self.assertEqual(choice["subagents"], 8)
        self.assertEqual(choice["size"]["size"], "large")
        self.assertEqual(choice["waves"], [8])

    def test_rounding_and_wave_cap_for_non_power_of_two_concurrency(self):
        choice = quality.choose_agents(
            "Design a 12-page brochure with 8 sections.", soft_cap=32,
            default_subagents=16, concurrency=6)
        self.assertEqual(choice["subagents"], 12)
        self.assertTrue(all(size <= 6 for size in choice["waves"]))
        self.assertEqual(sum(choice["waves"]), 12)

    def test_explicit_subagents_win_exactly_even_above_soft_cap(self):
        choice = quality.choose_subagents("a task", explicit=40, soft_cap=32, concurrency=8)
        self.assertEqual(choice["subagents"], 40)
        self.assertIn("explicitly requested", choice["reason"])

    def test_explicit_odd_count_keeps_partial_last_wave(self):
        choice = quality.choose_subagents("a task", explicit=17, soft_cap=32, concurrency=8)
        self.assertEqual(choice["subagents"], 17)
        self.assertIn("8 + 8 + 1", choice["reason"])

    def test_explicit_zero(self):
        choice = quality.choose_subagents("Design a poster", explicit=0)
        self.assertEqual(choice["subagents"], 0)

    def test_soft_cap_zero_and_disabled_default(self):
        choice = quality.choose_subagents("Write a short story about a robot.", soft_cap=0)
        self.assertEqual(choice["subagents"], 0)
        self.assertIn("soft cap 0", choice["reason"])
        disabled = quality.choose_subagents("Build a small app", default_subagents=0, soft_cap=32)
        self.assertEqual(disabled["subagents"], 0)
        self.assertIn("disabled", disabled["reason"])


class TaskTextTests(unittest.TestCase):
    def test_quality_task_contains_all_loop_requirements(self):
        skill = {"key": "poster", "title": "Poster", "content": "- [ ] check print size"}
        task = quality.build_harness_task(
            "Design a poster", 16, "real work", skill=skill, quality=True,
            quality_rounds=3, concurrency=8, independent_review=True)
        for phrase in ("at most 8 children", "Pipelined, learning waves", "SUB-LEADER",
                       "seeded", "wave-1-summary", "ACTIVATION_LIMIT_REACHED",
                       "read_image", "REVIEWER", ">= 8.5/10", "3 revision round",
                       "QUALITY_REPORT.md", "headless Chrome", "QR", "check print size",
                       "Independent verification", "self-score",
                       "Execution safety (hard requirement)", "timeout 60 chrome",
                       "gtimeout", "6 minutes", "7 minutes", "candidate file FIRST"):
            self.assertIn(phrase, task)
        self.assertIn("Work method (concurrency limit first):", task)
        self.assertIn("8 + 8", task)

    def test_explicit_odd_wave_text(self):
        task = quality.build_harness_task("a task", 9, "explicit", quality=False,
                                          concurrency=8)
        self.assertIn("8 + 1", task)
        self.assertIn("at most 8 children", task)

    def test_single_agent_task_does_not_ask_for_subagents(self):
        task = quality.build_harness_task("What is 2+2?", 0, "simple", quality=False)
        self.assertIn("single agent", task)
        self.assertNotIn("parallel()", task)
        self.assertNotIn("QUALITY_REPORT.md", task)

    def test_review_task_uses_original_brief_and_never_builder_conclusions(self):
        task = quality.build_review_task(
            "Design a poster with a QR code", ["poster.pdf", "poster.png"],
            skill={"title": "Poster", "content": "- [ ] QR decodes"}, quality_target=8.5)
        self.assertIn("Design a poster with a QR code", task)
        self.assertIn("poster.pdf", task)
        self.assertIn("QR decodes", task)
        self.assertIn("read_image", task)
        self.assertIn("REVIEW.md", task)
        self.assertIn("Do not trust the builder's QUALITY_REPORT.md", task)

    def test_revision_task_contains_fix_list(self):
        task = quality.build_revision_task(
            "Design a poster", ["poster.pdf"],
            ["Move the footer up", "Re-encode the QR"],
            ["Footer overlaps at 2cm", "QR does not decode"],
            skill={"title": "Poster", "content": "- [ ] check"})
        self.assertIn("Move the footer up", task)
        self.assertIn("Footer overlaps at 2cm", task)
        self.assertIn("REVISION agent", task)

    def test_parse_review(self):
        parsed = quality.parse_review(
            "# Independent review\n## Score\nFinal quality score: 7.2/10\n"
            "## Blocking defects\n- Headline overlaps footer in poster.pdf\n"
            "- QR does not decode\n## Non-blocking defects\n- Typo\n"
            "## Fix list\n1. Move footer\n2. Re-encode QR\n")
        self.assertEqual(parsed["score"], 7.2)
        self.assertEqual(parsed["blocking_defects"], 2)
        self.assertIn("Headline overlaps", parsed["defects"][0])
        self.assertEqual(parsed["fixes"], ["Move footer", "Re-encode QR"])

    def test_parse_clean_review(self):
        parsed = quality.parse_review(
            "## Score\nFinal quality score: 9.1/10\n## Blocking defects\nNone\n"
            "## Fix list\nNone\n")
        self.assertEqual(parsed["score"], 9.1)
        self.assertEqual(parsed["blocking_defects"], 0)

    def test_score_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "QUALITY_REPORT.md").write_text(
                "# Report\n\nRound 1: 7.0/10\nRound 2: 9.0/10\nFinal quality score: 9.0/10\n",
                encoding="utf-8")
            self.assertEqual(quality.extract_quality_score(str(workspace)), 9.0)
            self.assertEqual(quality.extract_quality_score(None, "Final quality score: 8.7/10"), 8.7)
            self.assertEqual(quality.extract_quality_score(
                None, "Final quality score: 8.7/10\nTarget: 9.5/10"), 8.7)
            self.assertEqual(quality.extract_quality_score(None, "| Final | 8.8/10 | 9.5/10 |"), 8.8)
            self.assertEqual(quality.extract_quality_score(
                None, "| Final quality score | 8.8/10 | Target: 9.0/10 |"), 8.8)
            self.assertIsNone(quality.extract_quality_score(None, "no score here"))

    def test_quality_verdict_control_flow(self):
        self.assertEqual(quality.quality_verdict(8.5, 0, 1, 3), "accept")
        self.assertEqual(quality.quality_verdict(8.6, 1, 1, 3), "revise")
        self.assertEqual(quality.quality_verdict(8.4, 0, 2, 3), "revise")
        self.assertEqual(quality.quality_verdict(8.4, 0, 3, 3), "stop")
        self.assertEqual(quality.quality_verdict(9.0, 0, 1, 3), "accept")

    def test_quality_default(self):
        cfg = load_config(config_path="/nonexistent/config.json", env={})
        self.assertTrue(quality.quality_default(16, cfg))
        self.assertFalse(quality.quality_default(0, cfg))


if __name__ == "__main__":
    unittest.main()
