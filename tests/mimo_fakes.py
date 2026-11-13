"""Test fakes: fake ``mimo`` executables emitting the MiMo JSON event stream.

This module owns the fake ``mimo`` programs used by the whole 3.15 suite --
tests never run the real ``mimo`` or touch the network.  The fake honours the
CLI contract produced by ``mimo_worker.build_argv`` and verified against the
real mimo 0.1.17 CLI (MIMO_FIX1.md)::

    mimo run "<task>" --format json --dir <folder> -m <model>
        [--variant <v>] [-s <session>] [--attach <url>]
        [--dangerously-skip-permissions]
    mimo serve --hostname <h> --port <p>

``mimo serve`` has no ``--host`` flag: like the real CLI the fake prints a
usage blurb and exits non-zero when ``--host`` (or ``--host=...``) appears
anywhere in argv, and never starts a server.  ``--port 0`` binds an ephemeral
port and the ready line reports the port actually bound.  After binding, the
fake prints exactly (real CLI format)::

    mimocode server listening on http://<hostname>:<port>

on stdout (``<hostname>`` defaults to ``127.0.0.1``), so with defaults the
line is ``mimocode server listening on http://127.0.0.1:<port>``.  This plain
line (not JSONL) is the contract ``mimo_worker`` parses to learn the bound
port.

HTTP surface of ``mimo serve`` (stdlib server on 127.0.0.1 only):

- ``GET /global/health`` -> 200, body ``ok``.
- ``GET /healthz``       -> 503.
- ``GET /doc``           -> 200.
- anything else          -> 404.

When ``MIMOCODE_SERVER_PASSWORD`` is set (non-empty) in the serve process,
EVERY HTTP request must carry ``Authorization: Basic base64(":" + password)``
(empty username, password part matching) or it is answered 401.  Without the
env var no auth is required (the real CLI's unsecured case).  When
``mimo run --attach <url>`` runs with ``MIMOCODE_SERVER_PASSWORD`` set, the
fake records its PRESENCE (never the value) as ``"password_env": true`` in
the state capture (``FAKE_MIMO_STATE``) so tests can assert attach runs were
given the password; the password never appears in stdout/stderr or captures.

and emits the pinned event-stream contract (JSONL on stdout)::

    {"type": "text", "data": {"text": "..."}}
    {"type": "step_start", "data": {"name": "..."}}
    {"type": "step_finish", "data": {"tokens": {"total": N, "input": N,
                                  "output": N, "reasoning": N, "cache": N}}}
    {"type": "tool_use", "data": {"name": "bash", "input": {...}}}
    {"type": "error", "error": {"data": {"message": "...", "statusCode": 401}}}
    {"type": "serve", "url": "http://host:port"}

The last ``text`` event of a successful run is the final answer.  Token usage
accumulates across ``step_finish`` events.  The ``serve`` event is an extra
helpful marker emitted after the ready line; the ready line itself is the
contract, and the serve URL capture file (``FAKE_MIMO_SERVE_URL``) is still
written for tests.

Modes (``write_fake_mimo(path, mode=...)``, overridable with env
``FAKE_MIMO_MODE`` when non-empty):

- ``ok``          text / step_start / tool_use / step_finish (two steps) /
                  final text; exit 0.
- ``text_only``   only text events; the last is the final answer; exit 0.
- ``error_auth``  one error event, message "401 Unauthorized: invalid api key",
                  statusCode 401; exit 1.
- ``error_limit`` one error event, statusCode 429, message containing
                  "rate limit"; exit 1.
- ``error_network`` one error event, message containing "fetch failed:
                  ECONNRESET" and "network" (no statusCode); exit 1.
- ``slow``        sleeps FAKE_MIMO_SLEEP seconds (default 3) emitting periodic
                  text/step events, then finishes; exit 0.
- ``silent``      emits nothing and stalls (bounded sleep) so callers hit their
                  own timeout.
- ``flaky``       first run fails with a network error (state-file counter),
                  later runs behave like ``ok``.
- ``serve``       runs the ``mimo serve`` behaviour even for ``run`` argv:
                  binds ``--hostname``/``--port``, prints the ready line,
                  serves the HTTP surface above.
- ``attach``      behaves like ``ok`` and accepts/ignores ``--attach <url>``.
- ``verbose``     like ``ok`` with extra ``tool_use`` events.

Environment (see :func:`base_env`): ``FAKE_MIMO_MODE``, ``FAKE_MIMO_SLEEP``,
``FAKE_MIMO_ARGV`` (argv capture file), ``FAKE_MIMO_STATE`` (flaky counter and
``password_env`` presence flag), ``FAKE_MIMO_SESSIONS`` (session record
directory), ``FAKE_MIMO_SERVE_URL`` (serve URL capture file),
``FAKE_MIMO_SERVE_PORT`` (default serve port), ``FAKE_MIMO_SERVE_HOST``
(default serve hostname), ``MIMOCODE_SERVER_PASSWORD`` (serve auth password /
run-side presence recording; the value is never echoed).

The fake is a plain Python script; on Windows the returned path is the
compiled launcher from ``tests.build_fake_dsh.build_launcher`` so it is
directly usable as ``MIMO_BIN``.
"""

