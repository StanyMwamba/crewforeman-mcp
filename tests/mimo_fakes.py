"""Test fakes: fake ``mimo`` executables emitting the real MiMo event stream.

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
EVERY HTTP request must carry ``Authorization: Basic base64("mimocode:" +
password)`` (username ``MIMOCODE_SERVER_USERNAME`` or ``mimocode`` as in the
real CLI -- commit 2c41628 -- password matching) or it is answered 401.
Without the env var no auth is required (the real CLI's unsecured case).
When ``mimo run --attach <url>`` runs with ``MIMOCODE_SERVER_PASSWORD`` set,
the fake records its PRESENCE (never the value) as ``"password_env": true``
in the state capture (``FAKE_MIMO_STATE``) so tests can assert attach runs
were given the password; the password never appears in stdout/stderr or
captures.

Event stream (``mimo run --format json``, JSONL on stdout) matches the real
0.1.17 schema recorded in ``tests/fixtures/mimo/`` (verified per
MIMO_FIX2.md): every event carries top-level ``type``, ``timestamp`` (ms
int), ``sessionID``, and a ``part`` payload (all except ``error``, which
carries ``error`` instead)::

    {"type": "text", "timestamp": <ms>, "sessionID": "ses_...", "part": {
        "id": "prt_...", "messageID": "msg_...", "sessionID": "ses_...",
        "type": "text", "text": "...",
        "time": {"start": <ms>, "end": <ms>}}}
    {"type": "step_start", ..., "part": {"id": "prt_...", "messageID":
        "msg_...", "sessionID": "ses_...", "snapshot": "<sha>",
        "type": "step-start"}}
    {"type": "step_finish", ..., "part": {"id": "prt_...", "reason":
        "tool-calls" | "stop", "snapshot": "<sha>", "messageID": "msg_...",
        "sessionID": "ses_...", "type": "step-finish", "tokens": {"total":
        N, "input": N, "output": N, "reasoning": N, "cache": {"read": N,
        "write": N}}, "cost": F}}
    {"type": "tool_use", ..., "part": {"type": "tool", "execution":
        "started", "callID": "call_...", "tool":
        "bash|read|edit|write|grep|glob|actor|task|...", "state": {"status":
        "completed", "input": {...}, "output": "...", "metadata": {...},
        "title": "...", "time": {"start": <ms>, "end": <ms>}}, "id":
        "prt_...", "messageID": "msg_...", "sessionID": "ses_..."}}
    {"type": "error", "timestamp": <ms>, "sessionID": "ses_...", "error": {
        "name": "APIError", "data": {"message": "...", "statusCode": N,
        "isRetryable": <bool>, "metadata": {...}}}}

There is NO legacy ``data`` payload anywhere.  The answer text is the
``part.text`` of the ``text`` events (last one = final answer); usage
accumulates across ``step_finish`` ``part.tokens`` and ``part.tokens.cache``
is a DICT with ``read``/``write`` counts (never an int) whose parts sum to
``total``; the CLI's own USD cost is ``part.cost``; subagents appear as
``tool_use`` events with ``part.tool == "actor"`` and
``part.state.input.operation.action == "spawn"``; errors carry
``error.name`` plus ``error.data.{message,statusCode,isRetryable,metadata}``.
``sessionID`` appears both top-level and inside ``part``; when ``-s
<session>`` is passed the fake substitutes it into the replayed fixture
events so resume flows see their own id (fixture ids are kept otherwise).

Permission enforcement (``MIMOCODE_CONFIG_CONTENT``): the worker renders the
permission map as pure JSON (``{"permission": {...}}``) into the
``MIMOCODE_CONFIG_CONTENT`` environment variable -- never a workspace
``mimocode.jsonc``.  The fake parses that env var at the start of run/serve
and ENFORCES it; an absent or empty value means no enforcement (the
pre-3.15 behaviour).  The rules, matching the
``deepseek_mcp.mimo_permissions`` map shape (``read``, ``edit``, ``glob``,
``grep``, ``list``, ``bash``, ``task``, ``actor``, ``skill``, ``webfetch``,
``external_directory``; values are plain ``allow``/``deny`` strings or
glob-maps as for ``bash``):

- ``permission.edit == "deny"`` (plain string): every ``edit``/``write``
  tool_use the fake emits fails (``part.state.status = "error"``,
  ``part.state.output = "permission denied: edit"``) and the fake never
  creates or modifies a file for it.  ``"allow"``/absent: tool calls
  complete normally.
- ``permission.bash``: a plain ``"deny"``/``"allow"`` string is an
  all-denied/all-allowed verdict; a glob-map is evaluated against the bash
  command with ``fnmatch`` (case-sensitive on every platform).  MATCHING
  RULE: explicit (non-``"*"``) entries decide; among the matching entries
  the most specific pattern wins -- specificity is the number of non-wildcard
  characters in the pattern -- and ``"deny"`` wins on equal specificity.
  With no explicit match the ``"*"`` catch-all fills the gap (read-only
  ``{"*": "deny", "ls*": "allow"}`` lets ``ls x`` through and denies
  ``rm x``; workspace-write ``{"*": "allow", "git push*": "deny"}`` denies
  ``git push``).  With no matching entry at all the command is allowed.
  Denied bash calls get ``status = "error"``, ``output =
  "permission denied: bash"``.

Enforcement applies to every mode INCLUDING fixture replays: a replayed
``tool_use`` whose ``part.tool`` is ``edit``/``write`` (or ``bash`` with a
denied ``part.state.input.command``) has its ``part.state.status`` flipped
to ``"error"`` and ``part.state.output`` set to the denial text; the rest of
the event stays verbatim.  ok/attach runs therefore respect the config they
receive.

Mode ``write_probe`` proves a read-only reviewer cannot write: it emits a
real-shaped ``tool_use`` with ``part.tool = "write"`` targeting
``probe-written.txt`` in the ``--dir`` workspace (the path is in
``part.state.input``); when edit is permitted the fake ACTUALLY creates that
file (small content) and the tool is ``completed``; when edit is denied no
file appears and the tool is ``error`` with the denial output.  Final text
event: ``"write probe done"``.

State capture: every run/serve records a ``mimocode_config_env`` summary in
``FAKE_MIMO_STATE`` -- ``{"present": true/false, "edit":
"allow"|"deny"|null, "bash": <the map or string or null>}`` (presence and
resolved rules ONLY; the full env value is never dumped).
``password_env`` recording is unchanged.

Streams are generated FROM the recorded fixture files in
``tests/fixtures/mimo/`` (baked into the generated script at
``write_fake_mimo`` time):

- ``ok`` (and ``attach``, ``flaky`` successes, ``verbose``) replays
  ``mimo-0.1.17-run-events.jsonl`` verbatim -- the exact answer text, tokens
  and costs that recording carries.  ``verbose`` appends extra real-shaped
  ``tool_use`` events.
- the READY pre-flight task ("Reply with exactly READY and nothing else.")
  replays ``mimo-0.1.17-attach-ready.jsonl`` (answer text part "READY").
- ``error_auth`` replays ``mimo-0.1.17-error-401.jsonl`` (``error.name``
  "APIError", ``data.statusCode`` 401, ``isRetryable`` false).
- ``error_limit`` / ``error_network`` emit the same real error shape with
  their own message/statusCode: 429 retryable, and 0 (no HTTP response)
  retryable with a "fetch failed: ECONNRESET (network unreachable)" message.

Modes (``write_fake_mimo(path, mode=...)``, overridable with env
``FAKE_MIMO_MODE`` when non-empty):

- ``ok``          replays the run-events fixture; exit 0.
- ``text_only``   only real-shaped text events; the last is the synthetic
                  final answer "Fake mimo final answer: done."; exit 0.
- ``error_auth``  the fixture 401 error line; exit 1.
- ``error_limit`` one real-shaped error event, statusCode 429; exit 1.
- ``error_network`` one real-shaped error event, statusCode 0, network
                  message; exit 1.
- ``slow``        sleeps FAKE_MIMO_SLEEP seconds (default 3) emitting
                  periodic real-shaped text/step events, then finishes with
                  the synthetic final answer; exit 0.
- ``silent``      emits nothing and stalls (bounded sleep) so callers hit
                  their own timeout.
- ``flaky``       first run fails with a network error (state-file counter),
                  later runs behave like ``ok``.
- ``serve``       runs the ``mimo serve`` behaviour even for ``run`` argv:
                  binds ``--hostname``/``--port``, prints the ready line,
                  serves the HTTP surface above.
- ``attach``      behaves like ``ok`` and accepts/ignores ``--attach <url>``.
- ``verbose``     like ``ok`` with extra ``tool_use`` events.
- ``write_probe`` emits one real-shaped ``write`` tool_use against
                  ``probe-written.txt`` (see the permission section above)
                  and the final text ``"write probe done"``; exit 0.

Environment (see :func:`base_env`): ``FAKE_MIMO_MODE``, ``FAKE_MIMO_SLEEP``,
``FAKE_MIMO_ARGV`` (argv capture file), ``FAKE_MIMO_STATE`` (flaky counter,
``password_env`` presence flag and the ``mimocode_config_env`` summary),
``FAKE_MIMO_SESSIONS`` (session record directory), ``FAKE_MIMO_SERVE_URL``
(serve URL capture file), ``FAKE_MIMO_SERVE_PORT`` (default serve port),
``FAKE_MIMO_SERVE_HOST`` (default serve hostname), ``MIMOCODE_SERVER_PASSWORD``
(serve auth password / run-side presence recording; the value is never
echoed), ``MIMOCODE_CONFIG_CONTENT`` (rendered permission JSON, enforced as
documented above; ``base_env`` clears it so tests stay hermetic).

The fake is a plain Python script; on Windows the returned path is the
compiled launcher from ``tests.build_fake_dsh.build_launcher`` so it is
directly usable as ``MIMO_BIN``.
"""

