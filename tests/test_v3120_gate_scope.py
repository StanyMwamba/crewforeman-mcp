"""S4 gate-scope tests: secret rule, path scope, no-product skip."""
import os
import tempfile
import types
import unittest
from unittest import mock

from deepseek_mcp import gate, path_scope

# Fake key material is assembled at runtime so this file never contains a
# contiguous credential-shaped literal.
_SK_PREFIX = "sk-"
_SK_BODY = "abcdefgh12345678"
_FAKE_SK = _SK_PREFIX + _SK_BODY
_FAKE_PASSWORD = "hunter2" + "hunter2"


def _cfg(**overrides):
    base = {
        "gate_enabled": True,
        "gate_secret_scan": True,
        "gate_timeout_seconds": 10,
        "gate_no_product_mode": "skip",
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _write(root, rel, text):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    return full


def _ok(name):
    return {
        "name": name, "status": "ok", "command": None, "exit_code": 0,
        "seconds": 0.0, "tests": 0, "failures": 0, "errors": 0,
        "output_digest": "", "output_tail": "", "tree_changed": False,
        "timed_out": False, "skipped_reason": None, "findings": [],
    }


class SecretRuleTests(unittest.TestCase):
    def test_non_literals_are_not_findings(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a.py", "\n".join([
                'api_key = int(os.environ.get("API_KEY", "0"))',
                "token = get_token()",
                "secret = some_variable",
                "client_secret = f\"{token}\"",
                "password = os.environ[\"PW\"]",
                "api_key = prefix + suffix",
                "",
            ]))
            cfg = _cfg()
            findings = gate.scan_files(cfg, root, ["a.py"])
            self.assertEqual(findings, [], findings)

    def test_quoted_literal_and_prefix_are_findings(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "quoted.py",
                   "password = '" + _FAKE_PASSWORD + "'\n")
            _write(root, "prefixed.py",
                   "value = \"" + _FAKE_SK + "\"\n")
            cfg = _cfg()
            quoted = gate.scan_files(cfg, root, ["quoted.py"])
            prefixed = gate.scan_files(cfg, root, ["prefixed.py"])
            self.assertTrue(quoted)
            self.assertTrue(any(f["rule_id"] == "generic-secret-assignment"
                                for f in quoted), quoted)
            self.assertTrue(prefixed)
            self.assertTrue(any(f["rule_id"] == "provider-api-key"
                                for f in prefixed), prefixed)


