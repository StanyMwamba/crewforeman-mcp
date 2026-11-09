"""Release metadata checks for crewforeman-mcp 3.13.0 (S6).

These are static/fast checks that keep the shipped version strings in code,
packaging and the smoke client aligned with the release.
"""

import re
import tempfile
import unittest
from pathlib import Path

import deepseek_mcp
from deepseek_mcp import config, observability, server


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
SMOKE = ROOT / "scripts" / "smoke_mcp.py"
EXPECTED = "3.13.0"


class VersionIdentityTests(unittest.TestCase):
    def test_package_config_and_observability_agree(self):
        self.assertEqual(deepseek_mcp.__version__, EXPECTED)
        self.assertEqual(config.SERVER_VERSION, EXPECTED)
        self.assertEqual(observability.SERVICE_VERSION, EXPECTED)
        self.assertEqual(deepseek_mcp.__version__, config.SERVER_VERSION)
        self.assertEqual(config.SERVER_VERSION, observability.SERVICE_VERSION)

    def test_pyproject_declares_the_release_version(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', text, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), EXPECTED)

    def test_smoke_client_info_declares_the_release_version(self):
        text = SMOKE.read_text(encoding="utf-8")
        match = re.search(
            r'"clientInfo":\s*\{[^}]*"version":\s*"([^"]+)"', text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), EXPECTED)
        self.assertIn('"version": "%s"' % EXPECTED, text)


class ServerInfoTests(unittest.TestCase):
    def cfg(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return config.load_config(
            config_path=Path(tmp.name) / "missing.json", env={})

    def test_initialize_server_info_matches_config(self):
        response = server.dispatch(
            "initialize", {"protocolVersion": "2025-06-18"}, cfg=self.cfg())
        self.assertNotIn("error", response)
        server_info = response["result"]["serverInfo"]
        self.assertEqual(server_info["name"], "crewforeman-mcp")
        self.assertEqual(server_info["version"], config.SERVER_VERSION)
        self.assertEqual(server_info["version"], EXPECTED)

    def test_tools_list_uses_the_same_server_version(self):
        # tools/list has no serverInfo block of its own; the advertised
        # server version lives in the initialize handshake, so pin both the
        # handshake and the config constant it is built from here.
        response = server.dispatch("tools/list", cfg=self.cfg())
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["tools"])
        self.assertEqual(config.SERVER_VERSION, EXPECTED)
        init = server.dispatch(
            "initialize", {"protocolVersion": "2025-06-18"}, cfg=self.cfg())
        self.assertEqual(
            init["result"]["serverInfo"]["version"], config.SERVER_VERSION)


if __name__ == "__main__":
    unittest.main()
