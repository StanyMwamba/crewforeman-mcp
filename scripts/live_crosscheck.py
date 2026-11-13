#!/usr/bin/env python3
"""Live crosscheck acceptance driver for CrewForeman 3.15.0 (stdlib only).

Drives the INSTALLED connector exactly as an MCP client would:

    python3 ~/DeepSeek-MCP/deepseek_mcp.py

over JSON-RPC stdio: initialize -> notifications/initialized -> tools/list ->
tools/call harness_deepseek (mode="crosscheck") -> poll deepseek_job_status
until a terminal state or the hard overall timeout.  If stdio cannot start or
the tool is missing, it falls back to the Python API
(``deepseek_mcp.server.tool_harness``).

It submits ONE small toy job in
``~/DeepSeek-MCP/harness-workspace/crosscheck-live-test`` and writes
``crosscheck-result.json`` with parts, crew pairing, crew score vs 10, audit
result, rescues, elapsed seconds and tokens.

Exit codes:
    0  job reached a terminal state AND the record was written
    1  hard timeout / no terminal state (record still written when possible)
    2  driver/startup error (record written with status="driver_error")

``--dry-run`` never starts the connector; it only exercises record writing.
This script is not run by the normal test suite.
"""

import argparse
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HOME = os.path.expanduser("~")
CONNECTOR = os.path.join(_HOME, "DeepSeek-MCP", "deepseek_mcp.py")
DEFAULT_WORKSPACE = os.path.join(
    _HOME, "DeepSeek-MCP", "harness-workspace", "crosscheck-live-test")
RESULT_NAME = "crosscheck-result.json"
PROMPT = (
    "Create hello.txt containing exactly the text hello (one line, no extra "
    "content) and test_hello.py, a small test that reads hello.txt and asserts "
    "its stripped content is 'hello'. Run the test and report the result."
)
THRESHOLD = 10.0
TERMINAL_STATUSES = frozenset(
    ("done", "completed", "success", "error", "failed", "cancelled",
     "canceled", "crosscheck_below_threshold", "review_failed")
)


class DriverError(RuntimeError):
    """The connector could not be started or spoke an unusable protocol."""


def is_terminal(status):
    """True when *status* is a terminal job state (case-insensitive)."""
    if not isinstance(status, str):
        return False
    return status.strip().lower() in TERMINAL_STATUSES