import inspect
import json
import os
import stat
from pathlib import Path

# Modes honoured by the fake; anything else falls back to the ``ok`` stream.
MODES = ("ok", "text_only", "error_auth", "error_limit", "error_network",
         "slow", "silent", "flaky", "serve", "attach", "verbose",
         "write_probe")

# Real mimo 0.1.17 recordings; the fake's streams are generated from these.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "mimo"
RUN_EVENTS_FIXTURE = "mimo-0.1.17-run-events.jsonl"
ATTACH_READY_FIXTURE = "mimo-0.1.17-attach-ready.jsonl"
ERROR_401_FIXTURE = "mimo-0.1.17-error-401.jsonl"


def fixture_lines(name):
    """Raw JSONL lines of fixture *name* (blank lines dropped)."""
    text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def fixture_events(name):
    """Parsed JSON objects of fixture *name* (ground-truth event shape)."""
    return [json.loads(line) for line in fixture_lines(name)]


# -- MIMOCODE_CONFIG_CONTENT permission enforcement ----------------------
#
# These helpers are the single source of truth for enforcement: their source
# is spliced verbatim into the generated fake script (see
# ``_permissions_source``) and they are importable here so tests exercise the
# exact same code.  They must stay self-contained (local imports only) for
# that splice.  The matching rule is documented in the module docstring.

