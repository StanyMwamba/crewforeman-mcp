"""Offline tests for the live crosscheck acceptance driver (stdlib only).

Run from this directory:  python3 -m unittest -v test_candidate
No connector is started and no network is touched; --dry-run writes a record.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "live_crosscheck_under_test", HERE.parent / "scripts" / "live_crosscheck.py")
lc = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = lc
_SPEC.loader.exec_module(lc)


class LiveCrosscheckDriverTests(unittest.TestCase):
    def test_is_terminal_statuses(self):
        for status in ("done", "DONE", "error", "failed", "cancelled",
                       "crosscheck_below_threshold"):
            self.assertTrue(lc.is_terminal(status), status)
        for status in ("running", "queued", "submitted", "", None, 3):
            self.assertFalse(lc.is_terminal(status), status)

    def test_build_record_extracts_nested_crosscheck(self):
        payload = {"run": {
            "status": "done",
            "stage": "complete",
            "tokens": {"input_tokens": 123, "output_tokens": 45,
                       "cache_read_tokens": 6},
            "crosscheck": {
                "parts": [{"id": "p1"}, {"id": "p2"}],
                "crew_pairing": {"p1": "crew-b", "p2": "crew-a"},
                "crew_score": 10.0,
                "threshold": 10.0,
                "audit": {"result": "pass"},
                "rescues": [{"part": "p1"}],
            },
        }}
        record = lc.build_record(payload, "workspace", 12.34, prompt="toy")
        self.assertEqual(record["status"], "done")
        self.assertTrue(record["terminal"])
        self.assertEqual(len(record["parts"]), 2)
        self.assertEqual(record["crew_pairing"]["p1"], "crew-b")
        self.assertEqual(record["crew_score"], 10.0)
        self.assertTrue(record["crew_score_vs_threshold"]["passed"])
        self.assertEqual(record["audit"], {"result": "pass"})
        self.assertEqual(record["rescue_count"], 1)
        self.assertEqual(record["tokens"],
                         {"input": 123, "output": 45, "cache": 6})
        self.assertAlmostEqual(record["elapsed_seconds"], 12.34, places=1)

    def test_dry_run_writes_json_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "crosscheck-workspace"
            code = lc.main(["--dry-run", "--workspace", str(workspace)])
            self.assertEqual(code, 0)
            result = workspace / lc.RESULT_NAME
            self.assertTrue(result.is_file())
            record = json.loads(result.read_text(encoding="utf-8"))
            for key in ("parts", "crew_pairing", "crew_score", "threshold",
                        "audit", "rescues", "elapsed_seconds", "tokens",
                        "status", "error"):
                self.assertIn(key, record)
            self.assertEqual(record["status"], "dry_run")
            self.assertTrue(record["dry_run"])
            self.assertEqual(record["threshold"], 10.0)
            self.assertEqual(record["tokens"],
                             {"input": 0, "output": 0, "cache": 0})

    def test_job_arguments_target_crosscheck_mode(self):
        args = lc.job_arguments(str(Path(tempfile.gettempdir()) / "ws"), 5)
        self.assertEqual(args["mode"], "crosscheck")
        self.assertEqual(args["subagents"], 2)
        self.assertTrue(args["quality"])
        self.assertTrue(args["independent_review"])
        self.assertEqual(args["crosscheck_threshold"], 10.0)
        self.assertLessEqual(args["job_max_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
