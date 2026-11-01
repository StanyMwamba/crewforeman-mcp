import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class EntryScriptTests(unittest.TestCase):
    def test_stdio_tools_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({
                "DEEPSEEK_MCP_CONFIG": str(Path(tmp) / "config.json"),
                "DEEPSEEK_MCP_LOG_DIR": str(Path(tmp) / "runs"),
                "DEEPSEEK_MCP_JOBS_DIR": str(Path(tmp) / "jobs"),
                "DSH_WORKSPACE": str(Path(tmp) / "workspace"),
                "DSH_SKILLS_DIR": str(ROOT / "skills"),
                "DEEPSEEK_API_KEY": "test-key-not-real",
            })
            proc = subprocess.run(
                [sys.executable, str(ROOT / "deepseek_mcp.py")],
                input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                      '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            self.assertEqual(len(responses), 2)
            tools = next(item["result"]["tools"] for item in responses
                         if item.get("id") == 2)
            self.assertIn("harness_deepseek", {tool["name"] for tool in tools})

    def test_mac_mode_only_exposes_mac_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({
                "DEEPSEEK_MCP_CONFIG": str(Path(tmp) / "config.json"),
                "DEEPSEEK_MCP_LOG_DIR": str(Path(tmp) / "runs"),
                "DEEPSEEK_MCP_JOBS_DIR": str(Path(tmp) / "jobs"),
                "DEEPSEEK_MCP_PID_FILE": str(Path(tmp) / "pid"),
                "DSH_SKILLS_DIR": str(ROOT / "skills"),
            })
            proc = subprocess.run(
                [sys.executable, str(ROOT / "deepseek_mcp.py"), "--tools", "mac"],
                input='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            response = json.loads(proc.stdout.splitlines()[0])
            names = {tool["name"] for tool in response["result"]["tools"]}
            self.assertEqual(names, {"keep_awake", "take_screenshot",
                                     "restart_client_app"})


if __name__ == "__main__":
    unittest.main()
