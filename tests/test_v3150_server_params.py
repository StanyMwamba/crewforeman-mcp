import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import server
from deepseek_mcp.config import load_config
from tests.fakes import base_env


class HarnessRoutingParamsTests(unittest.TestCase):
    """MiMo routing params on the harness_deepseek tool surface (3.15.0)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cfg(self, **extra):
        env = base_env(self.root, **extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def harness_props(self):
        response = server.dispatch("tools/list", cfg=self.cfg())
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        return tools["harness_deepseek"]["inputSchema"]["properties"]

    def test_crosscheck_crew_params_in_schema(self):
        props = self.harness_props()
        for name in ("crosscheck_crew_provider", "crosscheck_crew_model"):
            self.assertIn(name, props, name)
            self.assertEqual(props[name]["type"], "string", name)
            self.assertTrue(props[name].get("description"), name)
        provider_desc = props["crosscheck_crew_provider"]["description"]
        model_desc = props["crosscheck_crew_model"]["description"]
        self.assertIn("'mimo'", provider_desc)
        self.assertIn("'xiaomi/mimo-v2.6-pro'", provider_desc)
        self.assertIn("'mimo'", model_desc)
        self.assertIn("'xiaomi/mimo-v2.6-pro'", model_desc)

    def test_builder_and_reviewer_descriptions_mention_mimo(self):
        props = self.harness_props()
        for name in ("builder_model", "builder_provider",
                     "reviewer_model", "reviewer_provider"):
            self.assertEqual(props[name]["type"], "string", name)
            self.assertTrue(props[name].get("description"), name)
        self.assertIn("'mimo'", props["builder_provider"]["description"])
        self.assertIn("'xiaomi/mimo-v2.6-pro'", props["builder_model"]["description"])
        self.assertIn("'mimo'", props["reviewer_provider"]["description"])
        self.assertIn("'xiaomi/mimo-v2.6-pro'", props["reviewer_model"]["description"])

    def test_mimo_variant_param_in_schema(self):
        props = self.harness_props()
        self.assertIn("mimo_variant", props)
        self.assertEqual(props["mimo_variant"]["type"], "string")
        self.assertTrue(props["mimo_variant"].get("description"))
        self.assertIn("mimo", props["mimo_variant"]["description"])

    def test_no_sibling_tool_exposes_crosscheck_crew_params(self):
        response = server.dispatch("tools/list", cfg=self.cfg())
        for tool in response["result"]["tools"]:
            if tool["name"] == "harness_deepseek":
                continue
            props = tool["inputSchema"].get("properties") or {}
            self.assertNotIn("crosscheck_crew_provider", props, tool["name"])
            self.assertNotIn("crosscheck_crew_model", props, tool["name"])
            self.assertNotIn("mimo_variant", props, tool["name"])

    def test_mimo_routing_args_pass_through_to_run_harness(self):
        args = {
            "prompt": "demo task",
            "builder_provider": "mimo",
            "reviewer_provider": "mimo",
            "crosscheck_crew_provider": "mimo",
            "crosscheck_crew_model": "xiaomi/mimo-v2.6-pro",
        }
        cfg = self.cfg()
        with mock.patch("deepseek_mcp.harness.run_harness",
                        return_value=("done", {"id": "r1"}, None)) as run:
            text = server.tool_harness(dict(args), cfg)
        self.assertEqual(run.call_count, 1)
        self.assertIn("done", text)
        passed = run.call_args[0][0]
        self.assertEqual(passed["builder_provider"], "mimo")
        self.assertEqual(passed["reviewer_provider"], "mimo")
        self.assertEqual(passed["crosscheck_crew_provider"], "mimo")
        self.assertEqual(passed["crosscheck_crew_model"], "xiaomi/mimo-v2.6-pro")

    def test_tools_call_dispatch_forwards_routing_args_unchanged(self):
        captured = {}

        def fake_handler(handler_args, cfg):
            captured.update(handler_args)
            return "ok"

        arguments = {
            "prompt": "demo task",
            "builder_provider": "mimo",
            "builder_model": "xiaomi/mimo-v2.6-pro",
            "reviewer_provider": "mimo",
            "reviewer_model": "xiaomi/mimo-v2.6-pro",
            "crosscheck_crew_provider": "mimo",
            "crosscheck_crew_model": "xiaomi/mimo-v2.6-pro",
            "mimo_variant": "v2.6",
        }
        cfg = self.cfg()
        with mock.patch.dict(server.HANDLERS, {"harness_deepseek": fake_handler}):
            response = server.dispatch("tools/call", {"name": "harness_deepseek",
                                                      "arguments": dict(arguments)},
                                       cfg=cfg)
        self.assertFalse(response["result"]["isError"], response)
        for name, value in arguments.items():
            self.assertEqual(captured.get(name), value, name)

    def test_no_runtime_schema_validation_arguments_pass_unchanged(self):
        # dispatch() only requires arguments to be an object: the server does
        # not JSON-schema-validate tool input, so wrong types reach the
        # handler unchanged (the inputSchema types guide clients).
        args = {
            "prompt": "demo task",
            "crosscheck_crew_provider": 123,
            "builder_provider": ["mimo"],
        }
        cfg = self.cfg()
        with mock.patch("deepseek_mcp.harness.run_harness",
                        return_value=("done", {"id": "r1"}, None)) as run:
            server.tool_harness(dict(args), cfg)
        passed = run.call_args[0][0]
        self.assertEqual(passed["crosscheck_crew_provider"], 123)
        self.assertEqual(passed["builder_provider"], ["mimo"])


if __name__ == "__main__":
    unittest.main()
