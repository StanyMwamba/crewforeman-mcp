"""MiMo Code worker adapter: run, parse, errors, sessions, serve/attach, ping.

Drives the ``mimo`` CLI (``mimo run --format json``) as a second-vendor
CrewForeman worker.  The public API mirrors the contract pinned in
MIMO_PLAN.md: ``is_mimo_target``, ``build_argv``, ``parse_event``,
``classify_error``, ``execute``, ``MimoServer`` and ``ping``.

Event schema -- CANONICAL shape, verified on mimo 0.1.17 (recorded lines in
``tests/fixtures/mimo/``).  Every event carries a top-level ``type``,
``timestamp`` (ms) and ``sessionID``, plus a ``part`` object (everything
except ``error``, which has none):

- ``text``: ``part.text`` and ``part.time.{start,end}``.
- ``step_start``: ``part.type == "step-start"``, ``part.snapshot``.
- ``step_finish``: ``part.reason`` (``tool-calls`` | ``stop``),
  ``part.tokens`` = ``{total, input, output, reasoning, cache: {read,
  write}}`` (``cache`` is a DICT, not an int) and ``part.cost`` = the CLI's
  own USD cost for that step.
- ``tool_use``: ``part.tool``, ``part.callID``,
  ``part.state.{status, input, output, metadata, title}``; subagents are
  ``tool == "actor"`` with ``state.input.operation.action == "spawn"``.
- ``error``: top-level ``error.name`` and
  ``error.data.{message, statusCode, isRetryable, metadata}``.

:func:`parse_event` never raises and also accepts the older
``{"type": ..., "data": {...}}`` payload (legacy inline fakes) as a
tolerant fallback; the canonical ``part`` fields win when both are present.

Normalised event contract (keys of the dict :func:`parse_event` returns):

- every event: ``kind`` (``text`` | ``step_start`` | ``step_finish`` |
  ``tool_use`` | ``error`` | ``session`` | ``other``) and ``session_id``
  when known (top-level ``sessionID``, ``part.sessionID``, legacy
  spellings).
- text: ``text``, ``time`` (``{start, end}`` ms, or ``None``).
- step_start: ``name``, ``snapshot``.
- step_finish: ``name``, ``reason``, ``cost`` (float or ``None``),
  ``tokens`` = ``{total, input, output, reasoning, cache_read,
  cache_write}`` -- ``cache.read`` (or a legacy int ``cache``) maps to
  ``cache_read``, ``cache.write`` to ``cache_write``.
- tool_use: ``name``, ``status``, ``input``, ``call_id``.
- error: ``message``, ``status_code``, ``error_name``, ``is_retryable``.

:func:`execute` returns the concatenation of all text parts as the final
answer (joined with a blank line: the stream carries one ``text`` part per
assistant message part, so narrate-then-answer runs keep both -- a single
text part is returned unchanged).  Usage sums every ``step_finish`` and
``total_tokens`` is the sum of the five token counters.  Cost prefers the
CLI-reported ``part.cost`` sum (``cost_source: "vendor"``), else the price
table (``cost_source: "price_table"``), else unknown (``cost_known``
False, ``estimated_cost_usd`` None -- never 0.0 with known True).
"""

import base64
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from . import mimo_permissions
except ImportError:  # standalone use / W2 not packaged yet
    mimo_permissions = None


DEFAULT_MIMO_BIN = "mimo"
DEFAULT_MODEL = "xiaomi/mimo-v2.6-pro"
DEFAULT_PROBE_MODEL = "xiaomi/mimo-v2.6-flash"
MIMO_VENDOR = "Xiaomi (MiMo)"

# Case-insensitive substring aliases that resolve a provider/model/vendor
# label to the MiMo vendor.
MIMO_ALIASES = ("mimo", "xiaomi")

PRESETS = ("read-only", "workspace-write", "danger-full-access")

AUTH_REMEDY = ("Run `mimo providers login` for the `xiaomi` provider (or set "
               "`MIMO_API_KEY` / the provider's API key) and retry.")
LIMIT_REMEDY = ("MiMo concurrency/rate limit reached: reduce parallel agents "
                "and retry; CrewForeman will back off automatically where it "
                "can.")
NETWORK_REMEDY = ("Network failure talking to MiMo: check connectivity and "
                  "retry.")
TRANSIENT_REMEDY = "Transient MiMo error: retry the run."
PERMANENT_REMEDY = ("Permanent MiMo error: inspect the task, model and "
                    "permissions configuration.")
UNKNOWN_REMEDY = "Unclassified MiMo error: inspect the run output and retry."

