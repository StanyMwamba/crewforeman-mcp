"""Tests for :mod:`deepseek_mcp.mimo_worker`.

Fake ``mimo`` binaries only (recorded JSONL streams) -- never the real mimo,
never the network beyond 127.0.0.1.  Python 3.9-compatible, stdlib unittest,
Windows-safe paths via pathlib.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from deepseek_mcp import mimo_worker

try:
    from tests import fakes as _fakes
except ImportError:  # tests run as top-level modules
    import fakes as _fakes


FAKE_MIMO_BODY = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

argv = sys.argv[:]
capture = os.environ.get("FAKE_MIMO_ARGV")
if capture:
    try:
        Path(capture).write_text(json.dumps(argv), encoding="utf-8")
    except Exception:
        pass


def emit(obj):
    print(json.dumps(obj), flush=True)


def parse_flags(args):
    flags = {}
    positional = []
    index = 0
    while index < len(args):
        item = args[index]
        if item in ("--format", "--dir", "-m", "--variant", "-s", "--attach",
                    "--host", "--port"):
            if index + 1 < len(args):
                flags[item] = args[index + 1]
                index += 2
                continue
        if item.startswith("--"):
            flags[item] = True
        else:
            positional.append(item)
        index += 1
    return flags, positional


flags, positional = parse_flags(argv[1:])
mode = os.environ.get("FAKE_MIMO_MODE", "ok")
workspace = Path(flags.get("--dir") or os.getcwd())
task = positional[1] if len(positional) > 1 else (
    positional[0] if positional else "")

if positional and positional[0] == "serve":
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    host = flags.get("--host") or "127.0.0.1"
    port = int(flags.get("--port") or 0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            healthy = self.path.split("?")[0].rstrip("/") == "/healthz"
            body = b"ok" if healthy else b"missing"
            self.send_response(200 if healthy else 404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((host, port), Handler).serve_forever()
    sys.exit(0)

forced_text = os.environ.get("FAKE_MIMO_TEXT")
if task.startswith("Reply with exactly READY") and forced_text is None:
    emit({"type": "text", "data": {"text": "READY"}})
    sys.exit(0)

if mode == "silent":
    time.sleep(float(os.environ.get("FAKE_MIMO_SLEEP", "30")))
    sys.exit(0)

if mode == "error_auth":
    emit({"type": "error", "error": {"data": {
        "message": "Unauthorized: invalid API key", "statusCode": 401}}})
    sys.exit(1)
if mode == "error_limit":
    emit({"type": "error", "error": {"data": {
        "message": "Too many requests", "statusCode": 429}}})
    sys.exit(1)
if mode == "error_network":
    emit({"type": "error", "error": {"data": {
        "message": "network failure: ECONNRESET talking to MiMo API"}}})
    sys.exit(1)

if mode == "slow":
    for index in range(3):
        emit({"type": "step_start", "data": {"name": "slow-%d" % index}})
        emit({"type": "tool_use", "data": {"name": "bash",
                                          "input": {"command": "ls"}}})
        time.sleep(float(os.environ.get("FAKE_MIMO_STEP_SLEEP", "0.15")))
    emit({"type": "step_finish", "data": {"tokens": {
        "total": 30, "input": 10, "output": 15, "reasoning": 5, "cache": 5}}})
    emit({"type": "text", "data": {"text": "slow done"}})
    sys.exit(0)

session = os.environ.get("FAKE_MIMO_SESSION_ID")
if session:
    emit({"type": "session", "data": {"id": session}})
emit({"type": "step_start", "data": {"name": "plan"}})
emit({"type": "tool_use", "data": {"name": "bash", "input": {"command": "ls"}}})
emit({"type": "tool_use", "data": {"name": "read", "input": {"path": "x"}}})
emit({"type": "step_finish", "data": {"tokens": {
    "total": 100, "input": 40, "output": 30, "reasoning": 10, "cache": 20}}})
emit({"type": "step_start", "data": {"name": "act"}})
emit({"type": "step_finish", "data": {"tokens": {
    "total": 50, "input": 20, "output": 15, "reasoning": 5, "cache": 10}}})
try:
    (workspace / "out.txt").write_text("done\n", encoding="utf-8")
    (workspace / ".fake-mimo-state.json").write_text("{}", encoding="utf-8")
except Exception:
    pass
emit({"type": "text", "data": {
    "text": forced_text if forced_text is not None else "final answer"}})
sys.exit(0)
'''


class MimoWorkerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._suite_root = Path(tempfile.mkdtemp(prefix="mimo-worker-suite-"))
        cls.fake = _fakes.write_fake_script(
            cls._suite_root / "fake_mimo.py", FAKE_MIMO_BODY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls._suite_root), ignore_errors=True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mimo-worker-case-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.argv_capture = self.root / "argv.json"
        self.setenv(FAKE_MIMO_ARGV=str(self.argv_capture), FAKE_MIMO_MODE="ok")

    # -- helpers ---------------------------------------------------------

    def setenv(self, **values):
        for key, value in values.items():
            old = os.environ.get(key)

            def restore(old_value=old, name=key):
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

            self.addCleanup(restore)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

    def captured_argv(self):
        return json.loads(self.argv_capture.read_text(encoding="utf-8"))

    def assert_cost_invariant(self, result):
        """cost_known False must never pair with a 0.0 (or any) cost."""
        if not result["cost_known"]:
            self.assertIsNone(result["estimated_cost_usd"])

    def cfg(self, **extra):
        config = {"mimo_bin": str(self.fake)}
        config.update(extra)
        return config

    # -- pure API --------------------------------------------------------

    def test_is_mimo_target_alias_map(self):
        self.assertTrue(mimo_worker.is_mimo_target("mimo", "", ""))
        self.assertTrue(mimo_worker.is_mimo_target("", "xiaomi/mimo-v2.6-pro", ""))
        self.assertTrue(mimo_worker.is_mimo_target("", "", "Xiaomi (MiMo)"))
        self.assertTrue(mimo_worker.is_mimo_target("XIAOMI", "mimo-v2.6", ""))
        self.assertFalse(mimo_worker.is_mimo_target(
            "deepseek", "deepseek-chat", "DeepSeek"))
        self.assertFalse(mimo_worker.is_mimo_target())
        self.assertEqual(mimo_worker.MIMO_VENDOR, "Xiaomi (MiMo)")

    def test_build_argv_exact_tokens(self):
        argv = mimo_worker.build_argv(
            "do the thing", "/tmp/w", "xiaomi/mimo-v2.6-pro",
            variant="v1", session_id="sess-7",
            attach_url="http://127.0.0.1:9", mimo_bin="mimo",
            skip_permissions=True)
        self.assertEqual(argv, [
            "mimo", "run", "do the thing", "--format", "json",
            "--dir", "/tmp/w", "-m", "xiaomi/mimo-v2.6-pro",
            "--variant", "v1", "-s", "sess-7",
            "--attach", "http://127.0.0.1:9",
            "--dangerously-skip-permissions"])
        argv = mimo_worker.build_argv("t", "/tmp/w", "mimo/x", mimo_bin="mimo")
        self.assertEqual(argv, [
            "mimo", "run", "t", "--format", "json",
            "--dir", "/tmp/w", "-m", "mimo/x"])
        self.assertNotIn("--dangerously-skip-permissions", mimo_worker.build_argv(
            "t", "/tmp/w", "mimo/x", mimo_bin="mimo", skip_permissions=False))
        self.assertNotIn("--yolo", mimo_worker.build_argv(
            "t", "/tmp/w", "mimo/x", mimo_bin="mimo", skip_permissions=True))

    def test_parse_event_all_types_and_garbage(self):
        text = mimo_worker.parse_event(
            '{"type": "text", "data": {"text": "hello"}}')
        self.assertEqual(text["kind"], "text")
        self.assertEqual(text["text"], "hello")
        step = mimo_worker.parse_event(
            '{"type": "step_start", "data": {"name": "plan"}}')
        self.assertEqual(step["kind"], "step_start")
        self.assertEqual(step["name"], "plan")
        finish = mimo_worker.parse_event(
            '{"type": "step_finish", "data": {"tokens": {"total": 9, '
            '"input": 4, "output": 3, "reasoning": 1, "cache": 2}}}')
        self.assertEqual(finish["kind"], "step_finish")
        self.assertEqual(finish["tokens"], {"total": 9, "input": 4, "output": 3,
                                            "reasoning": 1, "cache": 2})
        tool = mimo_worker.parse_event(
            '{"type": "tool_use", "data": {"name": "bash", '
            '"input": {"command": "ls"}}}')
        self.assertEqual(tool["kind"], "tool_use")
        self.assertEqual(tool["name"], "bash")
        self.assertEqual(tool["input"], {"command": "ls"})
        err = mimo_worker.parse_event(
            '{"type": "error", "error": {"data": {"message": "Unauthorized", '
            '"statusCode": 401}}}')
        self.assertEqual(err["kind"], "error")
        self.assertEqual(err["message"], "Unauthorized")
        self.assertEqual(err["status_code"], 401)
        sess = mimo_worker.parse_event('{"type": "session", "data": {"id": "s-1"}}')
        self.assertEqual(sess["kind"], "session")
        self.assertEqual(sess["id"], "s-1")
        # Missing fields degrade; never raise.
        self.assertEqual(mimo_worker.parse_event('{"type": "text"}')["kind"],
                         "text")
        self.assertEqual(mimo_worker.parse_event('{"type": "text"}')["text"], "")
        zeros = mimo_worker.parse_event('{"type": "step_finish", "data": {}}')
        self.assertEqual(zeros["tokens"], {"total": 0, "input": 0, "output": 0,
                                           "reasoning": 0, "cache": 0})
        missing = mimo_worker.parse_event('{"type": "error"}')
        self.assertEqual(missing["kind"], "error")
        self.assertIsNone(missing["status_code"])
        string_code = mimo_worker.parse_event(
            '{"type": "error", "error": {"data": {"message": "x", '
            '"statusCode": "429"}}}')
        self.assertEqual(string_code["status_code"], 429)
        for garbage in ("", "   ", "not json", "[1, 2]", "null", "42",
                        '{"type": "mystery"}', '{"no": "type"}'):
            self.assertEqual(mimo_worker.parse_event(garbage)["kind"], "other",
                             garbage)

    def test_classify_error_buckets(self):
        auth = mimo_worker.classify_error("Unauthorized", 401)
        self.assertEqual(auth["class"], "auth")
        self.assertIn("mimo providers login", auth["remedy"])
        self.assertEqual(auth["status_code"], 401)
        self.assertEqual(mimo_worker.classify_error("denied", 403)["class"],
                         "auth")
        limit = mimo_worker.classify_error("Too many requests", 429)
        self.assertEqual(limit["class"], "limit")
        self.assertIn("reduce parallel", limit["remedy"])
        self.assertIn("back off", limit["remedy"])
        network = mimo_worker.classify_error("network failure: ECONNRESET", None)
        self.assertEqual(network["class"], "network")
        self.assertIn("check connectivity", network["remedy"])
        self.assertIsNone(network["status_code"])
        self.assertEqual(mimo_worker.classify_error("server exploded", 503)["class"],
                         "transient")
        self.assertEqual(mimo_worker.classify_error("invalid model", 400)["class"],
                         "permanent")
        for bucket in (auth, limit, network):
            self.assertTrue(bucket["remedy"])

    # -- execute() -------------------------------------------------------

    def test_execute_happy_path(self):
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "build something", timeout=15)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer"], "final answer")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])
        self.assertEqual(result["tokens_in"], 60)
        self.assertEqual(result["cache_read_tokens"], 30)
        self.assertEqual(result["tokens_out"], 45)
        self.assertEqual(result["reasoning_tokens"], 15)
        self.assertEqual(result["total_tokens"], 150)
        self.assertTrue(result["cost_known"])
        expected = round((60 * 0.435 + 30 * 0.0036 + 45 * 0.87) / 1e6, 6)
        self.assertEqual(result["estimated_cost_usd"], expected)
        self.assertEqual(result["usage"]["input_tokens"], 60)
        self.assertEqual(result["usage"]["cache_read_tokens"], 30)
        self.assertEqual(result["usage"]["output_tokens"], 45)
        self.assertEqual(result["usage"]["reasoning_tokens"], 15)
        self.assertEqual(result["tools_used"], {"bash": 1, "read": 1})
        self.assertIn("out.txt", result["output_files"])
        self.assertNotIn("mimocode.jsonc", result["output_files"])
        self.assertFalse([name for name in result["output_files"]
                          if name.startswith(".fake")])
        self.assertEqual(result["session_logs"], [])
        self.assertEqual(result["transport"], "mimo")
        self.assertEqual(result["cost_source"], "mimo_events")
        self.assertEqual(result["sandbox"], "workspace-write")
        self.assertEqual(result["models"], ["xiaomi/mimo-v2.6-pro"])
        self.assertEqual(result["stop_reason"], "ok")
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["stalled"])
        self.assertGreater(result["events"], 0)
        self.assertGreaterEqual(result["seconds"], 0.0)
        self.assert_cost_invariant(result)

    def test_execute_error_auth_limit_network(self):
        cases = [
            ("error_auth", "auth", "mimo providers login"),
            ("error_limit", "limit", "reduce parallel"),
            ("error_network", "network", "check connectivity"),
        ]
        for mode, stop_reason, remedy_fragment in cases:
            with self.subTest(mode=mode):
                self.setenv(FAKE_MIMO_MODE=mode)
                result = mimo_worker.execute(
                    self.cfg(), self.workspace, "fail please", timeout=15)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["stop_reason"], stop_reason)
                self.assertIn(remedy_fragment, result["error"])
                self.assertFalse(result["cost_known"])
                self.assert_cost_invariant(result)

    def test_execute_timeout_on_silent_fake(self):
        self.setenv(FAKE_MIMO_MODE="silent", FAKE_MIMO_SLEEP="30")
        started = time.time()
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "hang", timeout=1.0)
        self.assertLess(time.time() - started, 20)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["stalled"])
        self.assertEqual(result["stop_reason"], "timeout")
        self.assertIn("timed out", result["error"])
        self.assert_cost_invariant(result)

    def test_execute_slow_progress_cb(self):
        self.setenv(FAKE_MIMO_MODE="slow", FAKE_MIMO_STEP_SLEEP="0.15")
        lines = []
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "slow task", timeout=15,
            progress_cb=lines.append)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer"], "slow done")
        self.assertTrue(any("tool call(s)" in line for line in lines))
        self.assertTrue(any("step" in line for line in lines))
        self.assertGreaterEqual(len(lines), 3)

    def test_execute_session_resume_passes_dash_s(self):
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "resume me",
            session_id="sess-42", timeout=15)
        captured = self.captured_argv()
        self.assertIn("-s", captured)
        self.assertEqual(captured[captured.index("-s") + 1], "sess-42")
        self.assertEqual(result["transport_session_id"], "sess-42")
        self.assertEqual(result["status"], "ok")

    def test_execute_session_id_parsed_from_events(self):
        self.setenv(FAKE_MIMO_SESSION_ID="parsed-9")
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "fresh run", timeout=15)
        self.assertEqual(result["transport_session_id"], "parsed-9")

    def test_execute_never_skips_permissions_unless_asked(self):
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "plain", timeout=15)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("--dangerously-skip-permissions", self.captured_argv())
        result = mimo_worker.execute(
            self.cfg(mimo_skip_permissions=True), self.workspace, "plain",
            timeout=15)
        self.assertEqual(result["status"], "ok")
        captured = self.captured_argv()
        self.assertIn("--dangerously-skip-permissions", captured)
        self.assertNotIn("--yolo", captured)

    def test_execute_unknown_model_cost_is_unknown_not_zero(self):
        result = mimo_worker.execute(
            self.cfg(), self.workspace, "priceless",
            model="other/vendor-9000", timeout=15)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["cost_known"])
        self.assertIsNone(result["estimated_cost_usd"])
        self.assert_cost_invariant(result)

    def test_execute_cfg_price_override(self):
        result = mimo_worker.execute(
            self.cfg(mimo_prices={"other/vendor-9000": {
                "input": 1.0, "cache_hit": 0.5, "output": 2.0}}),
            self.workspace, "priced", model="other/vendor-9000", timeout=15)
        self.assertTrue(result["cost_known"])
        expected = round((60 * 1.0 + 30 * 0.5 + 45 * 2.0) / 1e6, 6)
        self.assertEqual(result["estimated_cost_usd"], expected)
        self.assert_cost_invariant(result)

    def test_permissions_fallback_standalone(self):
        saved = mimo_worker.mimo_permissions
        mimo_worker.mimo_permissions = None
        self.addCleanup(setattr, mimo_worker, "mimo_permissions", saved)
        result = mimo_worker.execute(
            self.cfg(mimo_skip_permissions=True), self.workspace, "fallback",
            timeout=15)
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.workspace / "mimocode.jsonc").is_file())
        self.assertIn("--dangerously-skip-permissions", self.captured_argv())
        self.assert_cost_invariant(result)

    # -- MimoServer ------------------------------------------------------

    def test_mimo_server_lifecycle_serve_attach_stop_cleanup(self):
        cfg = self.cfg()
        server = mimo_worker.MimoServer(
            cfg, host="127.0.0.1", port=0, ready_timeout=15.0)
        try:
            url = server.start()
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertTrue(server.ready())
            result = mimo_worker.execute(
                cfg, self.workspace, "attach task", attach_url=url,
                timeout=15)
            self.assertEqual(result["status"], "ok")
            captured = self.captured_argv()
            self.assertIn("--attach", captured)
            self.assertEqual(captured[captured.index("--attach") + 1], url)
            server.stop()
            self.assertFalse(server.ready())
        finally:
            server.cleanup()
            server.cleanup()  # idempotent

    # -- ping ------------------------------------------------------------

    def test_ping_ready_contract(self):
        cfg = self.cfg()
        result = mimo_worker.ping(cfg, timeout=15.0)
        self.assertIs(result["ok"], True)
        self.assertEqual(result["answer"], "READY")
        self.assertIsNone(result["error"])
        captured = self.captured_argv()
        self.assertEqual(captured[captured.index("-m") + 1],
                         "xiaomi/mimo-v2.6-flash")
        self.setenv(FAKE_MIMO_TEXT="NOPE")
        result = mimo_worker.ping(cfg, timeout=15.0)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["answer"], "NOPE")


if __name__ == "__main__":
    unittest.main()
