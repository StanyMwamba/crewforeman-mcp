"""Generic desktop/CLI MCP client helpers (v3.6.0 wave 1, task 3).

Copy-ready candidate for ``deepseek_mcp/clients.py``.  Standard library only,
Python 3.9 compatible, no network access and no personal paths.

Public API::

    restart_client_app(args, cfg=None, system=None, runner=None) -> str
    client_config_snippets() -> dict

``restart_client_app`` validates its input, then quits and reopens a named MCP
client application using the platform's normal tools:

* macOS   - ``osascript`` quit, wait, ``open -a``
* Windows - ``taskkill`` (ignored if the app is absent), wait, ``start``
* Linux   - exact ``pkill -x`` on the app's process name, wait, command lookup, else ``xdg-open``

It intentionally never raises for an unsupported platform or an application
that cannot be launched: callers get a plain ``"unsupported: ..."`` sentence
that is safe to show in a tool result.  Invalid *arguments* (bad app name or
wait time) raise ``ValueError``; this is programmer input, not environment
failure.

``runner`` is injectable for tests: a callable ``runner(argv, timeout=15)``
returning a ``subprocess.CompletedProcess``-like object with ``returncode``
and ``stderr``.  ``which`` is injectable as well for Linux command lookup.
"""

import os
import re
import shutil
import subprocess
import sys
import time

from . import restart_helper

__all__ = ["restart_client_app", "client_config_snippets",
           "DEFAULT_APP_NAME", "MAX_APP_NAME", "MAX_WAIT_SECONDS"]

DEFAULT_APP_NAME = "Claude"
MAX_APP_NAME = 60
DEFAULT_WAIT_SECONDS = 5.0
MAX_WAIT_SECONDS = 60.0
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]+$")
_TRUNCATE = 240


# ------------------------------------------------------------------ helpers

def _cfg_value(cfg, key):
    """Read one optional setting from a dict-like or attribute-like cfg."""
    if cfg is None:
        return None
    if isinstance(cfg, dict):
        return cfg.get(key)
    return getattr(cfg, key, None)


def _app_name(args, cfg):
    if isinstance(args, dict) and "app_name" in args:
        value = args["app_name"]
    else:
        value = _cfg_value(cfg, "client_app_name")
        if value is None or value == "":
            value = _cfg_value(cfg, "app_name")
        if value is None or value == "":
            value = DEFAULT_APP_NAME
    if not isinstance(value, str):
        raise ValueError("app_name must be a string")
    name = value.strip()
    if (not name or len(name) > MAX_APP_NAME or not _APP_NAME_RE.fullmatch(name)
            or not any(char.isalnum() for char in name)):
        raise ValueError(
            "app_name must be 1-%d characters using only letters, digits, "
            "spaces, dot, dash and underscore" % MAX_APP_NAME)
    return name


def _wait_seconds(args, cfg):
    value = args.get("wait_seconds") if isinstance(args, dict) else None
    if value is None:
        value = _cfg_value(cfg, "client_wait_seconds")
    if value is None:
        value = DEFAULT_WAIT_SECONDS
    if isinstance(value, bool):
        raise ValueError("wait_seconds must be a number between 0 and 60")
    try:
        wait = float(value)
    except (TypeError, ValueError):
        raise ValueError("wait_seconds must be a number between 0 and 60")
    if wait != wait or wait < 0 or wait > MAX_WAIT_SECONDS:  # NaN or out of range
        raise ValueError("wait_seconds must be a number between 0 and 60")
    return wait


def _default_runner(argv, timeout=15):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False)


def _run(runner, argv):
    """Run one command; return (returncode, short_stderr). Never raises."""
    runner = runner or _default_runner
    try:
        proc = runner(argv, timeout=15)
    except TypeError:
        # Small test doubles often accept just argv.  Only fall back once.
        try:
            proc = runner(argv)
        except Exception as exc:
            return 1, _short(exc)
    except Exception as exc:
        return 1, _short(exc)
    code = getattr(proc, "returncode", 0)
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 1
    detail = getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or ""
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    return code, _short(detail)


