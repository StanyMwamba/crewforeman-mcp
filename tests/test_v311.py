import re
import unittest

from deepseek_mcp import harness, quality


class WaveInstructionTests(unittest.TestCase):
    def test_never_asks_for_more_than_cap_in_one_parallel(self):
        for cap in (4, 8, 12):
            for count in (1, 7, 8, 9, 16, 24, 33):
                text = quality.build_harness_task("Design a poster", count, "test", concurrency=cap)
                self.assertNotRegex(text, r"run \d+ subagents? with parallel")
                self.assertIn("waves of AT MOST %d" % cap, text)
                for block in re.findall(r"parallel\(\[?[^)]*?(\d+)", text):
                    self.assertLessEqual(int(block), max(cap, count if count <= cap else cap))

    def test_first_instruction_is_the_limit(self):
        text = quality.build_harness_task("Design a poster", 16, "test", concurrency=8)
        method = text.split("Work method", 1)[1]
        self.assertTrue(method.startswith(" (concurrency limit first)"))
        self.assertIn("8 + 8", method[:300])


class NoBalanceTests(unittest.TestCase):
    def test_detects_insufficient_balance(self):
        failed = {"error": "dsh: QUOTA: Insufficient Balance (request_id: x)", "stderr_tail": ""}
        self.assertTrue(harness._is_no_balance(failed))
        self.assertTrue(harness._is_no_balance({"error": "", "stderr_tail": "HTTP 402"}))
        self.assertFalse(harness._is_no_balance({"error": "time limit reached"}))
        self.assertIn("platform.deepseek.com", harness.NO_BALANCE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