class TreeScanTests(unittest.TestCase):
    def test_3110_tree_has_no_findings_outside_split_fixtures(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = _cfg()
        fixtures = set(path_scope.split_fixture_paths(cfg))
        # Five known redaction/privacy fixtures carry split fake literals;
        # they are the only allowed findings in the whole 3.11.0 tree.
        self.assertEqual(len(fixtures), 5)
        findings = gate.scan_tree(cfg, repo, skip=[".git"])
        outside = [f for f in findings if f["path"] not in fixtures]
        self.assertEqual(outside, [], outside[:5])


class GateScopeTests(unittest.TestCase):
    def _baseline(self, root):
        return gate._snapshot(root)

    def test_docs_log_patch_only_skips_without_checks(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            before = self._baseline(root)
            _write(root, "README.md", "# hi\n")
            _write(root, "logs/run.log", "noise\n")
            _write(root, "fix.patch", "--- a\n")
            with mock.patch.object(gate, "_lint_check",
                                   side_effect=AssertionError("lint ran")), \
                    mock.patch.object(gate, "_tests_check",
                                      side_effect=AssertionError("tests ran")):
                result = gate.run_gate(_cfg(), root, {}, before=before)
            self.assertEqual(result["verdict"], "skipped")
            self.assertEqual(result["skip_reason"], "no_product_code_change")
            self.assertFalse(result["blocked_review"])
            self.assertEqual(result["scope"]["counts"]["product"], 0)
            by_name = {check["name"]: check for check in result["checks"]}
            self.assertEqual(by_name["lint"]["skipped_reason"],
                             "no_product_code_change")
            self.assertEqual(by_name["tests"]["skipped_reason"],
                             "no_product_code_change")
            self.assertEqual(by_name["secrets"]["status"], "ok")

    def test_product_secret_still_fails_skip_branch(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            before = self._baseline(root)
            _write(root, "README.md",
                   "api_key = '" + _FAKE_PASSWORD + "'\n")
            with mock.patch.object(gate, "_lint_check",
                                   side_effect=AssertionError("lint ran")), \
                    mock.patch.object(gate, "_tests_check",
                                      side_effect=AssertionError("tests ran")):
                result = gate.run_gate(_cfg(), root, {}, before=before)
            self.assertEqual(result["verdict"], "failed")
            self.assertTrue(result["blocked_review"])
            secrets = [c for c in result["checks"]
                       if c["name"] == "secrets"][0]
            self.assertEqual(secrets["status"], "failed")

    def test_split_fixture_secret_skipped_but_counts_product(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            before = self._baseline(root)
            _write(root, "tests/test_v3110_gate.py",
                   "TOKEN = \"" + _FAKE_SK + "\"\n")
            lint = mock.Mock(return_value=_ok("lint"))
            tests = mock.Mock(return_value=_ok("tests"))
            with mock.patch.object(gate, "_lint_check", lint), \
                    mock.patch.object(gate, "_tests_check", tests):
                result = gate.run_gate(_cfg(), root, {}, before=before)
            self.assertEqual(tests.call_count, 1)
            self.assertEqual(result["verdict"], "passed")
            self.assertEqual(result["scope"]["counts"]["product"], 1)
            self.assertEqual(result["scope"]["counts"]["split_fixture"], 1)
            secrets = [c for c in result["checks"]
                       if c["name"] == "secrets"][0]
            self.assertEqual(secrets["status"], "ok")
            self.assertEqual(secrets["findings"], [])

    def test_no_baseline_output_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "README.md", "# hi\n")
            with mock.patch.object(gate, "_lint_check",
                                   side_effect=AssertionError("lint ran")), \
                    mock.patch.object(gate, "_tests_check",
                                      side_effect=AssertionError("tests ran")):
                result = gate.run_gate(
                    _cfg(), root,
                    {"output_files": ["README.md", "logs/x.log"]},
                    before=None)
            self.assertEqual(result["verdict"], "skipped")
            self.assertTrue(result["scope"]["baseline_missing"])
            self.assertFalse(result["scope"]["product_outputs"])

    def test_no_baseline_product_output_runs_normal_gate(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "src/app.py", "x = 1\n")
            lint = mock.Mock(return_value=_ok("lint"))
            tests = mock.Mock(return_value=_ok("tests"))
            with mock.patch.object(gate, "_lint_check", lint), \
                    mock.patch.object(gate, "_tests_check", tests):
                result = gate.run_gate(
                    _cfg(), root, {"output_files": ["src/app.py"]},
                    before=None)
            self.assertEqual(result["verdict"], "passed")
            self.assertTrue(result["scope"]["baseline_missing"])
            self.assertTrue(result["scope"]["product_outputs"])
            self.assertEqual(tests.call_count, 1)

    def test_mode_off_restores_normal_gate(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            before = self._baseline(root)
            _write(root, "README.md", "# hi\n")
            lint = mock.Mock(return_value=_ok("lint"))
            tests = mock.Mock(return_value=_ok("tests"))
            with mock.patch.object(gate, "_lint_check", lint), \
                    mock.patch.object(gate, "_tests_check", tests):
                result = gate.run_gate(
                    _cfg(gate_no_product_mode="off"), root, {},
                    before=before)
            self.assertEqual(result["verdict"], "passed")
            self.assertEqual(tests.call_count, 1)


if __name__ == "__main__":
    unittest.main()
