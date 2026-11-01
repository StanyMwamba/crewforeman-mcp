"""v3.3.0 PART A: builder task and visual-skills execution safety."""

import unittest
from pathlib import Path

from deepseek_mcp import quality

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTER = REPO_ROOT / "skills" / "poster.md"
WEB = REPO_ROOT / "skills" / "web.md"


class BuilderSafetyTests(unittest.TestCase):
    def task(self, subagents=8):
        return quality.build_harness_task(
            "Design an A5 poster with a QR code.", subagents, "chosen automatically",
            quality=False, concurrency=8)

    def test_hard_timeout_and_portable_fallback(self):
        task = self.task()
        self.assertIn("Execution safety (hard requirement)", task)
        self.assertIn("timeout 60 chrome", task)
        self.assertIn("gtimeout 60", task)
        self.assertIn("perl -e 'alarm 60; exec @ARGV'", task)
        self.assertIn("timeoutMs", task)
        self.assertIn("does not reliably end the process group", task)

    def test_fresh_workspace_profile_and_cleanup(self):
        task = self.task()
        self.assertIn("FRESH `--user-data-dir`", task)
        self.assertIn("`HOME` and `TMPDIR`", task)
        self.assertIn("Kill leftover Chrome processes", task)
        self.assertIn("delete the profile", task)

    def test_two_attempts_then_ship_the_html(self):
        task = self.task()
        self.assertIn("at most 2 Chrome attempts", task)
        self.assertIn("render unavailable: <reason>", task)
        self.assertIn("not a reason to loop", task)
        self.assertIn("render only the chosen candidate", task)

    def test_candidate_first_and_time_budget(self):
        task = self.task()
        self.assertIn("candidate file FIRST", task)
        self.assertIn("6 minutes", task)
        self.assertIn("at most 12 tool calls", task)
        self.assertIn("7 minutes", task)
        self.assertIn("480 s stall watchdog", task)
        self.assertIn("`parallel()` barrier waits for every child", task)
        self.assertIn("even if QA is incomplete", task)

    def test_wave_contract_phrases_still_present(self):
        task = self.task()
        for phrase in ("at most 8 children", "one `parallel()` block",
                       "wave-1-results.md", "SUB-LEADER", "ACTIVATION_LIMIT_REACHED"):
            self.assertIn(phrase, task, phrase)

    def test_zero_and_one_agent_tasks_get_safety_and_budget(self):
        zero = quality.build_harness_task(
            "What is 2+2?", 0, "simple", quality=False, concurrency=8)
        one = quality.build_harness_task(
            "Render this HTML to PNG with headless Chrome", 1, "explicit",
            quality=False, concurrency=8)
        for task in (zero, one):
            self.assertIn("Execution safety (hard requirement)", task)
            self.assertIn("timeout 60 chrome", task)
            self.assertIn("FRESH `--user-data-dir`", task)
            self.assertIn("Time budget (hard requirement)", task)
            self.assertIn("6 minutes", task)
            self.assertIn("at most 12 tool calls", task)
            self.assertIn("Write the deliverable file FIRST", task)
        self.assertIn("single agent", zero)
        self.assertIn("use 1 subagent", one)
        # A truly simple task must not be told to start a workflow at all.
        self.assertNotIn("parallel()", zero)
        self.assertNotIn("7 minutes", zero)

    def test_quality_loop_timeboxes_the_render(self):
        task = quality.build_harness_task(
            "Design an A5 poster with a QR code.", 8, "chosen automatically",
            quality=True, concurrency=8)
        self.assertIn("headless Chrome under a hard timeout", task)
        self.assertIn("fresh `--user-data-dir`", task)
        self.assertIn("try at most twice", task)
        self.assertIn("record the limitation", task)


class VisualSkillSafetyTests(unittest.TestCase):
    def _check(self, path, noun):
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Headless Chrome and long commands", text)
        self.assertIn("timeout 60 chrome", text)
        self.assertIn("gtimeout 60", text)
        self.assertIn("perl -e 'alarm 60; exec @ARGV'", text)
        self.assertIn("fresh `--user-data-dir`", text)
        self.assertIn("`HOME` and", text)
        self.assertIn("Kill leftover Chrome processes", text)
        self.assertIn("at most two Chrome attempts", text)
        self.assertIn("render unavailable: <reason>", text)
        self.assertIn(noun, text)
        return text

    def test_poster_skill_has_the_hard_timeout_section(self):
        text = self._check(POSTER, "HTML source")
        self.assertLess(text.index("## Headless Chrome and long commands"),
                        text.index("## Layout, images and print production"))

    def test_web_skill_has_the_hard_timeout_section(self):
        text = self._check(WEB, "HTML page")
        self.assertLess(text.index("## Headless Chrome and long commands"),
                        text.index("## Layout and responsiveness"))


if __name__ == "__main__":
    unittest.main()
