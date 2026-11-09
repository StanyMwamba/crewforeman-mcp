#!/usr/bin/env python3
"""Client-independent stdio smoke test for the DeepSeek MCP server.

Standard library only, Python 3.9 compatible.  It speaks newline-delimited
JSON-RPC 2.0 directly, so it works without Claude Desktop, Cursor or any other
MCP client:

    python3 scripts/smoke_mcp.py
    python3 scripts/smoke_mcp.py --timeout 30 -- python3 -m deepseek_mcp

The default command is ``[sys.executable, "-m", "deepseek_mcp"]``.  Use
``--command`` (or a bare ``--``) to test another interpreter or entry point.
Each check prints one PASS line; any malformed response, JSON-RPC error, wrong
id or missing field prints FAIL and exits non-zero.  The server's stderr is
kept (bounded) and shown only on failure, so API keys are never echoed.
"""

import argparse
import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2025-11-25"
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 300.0
# start, initialize, notifications/initialized, tools/list, tools/call
CHECK_COUNT = 5


class SmokeFailure(Exception):
    pass


class StdioClient:
    def __init__(self, command, timeout=20.0):
        self.command = list(command)
        self.timeout = timeout
        self.stderr_lines = collections.deque(maxlen=200)
        self.lines = queue.Queue()
        self.next_id = 0
        self.proc = None

    # -- lifecycle ------------------------------------------------------
    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
        except OSError as exc:
            raise SmokeFailure("could not start %r: %s" % (self.command, exc))
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(("line", line))
        except Exception as exc:  # pragma: no cover - defensive
            self.lines.put(("error", str(exc)))
        finally:
            self.lines.put(("eof", None))

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip("\n"))
        except Exception:
            pass

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    # -- JSON-RPC -------------------------------------------------------
    def send(self, payload):
        if self.proc is None or self.proc.stdin is None:
            raise SmokeFailure("server process is not running")
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            raise SmokeFailure("could not write to server stdin: %s" % exc)

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.next_id, self.wait_for(self.next_id)

    def wait_for(self, want_id):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    "timed out after %.1fs waiting for response id %s"
                    % (self.timeout, want_id))
            try:
                kind, value = self.lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise SmokeFailure(
                        "server exited with code %s before response id %s"
                        % (self.proc.returncode, want_id))
                continue
            if kind == "eof":
                raise SmokeFailure(
                    "server stdout closed before response id %s (exit %s)"
                    % (want_id, self.proc.poll()))
            if kind == "error":
                raise SmokeFailure("could not read server stdout: %s" % value)
            text = (value or "").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                raise SmokeFailure(
                    "non-JSON line on stdout: %s" % _clip(text))
            if not isinstance(message, dict):
                raise SmokeFailure("stdout JSON frame is not an object")
            if "id" not in message:
                # Server notifications are allowed; they have no id.
                continue
            if message.get("id") != want_id:
                raise SmokeFailure(
                    "unexpected response id %r (wanted %r)"
                    % (message.get("id"), want_id))
            if message.get("jsonrpc") != "2.0":
                raise SmokeFailure("response is missing jsonrpc=2.0")
            if "error" in message:
                error = message.get("error")
                raise SmokeFailure(
                    "JSON-RPC error for id %s: %s"
                    % (want_id, _clip(json.dumps(error, sort_keys=True))))
            if not isinstance(message.get("result"), dict):
                raise SmokeFailure("response id %s has no object result" % want_id)
            return message["result"]

    def diagnostics(self):
        if not self.stderr_lines:
            return ""
        return "\n".join(list(self.stderr_lines)[-30:])


def _clip(text, limit=500):
    text = " ".join(str(text).split())
    return text[:limit]


def _require(condition, message):
    if not condition:
        raise SmokeFailure(message)


