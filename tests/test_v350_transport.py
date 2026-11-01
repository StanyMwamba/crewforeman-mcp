"""Tests for the documented DSH SDK stdio JSON-RPC transport.

A fake ``dsh`` script is generated into a temporary directory; no real dsh
process, network, or credentials are used.  Behaviour is selected through
``FAKE_DSH_MODE`` so the same script covers happy path, hangs, junk output,
early exit, and a shutdown-ignoring child.
"""
import json
import os
import stat
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from deepseek_mcp import transport  # noqa: E402


FAKE_DSH_BODY = r'''
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_DSH_MODE", "normal")

argv_file = os.environ.get("FAKE_DSH_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def respond(rid, result):
    emit({"jsonrpc": "2.0", "id": rid, "result": result})


if MODE == "exit_early":
    sys.exit(3)

if MODE == "junk":
    sys.stdout.write("this is not a json-rpc frame\n")
    sys.stdout.flush()
    sys.exit(4)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        frame = json.loads(line)
    except ValueError:
        continue
    rid = frame.get("id")
    method = frame.get("method")
    params = frame.get("params") or {}
    if method == "initialize":
        if MODE == "hang_init":
            continue
        params_file = os.environ.get("FAKE_DSH_PARAMS_FILE")
        if params_file:
            with open(params_file, "w", encoding="utf-8") as fh:
                json.dump(params, fh)
        respond(rid, {"serverInfo": {"name": "fake-dsh", "version": "0.0.1"},
                      "echo": params})
    elif method == "session/prompt":
        respond(rid, {"messageId": "msg-1"})
        emit({"jsonrpc": "2.0", "method": "session.event", "params": {
            "sessionId": "sess-1",
            "event": {"type": "message", "data": {"message": {
                "role": "assistant", "content": "first draft"}}}}})
        emit({"jsonrpc": "2.0", "method": "session.event", "params": {
            "sessionId": "sess-1",
            "event": {"data": {
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "final answer"}]},
                "usage": {"inputTokens": 10, "cacheReadTokens": 4,
                          "outputTokens": 7}}}}})
        emit({"jsonrpc": "2.0", "method": "session.status",
              "params": {"sessionId": "sess-1", "status": "idle"}})
    elif method == "session/wait":
        if MODE == "hang_wait":
            continue
        respond(rid, {})
    elif method == "shutdown":
        if MODE == "stubborn":
            time.sleep(60)
        respond(rid, {})
        break
    else:
        respond(rid, {})
'''


class TransportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "fake-dsh"
        body = FAKE_DSH_BODY.replace("__PYTHON__", sys.executable)
        self.script.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        self.script.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self, mode="normal"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_DSH_")}
        env["FAKE_DSH_MODE"] = mode
        env["FAKE_DSH_ARGV_FILE"] = str(self.tmp / "argv.json")
        env["FAKE_DSH_PARAMS_FILE"] = str(self.tmp / "params.json")
        return env

    def _client(self, mode="normal", timeout=10.0):
        return transport.SdkClient(str(self.script), str(self.tmp),
                                   env=self._env(mode), timeout=timeout)

    # -- happy path --------------------------------------------------------
    def test_initialize_prompt_wait(self):
        client = self._client()
        try:
            client.start()
            info = client.initialize(max_tokens=2048, timeout=5.0)
            self.assertEqual(info["name"], "fake-dsh")

            argv = json.loads((self.tmp / "argv.json").read_text(encoding="utf-8"))
            self.assertEqual(argv[1:3], ["--profile", "sdk"])
            self.assertEqual(argv[0], str(self.script))

            params = json.loads((self.tmp / "params.json").read_text(encoding="utf-8"))
            self.assertEqual(params["provider"], "deepseek-official")
            self.assertEqual(params["model"], "deepseek-flash")
            self.assertEqual(params["reasoningEffort"], "max")
            self.assertEqual(params["maxTokens"], 2048)

            message_id = client.prompt("hello there", timeout=5.0)
            self.assertEqual(message_id, "msg-1")
            result = client.wait(timeout=5.0)
            self.assertEqual(result["answer"], "final answer")
            self.assertEqual(result["usage"], {"input_tokens": 10,
                                               "cache_read_tokens": 4,
                                               "output_tokens": 7})
            self.assertEqual(result["session_id"], "sess-1")
            self.assertEqual(result["status"], "idle")
            self.assertGreaterEqual(len(result["events"]), 2)
            self.assertNotIn("first draft", result["answer"])
        finally:
            client.close(timeout=1.0)

    def test_run_happy_path(self):
        result = self._client().run("say hi", timeout=8.0)
        self.assertEqual(result["answer"], "final answer")
        self.assertEqual(result["transport"], "sdk")
        self.assertIsInstance(result["seconds"], float)
        self.assertGreaterEqual(result["seconds"], 0.0)

    # -- tolerant parsing (no child process needed) ------------------------
    def test_tolerant_event_shapes(self):
        client = transport.SdkClient("unused", ".", env={})
        client._handle_notification({"method": "session.event", "params": {
            "session": {"sessionId": "s-2"},
            "event": {"message": {"role": "assistant", "content": "hi there"},
                      "data": {"usage": {"input_tokens": 3,
                                         "output_tokens": 5}}}}})
        self.assertEqual(client._text, "hi there")
        self.assertEqual(client._usage["input_tokens"], 3)
        self.assertEqual(client._usage["output_tokens"], 5)
        self.assertEqual(client._session_id, "s-2")

        client._handle_notification({"method": "session.event", "params": {
            "event": {"data": {"message": {"role": "user",
                                           "content": "ignore me"}}}}})
        self.assertEqual(client._text, "hi there")

        client._handle_notification({"method": "session.event", "params": {
            "event": {"data": {"message": {
                "role": "assistant",
                "content": {"content": "nested answer"}}}}}})
        self.assertEqual(client._text, "nested answer")

        client._handle_notification({"method": "session.event", "params": {
            "event": {"message": {"role": "assistant", "content": [
                {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}}})
        self.assertEqual(client._text, "a\nb")

        client._handle_notification({"method": "session.status",
                                     "params": {"status": "idle"}})
        self.assertEqual(client._status, "idle")

    # -- failures ----------------------------------------------------------
    def test_wait_timeout_is_bounded(self):
        client = self._client("hang_wait")
        try:
            client.start()
            client.initialize(timeout=5.0)
            client.prompt("hang", timeout=5.0)
            started = time.monotonic()
            with self.assertRaises(transport.TransportError):
                client.wait(timeout=0.3)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            client.close(timeout=1.0)

    def test_non_json_stdout_is_sdk_unavailable(self):
        client = self._client("junk", timeout=3.0)
        try:
            client.start()
            with self.assertRaises(transport.SdkUnavailable):
                client.initialize(timeout=3.0)
        finally:
            client.close(timeout=1.0)

    def test_exit_before_initialize_is_sdk_unavailable(self):
        client = self._client("exit_early", timeout=3.0)
        try:
            client.start()
            with self.assertRaises(transport.SdkUnavailable):
                client.initialize(timeout=3.0)
        finally:
            client.close(timeout=1.0)

    def test_missing_binary_is_sdk_unavailable(self):
        client = transport.SdkClient(str(self.tmp / "no-such-dsh"), str(self.tmp),
                                     env=self._env())
        with self.assertRaises(transport.SdkUnavailable):
            client.start()

    def test_close_kills_stubborn_child_quickly(self):
        client = self._client("stubborn", timeout=10.0)
        client.start()
        client.initialize(timeout=5.0)
        proc = client._proc
        started = time.monotonic()
        client.close(timeout=2.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertIsNotNone(proc.poll())

    def test_errors_do_not_leak_env_secrets(self):
        env = self._env("junk")
        env["DEEPSEEK_API_KEY"] = "sk-" "verysecretvalue123456789"
        client = transport.SdkClient(str(self.script), str(self.tmp),
                                     env=env, timeout=3.0)
        try:
            client.start()
            with self.assertRaises(transport.SdkUnavailable) as ctx:
                client.initialize(timeout=3.0)
        finally:
            client.close(timeout=1.0)
        text = str(ctx.exception)
        self.assertNotIn("verysecretvalue", text)
        self.assertNotIn("sk-" "verysecret", text)

    def test_fallback_reason(self):
        self.assertIsNotNone(transport.fallback_reason(
            transport.SdkUnavailable("dsh exited")))
        self.assertIsNone(transport.fallback_reason(
            transport.TransportError("session/wait timed out")))
        self.assertIsNone(transport.fallback_reason(RuntimeError("boom")))

    def test_command_override_keeps_caller_wrapper(self):
        wrapped = ["sandbox-exec", "-p", "(version 1)", str(self.script),
                   "--profile", "sdk"]
        client = transport.SdkClient("unused", str(self.tmp), env={},
                                     command=wrapped)
        self.assertEqual(client._cmd, wrapped)

    def test_run_cancel_check_stops_the_child(self):
        client = self._client("hang_wait", timeout=10.0)
        started = time.monotonic()
        with self.assertRaises(transport.Cancelled):
            client.run("hang", timeout=10.0, cancel_check=lambda: True)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIsNotNone(client._proc.poll())


class HarnessSdkWiringTests(unittest.TestCase):
    def test_os_sandbox_wraps_sdk_child_and_forwards_cancel(self):
        from deepseek_mcp import harness
        from deepseek_mcp.config import load_config

        captured = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                captured["init"] = kwargs
                self._session_id = "fake-sdk"

            def run(self, *args, **kwargs):
                captured["run"] = kwargs
                return {"answer": "ok", "events": [], "usage": {}}

            def close(self, timeout=None):
                pass

        policy = {"os_sandbox": True, "sandbox": "read-only",
                  "preset": "read-only", "approval": "ask"}
        cancel = lambda: True
        with TemporaryDirectory() as ws:
            cfg = load_config(config_path=Path(ws) / "missing.json", env={})
            with mock.patch.object(harness.safety, "seatbelt_usable",
                                   return_value=True), \
                    mock.patch.object(harness.transport_mod, "SdkClient",
                                      FakeClient):
                result = harness._sdk_execution(
                    cfg, Path(ws), "task", 5, None, "reviewer",
                    policy, None, time.time(), 1.0, cancel_check=cancel)

        self.assertTrue(result["os_sandbox_wrapped"])
        self.assertEqual(captured["init"]["command"][0], "sandbox-exec")
        self.assertEqual(captured["init"]["command"][3:][-2:],
                         ["--profile", "sdk"])
        self.assertIs(captured["run"]["cancel_check"], cancel)


if __name__ == "__main__":
    unittest.main()