def _first(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _find(obj, key, depth=0):
    """Breadth-first search for the first non-None value under *key*."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for value in obj.values():
            found = _find(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found = _find(value, key, depth + 1)
            if found is not None:
                return found
    return None


def _maybe_json(text):
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return {"text": str(text)}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}
    return value if isinstance(value, dict) else {"value": value}


def _status(payload):
    status = _find(payload, "status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    text = payload.get("text") if isinstance(payload, dict) else None
    if isinstance(text, str):
        match = re.search(r"\bstatus\b\s*[:=]\s*([A-Za-z_][A-Za-z0-9_-]*)", text)
        if match:
            return match.group(1).lower()
    return None


def _run_id(payload):
    value = _find(payload, "run_id")
    if not value:
        value = _find(payload, "job_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    text = payload.get("text") if isinstance(payload, dict) else None
    if isinstance(text, str):
        match = re.search(
            r"\b(?:run|job)[ _-]?id\b\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
            text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tokens(payload):
    tokens = _find(payload, "tokens")
    if not isinstance(tokens, dict):
        tokens = {}

    def pick(*names):
        for name in names:
            value = _first(tokens.get(name), _find(payload, name))
            if value is not None:
                return _as_int(value)
        return 0

    return {
        "input": pick("input", "input_tokens", "tokens_in", "prompt_tokens"),
        "output": pick("output", "output_tokens", "tokens_out", "completion_tokens"),
        "cache": pick("cache", "cache_tokens", "cache_read_tokens", "cached_tokens"),
    }


def build_record(payload, workspace, elapsed_seconds, connector=CONNECTOR,
                 prompt=PROMPT, status=None, error=None, run_id=None):
    """Build the JSON-serialisable acceptance record from a status payload."""
    status = (status or _status(payload) or "unknown").strip().lower()
    crosscheck = _find(payload, "crosscheck")
    if not isinstance(crosscheck, dict):
        crosscheck = {}

    parts = _first(_find(payload, "parts"), crosscheck.get("parts"), [])
    if not isinstance(parts, list):
        parts = []
    pairing = _first(_find(payload, "crew_pairing"), _find(payload, "pairing"),
                     crosscheck.get("crew_pairing"), crosscheck.get("pairing"), {})
    if not isinstance(pairing, dict):
        pairing = {"value": pairing}

    score = _first(_find(payload, "crew_score"), crosscheck.get("crew_score"),
                   crosscheck.get("score"))
    if score is not None:
        score = _as_float(score, None)
    threshold = _first(_find(payload, "threshold"), crosscheck.get("threshold"), THRESHOLD)
    threshold = _as_float(threshold, THRESHOLD)

    audit = _first(_find(payload, "audit"), crosscheck.get("audit"),
                   _find(payload, "audit_result"), crosscheck.get("audit_result"))
    if audit is None:
        audit = {}

    rescues = _first(_find(payload, "rescues"), crosscheck.get("rescues"), [])
    if isinstance(rescues, (int, float)):
        rescue_count = _as_int(rescues)
        rescues = []
    elif isinstance(rescues, list):
        rescue_count = len(rescues)
    else:
        rescue_count = 0
        rescues = [rescues]

    record = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "connector": connector,
        "workspace": str(workspace),
        "run_id": run_id or _run_id(payload),
        "prompt": prompt,
        "status": status,
        "terminal": is_terminal(status),
        "error": error,
        "elapsed_seconds": round(_as_float(elapsed_seconds), 2),
        "parts": parts,
        "crew_pairing": pairing,
        "crew_score": score,
        "threshold": threshold,
        "crew_score_vs_threshold": {
            "score": score,
            "threshold": threshold,
            "passed": bool(score is not None and score >= threshold),
        },
        "audit": audit,
        "rescues": rescues,
        "rescue_count": rescue_count,
        "tokens": _tokens(payload),
    }
    return record


def write_record(path, record):
    """Atomically write the record next to the job workspace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".crosscheck-", suffix=".json",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _content_payload(result):
    """Normalise a tools/call result (content list / structuredContent)."""
    if not isinstance(result, dict):
        return {"value": result}
    if result.get("isError"):
        raise DriverError("tool error: %s" % _content_text(result))
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    text = _content_text(result)
    if text:
        return _maybe_json(text)
    return result


def _content_text(result):
    chunks = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
    return "\n".join(chunk for chunk in chunks if chunk)


class StdioClient(object):
    """Minimal MCP JSON-RPC stdio client (newline or Content-Length framing)."""

    def __init__(self, connector, python=None, timeout=30.0):
        self.connector = str(connector)
        self.python = python or os.environ.get("CREWFOREMAN_PYTHON") or sys.executable
        self.timeout = timeout
        self.proc = None
        self._next_id = 0
        self._queue = queue.Queue()
        self._stderr = []
        self._threads = []

    def start(self):
        if not os.path.isfile(self.connector):
            raise DriverError("connector not found: %s" % self.connector)
        self.proc = subprocess.Popen(
            [self.python, self.connector],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        self._threads = [stdout_thread, stderr_thread]

    def _pump_stdout(self):
        stream = self.proc.stdout
        while True:
            line = stream.readline()
            if not line:
                self._queue.put(None)
                return
            if not line.strip():
                continue
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    continue
                while True:
                    header = stream.readline()
                    if header in (b"\r\n", b"\n", b""):
                        break
                body = stream.read(length)
                message = self._decode(body)
            else:
                message = self._decode(line)
            if message is not None:
                self._queue.put(message)

    @staticmethod
    def _decode(raw):
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None

    def _pump_stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr.append(text)
                del self._stderr[:-40]

    def _send(self, message):
        if self.proc is None or self.proc.poll() is not None:
            raise DriverError("connector process is not running")
        wire = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(wire)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DriverError("connector stdin closed: %s" % exc)

    def _next(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError("timed out waiting for connector reply")
        try:
            message = self._queue.get(timeout=remaining)
        except queue.Empty:
            raise DriverError("timed out waiting for connector reply")
        if message is None:
            detail = "\n".join(self._stderr[-5:])
            raise DriverError("connector closed stdout%s" %
                              ((": " + detail) if detail else ""))
        return message

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self._send(message)

    def request(self, method, params=None, timeout=None):
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            message = self._next(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise DriverError("JSON-RPC %s failed: %s" %
                                  (method, message.get("error")))
            return message.get("result") or {}

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "crewforeman-live-crosscheck", "version": "1.0"},
        })
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        result = self.request("tools/list")
        names = []
        for tool in result.get("tools") or []:
            if isinstance(tool, dict) and tool.get("name"):
                names.append(tool["name"])
        return names

    def call_tool(self, name, arguments, timeout=None):
        result = self.request("tools/call", {"name": name, "arguments": arguments},
                              timeout=timeout)
        return _content_payload(result)

    def close(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        except OSError:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self.proc = None


def job_arguments(workspace, wait_seconds):
    """The one toy crosscheck job submitted by this driver."""
    return {
        "prompt": PROMPT,
        "title": "live-crosscheck-acceptance",
        "workspace": str(workspace),
        "mode": "crosscheck",
        "subagents": 2,
        "quality": True,
        "independent_review": True,
        "quality_rounds": 1,
        "crosscheck_crews": 2,
        "crosscheck_crew_size": 1,
        "crosscheck_threshold": THRESHOLD,
        "crosscheck_max_rounds": 1,
        "audit_review": True,
        "sandbox": "workspace-write",
        "job_max_seconds": 240,
        "wait_seconds": wait_seconds,
    }


def _poll(client, run_id, first_payload, deadline, poll_seconds):
    """Poll deepseek_job_status until terminal. Returns (payload, error)."""
    payload = first_payload
    last_error = None
    while True:
        status = _status(payload)
        if status and is_terminal(status):
            return payload, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_seconds, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            payload = client.call_tool(
                "deepseek_job_status", {"run_id": run_id},
                timeout=min(60.0, max(5.0, remaining)))
            last_error = None
        except DriverError as exc:
            last_error = str(exc)
            print("  status poll warning: %s" % last_error, flush=True)
        print("  [remaining %5.0fs] status=%s stage=%s" % (
            max(0.0, deadline - time.monotonic()),
            _status(payload) or "unknown",
            _find(payload, "stage") or "-"), flush=True)
    message = "overall timeout before a terminal state"
    if last_error:
        message += " (last poll error: %s)" % last_error
    return payload, message


def api_fallback(job_args):
    """Best-effort Python API path when stdio cannot start."""
    try:
        from deepseek_mcp import server as server_mod
    except Exception as exc:  # pragma: no cover - environment dependent
        raise DriverError("Python API import failed: %s" % exc)
    tool = getattr(server_mod, "tool_harness", None)
    if not callable(tool):
        raise DriverError("deepseek_mcp.server.tool_harness is unavailable")
    args = dict(job_args)
    args["wait_seconds"] = max(30, min(300, int(args.get("wait_seconds") or 30)))
    text = tool(args)
    payload = _maybe_json(text)
    if "status" not in payload:
        payload["status"] = "done"
    return payload


def _print_record(record, path):
    print("record: %s" % path, flush=True)
    print("status: %s (terminal=%s)" % (record["status"], record["terminal"]),
          flush=True)
    print("crew score: %s threshold: %s passed: %s" % (
        record["crew_score"], record["threshold"],
        record["crew_score_vs_threshold"]["passed"]), flush=True)
    print("parts: %d rescues: %d tokens: in=%d out=%d cache=%d" % (
        len(record["parts"]), record["rescue_count"],
        record["tokens"]["input"], record["tokens"]["output"],
        record["tokens"]["cache"]), flush=True)
    if record["error"]:
        print("error: %s" % record["error"], flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live crosscheck acceptance driver for CrewForeman 3.15.0")
    parser.add_argument("--connector", default=CONNECTOR)
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--result-name", default=RESULT_NAME)
    parser.add_argument("--python", default=None,
                        help="Python interpreter for the connector (default sys.executable)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Hard overall timeout in seconds (default 600)")
    parser.add_argument("--wait-seconds", type=float, default=5.0,
                        help="harness_deepseek wait before returning a job id")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Write a synthetic record without starting the connector")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    result_path = workspace / args.result_name
    started = time.monotonic()

    if args.dry_run:
        record = build_record({}, workspace, 0.0, args.connector, PROMPT,
                              status="dry_run")
        record["dry_run"] = True
        write_record(result_path, record)
        _print_record(record, result_path)
        return 0

    deadline = started + max(1.0, args.timeout)
    job_args = job_arguments(workspace, args.wait_seconds)
    client = None
    payload = {}
    error = None
    try:
        try:
            client = StdioClient(args.connector, python=args.python,
                                 timeout=min(30.0, max(5.0, args.timeout)))
            client.start()
            client.initialize()
            tools = client.list_tools()
            print("stdio: %d tools; harness_deepseek=%s" % (
                len(tools), "harness_deepseek" in tools), flush=True)
            if "harness_deepseek" not in tools:
                raise DriverError("harness_deepseek missing from tools/list")
            print("submit: mode=crosscheck workspace=%s" % workspace, flush=True)
            payload = client.call_tool("harness_deepseek", job_args)
            run_id = _run_id(payload)
            print("submitted: run_id=%s status=%s" % (
                run_id or "-", _status(payload) or "-"), flush=True)
            if run_id and not is_terminal(_status(payload)):
                payload, error = _poll(client, run_id, payload, deadline,
                                      max(1.0, args.poll_seconds))
            elif not run_id and not is_terminal(_status(payload)):
                error = "connector returned no run id and no terminal status"
        except Exception as exc:
            error = "stdio path failed: %s" % exc
            print("%s; trying Python API fallback" % error, flush=True)
            payload = api_fallback(job_args)

        record = build_record(payload, workspace, time.monotonic() - started,
                              args.connector, PROMPT, error=error)
        write_record(result_path, record)
        _print_record(record, result_path)
        return 0 if record["terminal"] else 1
    except Exception as exc:
        record = build_record(payload, workspace, time.monotonic() - started,
                              args.connector, PROMPT, status="driver_error",
                              error=str(exc))
        try:
            write_record(result_path, record)
            _print_record(record, result_path)
        except Exception as write_exc:
            print("could not write record: %s" % write_exc, file=sys.stderr)
        print("driver error: %s" % exc, file=sys.stderr, flush=True)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
