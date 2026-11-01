"""v3.3.0 PART C: classification, history nudging, escalation and lessons."""

import unittest

from deepseek_mcp import quality


def run(task_type, subagents, score=8.8, status="ok", cost=0.02, seconds=100,
        stalled=False, stop_reason=None, rescued=False, run_id=None):
    return {
        "kind": "harness", "id": run_id or ("run-%s-%s" % (task_type, subagents)),
        "task_type": task_type, "subagents": subagents, "quality_score": score,
        "status": status, "estimated_cost_usd": cost, "seconds": seconds,
        "stalled": stalled, "stop_reason": stop_reason, "rescued": rescued,
    }


class ClassificationTests(unittest.TestCase):
    def test_simple_question_is_zero(self):
        for prompt in ("What is 2+2?", "Who wrote Hamlet?", "Explain photosynthesis."):
            self.assertEqual(quality.classify_task(prompt)["task_type"], "simple")
            self.assertEqual(quality.choose_agents(prompt)["subagents"], 0)

    def test_definitional_technical_questions_are_simple(self):
        for prompt in ("What is an API?", "What is HTML?", "What is a CSV file?"):
            with self.subTest(prompt=prompt):
                self.assertEqual(quality.classify_task(prompt)["task_type"], "simple")
                self.assertEqual(quality.choose_agents(prompt)["subagents"], 0)

    def test_code_creative_and_research(self):
        cases = {
            "Build a Python CLI with unit tests and an API": "code",
            "Design a print poster with a QR code": "creative",
            "Analyse this CSV dataset and chart the regression": "research",
            "Research three competitors and compare their prices": "research",
            "Write a cover letter for a job application": "creative",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(quality.classify_task(prompt)["task_type"], expected)

    def test_size_estimate_uses_length_and_deliverables(self):
        small = quality.classify_task("Design a poster")["size"]
        self.assertEqual(small["size"], "small")
        medium = quality.classify_task(
            "Design a print poster with five sections and a QR code")["size"]
        self.assertEqual(medium["size"], "medium")
        large = quality.classify_task(
            "Design a 12-page brochure with 8 sections")["size"]
        self.assertEqual(large["size"], "large")
        self.assertGreaterEqual(large["deliverables"], 6)

    def test_baseline_start_counts(self):
        self.assertEqual(quality.choose_agents("Build a Python CLI")["subagents"], 4)
        large_code = quality.choose_agents(
            "Build an app with 20 deliverables and 8 sections")
        self.assertEqual(large_code["subagents"], 8)
        self.assertEqual(quality.choose_agents("Design a poster")["subagents"], 6)
        medium = quality.choose_agents(
            "Design a print poster with five sections and a QR code")
        self.assertEqual(medium["subagents"], 8)
        large = quality.choose_agents(
            "Design a 12-page brochure with 8 sections", default_subagents=16,
            concurrency=8)
        self.assertEqual(large["subagents"], 12)
        self.assertEqual(large["waves"], [8, 4])

    def test_explicit_count_is_exact_even_above_concurrency(self):
        choice = quality.choose_agents("a task", explicit=17, concurrency=8)
        self.assertEqual(choice["subagents"], 17)
        self.assertEqual(choice["waves"], [8, 8, 1])
        self.assertIn("exact count kept", choice["reason"])
        self.assertEqual(quality.choose_agents("a task", explicit=0)["subagents"], 0)

    def test_waves_never_exceed_concurrency(self):
        choice = quality.choose_agents(
            "Design a 12-page brochure with 8 sections", concurrency=4)
        self.assertTrue(all(size <= 4 for size in choice["waves"]))
        self.assertEqual(sum(choice["waves"]), choice["subagents"])

    def test_default_and_soft_cap_disable_or_cap(self):
        disabled = quality.choose_agents("Design a poster", default_subagents=0)
        self.assertEqual(disabled["subagents"], 0)
        self.assertIn("disabled", disabled["reason"])
        zero_soft = quality.choose_agents("Design a poster", soft_cap=0)
        self.assertEqual(zero_soft["subagents"], 0)
        self.assertIn("soft cap 0", zero_soft["reason"])
        capped = quality.choose_agents(
            "Design a 12-page brochure with 8 sections", default_subagents=4)
        self.assertLessEqual(capped["subagents"], 4)

    def test_reason_names_type_size_history_and_waves(self):
        choice = quality.choose_agents("Design a poster")
        self.assertIn("Task type creative/design", choice["reason"])
        self.assertIn("Baseline start 6", choice["reason"])
        self.assertIn("Waves at concurrency 8", choice["reason"])
        self.assertEqual(choice["agent_choice_reason"], choice["reason"])


class HistoryTests(unittest.TestCase):
    def test_history_nudges_towards_the_better_count(self):
        runs = [run("code", 6, 9.2), run("code", 6, 9.0), run("code", 4, 7.0)]
        choice = quality.choose_agents("Build a Python CLI with tests", runs=runs)
        self.assertEqual(choice["subagents"], 6)
        self.assertIn("History nudged the start +2 to 6", choice["reason"])
        self.assertEqual(choice["history"]["best_count"], 6)

    def test_history_nudges_down_when_smaller_scored_better(self):
        runs = [run("creative", 8, 7.0), run("creative", 8, 7.1),
                run("creative", 4, 9.1), run("creative", 4, 9.0)]
        choice = quality.choose_agents(
            "Design a print poster with five sections and a QR code", runs=runs)
        self.assertEqual(choice["subagents"], 4)
        self.assertIn("matched the smaller best count 4", choice["reason"])

    def test_stalled_larger_buckets_are_penalised(self):
        runs = [
            run("code", 8, 9.0, status="error", stalled=True, stop_reason="stalled"),
            run("code", 8, 9.0, status="error", stalled=True, stop_reason="stalled"),
            run("code", 4, 7.2), run("code", 4, 7.3),
        ]
        stats = quality.history_stats(runs, "code")
        self.assertEqual(stats["best_count"], 4)
        by_count = {row["count"]: row for row in stats["groups"]}
        self.assertEqual(by_count[8]["stall_rate"], 1.0)
        self.assertLess(by_count[8]["mean_utility"], by_count[4]["mean_utility"])

    def test_all_stalled_large_history_does_not_raise_the_start(self):
        runs = [
            run("code", 16, 9.0, status="error", stalled=True, stop_reason="stalled"),
            run("code", 16, 9.0, status="error", stalled=True, stop_reason="stalled"),
        ]
        choice = quality.choose_agents("Build a Python CLI with tests", runs=runs)
        self.assertEqual(choice["start_subagents"], 4)
        self.assertEqual(choice["subagents"], 4)
        self.assertIn("not raised", choice["reason"])
        self.assertEqual(choice["history"]["best_count"], 16)

    def test_insufficient_history_does_not_nudge(self):
        runs = [run("code", 6, 9.9)]
        choice = quality.choose_agents("Build a Python CLI", runs=runs)
        self.assertEqual(choice["subagents"], 4)
        self.assertIn("History did not change the start", choice["reason"])

    def test_one_run_in_the_best_bucket_is_not_enough(self):
        runs = [run("code", 8, 9.9, run_id="one"), run("code", 4, 5.0, run_id="two")]
        choice = quality.choose_agents("Build a Python CLI", runs=runs)
        self.assertEqual(choice["subagents"], 4)
        self.assertEqual(choice["history"]["best_count"], 8)

    def test_explicit_count_ignores_history(self):
        runs = [run("code", 6, 9.9), run("code", 6, 9.8)]
        choice = quality.choose_agents("Build a Python CLI", explicit=2, runs=runs)
        self.assertEqual(choice["subagents"], 2)
        self.assertEqual(choice["start_subagents"], 2)

    def test_tie_prefers_lower_cost(self):
        runs = [run("code", 4, 8.6, cost=0.01), run("code", 4, 8.6, cost=0.01),
                run("code", 8, 8.6, cost=0.10), run("code", 8, 8.6, cost=0.10)]
        stats = quality.history_stats(runs, "code")
        self.assertEqual(stats["best_count"], 4)

    def test_history_ignores_other_task_types_and_explicit_runs(self):
        runs = [run("creative", 8, 9.9), run("code", 0, 9.9),
                {"kind": "swarm", "subagents": 8, "quality_score": 10},
                {"kind": "harness", "task_type": "code", "subagents": 8,
                 "quality_score": 9.9, "status": "ok",
                 "subagent_reason": "Caller explicitly requested 8 subagent(s)"}]
        stats = quality.history_stats(runs, "code")
        self.assertEqual(stats["runs"], 0)
        self.assertIsNone(stats["best_count"])


class EscalationTests(unittest.TestCase):
    def test_accept_stops_early(self):
        self.assertIsNone(quality.plan_escalation("code", 4, 8.5, 0, 1, 8))
        self.assertIsNone(quality.plan_escalation("creative", 8, 9.2, 0, 1, 8))

    def test_automatic_count_escalates(self):
        plan = quality.plan_escalation("code", 4, 7.1, 2, 1, 8)
        self.assertEqual(plan["from"], 4)
        self.assertEqual(plan["to"], 6)
        self.assertEqual(plan["approach"], "more_agents")
        self.assertIn("7.10/10", plan["reason"])
        self.assertIn("2 blocking", plan["reason"])

    def test_at_concurrency_changes_approach(self):
        plan = quality.plan_escalation("creative", 8, 7.1, 1, 1, 8)
        self.assertEqual(plan["to"], 8)
        self.assertEqual(plan["approach"], "different_approach")
        self.assertIn("concurrency cap", plan["reason"])

    def test_explicit_count_is_not_increased(self):
        plan = quality.plan_escalation("code", 2, 6.0, 1, 1, 8, explicit=True)
        self.assertEqual(plan["to"], 2)
        self.assertTrue(plan["explicit"])
        self.assertIn("caller fixed the count", plan["reason"])

    def test_soft_cap_bounds_automatic_escalation(self):
        plan = quality.plan_escalation(
            "creative", 4, 7.0, 1, 1, concurrency=8,
            soft_cap=4, default_subagents=4)
        self.assertEqual(plan["to"], 4)
        self.assertEqual(plan["agent_ceiling"], 4)
        self.assertEqual(plan["approach"], "different_approach")
        self.assertIn("effective agent ceiling", plan["reason"])

    def test_default_subagents_bounds_automatic_escalation(self):
        plan = quality.plan_escalation(
            "code", 4, 7.0, 1, 1, concurrency=8,
            soft_cap=32, default_subagents=6)
        self.assertEqual(plan["to"], 6)
        self.assertEqual(plan["agent_ceiling"], 6)

    def test_revision_task_includes_escalation(self):
        plan = quality.plan_escalation("code", 4, 7.1, 2, 1, 8)
        task = quality.build_revision_task(
            "Build a CLI", ["cli.py"], ["Fix the crash"], ["It crashes"],
            escalation=plan)
        self.assertIn("Escalation for this round", task)
        self.assertIn("at most 6 subagents", task)
        self.assertIn("waves of at most 8", task)
        self.assertIn("one `parallel()` block", task)
        self.assertIn("different approach", task)

    def test_revision_without_escalation_is_unchanged(self):
        task = quality.build_revision_task(
            "Build a CLI", ["cli.py"], ["Fix the crash"], ["It crashes"])
        self.assertNotIn("Escalation for this round", task)


class RecurringLessonTests(unittest.TestCase):
    def _run(self, defects, task_type="creative", skill="poster", run_id=None):
        return {
            "id": run_id or "current", "kind": "harness", "task_type": task_type,
            "skill": skill, "subagents": 4,
            "review_rounds": [{
                "kind": "review", "round": 1, "score": 7.0,
                "blocking_defects": len(defects), "defects": defects,
            }],
        }

    def test_defect_kinds_are_stable(self):
        cases = {
            "Headline overlaps footer in poster.pdf": "overlap",
            "Missing required fact: opening hours": "missing required fact",
            "QR does not decode": "qr code",
            "Text is clipped at the bottom": "overflow or clipping",
            "Body font size is too small": "type size or contrast",
            "final.md is empty": "empty deliverable",
            "The unit test fails": "failing test or build",
            "It crashes on an empty input": "crash",
            "The PDF is broken": "broken artifact",
        }
        for text, expected in cases.items():
            self.assertEqual(quality.defect_kind(text), expected, text)

    def test_same_kind_in_two_runs_adds_one_lesson(self):
        current = self._run(["Headline overlaps footer in poster.pdf"])
        past = self._run(["Headline overlaps footer in poster.pdf"], run_id="past")
        lessons = quality.recurring_lessons(current, [past])
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["kind"], "overlap")
        self.assertEqual(lessons[0]["runs"], 2)
        self.assertEqual(lessons[0]["skill"], "poster")
        self.assertIn("Recurring creative defect (overlap)", lessons[0]["lesson"])

    def test_one_run_is_not_recurring(self):
        current = self._run(["Headline overlaps footer in poster.pdf"])
        self.assertEqual(quality.recurring_lessons(current, []), [])

    def test_duplicate_defects_in_one_run_count_once(self):
        current = self._run([
            "Headline overlaps footer", "Another overlap in the header"])
        past = self._run(["Footer overlap"], run_id="past")
        lessons = quality.recurring_lessons(current, [past])
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["runs"], 2)

    def test_other_task_type_is_ignored(self):
        current = self._run(["Headline overlaps footer"], task_type="creative")
        past = self._run(["Headline overlaps footer"], task_type="code", run_id="past")
        self.assertEqual(quality.recurring_lessons(current, [past]), [])

    def test_same_run_id_is_ignored(self):
        current = self._run(["Headline overlaps footer"], run_id="same")
        past = self._run(["Headline overlaps footer"], run_id="same")
        self.assertEqual(quality.recurring_lessons(current, [past]), [])

    def test_research_and_code_skill_mapping(self):
        research = self._run(["The chart labels overlap"], task_type="research",
                             skill=None)
        past = self._run(["Chart labels overlap"], task_type="research",
                         skill=None, run_id="past")
        lessons = quality.recurring_lessons(research, [past])
        self.assertEqual(lessons[0]["skill"], "data")
        code = self._run(["The unit test fails"], task_type="code", skill=None)
        code_past = self._run(["Unit test fails"], task_type="code", skill=None,
                              run_id="past")
        self.assertEqual(quality.recurring_lessons(code, [code_past])[0]["skill"], "code")


if __name__ == "__main__":
    unittest.main()
