import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import server
from deepseek_mcp.config import SERVER_VERSION, load_config
from tests.fakes import FakeDeepSeekServer, base_env

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"


class ServerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Never point the registry at the checkout: a listing may migrate and
        # write index.json/.versions/.capture into dev/skills.
        self.skills = self.root / "skills"
        shutil.copytree(str(REPO_SKILLS), str(self.skills))

    def cfg(self, **extra):
        env = base_env(self.root, **extra)
        env["DSH_SKILLS_DIR"] = str(self.skills)
        return load_config(config_path=self.root / "config.json", env=env)

    def test_initialize(self):
        response = server.dispatch("initialize", {"protocolVersion": "2025-06-18"}, cfg=self.cfg())
        self.assertEqual(response["result"]["serverInfo"]["name"],
                         "crewforeman-mcp")
        self.assertEqual(response["result"]["serverInfo"]["version"], "3.12.1")
        instructions = response["result"]["instructions"]
        self.assertIn("checkpoints every phase", instructions)
        self.assertIn("ok_below_gate", instructions)
        self.assertTrue(instructions.startswith(f"crewforeman-mcp {SERVER_VERSION}:"))
        self.assertNotIn("v3.1", instructions)
        unsupported = server.dispatch("initialize", {"protocolVersion": "1999-01-01"},
                                      cfg=self.cfg())
        self.assertEqual(unsupported["result"]["protocolVersion"], "2025-11-25")
        mac = server.dispatch("initialize", {}, tool_mode="mac", cfg=self.cfg())
        self.assertIn("Host helper tools only", mac["result"]["instructions"])
        self.assertNotIn("harness_deepseek", mac["result"]["instructions"])

    def test_tools_list_keeps_all_legacy_tools(self):
        response = server.dispatch("tools/list", cfg=self.cfg())
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        for name in ("ask_deepseek", "swarm_deepseek", "harness_deepseek",
                     "deepseek_job_status", "list_deepseek_runs", "read_deepseek_run",
                     "keep_awake", "take_screenshot", "restart_client_app"):
            self.assertIn(name, tools)
        for name in ("rate_deepseek_run", "list_skills", "read_skill"):
            self.assertIn(name, tools)
        # v3.5 client names remain callable even though the generic names are
        # the ones advertised by tools/list.
        for legacy, current in server.LEGACY_TOOL_ALIASES.items():
            self.assertNotIn(legacy, tools)
            self.assertIn(current, tools)
        harness = tools["harness_deepseek"]["inputSchema"]
        self.assertNotIn("subagents", harness.get("required", []))
        self.assertIn("quality", harness["properties"])
        self.assertIn("quality_rounds", harness["properties"])
        self.assertIn("independent_review", harness["properties"])
        self.assertIn("skill", harness["properties"])
        self.assertIn("EXPERIMENTAL", tools["swarm_deepseek"]["description"])
        self.assertIn("separate fresh harness reviewer", tools["harness_deepseek"]["description"].lower())
        description = tools["harness_deepseek"]["description"]
        self.assertIn("0 agents for a simple question", description)
        self.assertIn("4 for code", description)
        self.assertIn("never above `default_subagents` (16)", description)
        self.assertNotIn("16+", description)
        self.assertNotIn("soft-capped at 32", description)
        schema = tools["harness_deepseek"]["inputSchema"]["properties"]["subagents"]["description"]
        self.assertIn("0 for simple", schema)
        self.assertIn("6/8/12", schema)
        self.assertIn("Explicit values are kept exactly", schema)
        self.assertNotIn("16+", schema)
        self.assertNotIn("soft-capped at 32", schema)

    def test_every_tool_schema_is_consistent(self):
        response = server.dispatch("tools/list", cfg=self.cfg())
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(len(names), len(set(names)))
        for tool in response["result"]["tools"]:
            schema = tool["inputSchema"]
            self.assertEqual(schema.get("type"), "object")
            properties = schema.get("properties") or {}
            for required in schema.get("required") or []:
                self.assertIn(required, properties, tool["name"])
            self.assertIn("description", tool)

    def test_tools_list_mac_mode(self):
        response = server.dispatch("tools/list", tool_mode="mac", cfg=self.cfg())
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, set(server.MAC_TOOL_NAMES))

    def test_unknown_tool_is_an_mcp_error(self):
        response = server.dispatch("tools/call", {"name": "nope", "arguments": {}}, cfg=self.cfg())
        self.assertEqual(response["error"]["code"], -32602)

    def test_tool_error_is_returned_as_content(self):
        response = server.dispatch("tools/call", {"name": "read_deepseek_run",
                                                  "arguments": {"run_id": "missing"}}, cfg=self.cfg())
        self.assertTrue(response["result"]["isError"])
        self.assertIn("Error:", response["result"]["content"][0]["text"])

    def test_skills_tools(self):
        listing = server.dispatch("tools/call", {"name": "list_skills", "arguments": {}}, cfg=self.cfg())
        self.assertFalse(listing["result"]["isError"])
        self.assertIn("poster", listing["result"]["content"][0]["text"])
        content = server.dispatch("tools/call", {"name": "read_skill",
                                                 "arguments": {"name": "code"}}, cfg=self.cfg())
        self.assertIn("Code / app", content["result"]["content"][0]["text"])

    def test_keep_awake_status_does_not_start_anything(self):
        response = server.dispatch("tools/call", {"name": "keep_mac_awake",
                                                  "arguments": {"action": "status"}}, cfg=self.cfg())
        self.assertFalse(response["result"]["isError"])

    def test_ask_via_protocol_with_background_job(self):
        with FakeDeepSeekServer() as fake:
            cfg = self.cfg(base_url=fake.base_url)
            with mock.patch.dict(os.environ, base_env(self.root, base_url=fake.base_url), clear=False):
                response = server.dispatch("tools/call", {"name": "ask_deepseek", "arguments": {
                    "prompt": "protocol hello", "wait_seconds": 30}}, cfg=cfg)
            self.assertFalse(response["result"]["isError"], response)
            self.assertIn("DeepSeek run", response["result"]["content"][0]["text"])

    def test_ping_and_unknown_method(self):
        self.assertEqual(server.dispatch("ping", cfg=self.cfg())["result"], {})
        self.assertEqual(server.dispatch("bogus/method", cfg=self.cfg())["error"]["code"], -32601)

    def test_task_augmented_call_emits_progress_notifications(self):
        frames = []
        server.set_notifier(frames.append)
        self.addCleanup(server.set_notifier, None)

        def fake_handler(args, cfg):
            callback = server._job_handler_options().get("progress_cb")
            self.assertIsNotNone(callback)
            callback("step one")
            callback({"progress": "step two"})
            return "task finished"

        cfg = self.cfg()
        with mock.patch.dict(server.HANDLERS, {"ask_deepseek": fake_handler}):
            response = server.dispatch("tools/call", {
                "name": "ask_deepseek",
                "arguments": {"prompt": "progress test"},
                "_meta": {"progressToken": "tok-1"},
                "task": {"ttl": 60000},
            }, cfg=cfg)
        task_id = response["result"]["task"]["taskId"]

        deadline = time.monotonic() + 5.0
        task = None
        while time.monotonic() < deadline:
            task = server._task_store(cfg).get(task_id)
            if task.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.02)
        self.assertEqual((task or {}).get("status"), "completed")

        progress = [frame for frame in frames
                    if frame.get("method") == "notifications/progress"]
        self.assertEqual([frame["params"]["progress"] for frame in progress], [1, 2])
        self.assertTrue(all(frame["params"]["progressToken"] == "tok-1"
                            for frame in progress))

        # tasks/list is workspace-scoped and must work without a taskId.
        listing = server.dispatch("tasks/list", cfg=cfg)
        self.assertNotIn("error", listing)
        self.assertIn(task_id, [item["taskId"] for item in listing["result"]["tasks"]])


if __name__ == "__main__":
    unittest.main()
