"""3.14.2 audit: risk detection, sampling, spot checks, parsing, re-audit."""

import json
import os
import tempfile
import unittest

from deepseek_mcp import audit


def _cfg(state_dir):
    return {"state_dir": state_dir, "audit_forced_jobs": 5,
            "audit_spot_requirements": 3, "audit_spot_fixes": 2,
            "audit_risk_globs": [], "audit_seed": 0, "audit_review": None}


class RiskTests(unittest.TestCase):
    def test_default_risky_paths(self):
        for path in ("deepseek_mcp/sandbox_windows.py",
                     "deepseek_mcp/jobs.py",
                     "deepseek_mcp/loop_guard.py",
                     "install.sh",
                     ".github/workflows/ci.yml",
                     "lib/credentials.py"):
            result = audit.detect_risk("ordinary task", [path])
            self.assertTrue(result["risky"], path)
            self.assertTrue(result["reasons"], path)

    def test_brief_risk_words(self):
        result = audit.detect_risk(
            "Change the sandbox password and permission checks", [])
        self.assertTrue(result["risky"])
        words = " ".join(item["match"] for item in result["reasons"])
        for word in ("sandbox", "password", "permission"):
            self.assertIn(word, words)

    def test_clean_job_is_not_risky(self):
        result = audit.detect_risk("Add a docs page and a unit test",
                                   ["docs/PARALLEL.md", "tests/test_x.py"])
        self.assertFalse(result["risky"])
        self.assertEqual(result["reasons"], [])

    def test_extra_globs_are_additive(self):
        cfg = _cfg("/tmp") | {"audit_risk_globs": ["billing/**"]}
        self.assertTrue(audit.detect_risk("x", ["billing/pay.py"], cfg)["risky"])
        self.assertTrue(audit.detect_risk("x", ["deepseek_mcp/jobs.py"], cfg)["risky"])

    def test_extract_diff_paths(self):
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n"
            "diff --git a/docs/b.md b/docs/b.md\n"
            "--- a/docs/b.md\n+++ b/docs/b.md\n"
            "--- /dev/null\n+++ b/new.py\n")
        self.assertEqual(audit.extract_diff_paths(diff),
                         ["src/a.py", "docs/b.md", "new.py"])


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    def _prepare(self, **kwargs):
        defaults = dict(
            run={"id": kwargs.pop("job_id", "job-1")},
            workspace=self.tmp.name, brief="add tests",
            changed_paths=["tests/test_x.py"], requirements=[],
            fixes=[], crosscheck_mode=True, gate_passed=True,
            audit_review=True, sample_rate=0.0, seed=123)
        defaults.update(kwargs)
        return audit.prepare(self.cfg, **defaults)

    def test_full_rate_always_audits(self):
        plan = self._prepare(sample_rate=1.0)
        self.assertTrue(plan["sampled"])
        self.assertTrue(plan["record"]["enabled"])

    def test_zero_rate_samples_out_low_risk(self):
        plan = self._prepare(sample_rate=0.0)
        self.assertFalse(plan["sampled"])
        self.assertEqual(plan["record"]["verdict"], "sampled_out")

    def test_risky_is_never_sampled_out(self):
        plan = self._prepare(sample_rate=0.0, brief="fix the sandbox",
                             changed_paths=["deepseek_mcp/sandbox_linux.py"])
        self.assertTrue(plan["sampled"])
        self.assertEqual(plan["record"]["rate_source"], "risk")
        self.assertTrue(plan["risky"])
        self.assertTrue(plan["risk_why"])

    def test_same_seed_same_decision(self):
        first = self._prepare(sample_rate=0.5, seed=99)
        state, _ = audit.load_state(self.cfg, self.tmp.name)
        state["forced_remaining"] = 0
        audit.save_state(self.cfg, self.tmp.name, state)
        second = self._prepare(sample_rate=0.5, seed=99)
        self.assertEqual(first["sampled"], second["sampled"])

    def test_failed_claim_forces_next_five_jobs(self):
        status = audit.record_claim_failure(self.cfg, "job-1")
        self.assertEqual(status["forced_remaining"], 5)
        sampled = []
        for index in range(6):
            plan = self._prepare(job_id="job-%d" % (index + 2), sample_rate=0.0)
            sampled.append(plan["sampled"])
        self.assertEqual(sampled, [True, True, True, True, True, False])

    def test_disabled_without_crosscheck_or_gate(self):
        plan = self._prepare(crosscheck_mode=False)
        self.assertFalse(plan["enabled"])
        self.assertEqual(plan["reason"], "not_crosscheck")
        plan = self._prepare(gate_passed=False)
        self.assertFalse(plan["enabled"])
        self.assertEqual(plan["reason"], "gate_not_passed")

    def test_audit_review_false_disables(self):
        plan = self._prepare(audit_review=False)
        self.assertFalse(plan["enabled"])
        self.assertEqual(plan["reason"], "audit_review_false")

    def test_corrupt_state_forces_audit(self):
        path = os.path.join(self.tmp.name, "audit_state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        plan = self._prepare(sample_rate=0.0)
        self.assertTrue(plan["sampled"])
        self.assertEqual(plan["record"]["rate_source"], "state_failure")

    def test_state_is_persisted_between_calls(self):
        self._prepare(sample_rate=1.0)
        state, error = audit.load_state(self.cfg, self.tmp.name)
        self.assertIsNone(error)
        self.assertEqual(state["last_job_id"], "job-1")
        self.assertTrue(state["last_decision"]["sampled"])


class SpotTests(unittest.TestCase):
    def _requirements(self, count=6):
        return [{"id": "R%d" % index, "text": "requirement %d" % index,
                 "evidence_command": "python -m unittest tests.test_%d" % index}
                for index in range(1, count + 1)]

    def _fixes(self, count=4):
        return [{"id": "FIX-%d" % index, "text": "fix %d" % index,
                 "confirmed_by": "crew-2", "evidence_command": "cmd %d" % index}
                for index in range(1, count + 1)]

    def test_selection_is_deterministic_and_without_replacement(self):
        first = audit.select_spots(self._requirements(), self._fixes(),
                                   seed=7, n_req=3, n_fix=2)
        second = audit.select_spots(self._requirements(), self._fixes(),
                                    seed=7, n_req=3, n_fix=2)
        self.assertEqual([item["id"] for item in first["requirements"]],
                         [item["id"] for item in second["requirements"]])
        self.assertEqual(len(first["requirements"]), 3)
        self.assertEqual(len(first["fixes"]), 2)
        ids = [item["id"] for item in first["requirements"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(first["coverage_gap"])

    def test_short_pool_records_coverage_gap(self):
        spot = audit.select_spots(self._requirements(2), self._fixes(1),
                                  seed=7, n_req=3, n_fix=2)
        self.assertEqual(len(spot["requirements"]), 2)
        self.assertEqual(len(spot["fixes"]), 1)
        self.assertTrue(spot["coverage_gap"])
        self.assertEqual(spot["pool_sizes"], {"requirements": 2, "fixes": 1})

    def test_zero_requests_select_nothing(self):
        spot = audit.select_spots(self._requirements(), self._fixes(),
                                  seed=7, n_req=0, n_fix=0)
        self.assertEqual(spot["requirements"], [])
        self.assertEqual(spot["fixes"], [])


class PromptParseTests(unittest.TestCase):
    def test_prompt_block_requires_evidence_and_json(self):
        spot = audit.select_spots(
            [{"id": "R1", "text": "feature one",
              "evidence_command": "python -m unittest tests.test_one"}],
            [{"id": "FIX-1", "text": "fix one", "confirmed_by": "crew-2",
              "evidence_command": "python -m unittest tests.test_one"}],
            seed=5)
        block = audit.prompt_block(
            {"spot": spot, "record": {"sampled": True}},
            crew_evidence="crew-2 checked R1",
            test_evidence="tests: ok")
        self.assertIn("AUDIT_JSON", block)
        self.assertIn("python -m unittest tests.test_one", block)
        self.assertIn("crew claims that did not hold", block.lower())

    def test_parse_json_block(self):
        report = (
            "## REVIEW\nQuality score: 9.0\n"
            "<!-- AUDIT_JSON\n"
            '{"score": 8.5, "blocking": [{"id": "AUD-1", "text": "gap", '
            '"evidence": "cmd: fail"}], '
            '"crew_claims_that_did_not_hold": [{"claim": "tests pass", '
            '"why": "they fail", "evidence": "cmd"}], '
            '"spot_checks": [{"id": "R1", "result": "fail"}]}\n'
            "-->\n")
        result = audit.parse_audit(report, {})
        self.assertTrue(result["ran"])
        self.assertEqual(result["score"], 8.5)
        self.assertEqual(result["blocking_count"], 1)
        self.assertEqual(result["claims"][0]["claim"], "tests pass")
        self.assertEqual(result["spot_checks"][0]["id"], "R1")

    def test_parse_headings_fallback(self):
        report = (
            "## Connector audit\n- Audit score: 7.5\n- Audit blocking: 0\n"
            "### Crew claims that did not hold\n"
            "- claim: all tests pass | why: skipped\n")
        result = audit.parse_audit(report, {})
        self.assertTrue(result["ran"])
        self.assertEqual(result["score"], 7.5)
        self.assertEqual(result["blocking_count"], 0)
        self.assertEqual(len(result["claims"]), 1)

    def test_missing_audit_section_is_not_reported(self):
        result = audit.parse_audit("Quality score: 9.0\nBlocking defects: 0")
        self.assertFalse(result["ran"])

    def test_finish_sets_forced_rate_after_failed_claim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = _cfg(tmp.name)
        record = {"job_id": "job-42", "sampled": True}
        report = ('<!-- AUDIT_JSON\n{"score": 8.0, "blocking": [], '
                  '"crew_claims_that_did_not_hold": [{"claim": "x", '
                  '"why": "y", "evidence": "z"}]}\n-->')
        result = audit.finish(record, report, cfg)
        self.assertTrue(result["ran"])
        self.assertTrue(result["forced_rate_set"])
        state, _ = audit.load_state(cfg, tmp.name)
        self.assertEqual(state["forced_remaining"], 5)

    def test_reaudit_block_only_lists_prior_items(self):
        block = audit.reaudit_block(
            {"blocking": []},
            [{"id": "AUD-1", "text": "still broken", "evidence": "cmd"}])
        self.assertIn("AUD-1", block)
        self.assertIn("only", block.lower())
        self.assertNotIn("full audit", block.lower().split("do not repeat")[0])


class CrosscheckAuditInputsTests(unittest.TestCase):
    def test_requirements_and_confirmed_fixes(self):
        record = {"requirements": [
            {"id": "R1", "text": "one", "checked_by": "crew-2",
             "evidence": "cmd"},
            {"id": "R2", "text": "optional", "required": False,
             "checked_by": "crew-2", "evidence": "cmd"}],
            "findings": [
                {"id": "F1", "text": "fix", "status": "fixed",
                 "found_by_crew": "crew-2",
                 "confirmation": {"crew": "crew-2", "confirmed": True,
                                  "evidence": "cmd"}},
                {"id": "F2", "text": "unconfirmed", "status": "fixed",
                 "found_by_crew": "crew-2",
                 "confirmation": {"crew": "crew-1", "confirmed": True,
                                  "evidence": "cmd"}}]}
        self.assertEqual([item["id"] for item in
                          audit.requirements_for_audit(record)], ["R1"])
        self.assertEqual([item["id"] for item in
                          audit.confirmed_fixes(record)], ["F1"])


if __name__ == "__main__":
    unittest.main()
