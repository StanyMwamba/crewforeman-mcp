"""Tests for the durable checkpoint manifest (stdlib unittest only)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp.checkpoints import Checkpoint, resume_plan  # noqa: E402


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "checkpoints"
        self.cp = Checkpoint(self.root, "run-1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_load(self):
        manifest = self.cp.create(["builder", "review"], metadata={"wave": 1})
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["plan"], ["builder", "review"])
        self.assertEqual(manifest["phases"]["builder"]["status"], "pending")
        loaded = self.cp.load()
        self.assertEqual(loaded["run_id"], "run-1")
        self.assertEqual(loaded["metadata"], {"wave": 1})
        self.assertEqual(Checkpoint.find(self.root, "run-1")["run_id"], "run-1")
        self.assertIsNone(Checkpoint.find(self.root, "nope"))

    def test_begin_idempotent_keeps_started(self):
        self.cp.create(["builder", "review"])
        first = self.cp.begin("builder", note="x")
        started = first["phases"]["builder"]["started"]
        second = self.cp.begin("builder", note="y")
        entry = second["phases"]["builder"]
        self.assertEqual(entry["started"], started)
        self.assertEqual(entry["status"], "running")
        self.assertEqual(entry["data"]["note"], "y")
        self.assertTrue(entry["started"])

    def test_finish_creates_missing_begin_and_next_phase(self):
        self.cp.create(["builder", "review"])
        self.assertEqual(self.cp.next_phase(), "builder")
        self.cp.finish("builder", summary="ok")
        entry = self.cp.phase("builder")
        self.assertEqual(entry["status"], "done")
        self.assertIsNotNone(entry["started"])
        self.assertIsNotNone(entry["finished"])
        self.assertEqual(self.cp.next_phase(), "review")
        self.cp.finish("review")
        self.assertIsNone(self.cp.next_phase())
        self.assertIsNone(self.cp.phase("missing"))

    def test_failed_phase_is_resumed_by_next_phase(self):
        self.cp.create(["builder", "review"])
        self.cp.begin("builder")
        self.cp.fail("builder", "boom " "sk-" "abcdefgh12345678")
        entry = self.cp.phase("builder")
        self.assertEqual(entry["status"], "failed")
        self.assertNotIn("sk-" "abcdefgh12345678", entry["data"]["error"])
        self.assertEqual(self.cp.next_phase(), "builder")

    def test_interrupted_marks_running(self):
        self.cp.create(["builder", "review"])
        self.cp.begin("builder")
        manifest = self.cp.interrupted("ctrl-c Bearer abcdefgh12345")
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual(manifest["phases"]["builder"]["status"], "interrupted")
        self.assertEqual(manifest["phases"]["review"]["status"], "pending")
        self.assertNotIn("Bearer abcdefgh12345", manifest["interrupted"])
        self.assertEqual(manifest["phases"]["builder"]["data"]["interrupted"],
                         manifest["interrupted"])

    def test_finalize_stores_redacted_result(self):
        self.cp.create(["builder"])
        manifest = self.cp.finalize({"review": "approved", "token": "sk-" "secret12345678"})
        self.assertEqual(manifest["status"], "finished")
        self.assertTrue(manifest["finished"])
        self.assertIn("approved", manifest["result"])
        self.assertNotIn("sk-" "secret12345678", manifest["result"])

    def test_corrupt_and_truncated_file_load_none(self):
        self.cp.create(["builder"])
        with open(str(self.cp.path), "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "run_id": "run-1", "pla')
        self.assertIsNone(self.cp.load())
        # A corrupt manifest must not crash begin/finish: they recreate.
        manifest = self.cp.begin("builder")
        self.assertEqual(manifest["phases"]["builder"]["status"], "running")

    def test_atomic_no_tmp_leftovers(self):
        self.cp.create(["builder", "review"])
        for _ in range(5):
            self.cp.begin("builder")
            self.cp.finish("builder")
        self.cp.finalize("done")
        names = os.listdir(str(self.root))
        self.assertFalse([n for n in names if n.endswith(".tmp")], names)
        self.assertIn("run-1.json", names)
        with open(str(self.cp.path), encoding="utf-8") as handle:
            json.load(handle)

    def test_unsupported_and_unsafe_ids(self):
        for bad in ("", "../escape", "a/b", "x" * 129):
            with self.assertRaises(ValueError):
                Checkpoint(self.root, bad)

    def test_resume_plan(self):
        manifest = {
            "phases": {
                "builder": {"status": "done"},
                "review": {"status": "pending"},
            }
        }
        result = resume_plan(["builder", "review"], manifest)
        self.assertEqual(result["skip"], ["builder"])
        self.assertEqual(result["resume"], "review")
        self.assertFalse(result["restart_builder"])
        manifest["phases"]["builder"]["status"] = "interrupted"
        result = resume_plan(["builder", "review"], manifest)
        self.assertEqual(result["skip"], [])
        self.assertEqual(result["resume"], "builder")
        self.assertTrue(result["restart_builder"])
        self.assertEqual(
            resume_plan(["builder", "review"], None),
            {"skip": [], "resume": "builder", "restart_builder": True},
        )
        self.assertEqual(resume_plan([], None),
                         {"skip": [], "resume": None, "restart_builder": False})


if __name__ == "__main__":
    unittest.main()