# Fallback prices (USD per 1M tokens) matching the pinned MIMO_PRICES table.
# ``api.MIMO_PRICES`` wins when present; cfg ``mimo_prices`` overrides per
# model.  Unknown models stay unpriced on purpose.
_FALLBACK_MIMO_PRICES = {
    "mimo-v2.6-pro": {"input": 0.435, "cache_hit": 0.0036, "output": 0.87},
    "mimo-v2.6-flash": {"input": 0.14, "cache_hit": 0.0036, "output": 0.28},
}

_AUTH_MARKERS = ("unauthorized", "invalid api key", "invalid_api_key",
                 "not logged in", "authentication", "login required",
                 "providers login", "api key missing", "no api key")
_LIMIT_MARKERS = ("rate limit", "ratelimit", "rate_limit", "too many requests",
                  "quota exceeded", "quota_exceeded", "concurrency limit",
                  "max concurrent")
_NETWORK_MARKERS = ("network", "econnreset", "econnrefused", "etimedout",
                    "enotfound", "eai_again", "getaddrinfo", "socket",
                    "connection", "fetch failed", "fetch error", "unreachable",
                    "dns", "certificate", "ssl", "tls", "broken pipe",
                    "no route to host", "offline", "proxy")
_TRANSIENT_MARKERS = ("timeout", "timed out", "temporar", "unavailable",
                      "try again", "retry", "overloaded", "internal error",
                      "internal server error", "bad gateway", "gateway timeout",
                      "flaky", "busy")
_PERMANENT_MARKERS = ("invalid", "not found", "unsupported", "bad request",
                      "permission denied", "denied", "malformed",
                      "does not exist", "no such", "forbidden", "rejected")

_SENTINEL = object()
_MAX_OUTPUT_FILES = 100
_OUTPUT_SLACK = 2.0
_POLL_SECONDS = 0.1

# ``mimo serve`` prints this plain stdout line once it is listening (literal
# prefix; the host part matches --hostname).  With ``--port 0`` it is the only
# way to learn the OS-assigned port.
_READY_LINE_RE = re.compile(r"mimocode server listening on\s+(https?://\S+)")

# Health probe: GET <url>/global/health must answer 2xx.  /healthz returns
# 503 on the real CLI and is never a readiness signal.
_HEALTH_PATH = "/global/health"


def _cfg_get(cfg, key, default=None):
    """Read a config key from a dict-like cfg or an attribute object."""
    if cfg is None:
        return default
    try:
        value = cfg.get(key, default)
    except AttributeError:
        value = getattr(cfg, key, default)
    return default if value is None else value


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_str(*values):
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def is_mimo_target(provider="", model="", vendor=""):
    """True when provider/model/vendor resolves to the MiMo vendor."""
    for value in (provider, model, vendor):
        low = str(value or "").lower()
        for alias in MIMO_ALIASES:
            if alias in low:
                return True
    return False


def build_argv(task, workspace, model, *, variant=None, session_id=None,
               attach_url=None, mimo_bin=DEFAULT_MIMO_BIN,
               skip_permissions=False):
    """Build the ``mimo run`` argv list.

    ``mimo run "<task>" --format json --dir <workspace> -m <model>``
    ``[--variant <v>] [-s <session>] [--attach <url>]``
    ``[--dangerously-skip-permissions]`` -- the last flag appears only when
    ``skip_permissions`` is explicitly true.
    """
    argv = [str(mimo_bin or DEFAULT_MIMO_BIN), "run", str(task),
            "--format", "json", "--dir", str(workspace), "-m", str(model)]
    if variant:
        argv.extend(["--variant", str(variant)])
    if session_id:
        argv.extend(["-s", str(session_id)])
    if attach_url:
        argv.extend(["--attach", str(attach_url)])
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    return argv


def _extract_session_id(event, part, data):
    for value in (event.get("sessionID"), part.get("sessionID"),
                  event.get("session_id"), event.get("sessionId"),
                  data.get("session_id"), data.get("sessionId")):
        if isinstance(value, str) and value:
            return value
    session = event.get("session")
    if isinstance(session, str) and session:
        return session
    return ""


def _coerce_cost(*values):
    """First usable USD amount among *values*; ``None`` when absent/invalid."""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _coerce_tokens(raw):
    """Normalise a step token dict (real cache dict or legacy int ``cache``)."""
    raw = raw if isinstance(raw, dict) else {}
    cache = raw.get("cache")
    if isinstance(cache, dict):
        cache_read = _as_int(cache.get("read"))
        cache_write = _as_int(cache.get("write"))
    else:
        cache_read = _as_int(cache)
        cache_write = 0
    return {"total": _as_int(raw.get("total")),
            "input": _as_int(raw.get("input")),
            "output": _as_int(raw.get("output")),
            "reasoning": _as_int(raw.get("reasoning")),
            "cache_read": cache_read,
            "cache_write": cache_write}


