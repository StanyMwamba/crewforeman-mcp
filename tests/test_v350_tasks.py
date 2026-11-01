"""Tests for the v3.5.0 durable task store (fakes only, stdlib unittest)."""

import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

from deepseek_mcp.tasks import (  # noqa: E402
    STATUSES,
    ProgressEmitter,
    TaskStore,
    redact,
)


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dsh-tasks-test-")
        self.store = TaskStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _files(self):
        return sorted(glob.glob(os.path.join(self.root, "*")))

    def test_create_get_shape_and_defaults(self):
        task = self.store.create("search", {"query": "hello"})
        self.assertTrue(task["taskId"].startswith("task-"))
        self.assertEqual(task["status"], "working")
        self.assertEqual(task["ttl"], 3600)
        self.assertEqual(task["pollInterval"], 1000)
        self.assertEqual(task["version"], 1)
        self.assertIn("created", task)
        self.assertIn("updated", task)
        self.assertEqual(self.store.get(task["taskId"])["taskId"], task["taskId"])
        path = os.path.join(self.root, task["taskId"] + ".json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["version"], 1)
        self.assertEqual(on_disk["taskId"], task["taskId"])

    def test_create_with_explicit_id_and_duplicate(self):
        task = self.store.create("tool", task_id="build-1")
        self.assertEqual(task["taskId"], "build-1")
        with self.assertRaises(ValueError):
            self.store.create("tool", task_id="build-1")

    def test_invalid_task_ids(self):
        for bad in ("", "../evil", "a/b", "a b", "x" * 129, "../x"):
            with self.assertRaises(ValueError):
                self.store.create("tool", task_id=bad)
        for ok in ("a", "A.b_c-9", "task-0123abcd"):
            self.assertEqual(self.store.create("t", task_id=ok)["taskId"], ok)

    def test_get_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.get("task-nope")

    def test_bad_ttl_and_arguments(self):
        with self.assertRaises(ValueError):
            self.store.create("tool", ttl=0)
        with self.assertRaises(ValueError):
            self.store.create("tool", ttl=-5)
        with self.assertRaises(ValueError):
            self.store.create("tool", arguments={"bad": {1, 2, 3}})
        with self.assertRaises(ValueError):
            self.store.create("")

    def test_arguments_are_summarised_and_secrets_not_stored(self):
        task = self.store.create(
            "http",
            {
                "api_key": "sk-" "live-abcdefghijklmnop",
                "headers": {"Authorization": "Bearer abcdefghij"},
                "query": "plain text",
            },
        )
        with open(os.path.join(self.root, task["taskId"] + ".json"), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("sk-" "live-abcdefghijklmnop", raw)
        self.assertNotIn("abcdefghij", raw)
        self.assertIn("http", raw)
        self.assertNotIn("sk-" "live-abcdefghijklmnop", task.get("summary", ""))

    def test_result_complete_and_error_flag(self):
        task = self.store.create("tool")
        self.assertEqual(
            self.store.result(task["taskId"]),
            {
                "task": self.store.to_mcp(task),
                "result": None,
                "isError": False,
            },
        )
        done = self.store.complete(task["taskId"], {"answer": 42})
        self.assertEqual(done["status"], "completed")
        result = self.store.result(task["taskId"])
        self.assertEqual(result["result"], {"answer": 42})
        self.assertFalse(result["isError"])
        self.assertEqual(result["task"]["status"], "completed")

    def test_fail_redacts_and_marks_error(self):
        task = self.store.create("tool")
        failed = self.store.fail(
            task["taskId"],
            "boom " "sk-" "abcdefghijklmnop and Bearer abcdefghijklmnop",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("REDACTED", failed["error"])
        self.assertNotIn("sk-" "abcdefghijklmnop", failed["error"])
        result = self.store.result(task["taskId"])
        self.assertTrue(result["isError"])
        self.assertIn("REDACTED", result["result"])

    def test_cancel_is_idempotent_and_keeps_terminal_result(self):
        task = self.store.create("tool")
        first = self.store.cancel(task["taskId"], reason="user asked")
        self.assertEqual(first["status"], "cancelled")
        self.assertTrue(first["cancel_requested"])
        second = self.store.cancel(task["taskId"])
        self.assertEqual(first["updated"], second["updated"])
        self.assertEqual(second["cancelReason"], "user asked")
        self.assertTrue(self.store.is_cancelled(task["taskId"]))

        other = self.store.create("tool")
        self.store.complete(other["taskId"], "kept")
        cancelled = self.store.cancel(other["taskId"], "too late")
        self.assertEqual(cancelled["status"], "completed")
        self.assertEqual(self.store.get(other["taskId"])["result"], "kept")
        self.assertTrue(cancelled["cancel_requested"])

        third = self.store.create("tool")
        self.store.cancel(third["taskId"])
        after = self.store.complete(third["taskId"], "revived?")
        self.assertEqual(after["status"], "cancelled")
        self.assertNotIn("result", self.store.get(third["taskId"]))

    def test_cancel_missing_is_false(self):
        self.assertFalse(self.store.is_cancelled("task-missing"))

    def test_progress_monotonic_and_message(self):
        task = self.store.create("tool")
        self.store.progress(task["taskId"], 5, total=10, message="half")
        updated = self.store.progress(task["taskId"], 3, total=10)
        self.assertEqual(updated["progress"], 5)
        self.assertEqual(updated["statusMessage"], "half")
        updated = self.store.progress(task["taskId"], 8, total=10, message="later")
        self.assertEqual(updated["progress"], 8)
        self.assertEqual(updated["total"], 10)
        with self.assertRaises(ValueError):
            self.store.progress(task["taskId"], "lots")

    def test_update_rejects_unknown_status_and_protects_identity(self):
        task = self.store.create("tool")
        with self.assertRaises(ValueError):
            self.store.update(task["taskId"], status="bogus")
        updated = self.store.update(task["taskId"], status="input_required", owner="me")
        self.assertEqual(updated["status"], "input_required")
        self.assertEqual(updated["taskId"], task["taskId"])
        self.assertEqual(updated["created"], task["created"])

    def test_statuses_constant(self):
        self.assertEqual(
            STATUSES,
            ("working", "input_required", "completed", "failed", "cancelled"),
        )

    def test_list_newest_first_and_limit(self):
        ids = []
        for offset, name in enumerate(("a", "b", "c")):
            ids.append(self.store.create("tool", task_id=name, now=1000 + offset)["taskId"])
        listed = self.store.list(now=1001)
        self.assertEqual([t["taskId"] for t in listed], ["c", "b", "a"])
        self.assertEqual(
            [t["taskId"] for t in self.store.list(limit=2, now=1001)], ["c", "b"]
        )
        self.assertEqual(self.store.list(limit=0), [])
        self.assertEqual(len(listed), 3)

    def test_ttl_prune_deletes_file_and_raises(self):
        task = self.store.create("tool", ttl=10, now=1000)
        path = os.path.join(self.root, task["taskId"] + ".json")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.store.get(task["taskId"], now=1005)["taskId"], task["taskId"])
        with self.assertRaises(KeyError):
            self.store.get(task["taskId"], now=1020)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.store.list(now=1030), [])

    def test_ttl_prune_from_list(self):
        old = self.store.create("tool", ttl=5, now=1000)
        fresh = self.store.create("tool", ttl=500, now=1000)
        listed = self.store.list(now=1010)
        self.assertEqual([t["taskId"] for t in listed], [fresh["taskId"]])
        self.assertFalse(os.path.exists(os.path.join(self.root, old["taskId"] + ".json")))

    def test_corrupt_file_is_a_keyerror(self):
        with open(os.path.join(self.root, "broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(KeyError):
            self.store.get("broken")

    def test_no_temp_files_left_behind(self):
        task = self.store.create("tool")
        self.store.progress(task["taskId"], 1, message="x")
        self.store.complete(task["taskId"], "ok")
        self.store.fail(self.store.create("tool")["taskId"], "bad")
        self.store.cancel(self.store.create("tool")["taskId"])
        leftovers = [p for p in self._files() if ".tmp" in os.path.basename(p)]
        self.assertEqual(leftovers, [])
        self.assertEqual(len(glob.glob(os.path.join(self.root, "*.json"))), 3)

    def test_to_mcp_shape(self):
        task = self.store.create("tool", task_id="mcp-1")
        self.assertEqual(
            TaskStore.to_mcp(task),
            {
                "taskId": "mcp-1",
                "status": "working",
                "ttl": 3600,
                "pollInterval": 1000,
            },
        )
        self.assertNotIn("statusMessage", TaskStore.to_mcp(task))
        self.store.progress("mcp-1", 2, message="working hard")
        mapped = TaskStore.to_mcp(self.store.get("mcp-1"))
        self.assertEqual(mapped["statusMessage"], "working hard")
        self.store.cancel("mcp-1")
        self.assertEqual(TaskStore.to_mcp(self.store.get("mcp-1"))["status"], "cancelled")

    def test_redact_helper(self):
        text = redact("key " "sk-" "abcdefghijklmnop Bearer abcdefghijklmnop")
        self.assertNotIn("sk-" "abcdefghijklmnop", text)
        self.assertNotIn("Bearer abcdefghijklmnop", text)


class ProgressEmitterTest(unittest.TestCase):
    def test_payload_and_none_omission(self):
        sent = []
        emitter = ProgressEmitter(sent.append, "tok-1")
        emitter.emit(1, total=10, message="starting")
        emitter.emit(2)
        self.assertEqual(
            sent[0],
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "progressToken": "tok-1",
                    "progress": 1,
                    "total": 10,
                    "message": "starting",
                },
            },
        )
        self.assertEqual(
            sent[1]["params"], {"progressToken": "tok-1", "progress": 2}
        )
        self.assertNotIn("total", sent[1]["params"])
        self.assertNotIn("message", sent[1]["params"])

    def test_dedupes_identical_consecutive_emissions(self):
        sent = []
        emitter = ProgressEmitter(sent.append, "tok")
        emitter.emit(1, total=2)
        emitter.emit(1, total=2)
        emitter.emit(1, total=2, message="now a message")
        emitter.emit(2, total=2)
        emitter.emit(1, total=2)  # not consecutive with the earlier 1
        self.assertEqual([f["params"]["progress"] for f in sent], [1, 1, 2, 1])
        self.assertEqual(emitter.suppressed, 1)

    def test_swallows_sender_exceptions(self):
        def boom(_frame):
            raise RuntimeError("transport gone")

        emitter = ProgressEmitter(boom, "tok")
        self.assertIsNone(emitter.emit(1))
        self.assertIsNone(emitter.emit(2))  # still alive after a failure

    def test_emit_returns_frame(self):
        frame = ProgressEmitter(lambda _f: None, 7).emit(3)
        self.assertEqual(frame["params"]["progressToken"], 7)
        self.assertEqual(frame["params"]["progress"], 3)


if __name__ == "__main__":
    unittest.main()
