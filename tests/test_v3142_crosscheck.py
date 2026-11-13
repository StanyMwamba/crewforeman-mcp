"""3.14.2 crosscheck: config, parts, pairing, scoring, rounds, rescue.

Standard library only; no paid model call and no network.  Every test runs on
Windows, macOS and Linux.
"""

import json
import os
import tempfile
import unittest

from deepseek_mcp import crosscheck
from deepseek_mcp.quality import build_harness_task


def _part(ident, writes, deps=None, builder="crew-1", checker="crew-2",
          status="built"):
    return {"id": ident, "title": ident, "write_set": list(writes),
            "depends_on": list(deps or []), "builder_crew": builder,
            "checker_crew": checker, "status": status}


def _requirement(ident, part="p1", crew="crew-2", evidence="command: ok",
                 passed=True):
    return {"id": ident, "text": ident, "part_id": part, "required": True,
            "checked_by": crew, "evidence": evidence, "passed": passed}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_two_crews_of_four(self):
        cc = crosscheck.normalise_config({"mode": "crosscheck"}, {})
        self.assertEqual(cc["threshold"], 10.0)
        self.assertEqual(cc["max_rounds"], 3)
        self.assertEqual(cc["crews"], 2)
        self.assertEqual(cc["crew_size"], 4)
        self.assertEqual(cc["subagents"], 8)
        self.assertEqual(cc["crew_sizes"], [4, 4])
        self.assertTrue(cc["audit_review"])

    def test_disabled_by_default_and_standard(self):
        self.assertIsNone(crosscheck.normalise_config({}, {}))
        self.assertIsNone(crosscheck.normalise_config({"mode": "standard"}, {}))
        self.assertIsNone(crosscheck.normalise_config(
            {"mode": "off"}, {"default_mode": "crosscheck"}))

    def test_config_default_mode_enables(self):
        cc = crosscheck.normalise_config({}, {"default_mode": "crosscheck"})
        self.assertIsNotNone(cc)
        self.assertEqual(cc["threshold"], 10.0)

    def test_threshold_range_is_validated(self):
        for value in (-0.01, 10.01, 11, "not-a-number", float("nan")):
            with self.assertRaises(crosscheck.CrosscheckConfigError):
                crosscheck.normalise_config(
                    {"mode": "crosscheck", "crosscheck_threshold": value}, {})
        self.assertEqual(crosscheck.normalise_config(
            {"mode": "crosscheck", "crosscheck_threshold": 0}, {})["threshold"], 0.0)
        self.assertEqual(crosscheck.normalise_config(
            {"mode": "crosscheck", "crosscheck_threshold": 8.5}, {})["threshold"], 8.5)

    def test_explicit_subagents_split_evenly(self):
        self.assertEqual(crosscheck.split_subagents(12, 2), [6, 6])
        self.assertEqual(crosscheck.split_subagents(9, 2), [5, 4])
        self.assertEqual(crosscheck.split_subagents(10, 3), [4, 3, 3])
        cc = crosscheck.normalise_config(
            {"mode": "crosscheck", "subagents": 12}, {})
        self.assertEqual(cc["subagents"], 12)
        self.assertEqual(sum(cc["crew_sizes"]), 12)

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(crosscheck.CrosscheckConfigError):
            crosscheck.normalise_config({"mode": "yolo"}, {})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, data):
        path = os.path.join(self.tmp.name, "PLAN.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_three_parts_disjoint_and_ordered(self):
        self._write({"version": 1, "parts": [
            {"id": "p1", "write_set": ["a.py"]},
            {"id": "p2", "write_set": ["b.py"]},
            {"id": "p3", "write_set": ["c.py"], "depends_on": ["p1"]},
        ]})
        result = crosscheck.plan_parts(self.tmp.name)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([p["id"] for p in result["parts"]], ["p1", "p2", "p3"])
        self.assertEqual(crosscheck.order_parts(result["parts"]),
                         [["p1", "p2"], ["p3"]])
        self.assertEqual(result["parts"][2]["order"], 1)

    def test_overlapping_write_sets_are_invalid(self):
        self._write({"parts": [
            {"id": "p1", "write_set": ["src/"]},
            {"id": "p2", "write_set": ["src/a.py"]},
        ]})
        result = crosscheck.plan_parts(self.tmp.name)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("overlap" in item for item in result["errors"]))

    def test_one_part_is_invalid(self):
        self._write({"parts": [{"id": "p1", "write_set": ["a.py"]}]})
        result = crosscheck.plan_parts(self.tmp.name)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("at least 2" in item for item in result["errors"]))

    def test_cycle_is_invalid(self):
        self._write({"parts": [
            {"id": "p1", "write_set": ["a.py"], "depends_on": ["p2"]},
            {"id": "p2", "write_set": ["b.py"], "depends_on": ["p1"]},
        ]})
        result = crosscheck.plan_parts(self.tmp.name)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("cycle" in item for item in result["errors"]))

    def test_missing_plan_is_absent(self):
        self.assertEqual(crosscheck.plan_parts(self.tmp.name)["status"], "absent")

    def test_space_joined_write_set_is_split_before_disjointness(self):
        self._write({"parts": [
            {"id": "p1", "write_set": ["src/a.py src/b.py"]},
            {"id": "p2", "write_set": ["src/a.py"]},
        ]})
        result = crosscheck.plan_parts(self.tmp.name)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("overlap" in item for item in result["errors"]))

    def test_plan_globs_are_kept_and_checked_for_overlap(self):
        self._write({"parts": [
            {"id": "p1", "write_set": ["docs/*.md"]},
            {"id": "p2", "write_set": ["docs/b.md"]},
        ]})
        result = crosscheck.plan_parts(self.tmp.name)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("overlap" in item for item in result["errors"]))
        self.assertEqual(result["parts"][0]["write_set"], ["docs/*.md"])

    def test_artifact_roundtrip(self):
        self._write({"parts": [
            {"id": "p1", "write_set": ["a.py"]},
            {"id": "p2", "write_set": ["b.py"]},
        ]})
        record = crosscheck.new_record({"threshold": 10.0}, run_id="r1")
        record["parts"] = crosscheck.plan_parts(self.tmp.name)["parts"]
        record["findings"] = [{"id": "F1", "severity": "blocking",
                               "status": "open", "text": "boom"}]
        path = crosscheck.save_artifact(self.tmp.name, record)
        self.assertTrue(os.path.isfile(path))
        loaded = crosscheck.load_artifact(self.tmp.name)
        self.assertEqual(loaded["status"], "ok")
        self.assertEqual(len(loaded["data"]["parts"]), 2)
        self.assertEqual(loaded["data"]["findings"][0]["id"], "F1")