def parse_event(line):
    """Normalise one ``mimo run --format json`` JSONL event line.

    The canonical mimo 0.1.17 ``part`` shape is the source of truth (see
    the module docstring); a legacy ``data`` payload is accepted as a
    fallback.  Never raises: garbage or partially-shaped input degrades to
    a dict with ``kind`` set to ``"other"``.
    """
    event = line
    if isinstance(line, (bytes, bytearray)):
        try:
            line = line.decode("utf-8", "replace")
        except Exception:
            return {"kind": "other"}
    if not isinstance(event, dict):
        text = str(line).strip() if line is not None else ""
        if not text:
            return {"kind": "other"}
        try:
            event = json.loads(text)
        except Exception:
            return {"kind": "other"}
    if not isinstance(event, dict):
        return {"kind": "other"}
    typ = str(event.get("type") or "").lower().replace("-", "_")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    session_id = _extract_session_id(event, part, data)
    if typ == "text":
        time_raw = part.get("time")
        time_norm = None
        if isinstance(time_raw, dict):
            time_norm = {"start": _as_int(time_raw.get("start")),
                         "end": _as_int(time_raw.get("end"))}
        parsed = {"kind": "text",
                  "text": _first_str(part.get("text"), data.get("text"),
                                    event.get("text"), data.get("content"),
                                    event.get("content"), data.get("answer"),
                                    event.get("answer")),
                  "time": time_norm}
    elif typ == "step_start":
        parsed = {"kind": "step_start",
                  "name": _first_str(data.get("name"), event.get("name"))
                  or "step",
                  "snapshot": _first_str(part.get("snapshot"),
                                        data.get("snapshot"),
                                        event.get("snapshot")) or None}
    elif typ == "step_finish":
        raw = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        if not raw and isinstance(data.get("tokens"), dict):
            raw = data["tokens"]
        if not raw and isinstance(event.get("tokens"), dict):
            raw = event["tokens"]
        parsed = {"kind": "step_finish",
                  "name": _first_str(data.get("name"), event.get("name")),
                  "reason": _first_str(part.get("reason"), data.get("reason"),
                                      event.get("reason")) or None,
                  "tokens": _coerce_tokens(raw),
                  "cost": _coerce_cost(part.get("cost"), data.get("cost"),
                                      event.get("cost"))}
    elif typ == "tool_use":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_input = None
        for candidate in (state.get("input"), part.get("input"),
                         data.get("input")):
            if isinstance(candidate, dict):
                tool_input = candidate
                break
        parsed = {"kind": "tool_use",
                  "name": _first_str(part.get("tool"), data.get("name"),
                                     event.get("name"), data.get("tool"),
                                     part.get("name")) or "tool",
                  "status": _first_str(state.get("status"), data.get("status"),
                                       event.get("status")) or None,
                  "input": tool_input if tool_input is not None else {},
                  "call_id": _first_str(part.get("callID"),
                                        part.get("call_id"),
                                        data.get("callID"),
                                        data.get("call_id"),
                                        event.get("callID")) or None}
    elif typ == "error":
        err = event.get("error") if isinstance(event.get("error"), dict) else {}
        err_data = err.get("data") if isinstance(err.get("data"), dict) else {}
        message = _first_str(err_data.get("message"), err.get("message"),
                             data.get("message"), event.get("message"),
                             err.get("error"))
        status_code = None
        for value in (err_data.get("statusCode"), err_data.get("status_code"),
                      err.get("statusCode"), err.get("status_code"),
                      data.get("statusCode"), event.get("statusCode")):
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                status_code = value
                break
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                status_code = int(value.strip())
                break
        is_retryable = None
        for value in (err_data.get("isRetryable"), err_data.get("is_retryable"),
                      err.get("isRetryable"), err.get("is_retryable")):
            if isinstance(value, bool):
                is_retryable = value
                break
        parsed = {"kind": "error",
                  "message": message or "unknown mimo error",
                  "status_code": status_code,
                  "error_name": _first_str(err.get("name"),
                                           err_data.get("name"),
                                           event.get("error_name")) or None,
                  "is_retryable": is_retryable}
    elif typ in ("session", "session/start", "session_start"):
        parsed = {"kind": "session",
                  "id": session_id or _first_str(data.get("id"),
                                                 event.get("id")) or None}
    else:
        parsed = {"kind": "other", "type": typ or None}
    if session_id:
        parsed.setdefault("session_id", session_id)
    return parsed