def _short(value):
    text = " ".join(str(value or "").split())
    return text[:_TRUNCATE]


def _system(system):
    return str(system if system is not None else sys.platform)


def _family(system):
    system = _system(system).lower()
    if system.startswith("darwin") or system == "macos":
        return "macos"
    if system.startswith("win"):
        return "windows"
    if system.startswith("linux"):
        return "linux"
    return ""


def _unsupported(system, name, detail=""):
    suffix = " (%s)" % detail if detail else ""
    return ("unsupported: automatic restart is not available on platform %r%s; "
            "quit and reopen %s manually." % (_system(system), suffix, name))


def _launch_failed(name, platform, detail=""):
    suffix = ": %s" % detail if detail else "."
    return ("unsupported: %s could not be opened automatically on %s%s "
            "Quit and reopen it manually." % (name, platform, suffix))


def _linux_candidates(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    candidates = []
    for item in (slug, slug + "-desktop", slug + "-app"):
        if item and item not in candidates:
            candidates.append(item)
    return candidates


# --------------------------------------------------------------------- API


def _restart_status_path(args, cfg):
    value = args.get("restart_status_path") if isinstance(args, dict) else None
    if value in (None, ""):
        value = _cfg_value(cfg, "restart_status_path")
    if value in (None, ""):
        state_dir = _cfg_value(cfg, "state_dir")
        if not state_dir:
            state_dir = os.path.join(os.path.expanduser("~"), ".deepseek-mcp",
                                     "state")
        value = os.path.join(str(state_dir), "restart-status.json")
    return os.path.expanduser(str(value))


def _restart_number(args, cfg, key, default, low, high, integer=False):
    value = args.get(key) if isinstance(args, dict) else None
    if value is None:
        value = _cfg_value(cfg, key)
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError("%s must be a number between %s and %s" % (key, low, high))
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number between %s and %s" % (key, low, high))
    if number != number or number < low or number > high:
        raise ValueError("%s must be a number between %s and %s" % (key, low, high))
    return number


def restart_client_app(args, cfg=None, system=None, runner=None, which=None):
    """Quit and reopen a named MCP client app and return a clear message.

    The close is best-effort and runs in the detached helper together with the
    reopen and verification (see :mod:`deepseek_mcp.restart_helper`), so it
    survives the client exiting.  Progress is written to
    ``restart_status_path`` (default ``<state_dir>/restart-status.json``).

    ``args`` accepts ``app_name`` (default ``"Claude"``), ``wait_seconds``
    (default 5, clamped to 0..60), ``restart_status_path``,
    ``restart_helper_attempts`` and ``restart_helper_retry_seconds``.  For
    tests, ``close_command``/``open_command``/``verify_command`` override the
    platform commands.  ``cfg`` may provide the matching defaults.  ``system``
    defaults to ``sys.platform``; ``runner`` and ``which`` are injectable.

    Raises ``ValueError`` for invalid arguments.  For environment, platform or
    status-file failures it returns a plain explanatory sentence instead of
    raising.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("args must be a dict")
    name = _app_name(args, cfg)
    wait = _wait_seconds(args, cfg)
    family = _family(system)
    if not family:
        return _unsupported(system, name)

    status_path = _restart_status_path(args, cfg)
    attempts = _restart_number(args, cfg, "restart_helper_attempts", 3, 1, 10,
                               integer=True)
    retry_seconds = _restart_number(args, cfg, "restart_helper_retry_seconds",
                                    2.0, 0.0, 60.0)
    overrides = {}
    for key in ("close", "open", "verify"):
        value = args.get(key + "_command")
        if value is not None:
            overrides[key] = value
    try:
        commands = restart_helper.resolve_commands(family, name, overrides,
                                                   which=which)
    except Exception as exc:
        return _unsupported(system, name, _short(exc))

    # Spawn the detached helper BEFORE closing the app.  Closing can quit the
    # client and take this process with it; the helper is already in its own
    # session, so it still waits, reopens and verifies.  The helper performs
    # the best-effort close itself (the app may already be gone).
    now = restart_helper.utc_now()
    record = {
        "state": "scheduled",
        "app_name": name,
        "platform": family,
        "helper_pid": None,
        "status_path": status_path,
        "attempts": 0,
        "max_attempts": attempts,
        "retry_seconds": retry_seconds,
        "started_at": now,
        "updated_at": now,
        "error": "",
        "message": ("Restart scheduled; the detached helper will reopen and "
                    "verify %s." % name),
    }
    try:
        restart_helper.write_status(status_path, record)
    except Exception as exc:
        return ("restart could not be scheduled for %s: the status file %s is "
                "not writable (%s)." % (name, status_path, _short(exc)))

    helper_args = {
        "app_name": name,
        "system": family,
        "attempts": attempts,
        "retry_seconds": retry_seconds,
        "initial_delay": wait,
        "status_path": status_path,
        "timeout": 15.0,
        "close": commands["close"],
        "open": commands["open"],
        "verify": commands["verify"],
    }
    if commands.get("expected_executable"):
        helper_args["expected_exe"] = commands["expected_executable"]
    try:
        pid = restart_helper.spawn_restart_helper(helper_args)
    except Exception as exc:
        record["state"] = "failed"
        record["error"] = _short(exc)
        record["updated_at"] = restart_helper.utc_now()
        record["message"] = "Could not start the detached restart helper."
        try:
            restart_helper.write_status(status_path, record)
        except Exception:
            pass
        return ("restart failed for %s: could not start the detached helper "
                "(%s). Status file: %s" % (name, _short(exc), status_path))

    record["helper_pid"] = pid
    record["updated_at"] = restart_helper.utc_now()
    try:
        current = restart_helper.read_status(status_path)
        state = str(current.get("state") or "")
        if state in ("", "scheduled"):
            merged = dict(current or {})
            merged.update(record)
            restart_helper.write_status(status_path, merged)
        elif state == "waiting":
            current["helper_pid"] = pid
            current["updated_at"] = record["updated_at"]
            restart_helper.write_status(status_path, current)
    except Exception:
        pass
    return ("%s restart scheduled via a detached helper (pid %s). It will "
            "reopen and verify the app, retrying up to %d time(s). Status "
            "file: %s. Reconnect the MCP client if the app does not reappear."
            % (name, pid, attempts, status_path))


# ---------------------------------------------------------------- snippets

def client_config_snippets():
    """Return ready-to-use client configuration snippets keyed by client.

    Values are plain strings.  JSON entries are valid JSON; the Codex entry is
    valid TOML.  ``python3`` is a portable placeholder: replace it with the
    interpreter or absolute command that has this package installed.  Keys are
    never included; ``YOUR_DEEPSEEK_API_KEY`` is a placeholder for the user's
    own environment value.
    """
    stdio = {
        "command": "python3",
        "args": ["-m", "deepseek_mcp"],
        "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"},
    }
    desktop = {"mcpServers": {"deepseek": dict(stdio)}}
    vscode = {"servers": {"deepseek": dict(stdio, type="stdio")}}
    claude_code = ("claude mcp add deepseek "
                   "--env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY -- "
                   "python3 -m deepseek_mcp")
    gemini_cli = ("gemini mcp add --env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY "
                  "deepseek python3 -m deepseek_mcp")
    codex_toml = (
        '[mcp_servers.deepseek]\n'
        'command = "python3"\n'
        'args = ["-m", "deepseek_mcp"]\n'
        'env = { DEEPSEEK_API_KEY = "YOUR_DEEPSEEK_API_KEY" }\n'
    )
    return {
        "claude_desktop": _json(desktop),
        "claude_code": claude_code,
        "cursor": _json(desktop),
        "vscode": _json(vscode),
        "windsurf": _json(desktop),
        "cline": _json(desktop),
        "codex_cli": codex_toml,
        "gemini_cli": gemini_cli,
        "generic_stdio": _json(desktop),
    }


def _json(value):
    import json
    return json.dumps(value, indent=2, sort_keys=True)


if __name__ == "__main__":  # tiny manual check; never required by the server
    for _key, _value in client_config_snippets().items():
        print("[%s]\n%s\n" % (_key, _value))
