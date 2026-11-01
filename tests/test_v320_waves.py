"""Wave-2 tests for v3.2.0 R3: one ``workflow`` call per wave.

The builder task returned by ``quality.build_harness_task`` must:

* make a separate ``workflow`` call with exactly one ``parallel()`` block for
  each wave, instead of one large script that covers every wave;
* save each wave's results to ``wave-<n>-results.md`` before the next call;
* keep the files a hung wave already wrote and continue with the waves that
  already finished;
* preserve the pre-existing "concurrency limit first" contract.

These are pure task-text assertions: no Harness process and no temp files.
"""

import re
import unittest

from deepseek_mcp import quality


BRIEF = "Design a poster with a QR code and a one-page rationale."
REASON = "chosen automatically for real work"


def build_task(subagents, concurrency=8, quality_loop=False):
    return quality.build_harness_task(
        BRIEF,
        subagents,
        REASON,
        quality=quality_loop,
        concurrency=concurrency,
    )


class WaveInstructionTests(unittest.TestCase):
    def test_task_requires_separate_workflow_call_per_wave(self):
        # The rule must survive both the short (builder-only) task and the
        # full task with the quality loop appended.
        for quality_loop in (False, True):
            with self.subTest(quality_loop=quality_loop):
                task = build_task(16, quality_loop=quality_loop)
                separated = (
                    "its own `workflow` tool call" in task
                    or "separate `workflow`" in task
                )
                self.assertTrue(
                    separated,
                    "the wave instructions must require a separate workflow call "
                    "for every wave",
                )
                self.assertIn("one `parallel()` block", task)
                for stale in (
                    "run at most 7 builders",
                    "plus the 1 summarizer",
                    "same block",
                ):
                    self.assertNotIn(
                        stale,
                        task,
                        "old overlap/reserved-slot wording survived: %r" % stale,
                    )

    def test_saves_each_wave_before_next(self):
        task = build_task(16)
        self.assertIn("wave-1-results.md", task)
        self.assertIn("wave-2-results.md", task)
        self.assertIn("before starting the next wave", task)
        self.assertIn("before the next call", task)
        # The save instruction for a wave must precede the start of the next
        # wave, or a hang loses the file the next wave is supposed to read.
        self.assertLess(task.index("wave-1-results.md"), task.index("Wave 2"))
        self.assertLess(task.index("Wave 2"), task.index("wave-2-results.md"))

    def test_hung_wave_continues_with_finished_waves(self):
        task = build_task(16)
        self.assertIn("If a wave hangs", task)
        self.assertIn("continue with the waves already finished", task)
        self.assertIn("do not restart", task.lower())
        self.assertIn("do not re-run an earlier wave", task)

    def test_concurrency_limit_first_wording_preserved(self):
        task = build_task(16)
        self.assertIn("Work method", task)
        method = task.split("Work method", 1)[1]
        self.assertTrue(
            method.startswith(" (concurrency limit first)"),
            "method must start with the limit, got: %r" % method[:120],
        )
        self.assertIn("at most 8 children alive at once", method)
        self.assertIn("ACTIVATION_LIMIT_REACHED", method)
        self.assertIn("8 + 8", method[:300])

    def test_single_wave_still_gets_separate_call(self):
        task = build_task(7)
        self.assertIn("waves of AT MOST 8 (7)", task)
        self.assertIn("This run has a single wave", task)
        self.assertIn("wave-1-results.md", task)
        self.assertNotIn("wave-2-results.md", task)
        self.assertIn("its own `workflow` tool call", task)
        self.assertIn("exactly one `parallel()` block", task)
        self.assertIn("do not restart", task.lower())

    def test_single_agent_task_not_affected(self):
        task = quality.build_harness_task(
            "What is 2+2?", 0, "simple", quality=False
        )
        self.assertIn("single agent", task)
        self.assertNotIn("workflow", task.lower())
        self.assertNotIn("parallel()", task)
        self.assertNotIn("Pipelined, learning waves", task)
        self.assertNotIn("wave-1-results.md", task)
        self.assertNotIn("wave-2-results.md", task)

    def test_no_oversized_parallel_literal(self):
        for count in (16, 24, 33):
            with self.subTest(subagents=count):
                task = build_task(count)
                self.assertNotIn("parallel([", task)
                self.assertNotIn(
                    "run %d subagents with parallel" % count, task
                )
                self.assertNotRegex(
                    task, r"run \d+ subagents? with parallel"
                )

    def test_wave_sizes_cover_all_agents_within_concurrency(self):
        # The explicit wave plan ("8 + 8 + ...") is what the builder is told
        # to run one call at a time; every size must fit the concurrency.
        for count in (1, 9, 16, 24, 33):
            with self.subTest(subagents=count):
                task = build_task(count)
                match = re.search(r"waves of AT MOST 8 \(([^)]*)\)", task)
                self.assertIsNotNone(match, "wave sizes missing from the task")
                sizes = [int(part) for part in match.group(1).split(" + ")]
                self.assertEqual(sum(sizes), count)
                self.assertTrue(
                    all(0 < size <= 8 for size in sizes),
                    "a wave exceeds concurrency 8: %r" % (sizes,),
                )


if __name__ == "__main__":
    unittest.main()