def classify_error(message, status_code=None):
    """Classify a MiMo error into auth/limit/network/transient/permanent.

    Returns ``{"class", "remedy", "status_code"}`` with a vendor-correct
    remedy string; never raises.
    """
    text = str(message or "")
    low = text.lower()
    code = status_code if isinstance(status_code, int) and not isinstance(
        status_code, bool) else None
    if code in (401, 403) or any(marker in low for marker in _AUTH_MARKERS):
        return {"class": "auth", "remedy": AUTH_REMEDY, "status_code": code}
    if code == 429 or any(marker in low for marker in _LIMIT_MARKERS):
        return {"class": "limit", "remedy": LIMIT_REMEDY, "status_code": code}
    if any(marker in low for marker in _NETWORK_MARKERS):
        return {"class": "network", "remedy": NETWORK_REMEDY,
                "status_code": code}
    if code is not None and 500 <= code < 600:
        return {"class": "transient", "remedy": TRANSIENT_REMEDY,
                "status_code": code}
    if code is not None and 400 <= code < 500:
        return {"class": "permanent", "remedy": PERMANENT_REMEDY,
                "status_code": code}
    if any(marker in low for marker in _TRANSIENT_MARKERS):
        return {"class": "transient", "remedy": TRANSIENT_REMEDY,
                "status_code": code}
    if any(marker in low for marker in _PERMANENT_MARKERS):
        return {"class": "permanent", "remedy": PERMANENT_REMEDY,
                "status_code": code}
    return {"class": "unknown", "remedy": UNKNOWN_REMEDY, "status_code": code}


def _noise_output(rel):
    low = str(rel).replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    if base in ("mimocode.jsonc", ".ds_store"):
        return True
    if base.startswith((".fake", ".dsh-tmp-")):
        return True
    return base.endswith((".pyc", ".pyo", ".tmp"))


def _scan_outputs(workspace, started, slack=_OUTPUT_SLACK):
    root = Path(workspace)
    try:
        return sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.stat().st_mtime >= started - slack
            and not _noise_output(path.relative_to(root).as_posix())
        )[:_MAX_OUTPUT_FILES]
    except Exception:
        return []


def _tmp_dir():
    for name in ("TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _stderr_path():
    handle = tempfile.NamedTemporaryFile(
        prefix="mimo-stderr-", suffix=".log", dir=_tmp_dir(), delete=False)
    path = handle.name
    handle.close()
    return path


def _read_tail(path, limit=3000):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-limit:]


MIMO_SERVER_DEFAULT_USERNAME = "mimocode"


def _server_username():
    """Basic-auth user the real ``mimo serve`` expects (verified on 0.1.17).

    The CLI reads ``MIMOCODE_SERVER_USERNAME`` and defaults to ``mimocode``;
    an empty username is answered 401.
    """
    return os.environ.get("MIMOCODE_SERVER_USERNAME") or MIMO_SERVER_DEFAULT_USERNAME