EDIT_DENIED_OUTPUT = "permission denied: edit"
BASH_DENIED_OUTPUT = "permission denied: bash"


def config_env_summary():
    """Summary of ``MIMOCODE_CONFIG_CONTENT``: presence and resolved rules.

    Returns ``{"present": bool, "edit": "allow"|"deny"|None, "bash": rule}``
    where *rule* is the map's bash value (plain string, glob-map dict, or
    ``None``).  The raw env value is never echoed -- presence and resolved
    rules ONLY.  Absent/empty/unparseable content means no enforcement
    (unparseable keeps ``present`` True with null rules).
    """
    import json
    import os
    summary = {"present": False, "edit": None, "bash": None}
    raw = os.environ.get("MIMOCODE_CONFIG_CONTENT")
    if not raw or not raw.strip():
        return summary
    summary["present"] = True
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return summary
    permission = doc.get("permission") if isinstance(doc, dict) else None
    if not isinstance(permission, dict):
        return summary
    edit = permission.get("edit")
    if edit in ("allow", "deny"):
        summary["edit"] = edit
    bash = permission.get("bash")
    if isinstance(bash, str):
        if bash in ("allow", "deny"):
            summary["bash"] = bash
    elif isinstance(bash, dict):
        summary["bash"] = bash
    return summary


