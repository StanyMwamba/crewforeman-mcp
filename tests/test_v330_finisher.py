"""v3.3.0 PART B: the rescue finisher must be strict and self-limiting."""

import unittest

from deepseek_mcp import quality


BRIEF = ("Create a one-page A5 flyer for 'Le Petit Four' with exactly these facts: "
         "open 7 days 7am-7pm; sourdough baked daily; 12 rue Exemple. "
         "Render it to flyer.png with headless Chrome.")


class LeanFinisherTests(unittest.TestCase):
    def task(self, files=None, quality_on=True, skill=None):
        return quality.build_finisher_task(
            BRIEF, files if files is not None else ["candidate-a.html"],
            stop_reason="stalled", stopped_after=480,
            skill=skill, quality=quality_on)

    def test_hard_call_and_time_caps(self):
        task = self.task()
        self.assertIn("at most 12 tool calls", task)
        self.assertIn("Stop at 12 calls", task)
        self.assertIn("8 minutes", task)
        self.assertIn("minute 8", task)
        self.assertIn("Tool calls used: N of 12", task)

    def test_connector_limit_is_named_when_smaller(self):
        task = quality.build_finisher_task(BRIEF, ["a"], quality=False,
                                           limit_seconds=60)
        self.assertIn("may stop you at 60 s", task)
        self.assertIn("write the final answer before then", task)

    def test_no_redesign_no_profiles_no_audits(self):
        task = self.task()
        self.assertIn("No redesign", task)
        self.assertIn("Do not redesign anything", task)
        self.assertIn("No new browser profiles", task)
        self.assertIn("Do not create, inspect or repair persistent", task)
        self.assertIn("No new audits", task)
        self.assertIn("Do not write or run audit scripts", task)
        self.assertNotIn(".chrome-profiles", task)

    def test_pick_one_existing_candidate_and_minimal_fixes(self):
        task = self.task()
        self.assertIn("single best existing candidate", task)
        self.assertIn("most complete one", task)
        self.assertIn("at most three minimal fixes", task)
        self.assertIn("Do not add a new section", task)
        self.assertIn("Use the existing files", task)
        self.assertIn("Read at most the 2 most promising candidates", task)

    def test_timeboxed_commands_and_nonempty_verification(self):
        task = self.task()
        self.assertIn("timeout 60 chrome", task)
        self.assertIn("gtimeout", task)
        self.assertIn("Perl", task)
        self.assertIn("--user-data-dir", task)
        self.assertIn("try at most twice", task)
        self.assertIn("test -s", task)
        self.assertIn("size > 0", task)
        self.assertIn("non-empty", task)
        self.assertIn("zero-byte", task)

    def test_stop_and_review_ownership(self):
        task = self.task()
        self.assertIn("Stop.", task)
        self.assertEqual(task.count("REVIEW.md"), 1)
        review_line = [line for line in task.splitlines() if "REVIEW.md" in line][0]
        self.assertIn("Do not write", review_line)
        self.assertIn("0 subagents", task)
        self.assertIn("no `workflow`", task)

    def test_original_brief_and_quality_report(self):
        task = self.task()
        self.assertIn("## Original brief", task)
        self.assertIn(BRIEF, task)
        self.assertIn("QUALITY_REPORT.md", task)
        self.assertIn("Final quality score", task)
        self.assertIn("one write", task)
        self.assertIn("This is a rescue of an earlier interrupted attempt", task)
        self.assertIn("Deliverables:", task)
        self.assertIn("list the file names", task)

    def test_no_candidate_branch_builds_the_simplest_deliverable(self):
        task = quality.build_finisher_task(BRIEF, [], quality=False)
        self.assertIn("None were listed", task)
        self.assertIn("If no candidate exists", task)
        self.assertIn("simplest correct deliverable", task)
        self.assertIn("fewest tool calls", task)

    def test_quality_off_has_no_quality_report_words(self):
        task = self.task(quality_on=False)
        self.assertNotIn("QUALITY_REPORT.md", task)
        self.assertNotIn("Final quality score", task)
        self.assertIn("Do not write `REVIEW.md`", task)

    def test_quality_off_step_numbering_is_contiguous(self):
        task = self.task(quality_on=False)
        self.assertIn("\n6. Stop.", task)
        self.assertIn("\n7. End your final answer", task)
        self.assertNotIn("\n8. ", task)
        with_quality = self.task(quality_on=True)
        self.assertIn("\n7. Write or refresh `QUALITY_REPORT.md`", with_quality)
        self.assertIn("\n8. End your final answer", with_quality)

    def test_skill_none_is_omitted_and_skill_text_is_fenced_as_sanity_only(self):
        no_skill = self.task(skill=None)
        self.assertNotIn("Skill checklist", no_skill)
        with_skill = self.task(skill={"title": "Poster", "content": "- [ ] check QR"})
        self.assertIn("Skill checklist: Poster", with_skill)
        self.assertIn("final sanity list", with_skill)
        self.assertIn("not a licence to audit", with_skill)
        self.assertIn("- [ ] check QR", with_skill)

    def test_candidate_list_is_capped_at_40(self):
        names = ["candidate-%02d.html" % index for index in range(45)]
        task = quality.build_finisher_task(BRIEF, names, quality=False)
        self.assertIn("`candidate-00.html`", task)
        self.assertIn("`candidate-39.html`", task)
        self.assertNotIn("`candidate-40.html`", task)

    def test_stop_reason_variants(self):
        stalled = quality.build_finisher_task(BRIEF, ["a"], stopped_after=10, quality=False)
        self.assertIn("stalled with no progress events after 10 s", stalled)
        timed = quality.build_finisher_task(
            BRIEF, ["a"], stop_reason="time_limit", stopped_after=2, quality=False)
        self.assertIn("hit its time limit after 2 s", timed)
        early = quality.build_finisher_task(
            BRIEF, ["a"], stop_reason="other", quality=False)
        self.assertIn("stopped early", early)


if __name__ == "__main__":
    unittest.main()
