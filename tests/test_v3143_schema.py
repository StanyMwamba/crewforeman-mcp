"""FIX 9: the advertised harness_deepseek schema must expose 3.14.x options.

The 3.14.2 restart left a client without the new ``mode`` argument.  The
connector's ``tools/list`` handler is the single source of truth, so this test
exercises that handler (not just the TOOLS literal) and asserts every new
argument is present with the right enum.

Windows-compatible: no shell, no network, no temp files.
"""

import contextlib
import io
import json
import unittest

from deepseek_mcp import server

REQUIRED = {
    "mode": ["standard", "crosscheck"],
    "crosscheck_threshold": None,
    "crosscheck_crews": None,
    "crosscheck_crew_size": None,
    "crosscheck_max_rounds": None,
    "audit_review": None,
    "audit_sample_rate": None,
    "audit_spot_requirements": None,
    "audit_spot_fixes": None,
    "auto_parallel": None,
    "auto_parallel_min_confidence": None,
    "depends_on_policy": ["success", "finished"],
}


def _tools_list():
    """Run the real tools/list handler and parse the response it prints."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server.handle({"jsonrpc": "2.0", "id": 7,
                       "method": "tools/list", "params": {}})
    return json.loads(out.getvalue())


class ToolsListSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools_list()["result"]["tools"]
        self.harness = [tool for tool in self.tools
                        if tool["name"] == "harness_deepseek"][0]
        self.props = self.harness["inputSchema"]["properties"]

    def test_tools_list_contains_harness_deepseek(self):
        self.assertTrue(self.tools)
        self.assertIn("harness_deepseek", [tool["name"] for tool in self.tools])

    def test_every_new_argument_is_advertised(self):
        missing = [name for name in REQUIRED if name not in self.props]
        self.assertEqual(missing, [])

    def test_enums_are_exact(self):
        for name, expected in REQUIRED.items():
            if expected is None:
                continue
            with self.subTest(argument=name):
                self.assertEqual(self.props[name].get("enum"), expected)

    def test_mode_is_a_top_level_argument(self):
        mode = self.props["mode"]
        self.assertEqual(mode.get("type"), "string")
        self.assertIn("default_mode", mode.get("description") or "")

    def test_crew_defaults_are_documented(self):
        self.assertIn("2 crews x 4 agents",
                      self.props["mode"].get("description") or "")
        self.assertEqual(self.props["crosscheck_crews"].get("minimum"), 2)
        self.assertIn("default 4",
                      self.props["crosscheck_crew_size"].get("description")
                      or "")
        self.assertEqual(self.props["depends_on_policy"].get("enum"),
                         ["success", "finished"])

    def test_tools_for_mode_returns_the_same_schema(self):
        all_tools = server.tools_for_mode("all")
        harness = [tool for tool in all_tools
                   if tool["name"] == "harness_deepseek"][0]
        self.assertIn("mode", harness["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