def bash_allows(command, rule):
    """True when *command* is permitted by the bash permission *rule*.

    * ``None`` -- no enforcement (allowed).
    * string -- ``"deny"`` denies every command, ``"allow"`` allows all.
    * glob-map -- ``fnmatch`` patterns to ``"allow"``/``"deny"``.  Explicit
      (non-``"*"``) entries decide; among matching entries the most specific
      (most non-wildcard characters) wins, and ``"deny"`` wins on equal
      specificity.  With no explicit match the ``"*"`` catch-all fills the
      gap; with no matching entry at all the command is allowed.
    """
    if rule is None:
        return True
    if isinstance(rule, str):
        return rule != "deny"
    if not isinstance(rule, dict) or not rule:
        return True
    import fnmatch
    best = None
    for pattern, verdict in rule.items():
        if not isinstance(pattern, str) or pattern == "*":
            continue
        if verdict not in ("allow", "deny"):
            continue
        if not fnmatch.fnmatchcase(command, pattern):
            continue
        specificity = len([ch for ch in pattern if ch not in "*?"])
        allowed = verdict == "allow"
        if best is None or specificity > best[0]:
            best = (specificity, allowed)
        elif specificity == best[0] and not allowed:
            best = (specificity, False)  # deny wins on equal specificity
    if best is not None:
        return best[1]
    return rule.get("*") != "deny"


def apply_permission(event, summary):
    """Apply the permission *summary* to one emitted event (in place).

    Denied ``tool_use`` events get ``part.state.status = "error"`` and
    ``part.state.output`` set to the denial text; everything else in the
    event stays verbatim.  Returns True when the event was flipped.
    """
    if not isinstance(event, dict) or event.get("type") != "tool_use":
        return False
    part = event.get("part")
    if not isinstance(part, dict):
        return False
    state = part.get("state")
    if not isinstance(state, dict):
        return False
    rules = summary if isinstance(summary, dict) else {}
    tool = part.get("tool")
    if tool in ("edit", "write"):
        if rules.get("edit") == "deny":
            state["status"] = "error"
            state["output"] = EDIT_DENIED_OUTPUT
            return True
        return False
    if tool == "bash":
        tool_input = state.get("input")
        command = (tool_input.get("command")
                   if isinstance(tool_input, dict) else None)
        if isinstance(command, str) and not bash_allows(
                command, rules.get("bash")):
            state["status"] = "error"
            state["output"] = BASH_DENIED_OUTPUT
            return True
    return False


def _permissions_source():
    """Permission helper source, spliced into the generated fake script.

    The generated script must enforce the exact code tests import here, so
    ``write_fake_mimo`` embeds this text verbatim (placeholders ``__PERMISSIONS__``).
    """
    parts = ["EDIT_DENIED_OUTPUT = %r" % EDIT_DENIED_OUTPUT,
             "BASH_DENIED_OUTPUT = %r" % BASH_DENIED_OUTPUT]
    parts.extend(inspect.getsource(fn) for fn in
                 (config_env_summary, bash_allows, apply_permission))
    return "\n\n".join(parts)


