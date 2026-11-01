"""v3.6.0 cancellation integration tests (stdlib unittest, fakes only).

Covers direct run_harness cancellation, notifications/cancelled plumbing for
both requestId and taskId, and a tools/call end-to-end cancellation through
server.dispatch. No network, no real DSH, no paid API.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, server
from deepseek_mcp.config import load_config
from tests import fakes


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = fakes.write_fake_dsh(self.root / "fake-dsh")
        self.env = fakes.base_env(self.root, fake_dsh=self.fake)
        self.runs = self.root / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self):
        return load_config(config_path=self.root / "config.json", env=dict(self.env))

    def _saved_runs(self):
        saved = []
        for path in sorted(self.runs.glob("*.json")):
            try:
                saved.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return saved

    def test_direct_run_cancel_stops_process_and_persists_cancelled(self):
        cfg = self._cfg()
        event = threading.Event()
        outcome = {}

        def worker():
            try:
                harness.run_harness(
                    {"prompt": "cancel me directly", "workspace": str(self.workspace),
                     "subagents": 0, "quality": False, "quality_rounds": 0},
                    cfg, cancel_check=event.is_set)
            except BaseException as exc:  # noqa: BLE001 - test records any failure
                outcome["error"] = exc

        env = dict(self.env)
        env.update({"FAKE_DSH_MODE": "sleep", "FAKE_DSH_SLEEP": "10"})
        thread = threading.Thread(target=worker, daemon=True)
        started = time.monotonic()
        with mock.patch.dict(os.environ, env, clear=False):
            thread.start()
            time.sleep(0.5)
            event.set()
            thread.join(timeout=8)
        elapsed = time.monotonic() - started

        self.assertFalse(thread.is_alive(), "run_harness ignored cancellation")
        self.assertIn("error", outcome, "run_harness did not raise on cancellation")
        self.assertIsInstance(outcome["error"], RuntimeError)
        self.assertIn("cancelled", str(outcome["error"]).lower())
        # The fake would sleep 10 s; a prompt return proves the child was stopped.
        self.assertLess(elapsed, 6.0, "child process was not stopped quickly")
        statuses = [run.get("status") for run in self._saved_runs()]
        self.assertIn("cancelled", statuses,
                      "persisted run status was not cancelled: %r" % (statuses,))

    def test_handle_notification_sets_request_and_task_events(self):
        request_event = threading.Event()
        with server._inflight_lock:
            server._inflight["req-42"] = request_event
        try:
            server.handle_notification({
                "jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": "req-42"}})
            self.assertTrue(request_event.is_set(),
                            "requestId cancellation was not routed")
        finally:
            with server._inflight_lock:
                server._inflight.pop("req-42", None)

        task_id = "task-42"
        task_event = threading.Event()
        server._task_events[task_id] = task_event
        try:
            server.handle_notification({
                "jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"taskId": task_id}})
            self.assertTrue(task_event.is_set(),
                            "taskId cancellation was not routed")
        finally:
            server._task_events.pop(task_id, None)

    def test_tools_call_cancel_returns_iserror(self):
        cfg = self._cfg()
        event = threading.Event()
        started_marker = self.root / "child-started.txt"
        reply = {}
        failure = {}

        def worker():
            try:
                reply["value"] = server.dispatch(
                    "tools/call",
                    {"name": "harness_deepseek",
                     "arguments": {"prompt": "cancel dispatch", "subagents": 0,
                                   "quality": False,
                                   "workspace": str(self.workspace)}},
                    "all", cfg,
                    context={"request_id": "r1", "cancel_event": event})
            except BaseException as exc:  # noqa: BLE001 - test records any failure
                failure["error"] = exc

        env = dict(self.env)
        env.update({"FAKE_DSH_MODE": "sleep", "FAKE_DSH_SLEEP": "10",
                    "FAKE_DSH_LOG": str(started_marker)})
        thread = threading.Thread(target=worker, daemon=True)
        with mock.patch.dict(os.environ, env, clear=False):
            thread.start()
            deadline = time.monotonic() + 5
            while not started_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(started_marker.exists(),
                            "fake DSH child never started")
            event.set()
            thread.join(timeout=8)

        self.assertFalse(thread.is_alive(), "dispatch ignored cancellation")
        self.assertNotIn("error", failure, "dispatch raised: %r" % (failure,))
        result = reply.get("value") or {}
        payload = result.get("result") or {}
        self.assertTrue(payload.get("isError"),
                        "cancelled tools/call was not isError: %r" % (result,))
        text = " ".join(item.get("text", "") for item in payload.get("content") or [])
        self.assertIn("cancelled", text.lower())


if __name__ == "__main__":
    unittest.main()
