import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from bench import run_benchmark as bench
from deepseek_mcp.config import load_config
from deepseek_mcp.store import save_run
from tests.fakes import base_env

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    def __init__(self, run_id="run-bench-1", job_id="job-bench-1"):
        self.run_id = run_id
        self.job_id = job_id
        self.calls = []
        self.status_calls = 0

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "harness_deepseek":
            return ("[DeepSeek job %s is still running in the background (started).]"
                    % self.job_id)
        if name == "deepseek_job_status":
            self.status_calls += 1
            if self.status_calls < 2:
                return "[Job %s still running: working (updated now).]" % self.job_id
            return ("[DeepSeek Harness run %s | 12.5 s | quality 8.8/10 | saved: x]"
                    % self.run_id)
        raise AssertionError(name)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        shutil.copytree(str(ROOT / "skills"), str(self.skills))

    def test_run_benchmark_collects_score_time_and_cost(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        save_run({"id": "run-bench-1", "kind": "harness", "created": "2026-01-01T00:00:00+00:00",
                  "title": "bench", "model": "harness default", "status": "ok", "prompt": "p",
                  "quality_score": 8.8, "seconds": 12.5, "estimated_cost_usd": 0.0123,
                  "files": []}, cfg)
        task = {"key": "poster", "title": "Poster", "prompt": "make a poster"}
        output = self.root / "results.md"
        client = FakeClient()
        rows = bench.run_benchmark(client, tasks=[task], cfg=cfg, output=str(output),
                                   timeout=5, poll=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 8.8)
        self.assertEqual(rows[0]["seconds"], 12.5)
        self.assertEqual(rows[0]["cost"], 0.0123)
        text = output.read_text(encoding="utf-8")
        self.assertIn("Poster", text)
        self.assertIn("8.80/10", text)
        self.assertIn("$0.012300", text)
        self.assertEqual(client.status_calls, 2)

    def test_run_benchmark_records_error(self):
        cfg = load_config(config_path=self.root / "config.json", env=base_env(self.root))
        task = {"key": "letter", "title": "Letter", "prompt": "write"}

        class Broken:
            def call_tool(self, name, arguments):
                raise RuntimeError("boom")

        rows = bench.run_benchmark(Broken(), tasks=[task], cfg=cfg, timeout=1, poll=0)
        self.assertEqual(rows[0]["status"], "error: boom")

    def test_mcp_client_handshake_and_tool_call(self):
        env = base_env(self.root)
        env["DSH_SKILLS_DIR"] = str(self.skills)
        config_path = env["DEEPSEEK_MCP_CONFIG"]
        client = bench.MCPClient(python=sys.executable, server=str(ROOT / "deepseek_mcp.py"),
                                 env=env, config=config_path)
        try:
            text = client.call_tool("list_skills", {})
        finally:
            client.close()
        self.assertIn("poster", text)
        self.assertIn("code", text)

    def test_missing_run_is_an_error(self):
        cfg = load_config(config_path=self.root / "config.json", env=base_env(self.root))
        task = {"key": "poster", "title": "Poster", "prompt": "p"}
        row = bench.collect_row(task, "[DeepSeek Harness run missing-run | 3 s | saved: x]", cfg)
        self.assertTrue(row["status"].startswith("error:"))
        self.assertIn("run not saved", row["status"])

    def test_dry_run_writes_without_client(self):
        output = self.root / "results.md"
        result = bench.render_results(
            [{"task": "Poster", "score": None, "seconds": None, "cost": None,
              "status": "not run", "run_id": None}], output, dry_run=True)
        text = Path(result).read_text(encoding="utf-8")
        self.assertIn("DRY RUN", text)
        self.assertIn("Poster", text)

    def test_fixed_task_set(self):
        self.assertEqual([task["key"] for task in bench.TASKS], ["poster", "letter", "cli"])
        self.assertTrue(all(task["prompt"] for task in bench.TASKS))


if __name__ == "__main__":
    unittest.main()
