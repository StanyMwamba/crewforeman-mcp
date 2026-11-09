"""Test fakes: a local DeepSeek HTTP server and a fake ``dsh`` executable."""

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from deepseek_mcp import harness


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(body.decode("utf-8"))
        except Exception:
            request = {}
        self.server.requests.append(request)
        status, payload = self.server.responder(request)
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class FakeDeepSeekServer:
    """Threaded HTTP server implementing POST /chat/completions."""

    def __init__(self, responder=None):
        self.responder = responder or self.default_response
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.responder = self.responder
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @staticmethod
    def default_response(request):
        return 200, {
            "choices": [{"message": {"content": "Fake DeepSeek answer for: %s" % (
                (request.get("messages") or [{}])[-1].get("content", "")[:40]),
                "reasoning_content": "fake reasoning"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                      "prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 100},
        }

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%s" % (host, port)

    @property
    def requests(self):
        return self.httpd.requests

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def write_fake_dsh(path, mode="ok"):
    """Create an executable fake Harness CLI that emits --json events."""
    content = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time
from pathlib import Path

task = sys.argv[-1] if len(sys.argv) else ""
workspace = Path.cwd()
mode = os.environ.get("FAKE_DSH_MODE", "ok")

def project_key(cwd):
    # Independent port of the installed Harness projectKey, kept here so the
    # fake writes where real Harness would even if progress.py regresses.
    raw = str(cwd).encode("utf-16-le", "surrogatepass")
    readable = ""
    separator_run = False
    for index in range(0, len(raw), 2):
        code = raw[index] | (raw[index + 1] << 8)
        char = chr(code)
        if char in "/" + chr(92) + ":":
            if not separator_run:
                readable += "-"
            separator_run = True
        elif char != "~" and (char.isascii() and (char.isalnum() or char in "._-")):
            readable += char
            separator_run = False
        else:
            readable += "~%04X" % code
            separator_run = False
    return "--%s--" % ((readable.lstrip("-") or "root")[:251])

score = os.environ.get("FAKE_DSH_SCORE")
state_path = Path(os.environ.get("FAKE_DSH_STATE", str(workspace / ".fake-dsh-state.json")))
state = {}
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except Exception:
    state = {}

def save_state():
    try:
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass

def emit(payload):
    print(json.dumps(payload), flush=True)

def maybe_session_log():
    sessions = os.environ.get("FAKE_DSH_SESSIONS")
    if not sessions:
        return
    resolved = str(workspace.resolve())
    encoded = project_key(resolved)
    directory = Path(sessions) / encoded / ("session-" + str(os.getpid()))
    directory.mkdir(parents=True, exist_ok=True)
    model = os.environ.get("FAKE_DSH_MODEL", "deepseek-flash")
    lines = [{"type": "session", "version": 4, "id": "fake", "cwd": resolved}]
    lines.append({"type": "assistant/message", "seq": 1,
                  "data": {"message": {"id": "fake-message", "role": "assistant",
                                       "source": {"model": model}},
                           "usage": {"inputTokens": 1000, "cacheReadTokens": 2000,
                                     "outputTokens": 500}}})
    (directory / "session.v4.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

if os.environ.get("FAKE_DSH_LOG"):
    Path(os.environ["FAKE_DSH_LOG"]).write_text(task, encoding="utf-8")
if os.environ.get("FAKE_DSH_ARGV"):
    Path(os.environ["FAKE_DSH_ARGV"]).write_text(
        json.dumps(sys.argv), encoding="utf-8")

maybe_session_log()
if os.environ.get("FAKE_DSH_ENV_OUT"):
    Path(os.environ["FAKE_DSH_ENV_OUT"]).write_text(json.dumps({
        "TMPDIR": os.environ.get("TMPDIR"),
        "HOME": os.environ.get("HOME"),
    }), encoding="utf-8")
key_name = os.environ.get("FAKE_DSH_KEY_NAME")
if key_name and os.environ.get("FAKE_DSH_ENV_OUT"):
    value = os.environ.get(key_name) or ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else ""
    Path(os.environ["FAKE_DSH_ENV_OUT"] + ".key").write_text(json.dumps({
        "name": key_name,
        "present": bool(value),
        "sha256_12": digest,
    }), encoding="utf-8")
if task.lstrip().startswith("Reply with exactly READY"):
    emit({"type": "final", "text": "READY"})
    sys.exit(0)
emit({"type": "tool_call", "name": "bash"})
emit({"type": "tool_call", "name": "read"})
if mode == "flaky":
    state["flaky"] = int(state.get("flaky", 0)) + 1
    save_state()
    if state["flaky"] == 1:
        emit({"type": "error", "message": "fetch failed: ECONNRESET"})
        sys.exit(1)
if mode == "tick":
    sessions = os.environ.get("FAKE_DSH_SESSIONS")
    duration = float(os.environ.get("FAKE_DSH_TICK_SECONDS", "3.0"))
    interval = float(os.environ.get("FAKE_DSH_TICK_INTERVAL", "0.2"))
    child_id = os.environ.get("FAKE_DSH_CATALOG_CHILD")
    if sessions:
        resolved = str(workspace.resolve())
        encoded = project_key(resolved)
        directory = Path(sessions) / encoded / (child_id or ("session-" + str(os.getpid())))
        directory.mkdir(parents=True, exist_ok=True)
        if child_id:
            emit({"type": "subagent/catalog", "seq": 1,
                  "data": {"version": 0, "childId": child_id, "mode": "continuable"}})
        session_path = directory / "session.v4.jsonl"
        session_path.write_text(json.dumps(
            {"type": "session", "version": 4, "id": "tick", "cwd": resolved}) + "\n",
            encoding="utf-8")
        deadline = time.time() + duration
        seq = 0
        while time.time() < deadline:
            seq += 1
            with open(str(session_path), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"type": "assistant/message", "seq": seq,
                     "data": {"message": {"id": "tick-%d" % seq,
                                          "role": "assistant"}}}) + "\n")
            time.sleep(interval)
    emit({"type": "final", "text": "tick done"})
    sys.exit(0)
if mode == "catalog_sleep":
    child_id = os.environ.get("FAKE_DSH_CATALOG_CHILD", "fake-child-a")
    emit({"type": "session", "version": 4, "id": "session-fake-parent",
          "cwd": str(workspace.resolve())})
    emit({"type": "subagent/catalog", "seq": 1,
          "data": {"version": 0, "childId": child_id, "mode": "continuable"}})
    time.sleep(float(os.environ.get("FAKE_DSH_SLEEP", "3")))
    emit({"type": "final", "text": "catalog sleep done"})
    sys.exit(0)
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_DSH_SLEEP", "3")))
    sys.exit(0)
if mode == "tree":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal, time; "
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                              "time.sleep(30)"])
    # The grandchild is intentionally left alive for the process-tree kill
    # test.  Mark the handle as reaped so this fake-dsh process does not emit
    # "subprocess N is still running" ResourceWarnings on exit (NB6).
    child.returncode = 0
    Path(os.environ["FAKE_DSH_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
    emit({"type": "tool_call", "name": "bash"})
    time.sleep(30)
    sys.exit(0)
if mode == "error":
    emit({"type": "error", "message": os.environ.get("FAKE_DSH_ERROR_MESSAGE", "fake dsh failure")})
    sys.exit(1)
if mode == "plain":
    print("plain final text", flush=True)
    sys.exit(0)

if "SCORE REPROMPT" in task:
    state["reprompt"] = int(state.get("reprompt", 0)) + 1
    save_state()
    emit({"type": "session", "version": 4, "id": "session-" + str(os.getpid()),
          "cwd": str(workspace.resolve())})
    reprompt_mode = os.environ.get("FAKE_DSH_REVIEW_REPROMPT", "ok")
    if reprompt_mode == "error":
        emit({"type": "error", "message": "fake score reprompt failure"})
        sys.exit(1)
    if reprompt_mode == "missing":
        emit({"type": "final", "text": "Score unavailable. No parsable score."})
        sys.exit(0)
    if reprompt_mode == "score_only":
        emit({"type": "final", "text": "Final quality score: 9.2/10"})
        sys.exit(0)
    emit({"type": "final", "text": "## Score\nFinal quality score: 9.2/10\n"
          "Blocking defects: 0"})
    sys.exit(0)

if "INDEPENDENT REVIEWER" in task:
    if os.environ.get("FAKE_DSH_FAIL_REVIEW"):
        emit({"type": "error", "message": os.environ.get(
            "FAKE_DSH_REVIEW_ERROR_MESSAGE", "fake reviewer failure")})
        sys.exit(1)
    raw_scores = os.environ.get("FAKE_DSH_REVIEW_SCORES", "")
    scores = [item.strip() for item in raw_scores.split(",") if item.strip()]
    index = int(state.get("review", 0))
    review_score = scores[min(index, len(scores) - 1)] if scores else os.environ.get(
        "FAKE_DSH_REVIEW_SCORE", score or "8.6")
    missing_flags = [item.strip() for item in os.environ.get(
        "FAKE_DSH_REVIEW_MISSING_SCORE", "").split(",") if item.strip()]
    score_missing = bool(missing_flags) and missing_flags[
        min(index, len(missing_flags) - 1)] == "1"
    # A complete, otherwise-unscored report that closes with a category score
    # heading: the parser must not read ``## Score (test coverage)`` as overall.
    category_score = os.environ.get("FAKE_DSH_REVIEW_CATEGORY_SCORE") == "1"
    # A complete, otherwise-unscored report whose only number is a category /
    # target / visual-impact row in a pipe table.
    table_score = os.environ.get("FAKE_DSH_REVIEW_TABLE_SCORE", "").strip()
    state["review"] = index + 1
    save_state()
    emit({"type": "session", "version": 4, "id": "session-" + str(os.getpid()),
          "cwd": str(workspace.resolve())})
    if score_missing or category_score or table_score:
        visual = os.environ.get("FAKE_DSH_REVIEW_VISUAL_IMPACT", "").strip()
        blocking_missing = os.environ.get(
            "FAKE_DSH_REVIEW_MISSING_BLOCKING", "").strip()
        extended = ""
        if visual:
            extended += "\n## Visual impact\n" + visual + "/10\n"
        report = (
            "# Independent review\n\n## Score\nPending.\n\n"
            "## Blocking defects\n" +
            ("- Blocking: fake reviewer found a missing fact in "
             "deliverable.md\n" if blocking_missing else "None\n") +
            "\n## Non-blocking defects\n"
            "- Minor: fake non-blocking typo in deliverable.md\n\n"
            "## Fix list\n" + ("1. Fix the fake blocking issue in "
                               "deliverable.md\n" if blocking_missing
                               else "None\n") + extended)
        if category_score:
            report += "\n## Score (test coverage)\n10/10\n"
        elif table_score:
            row = {
                "visual": "| Visual impact | Score: 9/10 |",
                "target": "| Target | Final quality score: 9.5/10 |",
                "test_coverage": "| Test coverage | Score | 10/10 |",
                "code_quality": "| Code quality | Score: 9.0/10 |",
            }[table_score]
            report += "\n## Score details\n\n" + row + "\n"
        (workspace / "REVIEW.md").write_text(report, encoding="utf-8")
        emit({"type": "final", "text": "REVIEW.md written. Score unavailable."})
        sys.exit(0)
    raw_blocking = os.environ.get("FAKE_DSH_REVIEW_BLOCKING", "")
    blockings = [item.strip() for item in raw_blocking.split(",") if item.strip()]
    if blockings:
        blocking = int(blockings[min(index, len(blockings) - 1)])
    else:
        blocking = 0 if float(review_score) >= 8.5 else 1
    defects = ""
    if blocking:
        defects = "- Blocking: fake reviewer found a missing fact in deliverable.md\n"
    follow_raw = os.environ.get("FAKE_DSH_REVIEW_FOLLOW_UPS", "")
    follow_items = [item.strip() for item in follow_raw.split("|") if item.strip()]
    follow_block = ""
    if follow_items:
        follow_block = ("\n## Follow-ups (pre-existing, non-blocking)\n"
                        + "\n".join("- " + item for item in follow_items) + "\n")
    report = (
        "# Independent review\n\n## Score\nFinal quality score: " + review_score + "/10\n\n"
        "## Blocking defects\n" + (defects or "None\n") +
        "\n## Non-blocking defects\nNone\n" + follow_block + "\n## Fix list\n" +
        ("1. Fix the fake blocking issue in deliverable.md\n" if blocking else "None\n"))
    if os.environ.get("FAKE_DSH_REVIEW_READONLY"):
        # A read-only reviewer cannot write REVIEW.md, so its complete final
        # answer is the report the connector has to persist itself.  This
        # exercises the flow where the same text reaches parse_review twice.
        emit({"type": "final", "text": report})
    else:
        (workspace / "REVIEW.md").write_text(report, encoding="utf-8")
        emit({"type": "final", "text": "REVIEW.md written. Final quality score: " + review_score +
              "/10. Blocking defects: " + str(blocking)})
    sys.exit(0)

if "REVISION agent" in task:
    state["revision"] = int(state.get("revision", 0)) + 1
    save_state()
    (workspace / "deliverable.md").write_text("# Fake deliverable revised\n", encoding="utf-8")
    (workspace / "QUALITY_REPORT.md").write_text(
        "# Quality report\n\n- revised: yes\n- Final quality score: 8.0/10\n",
        encoding="utf-8")
    emit({"type": "final", "text": "Revision applied. Final quality score: 8.0/10"})
    sys.exit(0)

(workspace / "deliverable.md").write_text("# Fake deliverable\n", encoding="utf-8")
if score:
    (workspace / "QUALITY_REPORT.md").write_text(
        "# Quality report\n\n- checklist: ok\n- Final quality score: " + score + "/10\n",
        encoding="utf-8")
emit({"type": "final", "text": "Deliverable written. Final quality score: " + (score or "8.6") + "/10"})
sys.exit(int(os.environ.get("FAKE_DSH_EXIT", "0")))
'''
    path = Path(path)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if os.name == "nt":
        # Windows CreateProcess cannot run a shebang script; compile the
        # test-only launcher and return it.  A missing compiler is fatal.
        try:
            from . import build_fake_dsh
        except ImportError:  # tests run as top-level modules
            import build_fake_dsh
        return build_fake_dsh.build_launcher(path)
    return path


def write_session_log(sessions_dir, workspace, entries=None, model="deepseek-flash",
                      filename="session.v4.jsonl"):
    """Write a plain (uncompressed) session log for cost-parser tests."""
    entries = entries or [
        {"input": 1000, "cache": 2000, "output": 500},
    ]
    encoded = harness.encode_workspace(workspace)
    directory = Path(sessions_dir) / encoded / "session-test"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    lines = [{"type": "session", "version": 4, "id": "test", "cwd": str(Path(workspace).resolve())}]
    for index, entry in enumerate(entries):
        usage = {"inputTokens": entry["input"], "cacheReadTokens": entry["cache"],
                 "outputTokens": entry["output"], "cacheWriteTokens": 0}
        event = {"type": "assistant/message", "seq": index,
                 "data": {"message": {"id": "message-%d" % index, "role": "assistant",
                                      "source": {"model": entry.get("model", model)}},
                          "usage": usage}}
        lines.append(event)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def base_env(root, fake_dsh=None, sessions=None, base_url=None):
    root = Path(root)
    env = {
        "DEEPSEEK_MCP_CONFIG": str(root / "config.json"),
        "DEEPSEEK_MCP_LOG_DIR": str(root / "runs"),
        "DEEPSEEK_MCP_JOBS_DIR": str(root / "jobs"),
        "DEEPSEEK_MCP_PID_FILE": str(root / "keep_awake.pid"),
        "DSH_SKILLS_DIR": str(root / "skills"),
        "DSH_WORKSPACE": str(root / "harness-workspace"),
        "DSH_SESSIONS_DIR": str(sessions or (root / "sessions")),
        "DSH_STATE_DIR": str(root / "state"),
        "FAKE_DSH_STATE": str(root / "fake-dsh-state.json"),
        "DEEPSEEK_API_KEY": "test-key-not-real",
        "DEEPSEEK_NO_CAFFEINATE": "1",
        # Tests must never launch a real browser; visual tests opt in with a
        # fake chrome binary and DSH_RENDER_PROBE=1.
        "DSH_RENDER_PROBE": "0",
        # P3-2: existing suites are not charged/perturbed by the reviewer
        # pre-flight; tests that exercise it opt in explicitly.
        "DSH_REVIEW_PREFLIGHT": "0",
        # The fake reviewer wrapper is a cross-vendor candidate; a real
        # dsh-reviewer-kimi.sh is inferred from its basename instead.
        "DSH_REVIEWER_VENDOR": "Kimi (Moonshot)",
        # The fake DSH writes session/marker files outside its workspace.  The
        # unit suite is not a sandbox test, so keep the OS wrapper off here as
        # well as in tests/__init__.py; sandbox tests opt in explicitly.
        "DSH_OS_SANDBOX": "0",
    }
    if fake_dsh:
        env["DSH_BIN"] = str(fake_dsh)
    if base_url:
        env["DEEPSEEK_BASE_URL"] = base_url
    return env


def write_fake_script(path, body):
    """Write a Python helper script and make it runnable on this host.

    On POSIX the script is executed through its shebang.  Windows
    ``CreateProcess`` cannot run a shebang script, so compile the same
    test-only launcher used for the fake ``dsh`` and return the ``.exe`` path.
    Callers must use the returned path, not the script path, as ``DSH_BIN`` or
    ``DSH_CHROME_BIN``.
    """
    path = Path(path)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if os.name == "nt":
        try:
            from . import build_fake_dsh
        except ImportError:  # tests run as top-level modules
            import build_fake_dsh
        return build_fake_dsh.build_launcher(path)
    return path


def write_fake_chrome(path):
    """Create an executable fake Chrome for render-probe tests only."""
    content = '''#!/usr/bin/env python3
import os
import sys
import time

args = sys.argv[1:]
log = os.environ.get("FAKE_CHROME_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")
if "--version" in args:
    print("Fake Chrome 123.0")
    sys.exit(0)
if "--dump-dom" in args:
    if os.environ.get("FAKE_CHROME_SLEEP"):
        time.sleep(float(os.environ["FAKE_CHROME_SLEEP"]))
    print("<html><body>ok</body></html>")
    sys.exit(0)
sys.exit(2)
'''
    return write_fake_script(path, content)
