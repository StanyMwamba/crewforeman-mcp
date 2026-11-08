"""Unit tests for the F1 deterministic gate (deepseek_mcp.gate)."""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import gate, review_context, safety


def make_cfg(state_dir, **overrides):
    values = {
        "gate_enabled": True,
        "gate_tests": "auto",
        "gate_test_command": "",
        "gate_lint": "auto",
        "gate_lint_command": "",
        "gate_secret_scan": True,
        "gate_secret_patterns": [],
        "gate_timeout_seconds": 30.0,
        "gate_max_revisions": 2,
        "gate_max_output_chars": 4000,
        "state_dir": state_dir,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        self.state = os.path.join(self.ws, "state")
        self.cfg = make_cfg(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def write(self, rel, text, binary=False):
        path = os.path.join(self.ws, rel)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8"}
        with open(path, mode, **kwargs) as handle:
            handle.write(text)
        return path

    def passing_tests(self):
        self.write(
            "tests/test_ok.py",
            "import unittest\n\n"
            "class Ok(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n")

    def check(self, result, name):
        for item in result["checks"]:
            if item["name"] == name:
                return item
        self.fail("missing check %r in %r" % (name, result["checks"]))

    # -- tests -----------------------------------------------------------

    def test_gate_passes_and_records_signed_evidence(self):
        self.passing_tests()
        before = safety.snapshot_workspace(self.ws)
        run = {}
        result = gate.run_gate(self.cfg, self.ws, run, before=before)
        self.assertEqual(result["verdict"], "passed", result)
        self.assertFalse(result["blocked_review"])
        self.assertTrue(result["tree_sha256"])
        names = [check["name"] for check in result["checks"]]
        self.assertEqual(names, ["secrets", "lint", "tests"])
        tests = self.check(result, "tests")
        self.assertEqual(tests["status"], "ok")
        self.assertEqual(tests["tests"], 1)
        key_id, key = review_context.ensure_evidence_key(self.state)
        self.assertEqual(tests["key_id"], key_id)
        self.assertTrue(tests["signed"])
        self.assertTrue(review_context.verify_signature(
            tests, tests["signature"], key))
        self.assertEqual(len(run["review_test_evidence"]), 1)
        evidence = run["review_test_evidence"][0]
        self.assertEqual(evidence["phase"], "deterministic_gate")
        self.assertTrue(review_context.verify_signature(
            evidence, evidence["signature"], key))
        self.assertIn("passed", gate.format_gate_evidence(result))

    def test_gate_secret_never_echoed(self):
        secret = "sk-" + "Z" * 40
        before = safety.snapshot_workspace(self.ws)
        self.write("config.py", "API_TOKEN = '%s'\n" % secret)
        result = gate.run_gate(self.cfg, self.ws, {}, before=before)
        self.assertEqual(result["verdict"], "failed")
        secrets = self.check(result, "secrets")
        self.assertEqual(secrets["status"], "failed")
        self.assertTrue(secrets["findings"])
        self.assertEqual(len(secrets["findings"][0]["prefix"]), 4)
        serialised = json.dumps(result, default=str)
        self.assertNotIn(secret, serialised)
        self.assertNotIn(secret, gate.format_gate_evidence(result))
        defects, fixes = gate.synthetic_defects(result)
        self.assertTrue(defects)
        self.assertEqual(len(defects), len(fixes))

    def test_gate_secret_inline_allowlist(self):
        before = safety.snapshot_workspace(self.ws)
        self.write(
            "safe.py",
            "# pragma: allowlist secret\n"
            "password = 'correct-horse-battery'\n"
            "api_key = 'another-" "secret-value'  # gitleaks:allow\n")
        result = gate.run_gate(self.cfg, self.ws, {}, before=before)
        self.assertNotEqual(result["verdict"], "failed")
        secrets = self.check(result, "secrets")
        self.assertEqual(secrets["status"], "ok")
        self.assertTrue(secrets["findings"])
        self.assertTrue(all(item["allowlisted"] for item in secrets["findings"]))

    def test_gate_secret_skips_binary_and_large(self):
        before = safety.snapshot_workspace(self.ws)
        self.write("blob.bin", b"\x00AKIAABCDEFGHIJKLMNOP\x00", binary=True)
        self.write("huge.txt", "AKIA" "ABCDEFGHIJKLMNOP\n"
                   + "x" * (gate.MAX_SCAN_BYTES + 16))
        result = gate.run_gate(self.cfg, self.ws, {}, before=before)
        secrets = self.check(result, "secrets")
        self.assertEqual(secrets["findings"], [])
        self.assertGreaterEqual(secrets["files_skipped_binary"], 1)
        self.assertGreaterEqual(secrets["files_skipped_large"], 1)
        self.assertNotEqual(result["verdict"], "failed")

    def test_gate_secret_custom_pattern(self):
        self.cfg.gate_secret_patterns = [r"MYCUSTOM-[A-Z0-9]{6}"]
        before = safety.snapshot_workspace(self.ws)
        self.write("custom.py", "VALUE = 'MYCUSTOM-ABC123'\n")
        result = gate.run_gate(self.cfg, self.ws, {}, before=before)
        self.assertEqual(result["verdict"], "failed")
        secrets = self.check(result, "secrets")
        rules = {item["rule_id"] for item in secrets["findings"]}
        self.assertIn("custom-1", rules)

    def test_gate_explicit_missing_command_fails(self):
        self.cfg.gate_test_command = "crewforeman-no-such-command-xyz"
        result = gate.run_gate(self.cfg, self.ws, {})
        self.assertEqual(result["verdict"], "failed")
        tests = self.check(result, "tests")
        self.assertEqual(tests["status"], "error")
        self.assertIsNone(tests["skipped_reason"])
        # An explicitly configured lint command that cannot start is an error.
        self.cfg = make_cfg(self.state, gate_test_command="",
                            gate_tests="off",
                            gate_lint_command="crewforeman-no-such-lint-xyz")
        result = gate.run_gate(self.cfg, self.ws, {})
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(self.check(result, "lint")["status"], "error")

    def test_gate_absent_lint_tool_is_skip(self):
        self.write("ruff.toml", "line-length = 88\n")
        with mock.patch.object(gate.shutil, "which", return_value=None):
            result = gate.run_gate(self.cfg, self.ws, {})
        lint = self.check(result, "lint")
        self.assertEqual(lint["status"], "skipped")
        self.assertTrue(lint["skipped_reason"].startswith("tool_not_found"))
        self.assertEqual(result["verdict"], "skipped")

    def test_gate_test_timeout_kills_child(self):
        self.cfg.gate_timeout_seconds = 1.0
        self.cfg.gate_tests = [
            sys.executable, "-c", "import time; time.sleep(10)"]
        result = gate.run_gate(self.cfg, self.ws, {})
        tests = self.check(result, "tests")
        self.assertEqual(tests["status"], "timed_out")
        self.assertTrue(tests["timed_out"])
        self.assertEqual(result["verdict"], "failed")
        self.assertLess(result["seconds"], 5.0)

    def test_gate_detects_tree_mutation(self):
        self.passing_tests()
        before = safety.snapshot_workspace(self.ws)
        self.cfg.gate_test_command = [
            sys.executable, "-c",
            "open('mutated.txt', 'w').write('x')"]
        result = gate.run_gate(self.cfg, self.ws, {}, before=before)
        tests = self.check(result, "tests")
        self.assertEqual(tests["status"], "failed")
        self.assertTrue(tests["tree_changed"])
        self.assertEqual(result["verdict"], "failed")
        paths = [item["path"] for item in tests["tree_changes"]]
        self.assertIn("mutated.txt", paths)
        defects, fixes = gate.synthetic_defects(result)
        self.assertTrue(any("mutated" in defect for defect in defects))

    def test_gate_skips_when_no_tests_or_lint(self):
        result = gate.run_gate(self.cfg, self.ws, {})
        self.assertEqual(result["verdict"], "skipped")
        self.assertEqual(result["skip_reason"], "no_tests_or_lint")
        self.assertFalse(result["blocked_review"])
        self.assertEqual(self.check(result, "tests")["status"], "skipped")
        self.assertEqual(self.check(result, "lint")["status"], "skipped")

    def test_gate_disabled_is_skip(self):
        self.cfg.gate_enabled = False
        result = gate.run_gate(self.cfg, self.ws, {})
        self.assertEqual(result["verdict"], "skipped")
        self.assertEqual(result["skip_reason"], "gate_disabled")
        self.assertEqual(result["checks"], [])
        self.assertFalse(result["blocked_review"])

    def test_gate_internal_error_fails_closed(self):
        self.passing_tests()
        with mock.patch.object(review_context, "run_test_suite",
                               side_effect=RuntimeError("kaboom")):
            result = gate.run_gate(self.cfg, self.ws, {})
        self.assertEqual(result["verdict"], "error")
        self.assertTrue(result["blocked_review"])
        self.assertIn("kaboom", result["gate_error"])
        self.assertNotIn(self.ws, result["gate_error"])

    def test_gate_output_tail_capped_and_redacted(self):
        self.cfg.gate_max_output_chars = 120
        secret = "sk-" + "Q" * 40
        self.cfg.gate_tests = [
            sys.executable, "-c",
            "print('x' * 1000); print('%s')" % secret]
        result = gate.run_gate(self.cfg, self.ws, {})
        tests = self.check(result, "tests")
        self.assertEqual(tests["status"], "ok")
        self.assertLessEqual(len(tests["output_tail"]), 120)
        self.assertIn("[redacted-api-key]", tests["output_tail"])
        self.assertNotIn(secret, json.dumps(result, default=str))
        self.assertEqual(result["verdict"], "passed")


if __name__ == "__main__":
    unittest.main()