def run(command, timeout, config=None):
    if config:
        command = list(command) + ["--config", config]
    client = StdioClient(command, timeout=timeout)
    passed = 0
    try:
        client.start()
        print("PASS start (%s)" % " ".join(command))
        passed += 1

        # 1. initialize
        init_id, result = client.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "crewforeman-mcp-smoke", "version": "3.13.3"},
        })
        _require(isinstance(result.get("protocolVersion"), str)
                 and result["protocolVersion"],
                 "initialize result has no protocolVersion string")
        _require(isinstance(result.get("capabilities"), dict),
                 "initialize result has no capabilities object")
        _require(isinstance(result.get("serverInfo"), dict)
                 and isinstance(result["serverInfo"].get("name"), str),
                 "initialize result has no serverInfo.name")
        print("PASS initialize (protocol %s, server %s)"
              % (result["protocolVersion"], result["serverInfo"]["name"]))
        passed += 1

        # 2. initialized notification (no response expected)
        client.notify("notifications/initialized", {})
        print("PASS notifications/initialized")
        passed += 1

        # 3. tools/list
        _list_id, result = client.request("tools/list", {})
        tools = result.get("tools")
        _require(isinstance(tools, list) and tools,
                 "tools/list returned no tools")
        names = []
        for tool in tools:
            _require(isinstance(tool, dict), "tools/list entry is not an object")
            _require(isinstance(tool.get("name"), str) and tool["name"],
                     "tools/list entry has no name")
            _require(isinstance(tool.get("inputSchema"), dict),
                     "tool %r has no inputSchema object" % tool.get("name"))
            names.append(tool["name"])
        _require("list_skills" in names,
                 "list_skills is missing from tools/list")
        print("PASS tools/list (%d tools: %s)"
              % (len(names), ", ".join(names[:6]) + ("..." if len(names) > 6 else "")))
        passed += 1

        # 4. tools/call list_skills
        _call_id, result = client.request("tools/call", {
            "name": "list_skills",
            "arguments": {},
        })
        content = result.get("content")
        _require(isinstance(content, list) and content,
                 "tools/call list_skills returned no content")
        first = content[0]
        _require(isinstance(first, dict) and first.get("type") == "text",
                 "tools/call content is not a text block")
        _require(isinstance(first.get("text"), str) and first["text"].strip(),
                 "tools/call text content is empty")
        _require(result.get("isError") is False,
                 "tools/call list_skills reported isError=true")
        print("PASS tools/call list_skills (%d chars)" % len(first["text"]))
        passed += 1

        print("PASS stdio smoke test (%d/%d checks)" % (passed, CHECK_COUNT))
        return 0
    except SmokeFailure as exc:
        print("FAIL %s" % exc)
        diagnostics = client.diagnostics()
        if diagnostics:
            print("--- server stderr (tail) ---")
            print(diagnostics)
        return 1
    except Exception as exc:  # never leak a traceback into client logs
        print("FAIL unexpected error: %s: %s" % (type(exc).__name__, exc))
        return 1
    finally:
        client.stop()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Client-independent stdio smoke test for deepseek-mcp.")
    parser.add_argument("--command", nargs=argparse.REMAINDER,
                        default=None,
                        help="server command (everything after --command); "
                             "default: this Python -m deepseek_mcp")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds per response (default %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--config", default=None,
                        help="optional path passed to the server as --config")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if argv and argv[0] == "--":
        argv = argv[1:]
    args = parser.parse_args(argv)
    timeout = args.timeout
    if timeout != timeout or timeout <= 0 or timeout > MAX_TIMEOUT:
        print("FAIL --timeout must be a number above 0 and at most %d"
              % MAX_TIMEOUT)
        return 2
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [sys.executable, "-m", "deepseek_mcp"]
    if args.config:
        if not os.path.isfile(args.config):
            print("FAIL config file not found: %s" % args.config)
            return 2
    return run(command, timeout, config=args.config)


if __name__ == "__main__":
    sys.exit(main())
