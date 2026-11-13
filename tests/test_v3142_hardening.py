"""3.14.3 hardening: fail-closed gate, rescue classification, crosscheck wiring."""

import unittest

from deepseek_mcp import crosscheck
from deepseek_mcp import harness


def _part(ident, builder="crew-1", checker="crew-2", status="built"):
    return {"id": ident, "write_set": [ident + ".py"], "builder_crew": builder,
            "checker_crew": checker, "status": status}


class RescueClassificationTests(unittest.TestCase):
    def test_crosscheck_rescues_balance_and_network_stops(self):
        self.assertIn("no_balance", harness._RESCUE_STOP_REASONS)
        self.assertIn("network", harness._RESCUE_STOP_REASONS)

    def test_network_failure_is_classified_but_balance_is_not(self):
        self.assertTrue(harness._is_network_failure(
            {"status": "error", "error": "ECONNRESET while reading stream"}))
        self.assertTrue(harness._is_network_failure(
            {"status": "error", "stderr_tail": "HTTP 503 Service Unavailable"}))
        self.assertFalse(harness._is_network_failure(
            {"status": "error", "error": "Insufficient Balance"}))
        self.assertFalse(harness._is_network_failure(
            {"status": "ok", "error": "ECONNRESET"}))

    def test_successful_rescue_reconciles_a_failed_part_status(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [_part("p1", status="failed"),
                           _part("p2", builder="crew-2", checker="crew-1")]
        record = crosscheck.apply_rescue(record, {
            "attempted": True, "reason": "partial build", "status": "ok",
            "part_id": "p1", "rescue_crew": "finisher",
        }, rescued=True)
        self.assertTrue(record["parts"][0]["rescued"])
        self.assertEqual(record["parts"][0]["status"], "built")
        self.assertFalse(record["parts"][1].get("rescued"))
        # A failed rescue must not mark anything and must cap an otherwise
        # perfect score at 8.0.
        record = crosscheck.apply_rescue(record, {
            "attempted": True, "reason": "finisher crashed",
            "status": "finisher_error", "part_id": "p2",
        }, rescued=False)
        self.assertFalse(record["parts"][1].get("rescued"))
        requirements = [
            {"id": "R1", "text": "a", "part_id": "p1",
             "checked_by": "crew-2", "evidence": "cmd"},
            {"id": "R2", "text": "b", "part_id": "p2",
             "checked_by": "crew-1", "evidence": "cmd"},
        ]
        result = crosscheck.evaluate(
            record["parts"], record["findings"], requirements,
            {"status": "passed"}, record["rescues"], threshold=10.0,
            brief_count=2)
        self.assertEqual(result["score"], 8.0)
        self.assertIn("rescue_failed", result["reasons"])


class RescueTargetingTests(unittest.TestCase):
    def test_production_shape_marks_only_unfinished_parts(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [
            _part("p1", builder="crew-1", checker="crew-2"),
            _part("p2", builder="crew-2", checker="crew-1", status="failed"),
            _part("p3", builder="crew-1", checker="crew-2", status="planned"),
        ]
        record = crosscheck.apply_rescue(record, {
            "attempted": True, "reason": "builder_timeout",
            "builder_error": "timeout", "status": "ok",
        }, rescued=True)
        self.assertFalse(record["parts"][0].get("rescued"))
        self.assertTrue(record["parts"][1].get("rescued"))
        self.assertTrue(record["parts"][2].get("rescued"))
        self.assertEqual([row["part_id"] for row in record["rescues"]],
                         ["p2", "p3"])

    def test_crew_rescue_crew_cannot_certify_the_rescued_part(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [
            _part("p1", builder="crew-1", checker="crew-2"),
            _part("p2", builder="crew-2", checker="crew-1"),
        ]
        record = crosscheck.apply_rescue(record, {
            "part_id": "p1", "reason": "finisher took over",
            "rescue_crew": "crew-2",
        }, rescued=True)
        self.assertEqual(record["rescues"][0]["rescue_crew"], "crew-2")
        requirements = [
            {"id": "R1", "text": "a", "part_id": "p1",
             "checked_by": "crew-2", "evidence": "cmd"},
            {"id": "R2", "text": "b", "part_id": "p2",
             "checked_by": "crew-1", "evidence": "cmd"},
        ]
        result = crosscheck.evaluate(
            record["parts"], [], requirements, {"status": "passed"},
            record["rescues"], threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["requirements"], 0.5)
        self.assertLess(result["score"], 10.0)


class RescueBuildsPlannedPartTests(unittest.TestCase):
    def test_successful_rescue_completes_a_planned_part(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [{
            "id": "p1", "write_set": ["a.py"], "builder_crew": "crew-1",
            "checker_crew": "crew-2", "status": "planned"}]
        record = crosscheck.apply_rescue(record, {
            "attempted": True, "reason": "stall", "status": "ok",
        }, rescued=True)
        self.assertTrue(record["parts"][0]["rescued"])
        self.assertEqual(record["parts"][0]["status"], "built")
        requirements = [{"id": "R1", "part_id": "p1",
                         "checked_by": "crew-2", "evidence": "cmd"}]
        result = crosscheck.evaluate(
            record["parts"], [], requirements, {"status": "passed"},
            record["rescues"], threshold=10.0, brief_count=1)
        self.assertTrue(result["parts_ok"])
        self.assertEqual(result["score"], 10.0)

    def test_whole_run_rescue_completes_pending_and_queued_parts(self):
        for status in ("pending", "queued"):
            record = crosscheck.new_record({"threshold": 10.0})
            record["parts"] = [{
                "id": "p1", "write_set": ["a.py"], "builder_crew": "crew-1",
                "checker_crew": "crew-2", "status": status}]
            record = crosscheck.apply_rescue(record, {
                "attempted": True, "reason": "stall", "status": "ok",
            }, rescued=True)
            self.assertEqual(record["parts"][0]["status"], "built", status)
            self.assertTrue(record["parts"][0]["rescued"], status)
            requirements = [{"id": "R1", "part_id": "p1",
                             "checked_by": "crew-2", "evidence": "cmd"}]
            result = crosscheck.evaluate(
                record["parts"], [], requirements, {"status": "passed"},
                record["rescues"], threshold=10.0, brief_count=1)
            self.assertTrue(result["parts_ok"], status)
            self.assertEqual(result["score"], 10.0, status)


class UnbuiltPartsTests(unittest.TestCase):
    def test_planned_parts_cannot_score_ten(self):
        parts = [
            {"id": "p1", "write_set": ["a.py"], "builder_crew": "crew-1",
             "checker_crew": "crew-2", "status": "planned"},
            {"id": "p2", "write_set": ["b.py"], "builder_crew": "crew-2",
             "checker_crew": "crew-1", "status": "planned"},
        ]
        requirements = [
            {"id": "R1", "part_id": "p1", "checked_by": "crew-2",
             "evidence": "cmd"},
            {"id": "R2", "part_id": "p2", "checked_by": "crew-1",
             "evidence": "cmd"},
        ]
        result = crosscheck.evaluate(
            parts, [], requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["score"], 6.0)
        self.assertFalse(result["passed"])
        self.assertFalse(result["parts_ok"])
        self.assertIn("part_failed", result["reasons"])

    def test_build_contract_names_the_connector_checklist(self):
        cc = crosscheck.normalise_config({"mode": "crosscheck"}, {})
        contract = crosscheck.build_contract(
            cc, {"parts": []}, {},
            "Build a CLI.\n- must support JSON output\n- must write tests")
        self.assertIn("Connector brief checklist", contract)
        self.assertIn("R1", contract)
        self.assertIn("support JSON output", contract)


class ScoringIndependenceTests(unittest.TestCase):
    def test_unconfirmed_blocking_fix_costs_only_the_fixes_condition(self):
        parts = [_part("p1"), _part("p2", builder="crew-2", checker="crew-1")]
        findings = [{"id": "F1", "part_id": "p1", "severity": "blocking",
                     "status": "fixed", "found_by_crew": "crew-2",
                     "text": "unconfirmed"}]
        requirements = [
            {"id": "R1", "text": "a", "part_id": "p1",
             "checked_by": "crew-2", "evidence": "cmd"},
            {"id": "R2", "text": "b", "part_id": "p2",
             "checked_by": "crew-1", "evidence": "cmd"},
        ]
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["blocking"], 1.0)
        self.assertEqual(result["conditions"]["fixes"], 0.0)
        self.assertEqual(result["score"], 8.0)

    def test_unrecognized_blocking_status_is_open(self):
        parts = [_part("p1"), _part("p2", builder="crew-2", checker="crew-1")]
        findings = [{"id": "F1", "part_id": "p1", "severity": "blocking",
                     "status": "wontfix", "text": "still broken"}]
        requirements = [
            {"id": "R1", "text": "a", "part_id": "p1",
             "checked_by": "crew-2", "evidence": "cmd"},
            {"id": "R2", "text": "b", "part_id": "p2",
             "checked_by": "crew-1", "evidence": "cmd"},
        ]
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["blocking"], 0.0)
        self.assertLess(result["score"], 10.0)
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
