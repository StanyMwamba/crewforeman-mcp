"""v3.11.0 status/event contract tests: the ``gate_failed`` status."""

import unittest

from deepseek_mcp import events
from deepseek_mcp.status import (FAILURE_STATUSES, STATUSES, STATUS_MEANING,
                                 final_status)


class GateFailedStatusTests(unittest.TestCase):
    def test_statuses_include_gate_failed(self):
        self.assertIn("gate_failed", STATUSES)

    def test_failure_statuses_include_gate_failed(self):
        self.assertIn("gate_failed", FAILURE_STATUSES)

    def test_status_meaning_has_gate_failed(self):
        self.assertIn("gate_failed", STATUS_MEANING)
        self.assertTrue(STATUS_MEANING["gate_failed"])

    def test_final_status_gate_failed_precedes_review_mapping(self):
        status, meaning, passed = final_status(
            builder_status="ok", review_status="error", score=1.0,
            blocking_defects=3, gate_status="gate_failed")
        self.assertEqual(status, "gate_failed")
        self.assertEqual(meaning, STATUS_MEANING["gate_failed"])
        self.assertIsNone(passed)
        # A failed builder still wins: the gate only runs after success.
        self.assertEqual(final_status(builder_status="error",
                                      gate_status="gate_failed")[0], "error")

    def test_resume_final_status_gate_failed(self):
        # The resume path passes rescued/review-skipped state; gate_failed
        # must still be returned before the review mapping.
        status, meaning, passed = final_status(
            builder_status="ok", review_status="skipped", score=None,
            rescued=True, review_recorded=False, gate_status="gate_failed")
        self.assertEqual(status, "gate_failed")
        self.assertEqual(meaning, STATUS_MEANING["gate_failed"])
        self.assertIsNone(passed)

    def test_events_failure_attention_includes_gate_failed(self):
        self.assertIn("gate_failed", events._FAILURE_ATTENTION)


if __name__ == "__main__":
    unittest.main()