import json
import os
import stat
from pathlib import Path

# Modes honoured by the fake; anything else falls back to the ``ok`` stream.
MODES = ("ok", "text_only", "error_auth", "error_limit", "error_network",
         "slow", "silent", "flaky", "serve", "attach", "verbose")

# The generated script.  ``__DEFAULT_MODE__`` is replaced at write time.
_FAKE_MIMO_BODY = r'''#!/usr/bin/env python3
"""Fake mimo CLI for the CrewForeman test suite (JSONL event stream)."""
import base64
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

DEFAULT_MODE = __DEFAULT_MODE__
FINAL_ANSWER = "Fake mimo final answer: done."

# Real CLI (mimo 0.1.17): mimo serve has NO --host flag; it is --hostname/--port.
SERVE_USAGE = (
    "usage: mimo serve [--hostname HOSTNAME] [--port PORT]\n"
    "\n"
    "Serve the MiMo HTTP server.\n"
    "\n"
    "optional arguments:\n"
    "  --hostname HOSTNAME  bind hostname (default: 127.0.0.1)\n"
    "  --port PORT          bind port (0 = OS-assigned)\n"
)

VALUE_FLAGS = {
    "--format": "format", "--dir": "dir", "-m": "model", "--model": "model",
    "--variant": "variant", "-s": "session", "--session": "session",
    "--attach": "attach", "--hostname": "hostname", "--port": "port",
}
BOOL_FLAGS = {"--dangerously-skip-permissions": "skip_permissions",
              "--yolo": "skip_permissions"}


def parse_cli(argv):
    """Parse the mimo CLI loosely: flags in any order, task after 'run'."""
    opts = {"subcommand": None, "task": None, "format": None, "dir": None,
            "model": None, "variant": None, "session": None, "attach": None,
            "hostname": None, "port": None, "skip_permissions": False}
    positionals = []
    args = list(argv[1:])
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in BOOL_FLAGS:
            opts[BOOL_FLAGS[arg]] = True
            index += 1
            continue
        name, value = arg, None
        if arg.startswith("-") and "=" in arg:
            name, value = arg.split("=", 1)
        if name in VALUE_FLAGS:
            if value is None:
                index += 1
                value = args[index] if index < len(args) else ""
            opts[VALUE_FLAGS[name]] = value
            index += 1
            continue
        if not arg.startswith("-"):
            positionals.append(arg)
        index += 1
    if positionals:
        if positionals[0] in ("run", "serve"):
            opts["subcommand"] = positionals[0]
            if len(positionals) > 1:
                opts["task"] = positionals[1]
        else:
            opts["task"] = positionals[0]
    return opts


def emit(payload):
    try:
        print(json.dumps(payload), flush=True)
    except (BrokenPipeError, OSError):
        sys.exit(0)


def emit_error(message, status_code=None):
    data = {"message": message}
    if status_code is not None:
        data["statusCode"] = status_code
    emit({"type": "error", "error": {"data": data}})


def step_finish(step):
    return {"type": "step_finish", "data": {"tokens": {
        "total": 100 + 20 * step, "input": 60 + 10 * step,
        "output": 30 + 8 * step, "reasoning": 5 + step,
        "cache": 15 + 5 * step}}}


def run_stream(task, verbose=False, text_only=False):
    emit({"type": "text", "data": {"text": "Working on: " + (task or "")[:60]}})
    if text_only:
        emit({"type": "text", "data": {"text": FINAL_ANSWER}})
        return
    tools = (["bash", "read", "grep"] if verbose else ["bash", "edit"])
    steps = 2
    for step in range(1, steps + 1):
        emit({"type": "step_start", "data": {"name": "step-%d" % step}})
        for tool in tools:
            emit({"type": "tool_use",
                  "data": {"name": tool, "input": {"command": tool}}})
        emit(step_finish(step))
    emit({"type": "text", "data": {"text": FINAL_ANSWER}})


def run_slow():
    duration = 3.0
    raw = os.environ.get("FAKE_MIMO_SLEEP")
    if raw:
        try:
            duration = float(raw)
        except ValueError:
            duration = 3.0
    deadline = time.time() + duration
    seq = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        seq += 1
        emit({"type": "text", "data": {"text": "still working (%d)" % seq}})
        emit({"type": "step_start", "data": {"name": "slow-%d" % seq}})
        time.sleep(min(0.2, remaining))
        emit(step_finish(seq))
    emit({"type": "text", "data": {"text": FINAL_ANSWER}})


def maybe_session_record(opts):
    sessions = os.environ.get("FAKE_MIMO_SESSIONS")
    if not sessions:
        return
    sid = opts.get("session") or ("fake-session-" + str(os.getpid()))
    directory = Path(sessions)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        workspace = Path(opts["dir"] or os.getcwd()).resolve()
        (directory / ("session-" + str(os.getpid()) + ".json")).write_text(
            json.dumps({"session": sid, "cwd": str(workspace),
                        "model": opts.get("model") or "",
                        "task": opts.get("task") or ""}),
            encoding="utf-8")
    except OSError:
        pass


def run_serve(opts):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    host = (opts.get("hostname") or os.environ.get("FAKE_MIMO_SERVE_HOST")
            or "127.0.0.1")
    raw_port = opts.get("port")
    if raw_port is None or raw_port == "":
        raw_port = os.environ.get("FAKE_MIMO_SERVE_PORT") or "0"
    try:
        port = int(raw_port)
    except ValueError:
        port = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _authorized(self):
            password = os.environ.get("MIMOCODE_SERVER_PASSWORD")
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            except Exception:
                return False
            user, sep, supplied = decoded.partition(":")
            return sep == ":" and user == "" and supplied == password

        def _send(self, status, body, write_body):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="mimocode"')
            self.end_headers()
            if write_body:
                self.wfile.write(body)

        def _respond(self, write_body):
            if not self._authorized():
                self._send(401, b"unauthorized", write_body)
                return
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/global/health":
                self._send(200, b"ok", write_body)
            elif path == "/healthz":
                self._send(503, b"unhealthy", write_body)
            elif path == "/doc":
                self._send(200, b"doc", write_body)
            else:
                self._send(404, b"", write_body)

        def do_GET(self):
            self._respond(True)

        def do_HEAD(self):
            self._respond(False)

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    httpd = HTTPServer((host, port), Handler)
    bound = httpd.server_address[1]
    url = "http://%s:%s" % (host, bound)
    # Real CLI ready line, printed after the bind with the real bound port.
    print("mimocode server listening on %s" % url, flush=True)
    emit({"type": "serve", "url": url})
    capture = os.environ.get("FAKE_MIMO_SERVE_URL")
    if capture:
        try:
            Path(capture).write_text(
                json.dumps({"url": url, "host": host, "port": bound}),
                encoding="utf-8")
        except OSError:
            pass
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()

    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    worker = threading.Thread(target=httpd.serve_forever)
    worker.daemon = True
    worker.start()
    stop.wait()
    httpd.shutdown()
    httpd.server_close()
    worker.join(5)
    sys.exit(0)


def state_path(opts):
    workspace = Path(opts["dir"] or os.getcwd())
    return Path(os.environ.get("FAKE_MIMO_STATE")
                or str(workspace / ".fake-mimo-state.json"))


def load_state(state_path):
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state_path, state):
    try:
        Path(state_path).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def record_password_env(opts):
    """Record that MIMOCODE_SERVER_PASSWORD was present; never its value."""
    path = state_path(opts)
    state = load_state(path)
    state["password_env"] = True
    save_state(path, state)


def main():
    opts = parse_cli(sys.argv)
    if os.environ.get("FAKE_MIMO_ARGV"):
        try:
            Path(os.environ["FAKE_MIMO_ARGV"]).write_text(
                json.dumps(sys.argv), encoding="utf-8")
        except OSError:
            pass
    # Real CLI has no --host flag: it prints usage/help and exits non-zero
    # without starting a server.
    for arg in sys.argv[1:]:
        if arg == "--host" or arg.startswith("--host="):
            sys.stderr.write(SERVE_USAGE)
            sys.stderr.write(
                "mimo serve: error: unrecognized arguments: %s\n" % arg)
            sys.exit(2)
    mode = os.environ.get("FAKE_MIMO_MODE") or DEFAULT_MODE
    if opts["subcommand"] == "serve" or mode == "serve":
        run_serve(opts)
    # Run-side (incl. --attach): note password-env presence, never the value.
    if os.environ.get("MIMOCODE_SERVER_PASSWORD"):
        record_password_env(opts)
    if mode == "silent":
        time.sleep(300)
        sys.exit(0)
    if mode == "error_auth":
        emit_error("401 Unauthorized: invalid api key", 401)
        sys.exit(1)
    if mode == "error_limit":
        emit_error("429 rate limit exceeded: reduce parallel agents and retry",
                   429)
        sys.exit(1)
    if mode == "error_network":
        emit_error("fetch failed: ECONNRESET (network unreachable)")
        sys.exit(1)
    if mode == "flaky":
        counter_path = state_path(opts)
        state = load_state(counter_path)
        state["flaky"] = int(state.get("flaky", 0)) + 1
        save_state(counter_path, state)
        if state["flaky"] == 1:
            emit_error("fetch failed: ECONNRESET (network unreachable)")
            sys.exit(1)
    maybe_session_record(opts)
    task = opts["task"] or ""
    if task.lstrip().startswith("Reply with exactly READY"):
        emit({"type": "text", "data": {"text": "READY"}})
        sys.exit(0)
    if mode == "slow":
        run_slow()
        sys.exit(0)
    if mode == "text_only":
        run_stream(task, text_only=True)
        sys.exit(0)
    if mode == "verbose":
        run_stream(task, verbose=True)
        sys.exit(0)
    run_stream(task)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
'''