class PairingTests(unittest.TestCase):
    def test_no_crew_checks_its_own_part(self):
        parts = [_part("p%d" % index, ["f%d.py" % index])
                 for index in range(1, 7)]
        pairs = crosscheck.pairing(parts, ["crew-1", "crew-2", "crew-3"], 1)
        for part_id, pair in pairs.items():
            self.assertNotEqual(pair["builder"], pair["checker"],
                                "part %s self-checks" % part_id)
        self.assertEqual(set(pairs), {part["id"] for part in parts})

    def test_rotation_balances_builders(self):
        parts = [_part("p%d" % index, ["f%d.py" % index])
                 for index in range(1, 5)]
        pairs = crosscheck.pairing(parts, ["crew-1", "crew-2"], 1)
        builders = [pairs["p%d" % i]["builder"] for i in range(1, 5)]
        self.assertEqual(builders.count("crew-1"), 2)
        self.assertEqual(builders.count("crew-2"), 2)

    def test_two_crews_always_cross(self):
        parts = [_part("p%d" % index, ["f%d.py" % index])
                 for index in range(1, 4)]
        pairs = crosscheck.pairing(parts, ["crew-1", "crew-2"], 2)
        for pair in pairs.values():
            self.assertEqual({pair["builder"], pair["checker"]},
                             {"crew-1", "crew-2"})