def _basic_auth_header(password):
    """``Authorization: Basic base64(user + ":" + password)``, user ``mimocode``."""
    raw = (_server_username() + ":" + str(password)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _scrub_secret(text, secret):
    """Strip *secret* from *text* so it can never leak into results."""
    if not secret or text is None:
        return text
    try:
        return str(text).replace(str(secret), "***")
    except Exception:
        return text


def _health_request(url, password=None, timeout=1.0):
    """Probe ``GET <url>/global/health`` once.

    True only for a 2xx response.  Any other HTTP answer (401/403 included)
    is not ready -- the probe fails closed.  ``None`` means no HTTP response
    at all (connection-level failure), which lets :meth:`MimoServer.ready`
    try its documented TCP last resort.  When *password* is set the request
    carries ``Authorization: Basic`` with the ``mimocode`` username (harmless when
    the server ignores it, required when it demands it).
    """
    request = urllib.request.Request(str(url).rstrip("/") + _HEALTH_PATH)
    if password:
        request.add_header("Authorization", _basic_auth_header(password))
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError:
        return False
    except Exception:
        return None
    try:
        response.read()
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
    finally:
        try:
            response.close()
        except Exception:
            pass
    try:
        return 200 <= int(status) < 300
    except (TypeError, ValueError):
        return False


def _kill_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _popen_kwargs():
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _fallback_write_run_config(preset, workspace, path=None):
    root = Path(workspace)
    target = Path(path) if path else root / "mimocode.jsonc"
    allow = {"read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
             "task": "allow", "actor": "allow", "skill": "allow",
             "webfetch": "allow"}
    if preset == "read-only":
        permission = dict(allow, edit="deny", bash="deny")
    elif preset == "danger-full-access":
        permission = dict(allow, edit="allow", bash="allow")
    else:
        permission = dict(allow, edit="allow", bash="allow")
    permission["external_directory"] = {"*": "deny", str(root) + "/**": "allow"}
    try:
        target.write_text(json.dumps({"permission": permission}, indent=2),
                          encoding="utf-8")
    except Exception:
        pass
    return target


def _write_run_config(preset, workspace):
    if mimo_permissions is not None:
        return mimo_permissions.write_run_config(preset, workspace)
    return _fallback_write_run_config(preset, workspace)


def _skip_permissions_args(cfg):
    if mimo_permissions is not None:
        return list(mimo_permissions.skip_permissions_args(cfg) or [])
    if _cfg_get(cfg, "mimo_skip_permissions", False):
        return ["--dangerously-skip-permissions"]
    return []


def _preset_for_role(role, cfg=None):
    if mimo_permissions is not None:
        try:
            return mimo_permissions.preset_for_role(role, cfg)
        except Exception:
            pass
    explicit = _cfg_get(cfg, "mimo_sandbox", None)
    if isinstance(explicit, str) and explicit in PRESETS:
        return explicit
    low = str(role or "").lower()
    if low in ("reviewer", "audit", "probe", "preflight"):
        return "read-only"
    return "workspace-write"


def _resolve_preset(cfg, sandbox, label):
    for candidate in (sandbox, _cfg_get(cfg, "mimo_sandbox", None)):
        if isinstance(candidate, str) and candidate in PRESETS:
            return candidate
    return _preset_for_role(label, cfg)


def _price_table(cfg):
    prices = {}
    for key, value in _FALLBACK_MIMO_PRICES.items():
        prices[key] = dict(value)
    try:
        from .api import MIMO_PRICES  # noqa: WPS433 (lazy: avoid import cycles)
        if isinstance(MIMO_PRICES, dict):
            for key, value in MIMO_PRICES.items():
                if isinstance(value, dict):
                    prices[str(key)] = value
    except Exception:
        pass
    override = _cfg_get(cfg, "mimo_prices", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if isinstance(value, dict):
                prices[str(key)] = value
    return prices


def _model_prices(prices, model):
    name = str(model or "")
    for key in (name, name.split("/")[-1], name.split(":")[-1]):
        if not key:
            continue
        entry = prices.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def _estimate_cost(entry, tokens_in, cache_read, tokens_out):
    """Return ``(cost, known)``; unknown prices must never yield 0.0."""
    if not isinstance(entry, dict):
        return None, False
    try:
        input_price = float(entry.get("input"))
        output_price = float(entry.get("output"))
        cache_raw = entry.get("cache_hit", input_price)
        cache_price = float(input_price if cache_raw is None else cache_raw)
    except (TypeError, ValueError):
        return None, False
    cost = (tokens_in * input_price + cache_read * cache_price
            + tokens_out * output_price) / 1000000.0
    return round(cost, 6), True


def execute(cfg, workspace, task, *, model=None, variant=None, session_id=None,
            label="builder", progress_cb=None, sandbox="workspace-write",
            attach_url=None, cancel_check=None, timeout=None,
            mimo_bin=None, live_usage_cb=None, server_password=None):
    """Run one ``mimo run`` job and return a harness-compatible result dict.

    Streams JSONL events off stdout, accumulates step usage, scans the
    workspace for changed files, classifies vendor errors and honours
    ``timeout`` / ``cancel_check``.  Temp files honour TMPDIR.

    ``server_password`` reaches the child only through the
    ``MIMOCODE_SERVER_PASSWORD`` env var (never argv) so attach runs can
    authenticate to ``mimo serve``.  The password is scrubbed out of every
    string this function returns and is never recorded anywhere.
    """
    started = time.time()
    workspace = Path(workspace)
    secret = str(server_password) if server_password else ""
    child_env = None
    if secret:
        child_env = os.environ.copy()
        child_env["MIMOCODE_SERVER_PASSWORD"] = secret
    model = (model or _cfg_get(cfg, "mimo_default_model") or DEFAULT_MODEL)
    variant = variant or _cfg_get(cfg, "mimo_variant") or None
    if not variant:
        variant = None
    binary = mimo_bin or _cfg_get(cfg, "mimo_bin") or DEFAULT_MIMO_BIN
    preset = _resolve_preset(cfg, sandbox, label)
    try:
        _write_run_config(preset, workspace)
    except Exception:
        pass
    skip_args = _skip_permissions_args(cfg)
    argv = build_argv(task, workspace, model, variant=variant,
                      session_id=session_id, attach_url=attach_url,
                      mimo_bin=binary,
                      skip_permissions=bool(skip_args))

    tools = {}
    events_count = 0
    errors = []          # (message, status_code)
    text_parts = []
    session_from_events = None
    tokens = {"input": 0, "cache_read": 0, "cache_write": 0,
              "output": 0, "reasoning": 0}
    vendor_cost = 0.0
    vendor_cost_steps = 0
    subagent_spawns = 0
    saw_usage = False
    timed_out = False
    cancelled = False
    stop_reason = None
    live_stop = None
    exit_code = None
    stderr_file_path = _stderr_path()
    stderr_tail = ""

    proc = None
    try:
        try:
            stderr_handle = open(stderr_file_path, "w", encoding="utf-8")
        except Exception:
            stderr_handle = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=stderr_handle,
                env=child_env, text=True, bufsize=1, **_popen_kwargs())
        except OSError as exc:
            try:
                stderr_handle.close()
            except Exception:
                pass
            return _result(
                status="error", answer=None, exit_code=None,
                error=_scrub_secret("failed to start mimo: %s" % exc, secret),
                tools=tools,
                events_count=events_count, outputs=[], started=started,
                stderr_tail="", tokens=tokens, saw_usage=False,
                vendor_cost=0.0, vendor_cost_steps=0, subagent_spawns=0,
                model=model, cfg=cfg, preset=preset, stalled=False,
                timed_out=False, stop_reason="error",
                transport_session_id=session_id)
        try:
            stderr_handle.close()
        except Exception:
            pass

        queue_ = queue.Queue()

        def _pump(stream):
            try:
                for raw in iter(stream.readline, ""):
                    queue_.put(raw)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
                queue_.put(_SENTINEL)

        thread = threading.Thread(target=_pump, args=(proc.stdout,), daemon=True)
        thread.start()
        deadline = (started + float(timeout)) if timeout else None

        while True:
            line = None
            try:
                line = queue_.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                line = None
            if line is _SENTINEL:
                break
            if line is not None and str(line).strip():
                raw = None
                try:
                    raw = json.loads(line)
                except Exception:
                    raw = None
                if raw is not None:
                    events_count += 1
                parsed = parse_event(raw if isinstance(raw, dict) else line)
                if live_usage_cb is not None:
                    try:
                        live_stop = live_usage_cb(
                            raw if isinstance(raw, dict) else line, parsed)
                    except Exception:
                        live_stop = None
                kind = parsed.get("kind")
                if kind == "tool_use":
                    name = parsed.get("name") or "tool"
                    tools[name] = tools.get(name, 0) + 1
                    if _is_subagent_spawn(name, parsed.get("input")):
                        subagent_spawns += 1
                    if progress_cb:
                        progress_cb("%s: %d tool call(s), last: %s, %d s" % (
                            label, sum(tools.values()), name,
                            round(time.time() - started)))
                elif kind == "text":
                    text_parts.append(parsed.get("text") or "")
                elif kind == "step_start":
                    if progress_cb:
                        progress_cb("%s: step %s" % (
                            label, parsed.get("name") or "step"))
                elif kind == "step_finish":
                    step_tokens = parsed.get("tokens") or {}
                    for key in ("input", "cache_read", "cache_write",
                                "output", "reasoning"):
                        tokens[key] += _as_int(step_tokens.get(key))
                    step_cost = parsed.get("cost")
                    if step_cost is not None:
                        vendor_cost += step_cost
                        vendor_cost_steps += 1
                    saw_usage = True
                    if progress_cb:
                        progress_cb("%s: step finished (%d tokens)" % (
                            label, _as_int(step_tokens.get("total"))))
                elif kind == "error":
                    errors.append((parsed.get("message") or "unknown mimo error",
                                   parsed.get("status_code")))
                elif kind == "session":
                    if parsed.get("id") and not session_from_events:
                        session_from_events = parsed.get("id")
                if parsed.get("session_id") and not session_from_events:
                    session_from_events = parsed.get("session_id")

            if live_stop:
                stop_reason = "cancelled"
                cancelled = True
                _kill_process(proc)
                break
            if cancel_check is not None:
                try:
                    wants_stop = bool(cancel_check())
                except Exception:
                    wants_stop = False
                if wants_stop:
                    stop_reason = "cancelled"
                    cancelled = True
                    _kill_process(proc)
                    break
            if deadline is not None and time.time() >= deadline:
                timed_out = True
                stop_reason = "timeout"
                _kill_process(proc)
                break

        try:
            proc.wait(timeout=10)
        except Exception:
            _kill_process(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        exit_code = proc.returncode
    finally:
        stderr_tail = _read_tail(stderr_file_path)
        try:
            os.unlink(stderr_file_path)
        except Exception:
            pass

    outputs = _scan_outputs(workspace, started)
    final_text = "\n\n".join(text_parts) if text_parts else None
    if errors:
        message, status_code = errors[0]
        combined = "; ".join(item[0] for item in errors)
        classification = classify_error(message, status_code)
        error_text = "%s. %s" % (combined, classification["remedy"])
        if stop_reason is None:
            stop_reason = classification["class"]
            if stop_reason not in ("auth", "limit", "network"):
                stop_reason = "error"
        status = "stopped" if cancelled else "error"
    elif cancelled:
        error_text = "cancelled by client"
        status = "stopped"
    elif timed_out:
        error_text = "timed out after %s s" % timeout
        status = "error"
    elif exit_code == 0 and final_text is not None:
        error_text = None
        status = "ok"
    else:
        status = "error"
        error_text = (stderr_tail[-1500:] if exit_code else None) or (
            "mimo exited with code %s" % exit_code)
    if stop_reason is None:
        stop_reason = "ok" if status == "ok" else "error"
    stalled = bool(timed_out and events_count == 0)
    final_text = _scrub_secret(final_text, secret)
    stderr_tail = _scrub_secret(stderr_tail, secret)
    error_text = _scrub_secret(error_text, secret)
    return _result(
        status=status, answer=final_text, exit_code=exit_code,
        error=error_text, tools=tools, events_count=events_count,
        outputs=outputs, started=started, stderr_tail=stderr_tail,
        tokens=tokens, saw_usage=saw_usage,
        vendor_cost=vendor_cost, vendor_cost_steps=vendor_cost_steps,
        subagent_spawns=subagent_spawns, model=model, cfg=cfg,
        preset=preset, stalled=stalled, timed_out=timed_out,
        stop_reason=stop_reason,
        transport_session_id=session_from_events or session_id)


def _is_subagent_spawn(name, tool_input):
    """True for an ``actor`` tool call with ``operation.action == "spawn"``."""
    if str(name or "").lower() != "actor":
        return False
    operation = tool_input.get("operation") if isinstance(
        tool_input, dict) else None
    return (isinstance(operation, dict)
            and str(operation.get("action") or "").lower() == "spawn")


def _result(status, answer, exit_code, error, tools, events_count, outputs,
            started, stderr_tail, tokens, saw_usage, vendor_cost,
            vendor_cost_steps, subagent_spawns, model, cfg, preset,
            stalled, timed_out, stop_reason, transport_session_id):
    """Assemble the harness-compatible execution dict.

    Cost resolution order: the CLI-reported ``part.cost`` sum
    (``cost_source: "vendor"``), then price-table pricing
    (``cost_source: "price_table"``), else unknown -- ``cost_known``
    False and ``estimated_cost_usd`` None, never 0.0 with known True.
    """
    if not saw_usage:
        cost, cost_known = None, False
        cost_source = "unknown"
        tokens_out = {"input": None, "cache_read": None, "cache_write": None,
                      "output": None, "reasoning": None, "total": None}
        usage = None
    else:
        if vendor_cost_steps > 0:
            cost = round(float(vendor_cost), 6)
            cost_known = True
            cost_source = "vendor"
        else:
            cost, cost_known = _estimate_cost(
                _model_prices(_price_table(cfg), model),
                tokens["input"], tokens["cache_read"], tokens["output"])
            cost_source = "price_table" if cost_known else "unknown"
        tokens_out = dict(tokens)
        tokens_out["total"] = (tokens["input"] + tokens["cache_read"]
                               + tokens["cache_write"] + tokens["output"]
                               + tokens["reasoning"])
        usage = {
            "input_tokens": tokens_out["input"],
            "cache_read_tokens": tokens_out["cache_read"],
            "cache_write_tokens": tokens_out["cache_write"],
            "output_tokens": tokens_out["output"],
            "reasoning_tokens": tokens_out["reasoning"],
            "total_tokens": tokens_out["total"],
            "models": [str(model)],
            "estimated_cost_usd": cost,
            "cost_known": bool(cost_known),
        }
    return {
        "status": status,
        "answer": answer,
        "exit_code": exit_code,
        "error": error,
        "tools_used": dict(tools),
        "subagent_spawns": int(subagent_spawns),
        "events": events_count,
        "output_files": list(outputs),
        "seconds": round(time.time() - started, 1),
        "stderr_tail": stderr_tail,
        "session_logs": [],
        "tokens_in": tokens_out["input"],
        "cache_read_tokens": tokens_out["cache_read"],
        "cache_write_tokens": tokens_out["cache_write"],
        "tokens_out": tokens_out["output"],
        "reasoning_tokens": tokens_out["reasoning"],
        "total_tokens": tokens_out["total"],
        "estimated_cost_usd": cost,
        "cost_source": cost_source,
        "cost_known": bool(cost_known),
        "models": [str(model)],
        "usage": usage,
        "stalled": bool(stalled),
        "timed_out": bool(timed_out),
        "stop_reason": stop_reason,
        "transport": "mimo",
        "transport_session_id": transport_session_id,
        "sandbox": preset,
        "os_sandbox": {"wrapped": False},
    }


class MimoServer:
    """Lifecycle for ``mimo serve`` plus ``mimo run --attach <url>``.

    The server process gets its own session/process group so cleanup can
    never kill the parent.  ``port=0`` means OS-assigned: the bound port is
    learned from the ``mimocode server listening on http://host:port`` line
    the CLI prints on stdout (the only way to learn it), while an explicit
    port may construct the URL directly -- the parsed line still wins when
    present.  A random per-server password is generated at :meth:`start`,
    handed to the child only through the ``MIMOCODE_SERVER_PASSWORD`` env
    var (never argv) and exposed as :attr:`password` so attach runs can
    authenticate.  The password is never logged or included in error text.
    """

    def __init__(self, cfg, *, mimo_bin=None, host="127.0.0.1", port=0,
                 ready_timeout=20.0):
        self.cfg = cfg
        self.mimo_bin = (mimo_bin or _cfg_get(cfg, "mimo_bin")
                         or DEFAULT_MIMO_BIN)
        self.host = str(host or "127.0.0.1")
        self.port = int(port or 0)
        self.ready_timeout = float(ready_timeout)
        self.password = None
        self._proc = None
        self._url = None
        self._ready_url = None
        self._stderr_path = None

    def start(self):
        """Spawn ``mimo serve`` and return the attach url; raises on failure."""
        if self._proc is not None:
            raise RuntimeError("MimoServer already started")
        self.password = secrets.token_urlsafe(24)
        argv = [str(self.mimo_bin), "serve", "--hostname", self.host,
                "--port", str(self.port)]
        env = os.environ.copy()
        env["MIMOCODE_SERVER_PASSWORD"] = self.password
        self._stderr_path = _stderr_path()
        stderr_handle = open(self._stderr_path, "w", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=stderr_handle, env=env,
                text=True, bufsize=1, **_popen_kwargs())
        finally:
            stderr_handle.close()
        self._ready_url = None
        thread = threading.Thread(
            target=self._pump_stdout, args=(self._proc.stdout,), daemon=True)
        thread.start()
        fallback = ("http://%s:%s" % (self.host, self.port)
                    if self.port else None)
        self._url = None
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                break
            if self._ready_url:
                self._url = self._ready_url
            elif fallback:
                self._url = fallback
            if self._url and self.ready():
                return self._url
            time.sleep(0.05)
        tail = _scrub_secret(_read_tail(self._stderr_path, 500), self.password)
        target = self._url or fallback or "http://%s:%s" % (self.host,
                                                            self.port)
        self.stop()
        self.cleanup()
        raise RuntimeError("mimo serve did not become ready on %s%s" % (
            target, (": " + tail.strip()) if tail.strip() else ""))

    def _pump_stdout(self, stream):
        """Drain serve stdout and capture the ``listening on`` ready line."""
        try:
            for raw in iter(stream.readline, ""):
                match = _READY_LINE_RE.search(raw)
                if match:
                    self._ready_url = match.group(1)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def ready(self):
        """True when ``GET <url>/global/health`` returns 2xx.

        ``/healthz`` returns 503 on the real CLI and is never a readiness
        signal.  With a password set every probe sends ``Authorization:
        Basic`` for user ``mimocode``; 401/403 -- like any non-2xx -- reports
        not-ready and :meth:`start` retries within ``ready_timeout``.
        Last resort only: when the HTTP request is entirely unreachable
        (no response at all), a bare TCP connect is tried -- that proves a
        listener exists, not that the server is healthy.
        """
        if self._proc is None or self._proc.poll() is not None:
            return False
        url = self._url
        if not url and self.port:
            url = "http://%s:%s" % (self.host, self.port)
        if not url:
            return False
        status = _health_request(url, self.password, timeout=1.0)
        if status is not None:
            return status
        # HTTP entirely unreachable: TCP connect as the documented last
        # resort (degraded signal -- listener present, health unknown).
        try:
            sock = socket.create_connection((self.host, self._port()), 1.0)
            sock.close()
            return True
        except Exception:
            return False

    def _port(self):
        if self.port:
            return self.port
        if self._url:
            try:
                return int(self._url.rsplit(":", 1)[-1])
            except ValueError:
                return 0
        return 0

    def stop(self, timeout=5.0):
        """SIGTERM the process group, SIGKILL after ``timeout`` seconds."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            deadline = time.time() + float(timeout)
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                _kill_process(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def cleanup(self):
        """Idempotent teardown for finally/crash paths."""
        try:
            self.stop()
        except Exception:
            pass
        self._proc = None
        if self._stderr_path:
            try:
                os.unlink(self._stderr_path)
            except Exception:
                pass
            self._stderr_path = None


def ping(cfg, *, model=None, mimo_bin=None, timeout=45.0):
    """Probe a MiMo target; ``ok`` when the answer contains READY."""
    probe_model = (model or _cfg_get(cfg, "mimo_probe_model")
                   or DEFAULT_PROBE_MODEL)
    tmp_root = tempfile.mkdtemp(prefix="mimo-ping-")
    try:
        result = execute(
            cfg, tmp_root, "Reply with exactly READY and nothing else.",
            model=probe_model, label="probe", sandbox="read-only",
            mimo_bin=mimo_bin, timeout=timeout)
    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass
    answer = result.get("answer")
    answer_text = "" if answer is None else str(answer)
    return {"ok": "READY" in answer_text,
            "answer": answer_text,
            "error": result.get("error")}