# The generated script.  Placeholders ``__DEFAULT_MODE__``, ``__RUN_EVENTS__``,
# ``__READY_EVENTS__``, ``__ERROR_401__`` and ``__PERMISSIONS__`` are filled
# at write_fake_mimo time (the three fixtures are baked in as raw JSONL
# lines; ``__PERMISSIONS__`` is the enforcement source above).
_FAKE_MIMO_BODY = r'''#!/usr/bin/env python3
"""Fake mimo CLI for the CrewForeman test suite (real 0.1.17 JSONL events).

Generated by tests.mimo_fakes.write_fake_mimo; the ok/READY/401 streams are
baked from tests/fixtures/mimo/ at generation time.
"""
import base64
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

DEFAULT_MODE = __DEFAULT_MODE__
# Raw JSONL lines baked from the real mimo 0.1.17 recordings.
RUN_EVENTS = __RUN_EVENTS__
READY_EVENTS = __READY_EVENTS__
ERROR_401 = __ERROR_401__

# Synthetic (non-fixture) modes finish with this text; the ok family answers
# with the run-events fixture's own text.
FINAL_ANSWER = "Fake mimo final answer: done."
SNAPSHOT = "f" * 40

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

# MIMOCODE_CONFIG_CONTENT permission enforcement (spliced from
# tests/mimo_fakes._permissions_source; see the module docstring of
# tests.mimo_fakes for the matching rule).
__PERMISSIONS__


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


def emit_raw(line):
    try:
        print(line, flush=True)
    except (BrokenPipeError, OSError):
        sys.exit(0)


def default_session():
    """Session id of the baked run-events recording."""
    try:
        return json.loads(RUN_EVENTS[0]).get("sessionID") or "ses_fake_mimo"
    except Exception:
        return "ses_fake_mimo"


def replay(lines, session=None, summary=None):
    """Print baked fixture JSONL lines, honouring session + permissions.

    With *session* (the ``-s`` value) the top-level and ``part.sessionID``
    fields are substituted so resume flows see their own id; with *summary*
    (the MIMOCODE_CONFIG_CONTENT rules) denied tool_use events are flipped
    to error status (rest verbatim).  Lines needing no change are printed
    byte-identically.
    """
    for raw in lines:
        event = None
        if session or summary:
            try:
                event = json.loads(raw)
            except Exception:
                event = None
        if isinstance(event, dict):
            flipped = apply_permission(event, summary)
            if session:
                if "sessionID" in event:
                    event["sessionID"] = session
                part = event.get("part")
                if isinstance(part, dict) and "sessionID" in part:
                    part["sessionID"] = session
            if flipped or session:
                emit(event)
                continue
        emit_raw(raw)


def _ms():
    return int(time.time() * 1000)


def text_event(text, session, seq):
    started = _ms()
    ended = _ms()
    return {"type": "text", "timestamp": ended, "sessionID": session,
            "part": {"id": "prt_fake_%06d" % seq,
                     "messageID": "msg_fake_%06d" % seq,
                     "sessionID": session, "type": "text", "text": text,
                     "time": {"start": started, "end": ended}}}


def step_start_event(session, seq):
    return {"type": "step_start", "timestamp": _ms(), "sessionID": session,
            "part": {"id": "prt_fake_%06d" % seq,
                     "messageID": "msg_fake_%06d" % seq,
                     "sessionID": session, "snapshot": SNAPSHOT,
                     "type": "step-start"}}


def step_tokens(step):
    """Synthetic per-step usage; total always equals the parts' sum."""
    input_t = 60 + 10 * step
    output_t = 30 + 8 * step
    reasoning_t = 5 + step
    cache_read = 15 + 5 * step
    cache_write = 0
    return {"total": input_t + output_t + reasoning_t + cache_read + cache_write,
            "input": input_t, "output": output_t, "reasoning": reasoning_t,
            "cache": {"read": cache_read, "write": cache_write}}


def step_finish_event(step, reason, session, seq):
    return {"type": "step_finish", "timestamp": _ms(), "sessionID": session,
            "part": {"id": "prt_fake_%06d" % seq, "reason": reason,
                     "snapshot": SNAPSHOT,
                     "messageID": "msg_fake_%06d" % seq,
                     "sessionID": session, "type": "step-finish",
                     "tokens": step_tokens(step),
                     "cost": round(0.001 * step, 6)}}


def tool_event(tool, tool_input, output, title, session, seq):
    started = _ms()
    ended = _ms()
    return {"type": "tool_use", "timestamp": ended, "sessionID": session,
            "part": {"type": "tool", "execution": "started",
                     "callID": "call_fake_%06d" % seq, "tool": tool,
                     "state": {"status": "completed", "input": tool_input,
                               "output": output,
                               "metadata": {"truncated": False},
                               "title": title,
                               "time": {"start": started, "end": ended}},
                     "id": "prt_fake_%06d" % seq,
                     "messageID": "msg_fake_%06d" % seq,
                     "sessionID": session}}


def error_event(message, status_code, is_retryable, session):
    """Real 0.1.17 error shape (see the error-401 recording)."""
    return {"type": "error", "timestamp": _ms(), "sessionID": session,
            "error": {"name": "APIError",
                      "data": {"message": message,
                               "statusCode": status_code,
                               "isRetryable": bool(is_retryable),
                               "metadata": {
                                   "providerID": "xiaomi",
                                   "url": "https://api.xiaomimimo.com/v1/chat/completions"}}}}


def run_ok(opts, verbose=False, summary=None):
    """Replay the run-events recording; verbose appends extra tool_use.

    Both the replayed and the extra tool_use events honour *summary*
    (MIMOCODE_CONFIG_CONTENT rules).
    """
    replay(RUN_EVENTS, opts.get("session"), summary)
    if not verbose:
        return
    session = opts.get("session") or default_session()
    extras = [
        ("read", {"file_path": "NOTES.md"},
         "<path>NOTES.md</path>\n<type>file</type>\n<content>\n1: notes\n</content>",
         "NOTES.md"),
        ("bash", {"command": "ls", "description": "List files"},
         "NOTES.md", "List files"),
        ("write", {"file_path": "NOTES.md", "content": "done\n"},
         "Wrote file successfully.", "NOTES.md"),
    ]
    for offset, (tool, tool_input, output, title) in enumerate(extras, 1):
        event = tool_event(tool, tool_input, output, title, session,
                           100 + offset)
        apply_permission(event, summary)
        emit(event)


def run_text_only(opts):
    session = opts.get("session") or default_session()
    task = opts.get("task") or ""
    emit(text_event("Working on: " + task[:60], session, 1))
    emit(text_event(FINAL_ANSWER, session, 2))


def run_slow(opts):
    duration = 3.0
    raw = os.environ.get("FAKE_MIMO_SLEEP")
    if raw:
        try:
            duration = float(raw)
        except ValueError:
            duration = 3.0
    session = opts.get("session") or default_session()
    deadline = time.time() + duration
    seq = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        seq += 1
        emit(text_event("still working (%d)" % seq, session, seq * 10 + 1))
        emit(step_start_event(session, seq * 10 + 2))
        time.sleep(min(0.2, remaining))
        emit(step_finish_event(seq, "tool-calls", session, seq * 10 + 3))
    emit(text_event(FINAL_ANSWER, session, seq * 10 + 9))


def run_write_probe(opts, summary):
    """write_probe mode: one real write tool_use against probe-written.txt.

    When edit is permitted the file is actually created under ``--dir`` and
    the tool completes; when edit is denied no file appears and the tool
    fails with the denial output.  Final text event: "write probe done".
    """
    session = opts.get("session") or default_session()
    workspace = Path(opts["dir"] or os.getcwd())
    target = workspace / "probe-written.txt"
    event = tool_event("write",
                       {"file_path": str(target), "content": "probe write\n"},
                       "Wrote file successfully.", "probe-written.txt",
                       session, 1)
    if not apply_permission(event, summary):
        try:
            target.write_text("probe write\n", encoding="utf-8")
        except OSError:
            pass
    emit(event)
    emit(text_event("write probe done", session, 2))


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
            expected_user = os.environ.get("MIMOCODE_SERVER_USERNAME") or "mimocode"
            return sep == ":" and user == expected_user and supplied == password

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


def record_config_env(opts, summary):
    """Record the mimocode_config_env summary (presence + resolved rules)."""
    path = state_path(opts)
    state = load_state(path)
    state["mimocode_config_env"] = summary
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
    # Parse + enforce MIMOCODE_CONFIG_CONTENT at the start of run/serve
    # behaviour (absent/empty = no enforcement) and capture the summary.
    summary = config_env_summary()
    record_config_env(opts, summary)
    if opts["subcommand"] == "serve" or mode == "serve":
        run_serve(opts)
    # Run-side (incl. --attach): note password-env presence, never the value.
    if os.environ.get("MIMOCODE_SERVER_PASSWORD"):
        record_password_env(opts)
    if mode == "silent":
        time.sleep(300)
        sys.exit(0)
    session = opts.get("session") or default_session()
    if mode == "error_auth":
        # The real 401 recording (APIError / statusCode 401).
        replay([ERROR_401], opts.get("session"), summary)
        sys.exit(1)
    if mode == "error_limit":
        emit(error_event(
            "429 rate limit exceeded: reduce parallel agents and retry",
            429, True, session))
        sys.exit(1)
    if mode == "error_network":
        # statusCode 0 = no HTTP response (client-side network failure).
        emit(error_event("fetch failed: ECONNRESET (network unreachable)",
                         0, True, session))
        sys.exit(1)
    if mode == "flaky":
        counter_path = state_path(opts)
        state = load_state(counter_path)
        state["flaky"] = int(state.get("flaky", 0)) + 1
        save_state(counter_path, state)
        if state["flaky"] == 1:
            emit(error_event("fetch failed: ECONNRESET (network unreachable)",
                             0, True, session))
            sys.exit(1)
    maybe_session_record(opts)
    task = opts["task"] or ""
    if task.lstrip().startswith("Reply with exactly READY"):
        # The real attach-ready recording (text part "READY").
        replay(READY_EVENTS, opts.get("session"), summary)
        sys.exit(0)
    if mode == "slow":
        run_slow(opts)
        sys.exit(0)
    if mode == "text_only":
        run_text_only(opts)
        sys.exit(0)
    if mode == "write_probe":
        run_write_probe(opts, summary)
        sys.exit(0)
    if mode == "verbose":
        run_ok(opts, verbose=True, summary=summary)
        sys.exit(0)
    run_ok(opts, summary=summary)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
'''