class ScoringTests(unittest.TestCase):
    def _full(self):
        parts = [_part("p1", ["a.py"]), _part("p2", ["b.py"],
                                              builder="crew-2",
                                              checker="crew-1")]
        findings = [
            {"id": "F1", "part_id": "p1", "severity": "blocking",
             "status": "fixed", "found_by_crew": "crew-2",
             "text": "breaks on empty input",
             "fix": {"crew": "crew-1", "round": 2,
                     "evidence": "command: patch applied"},
             "confirmation": {"crew": "crew-2", "confirmed": True,
                              "evidence": "command: test -q; 12 passed"}},
            {"id": "F2", "part_id": "p1", "severity": "minor",
             "status": "justified", "found_by_crew": "crew-2",
             "text": "style",
             "justification": {"text": "matches the existing public API",
                               "accepted_by_crew": "crew-2"}},
        ]
        requirements = [_requirement("R1", "p1", "crew-2"),
                        _requirement("R2", "p2", "crew-1")]
        return parts, findings, requirements

    def test_all_five_conditions_score_ten(self):
        parts, findings, requirements = self._full()
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["score"], 10.0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["conditions"], {
            "tests": 1.0, "requirements": 1.0, "blocking": 1.0,
            "minors": 1.0, "fixes": 1.0})

    def test_compile_smoke_is_not_a_full_test_suite(self):
        parts, findings, requirements = self._full()
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "smoke_passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["tests"], 0.5)
        self.assertEqual(result["score"], 9.0)
        self.assertFalse(result["passed"])
        self.assertIn("tests_smoke_passed", result["reasons"])

    def test_missing_part_status_cannot_certify_ten(self):
        parts, findings, requirements = self._full()
        parts[0].pop("status")
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertLessEqual(result["score"], 6.0)
        self.assertFalse(result["passed"])
        self.assertIn("part_failed", result["reasons"])

    def test_finding_crew_cannot_fix_and_confirm_the_same_item(self):
        parts, findings, requirements = self._full()
        findings[0]["fix"] = {"crew": "crew-2", "round": 2,
                              "evidence": "self patch"}
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["fixes"], 0.0)
        self.assertEqual(result["score"], 8.0)

    def test_fix_must_be_by_the_builder_or_rescuer(self):
        parts, findings, requirements = self._full()
        findings[0]["fix"] = {"crew": "crew-9", "round": 2,
                              "evidence": "someone else"}
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["fixes"], 0.0)

    def test_unknown_part_requirement_cannot_self_certify(self):
        parts, findings, requirements = self._full()
        requirements[0]["part_id"] = "p-does-not-exist"
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["requirements"], 0.5)
        self.assertLess(result["score"], 10.0)

    def test_requirement_must_be_checked_by_the_paired_checker(self):
        parts, findings, requirements = self._full()
        # crew-3 did not build p1 and is non-empty, but is not p1's checker.
        requirements[0]["checked_by"] = "crew-3"
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["requirements"], 0.5)
        self.assertLess(result["score"], 10.0)

    def test_missing_part_requirement_cannot_self_certify(self):
        parts, findings, requirements = self._full()
        requirements[1].pop("part_id", None)
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["requirements"], 0.5)

    def test_failed_tests_costs_two_points(self):
        parts, findings, requirements = self._full()
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "failed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["score"], 8.0)
        self.assertFalse(result["passed"])
        self.assertIn("tests_failed", result["reasons"])

    def test_open_blocking_costs_two_points(self):
        parts, findings, requirements = self._full()
        findings.append({"id": "F3", "part_id": "p2", "severity": "BLOCKING",
                         "status": "open", "found_by_crew": "crew-2",
                         "text": "still broken"})
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["score"], 8.0)
        self.assertEqual(result["counts"]["blocking_open"], 1)

    def test_unchecked_requirement_is_fractional(self):
        parts, findings, requirements = self._full()
        requirements[1]["evidence"] = ""
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=9.0, brief_count=2)
        self.assertEqual(result["conditions"]["requirements"], 0.5)
        self.assertEqual(result["score"], 9.0)
        self.assertTrue(result["passed"])

    def test_brief_count_larger_than_records_cannot_reach_ten(self):
        parts, findings, requirements = self._full()
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=4)
        self.assertEqual(result["conditions"]["requirements"], 0.5)
        self.assertLess(result["score"], 10.0)

    def test_unconfirmed_fix_is_fractional(self):
        parts, findings, requirements = self._full()
        findings[0]["confirmation"] = {"crew": "crew-1", "confirmed": True,
                                       "evidence": "wrong crew"}
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["fixes"], 0.0)
        self.assertEqual(result["score"], 8.0)

    def test_failed_part_caps_score(self):
        parts, findings, requirements = self._full()
        parts[0]["status"] = "failed"
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=7.0, brief_count=2)
        self.assertLessEqual(result["score"], 6.0)
        self.assertFalse(result["passed"])

    def test_summary_counts_real_and_false_alarm(self):
        findings = [
            {"id": "F1", "severity": "blocking", "lens": "breaker",
             "status": "open"},
            {"id": "F2", "severity": "minor", "lens": "spec",
             "status": "false_alarm"},
            {"id": "F2", "severity": "minor", "lens": "spec",
             "status": "false_alarm"},
        ]
        summary = crosscheck.summarise_findings(findings)
        self.assertEqual(summary["raw"], 3)
        self.assertEqual(summary["unique"], 2)
        self.assertEqual(summary["real"], 1)
        self.assertEqual(summary["false_alarm"], 1)
        self.assertEqual(summary["breaker"], 1)
        self.assertEqual(summary["spec"], 1)


    def test_unknown_blocking_status_stays_open(self):
        parts, findings, requirements = self._full()
        for status in ("resolved", "closed", "wontfix", "done", "justified"):
            finding = dict(findings[0], status=status)
            result = crosscheck.evaluate(
                parts, [finding], requirements, {"status": "passed"},
                threshold=10.0, brief_count=2)
            self.assertEqual(result["conditions"]["blocking"], 0.0, status)
            self.assertEqual(result["score"], 8.0, status)
            self.assertFalse(result["passed"], status)

    def test_builder_cannot_confirm_its_own_fix(self):
        parts, findings, requirements = self._full()
        findings[0]["found_by_crew"] = "crew-1"
        findings[0]["confirmation"] = {"crew": "crew-1", "confirmed": True,
                                       "evidence": "self check"}
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_count=2)
        self.assertEqual(result["conditions"]["fixes"], 0.0)
        self.assertEqual(result["score"], 8.0)

    def test_connector_brief_ids_must_be_covered(self):
        parts, findings, requirements = self._full()
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_items=[{"id": "R1"}, {"id": "R2"}])
        self.assertEqual(result["conditions"]["requirements"], 1.0)
        self.assertEqual(result["score"], 10.0)
        result = crosscheck.evaluate(
            parts, findings, requirements, {"status": "passed"},
            threshold=10.0, brief_items=[{"id": "R1"}, {"id": "R3"}])
        self.assertEqual(result["conditions"]["requirements"], 0.5)
        self.assertLess(result["score"], 10.0)

    def test_small_subagent_budget_is_not_inflated(self):
        self.assertEqual(crosscheck.split_subagents(2, 5), [1, 1])
        self.assertEqual(sum(crosscheck.split_subagents(3, 5)), 3)
        cc = crosscheck.normalise_config(
            {"mode": "crosscheck", "subagents": 2,
             "crosscheck_crews": 5}, {})
        self.assertEqual(cc["subagents"], 2)
        self.assertEqual(sum(cc["crew_sizes"]), 2)
        self.assertEqual(cc["crews"], 2)

    def test_rescue_keeps_failed_and_rescue_crew(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [
            _part("p1", ["a.py"], builder="crew-1", checker="crew-2"),
            _part("p2", ["b.py"], builder="crew-2", checker="crew-1"),
        ]
        record["pairing"] = {"p1": {"checker": "crew-2"},
                             "p2": {"checker": "crew-1"}}
        record = crosscheck.apply_rescue(
            record, {"reason": "stall", "failed_crew": "crew-1",
                     "rescue_crew": "crew-3"}, rescued=True)
        self.assertTrue(record["parts"][0].get("rescued"))
        self.assertEqual(record["parts"][0]["rescue_crew"], "crew-3")
        self.assertEqual(record["rescues"][0]["failed_crew"], "crew-1")
        self.assertEqual(record["rescues"][0]["rescue_crew"], "crew-3")

    def test_failed_rescue_marks_no_part_rescued_and_caps_score(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [_part("p1", ["a.py"])]
        record = crosscheck.apply_rescue(
            record, {"status": "finisher_error"}, rescued=False)
        self.assertFalse(record["parts"][0].get("rescued"))
        self.assertEqual(record["rescues"][0]["status"], "finisher_error")
        result = crosscheck.evaluate(
            record["parts"], [], [_requirement("R1")],
            {"status": "passed"}, record["rescues"], threshold=10.0,
            brief_count=1)
        self.assertLessEqual(result["score"], 8.0)
        self.assertIn("rescue_failed", result["reasons"])



class RoundTests(unittest.TestCase):
    def test_fix_then_stop_at_max_rounds(self):
        cc = {"max_rounds": 3, "threshold": 10.0}
        low = {"score": 8.0, "passed": False}
        high = {"score": 10.0, "passed": True}
        self.assertEqual(crosscheck.loop_decision(1, low, cc), "fix")
        self.assertEqual(crosscheck.loop_decision(2, low, cc), "fix")
        self.assertEqual(crosscheck.loop_decision(3, low, cc), "stop")
        self.assertEqual(crosscheck.loop_decision(1, high, cc), "pass")

    def test_below_threshold_status_and_detail(self):
        record = crosscheck.new_record({"threshold": 10.0, "max_rounds": 3})
        record["rounds"] = [{"round": 1}, {"round": 2}, {"round": 3}]
        evaluation = {"score": 8.0, "threshold": 10.0, "passed": False,
                      "reasons": ["requirements_unchecked"],
                      "conditions": {}}
        record = crosscheck.apply_status(record, evaluation)
        self.assertEqual(record["status"], "crosscheck_below_threshold")
        status, detail = crosscheck.status_mapping(record)
        self.assertEqual(status, "crosscheck_below_threshold")
        self.assertIn("below threshold", detail)

    def test_passed_status(self):
        record = crosscheck.new_record({"threshold": 10.0})
        evaluation = {"score": 10.0, "threshold": 10.0, "passed": True,
                      "reasons": [], "conditions": {}}
        record = crosscheck.apply_status(record, evaluation)
        self.assertEqual(crosscheck.status_mapping(record)[0], "ok")


class RescueTests(unittest.TestCase):
    def test_rescued_part_is_recorded_and_still_needs_external_check(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [
            _part("p1", ["a.py"], builder="crew-1", checker="crew-2"),
            _part("p2", ["b.py"], builder="crew-2", checker="crew-1"),
        ]
        record = crosscheck.apply_rescue(
            record, {"reason": "stall watchdog", "failed_crew": "crew-1"},
            rescued=True)
        self.assertTrue(record["parts"][0].get("rescued"))
        self.assertEqual(record["rescues"][0]["part_id"], "p1")
        self.assertEqual(record["rescues"][0]["checker_crew"], "crew-2")
        evaluation = crosscheck.evaluate(
            record["parts"], record["findings"], [],
            {"status": "passed"}, record["rescues"], threshold=10.0,
            brief_count=1)
        self.assertLess(evaluation["score"], 10.0)

    def test_unknown_explicit_rescue_target_never_widens_to_all_parts(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [
            _part("p1", ["a.py"], status="failed"),
            _part("p2", ["b.py"], status="failed"),
        ]
        record = crosscheck.apply_rescue(
            record, {"part_id": "typo", "status": "ok",
                     "rescue_crew": "finisher"}, rescued=True)
        self.assertEqual([part["status"] for part in record["parts"]],
                         ["failed", "failed"])
        self.assertFalse(any(part.get("rescued") for part in record["parts"]))
        self.assertEqual(record["rescues"][0]["scope"], "unknown_part")
        evaluation = crosscheck.evaluate(
            record["parts"], [], [], {"status": "passed"},
            record["rescues"], threshold=10.0)
        self.assertLessEqual(evaluation["score"], 8.0)
        self.assertIn("rescue_failed", evaluation["reasons"])

    def test_unknown_failed_crew_does_not_rescue_every_part(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [
            _part("p1", ["a.py"], status="failed"),
            _part("p2", ["b.py"], status="failed"),
        ]
        record = crosscheck.apply_rescue(
            record, {"failed_crew": "crew-typo", "status": "ok",
                     "rescue_crew": "finisher"}, rescued=True)
        self.assertEqual([part["status"] for part in record["parts"]],
                         ["failed", "failed"])
        self.assertEqual(record["rescues"][0]["scope"], "unknown_crew")
        self.assertEqual(record["rescues"][0]["part_id"], "unknown-crew")
        evaluation = crosscheck.evaluate(
            record["parts"], [], [], {"status": "passed"},
            record["rescues"], threshold=10.0)
        self.assertLessEqual(evaluation["score"], 8.0)

    def test_rescue_does_not_cap_a_green_result(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["parts"] = [_part("p1", ["a.py"])]
        record = crosscheck.apply_rescue(
            record, {"reason": "whole-run stall"}, rescued=True)
        self.assertTrue(record["rescues"])
        # The 'all' rescue is informational when no part needed a status
        # change; a part that was actually rescued is marked in the test above.
        self.assertEqual(record["rescues"][0]["part_id"], "all")


class BuilderTaskTests(unittest.TestCase):
    def test_crosscheck_replaces_generic_wave_method(self):
        task = build_harness_task(
            "brief", 8, "crosscheck crews", quality=False, crosscheck=True,
            extra_sections=["## Crosscheck mode (mandatory)"])
        self.assertIn("CROSSCHECK CREWS", task)
        self.assertIn("Crosscheck mode", task)
        self.assertNotIn("Pipelined, learning waves", task)
        self.assertNotIn("SUB-LEADER", task)

    def test_normal_task_keeps_generic_wave_method(self):
        task = build_harness_task(
            "brief", 8, "normal", quality=False, crosscheck=False)
        self.assertIn("Pipelined, learning waves", task)


class PromptTests(unittest.TestCase):
    def test_contract_names_crews_and_artifact(self):
        cc = crosscheck.normalise_config({"mode": "crosscheck"}, {})
        parts = [_part("p1", ["a.py"]), _part("p2", ["b.py"],
                                              builder="crew-2",
                                              checker="crew-1")]
        contract = crosscheck.build_contract(
            cc, {"parts": parts}, crosscheck.pairing(parts, ["crew-1", "crew-2"]),
            "brief")
        self.assertIn("crosscheck", contract.lower())
        self.assertIn("CROSSCHECK.json", contract)
        self.assertIn("DISJOINT", contract)
        self.assertIn("BREAKERS", contract)
        self.assertIn("SPEC CHECKERS", contract)

    def test_fix_task_lists_open_blocking(self):
        record = crosscheck.new_record({"threshold": 10.0})
        record["findings"] = [{"id": "F1", "severity": "blocking",
                               "status": "open", "file": "a.py", "line": 3,
                               "text": "boom", "evidence": "cmd"}]
        task = crosscheck.build_fix_task(
            {"max_rounds": 3, "threshold": 10.0}, record,
            {"score": 8.0, "threshold": 10.0, "reasons": ["blocking_open"]},
            2, "brief")
        self.assertIn("F1", task)
        self.assertIn("CROSSCHECK.json", task)
        self.assertIn("a.py:3", task)


if __name__ == "__main__":
    unittest.main()
