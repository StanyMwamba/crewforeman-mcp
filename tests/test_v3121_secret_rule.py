"""S5 3.12.1 tests: JSON/YAML literal secret detection."""
import os
import tempfile
import time
import types
import unittest

from deepseek_mcp import gate, path_scope

# Fake key material is assembled at runtime so this file never contains a
# contiguous credential-shaped literal for the tree scan to flag.
_FAKE_LITERAL = "hunter2" + "hunter2"
_BARE_LITERAL = "abc123" + "def456"
_ENV_REF = "${" + "DB_" + "PASSWORD"
_UPPER_ENV = "YOUR_" + "API_KEY"


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


class JsonLiteralTests(unittest.TestCase):
    def test_json_quoted_literals_are_findings(self):
        with tempfile.TemporaryDirectory() as root:
            body = ('{\n"password": "' + _FAKE_LITERAL + '",\n'
                    '"api_key": "' + _FAKE_LITERAL + '"\n}\n')
            _write(root, "config.json", body)
            findings = gate.scan_files(_cfg(), root, ["config.json"])
            self.assertEqual(len(findings), 2, findings)
            self.assertTrue(all(f["rule_id"] == "generic-secret-assignment"
                                for f in findings), findings)
            self.assertEqual([f["line"] for f in findings], [2, 3])


class YamlLiteralTests(unittest.TestCase):
    def test_yaml_bare_literal_is_finding(self):
        with tempfile.TemporaryDirectory() as root:
            body = "api_key: " + _BARE_LITERAL
            _write(root, "settings.yaml", body + "\n")
            findings = gate.scan_files(_cfg(), root, ["settings.yaml"])
            self.assertEqual(len(findings), 1, findings)
            self.assertEqual(findings[0]["rule_id"], "generic-secret-literal")

    def test_literal_like_helper(self):
        self.assertTrue(gate._literal_like(_FAKE_LITERAL))
        self.assertTrue(gate._literal_like(_BARE_LITERAL))
        # Base64 content keeps its '+' and trailing '=' padding.
        self.assertTrue(gate._literal_like("QUJD+REVG" + "R0hJ"))
        for value in (
                "short", "DB_PASSWORD", "some_variable", "os.environ",
                "your_api_key", "example-token-1", "sample-token-1",
                "changeme1234", "change-me-1234", "placeholder-1",
                "replace-me-1", "dummy-token-1", "redacted-1", "xxxxyyyy",
                "todo-token-1", "nonevalue", "nullvalue", "truevalue",
                "falsevalue", _ENV_REF, "get_token()",
                "prefix+suffix", 'f"{value}"', "abc def ghi"):
            self.assertFalse(gate._literal_like(value), value)

    def test_non_literals_produce_no_findings(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "code.py", "\n".join([
                'api_key = int(os.environ.get("API_KEY", "0"))',
                "token = get_token()",
                "secret = some_variable",
                'client_secret = f"{token}"',
                'password = os.environ["PW"]',
                "api_key = prefix + suffix",
                "",
            ]))
            findings = gate.scan_files(_cfg(), root, ["code.py"])
            self.assertEqual(findings, [], findings)

    def test_placeholders_produce_no_findings(self):
        with tempfile.TemporaryDirectory() as root:
            json_body = ('{"password": "' + _ENV_REF + '"}\n'
                         '{"api_key": "' + _UPPER_ENV + '"}\n')
            _write(root, "config.json", json_body)
            _write(root, "settings.yaml", "api_key: null\n")
            findings = gate.scan_files(
                _cfg(), root, ["config.json", "settings.yaml"])
            self.assertEqual(findings, [], findings)


class AnnotationAndReferenceTests(unittest.TestCase):
    """Typed Python code is not a hard-coded secret.

    A bare-literal rule that only checks length turns ``token: Optional[str]``
    into a finding: the value capture stops at the type subscript.  These
    annotations and references are ordinary code and must stay clean.
    """

    def test_type_annotations_produce_no_findings(self):
        lines = (
            "token: Optional[str]",
            "password: Optional[str] = None",
            "api_key: Optional[str] = None",
            "api_key: SecretStr",
            "token: Callable[[], str]",
            "token: TokenManager",
            "secret: SecretStore",
        )
        for line in lines:
            with self.subTest(line=line), tempfile.TemporaryDirectory() as root:
                _write(root, "annotations.py", line + "\n")
                findings = gate.scan_files(_cfg(), root, ["annotations.py"])
                self.assertEqual(findings, [], (line, findings))

    def test_typed_module_produces_no_findings(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "settings.py", "\n".join([
                "from typing import Callable, Optional",
                "",
                "from pydantic import BaseModel, SecretStr",
                "",
                "",
                "class Settings(BaseModel):",
                "    api_key: Optional[str] = None",
                "    api_key: SecretStr",
                "    password: Optional[str] = None",
                "    token: Callable[[], str]",
                "",
                "",
                "class Cache:",
                "    token: TokenManager",
                "    secret: SecretStore",
                "",
            ]))
            findings = gate.scan_files(_cfg(), root, ["settings.py"])
            self.assertEqual(findings, [], findings)

    def test_identifier_references_produce_no_findings(self):
        for line in ("token: getToken", "secret: loadSecret",
                     "api_key: readApiKey"):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as root:
                _write(root, "refs.py", line + "\n")
                findings = gate.scan_files(_cfg(), root, ["refs.py"])
                self.assertEqual(findings, [], (line, findings))

    def test_type_names_are_not_literals(self):
        for value in ("Optional", "SecretStr", "TokenManager", "SecretStore"):
            self.assertFalse(gate._literal_like(value), value)
        # Real bare literal values and base64 blobs remain literal-like.
        self.assertTrue(gate._literal_like(_BARE_LITERAL))
        self.assertTrue(gate._literal_like("QUJD+REVG" + "R0hJ"))


class SplitFixtureTests(unittest.TestCase):
    def test_configured_split_fixture_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "tests/known_fixture.py",
                   "password = '" + _FAKE_LITERAL + "'\n")
            _write(root, "app.py", "password = '" + _FAKE_LITERAL + "'\n")
            cfg = _cfg(gate_split_fixture_paths=["tests/known_fixture.py"])
            self.assertEqual(path_scope.split_fixture_paths(cfg),
                             ["tests/known_fixture.py"])
            # scan_files is the raw scanner; run_gate/_secret_check applies
            # the split-fixture exclusion.
            self.assertEqual(
                len(gate.scan_files(cfg, root, ["tests/known_fixture.py"])), 1)
            result = gate._secret_check(
                cfg, root, None, None, time.monotonic() + 10,
                only_paths=["tests/known_fixture.py"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["findings"], [])
            product = gate.scan_files(cfg, root, ["app.py"])
            self.assertEqual(len(product), 1, product)


class WholeTreeTests(unittest.TestCase):
    def test_tree_has_no_findings_outside_split_fixtures(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = _cfg()
        fixtures = set(path_scope.split_fixture_paths(cfg))
        findings = gate.scan_tree(cfg, repo, skip=[".git"])
        outside = [f for f in findings if f["path"] not in fixtures]
        self.assertEqual(
            [(f["path"], f["rule_id"]) for f in outside[:5]], [],
            outside[:5])