def write_fake_mimo(path, mode="ok"):
    """Create an executable fake ``mimo`` CLI emitting real JSONL events.

    Returns the path to set as ``MIMO_BIN``.  On Windows CreateProcess cannot
    run a shebang script, so the compiled test-only launcher is returned
    instead (a missing compiler is fatal).  *mode* is the default behaviour;
    a non-empty ``FAKE_MIMO_MODE`` environment value overrides it at run time.
    The ok/READY/401 streams are baked from ``tests/fixtures/mimo/`` here;
    the ``MIMOCODE_CONFIG_CONTENT`` enforcement helpers are spliced in from
    :func:`_permissions_source` so the script enforces exactly what tests
    import from this module.
    """
    content = _FAKE_MIMO_BODY
    content = content.replace("__DEFAULT_MODE__", repr(mode))
    content = content.replace(
        "__RUN_EVENTS__", json.dumps(fixture_lines(RUN_EVENTS_FIXTURE)))
    content = content.replace(
        "__READY_EVENTS__", json.dumps(fixture_lines(ATTACH_READY_FIXTURE)))
    content = content.replace(
        "__ERROR_401__", json.dumps(fixture_lines(ERROR_401_FIXTURE)[0]))
    content = content.replace("__PERMISSIONS__", _permissions_source())
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
    ``MIMOCODE_CONFIG_CONTENT`` is cleared (``""`` = no enforcement) so runs
    stay hermetic; pass a JSON string in *extra* to exercise enforcement.
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
        # "" clears any inherited config: no permission enforcement.
        "MIMOCODE_CONFIG_CONTENT": "",
    }
    env.update(extra)
    return env