def write_fake_mimo(path, mode="ok"):
    """Create an executable fake ``mimo`` CLI emitting JSONL events.

    Returns the path to set as ``MIMO_BIN``.  On Windows CreateProcess cannot
    run a shebang script, so the compiled test-only launcher is returned
    instead (a missing compiler is fatal).  *mode* is the default behaviour;
    a non-empty ``FAKE_MIMO_MODE`` environment value overrides it at run time.
    """
    content = _FAKE_MIMO_BODY.replace("__DEFAULT_MODE__", repr(mode))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if os.name == "nt":
        # Windows CreateProcess cannot execute a shebang script; compile the
        # test-only launcher and return it.  A missing compiler is fatal.
        try:
            from . import build_fake_dsh
        except ImportError:  # tests run as top-level modules
            import build_fake_dsh
        return build_fake_dsh.build_launcher(path)
    return path


def base_env(root, fake_mimo=None, **extra):
    """Environment for running the fake ``mimo`` under an isolated temp *root*.

    Mirrors the keys of :func:`tests.fakes.base_env` (all paths under *root*)
    plus the MiMo knobs: ``MIMO_BIN``, ``FAKE_MIMO_ARGV``, ``FAKE_MIMO_MODE``,
    ``FAKE_MIMO_SLEEP``, ``FAKE_MIMO_SESSIONS``, ``FAKE_MIMO_SERVE_URL``,
    ``FAKE_MIMO_SERVE_PORT``, ``FAKE_MIMO_STATE``.  ``FAKE_MIMO_MODE`` is set
    to ``""`` meaning "defer to the mode baked into the fake by
    ``write_fake_mimo``"; pass e.g. ``FAKE_MIMO_MODE="slow"`` to override.
    *extra* keys update the returned dict.
    """
    root = Path(root)
    env = {
        "DEEPSEEK_MCP_CONFIG": str(root / "config.json"),
        "DEEPSEEK_MCP_LOG_DIR": str(root / "runs"),
        "DEEPSEEK_MCP_JOBS_DIR": str(root / "jobs"),
        "DEEPSEEK_MCP_PID_FILE": str(root / "keep_awake.pid"),
        "DSH_SKILLS_DIR": str(root / "skills"),
        "DSH_WORKSPACE": str(root / "harness-workspace"),
        "DSH_SESSIONS_DIR": str(root / "sessions"),
        "DSH_STATE_DIR": str(root / "state"),
        "DEEPSEEK_API_KEY": "test-key-not-real",
        "DEEPSEEK_NO_CAFFEINATE": "1",
        # Tests must never launch a real browser.
        "DSH_RENDER_PROBE": "0",
        # The reviewer pre-flight is exercised only by tests that opt in.
        "DSH_REVIEW_PREFLIGHT": "0",
        "DSH_REVIEWER_VENDOR": "Kimi (Moonshot)",
        # The unit suite is not a sandbox test.
        "DSH_OS_SANDBOX": "0",
        "MIMO_BIN": str(fake_mimo) if fake_mimo else "mimo",
        "FAKE_MIMO_ARGV": str(root / "fake-mimo-argv.json"),
        # "" defers to the mode baked by write_fake_mimo; non-empty overrides.
        "FAKE_MIMO_MODE": "",
        "FAKE_MIMO_SLEEP": "3",
        "FAKE_MIMO_SERVE_PORT": "0",
        "FAKE_MIMO_STATE": str(root / "fake-mimo-state.json"),
        "FAKE_MIMO_SESSIONS": str(root / "fake-mimo-sessions"),
        "FAKE_MIMO_SERVE_URL": str(root / "fake-mimo-serve-url.json"),
    }
    env.update(extra)
    return env
