"""Detect MiMo Code's concurrent-agent ceiling and record limit backoffs.

MiMo Code (``mimo``) caps parallel workflow agents with
``workflow.maxConcurrentAgents`` in ``mimocode.jsonc`` (JSONC: ``//`` and
``/* */`` comments and trailing commas are allowed).  Two files can carry the
key:

* the global config ``~/.config/mimocode/mimocode.jsonc``;
* the project config ``<project_dir or cwd>/mimocode.jsonc``.

Both are strictly read-only here: this module never creates or modifies them.
When both configure a positive value the larger one wins ("use the max by
default"); when neither does, the MiMo documented default
``min(16, 2 * cpu)`` applies.  A CrewForeman override
(``cfg.mimo_max_concurrent_agents``) wins over everything.  A ``0`` in
``mimocode.jsonc`` means "MiMo picks its own default" per the MiMo docs; it
is treated as absent and still reported in ``values_seen``.

After a limit/429 error, :func:`note_limit_error` records a small backoff
event in the CrewForeman state directory (never ``~/.config``) and suggests
half the effective limit (floor 1).

Python 3.9 compatible; standard library only.
"""

import datetime as dt
import json
import os
from pathlib import Path

# The global config path.  A module constant (not just a baked-in string) so
# tests can point it at a temp file and never read the operator's real
# ~/.config/mimocode/mimocode.jsonc; ``read_mimo_limit`` also accepts an
# explicit ``global_path=`` for the same reason.
GLOBAL_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "mimocode", "mimocode.jsonc")
PROJECT_CONFIG_NAME = "mimocode.jsonc"
STATE_FILENAME = "mimo-concurrency.json"

_MISSING = object()


def DEFAULT_LIMIT(cpu):
    """MiMo's documented default: ``min(16, 2 * cpu)``."""
    return min(16, 2 * cpu)


def default_limit():
    """Built-in default for this machine: ``DEFAULT_LIMIT`` of the CPU count."""
    return DEFAULT_LIMIT(os.cpu_count() or 1)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(now=None):
    return (now or _now()).isoformat(timespec="seconds")


def _cfg_value(cfg, name, default=None):
    try:
        return cfg.get(name, default) if hasattr(cfg, "get") else getattr(cfg, name, default)
    except Exception:
        return default


def _positive_int(raw):
    """Return ``int(raw)`` when it is a positive int-like value, else None."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ------------------------------------------------------------------ JSONC

def _strip_jsonc(text):
    """Return *text* with comments and trailing commas removed.

    String-aware: ``//`` and ``/* */`` inside JSON strings (URLs, globs) and
    commas inside strings are left alone.  Comments go first so a trailing
    comma followed by a comment before ``}``/``]`` is still recognised.
    """
    return _strip_trailing_commas(_strip_comments(text))


def _strip_comments(text):
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text):
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_jsonc(text):
    try:
        return json.loads(_strip_jsonc(text))
    except ValueError:
        return None


def _read_max_concurrent(path):
    """Return the raw ``workflow.maxConcurrentAgents`` value, or ``_MISSING``.

    Read-only: a missing or unparseable file simply yields ``_MISSING``; no
    file is ever created or modified.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return _MISSING
    data = _parse_jsonc(text)
    if not isinstance(data, dict):
        return _MISSING
    workflow = data.get("workflow")
    if not isinstance(workflow, dict) or "maxConcurrentAgents" not in workflow:
        return _MISSING
    return workflow["maxConcurrentAgents"]


# ------------------------------------------------------------- public API

def read_mimo_limit(cfg=None, *, project_dir=None, global_path=None):
    """Return the effective MiMo agent limit and where it came from.

    Reads ``workflow.maxConcurrentAgents`` from the global config
    (``global_path`` or :data:`GLOBAL_CONFIG_PATH`) and the project config
    (``<project_dir or cwd>/mimocode.jsonc``), read-only.  Effective limit:
    the ``cfg.mimo_max_concurrent_agents`` override when positive (source
    ``"config"``), else the largest positive configured value (source
    ``"global_config"``/``"project_config"``; a tie goes to the project), else
    the built-in default (source ``"default"``).

    Returns ``{"limit": int, "source": str, "values_seen": dict}``;
    ``values_seen`` maps each source consulted to the raw value found
    (``0`` values included, so callers can see they were treated as absent)
    and always includes ``"default"``.
    """
    values_seen = {"default": default_limit()}

    override_raw = _cfg_value(cfg, "mimo_max_concurrent_agents", None)
    if override_raw is not None:
        values_seen["config"] = override_raw
    override = _positive_int(override_raw)
    if override is not None:
        return {"limit": override, "source": "config", "values_seen": values_seen}

    global_raw = _read_max_concurrent(
        global_path if global_path is not None else GLOBAL_CONFIG_PATH)
    if global_raw is not _MISSING:
        values_seen["global_config"] = global_raw
    if project_dir is not None:
        project_path = Path(project_dir) / PROJECT_CONFIG_NAME
    else:
        project_path = Path.cwd() / PROJECT_CONFIG_NAME
    project_raw = _read_max_concurrent(project_path)
    if project_raw is not _MISSING:
        values_seen["project_config"] = project_raw

    best = None
    best_source = "default"
    for name, raw in (("global_config", global_raw),
                      ("project_config", project_raw)):
        value = _positive_int(raw)
        if value is None:
            continue
        if best is None or value > best or (value == best and name == "project_config"):
            best = value
            best_source = name
    if best is not None:
        return {"limit": best, "source": best_source, "values_seen": values_seen}
    return {"limit": values_seen["default"], "source": "default",
            "values_seen": values_seen}


def get_concurrency(cfg=None, **kwargs):
    """Convenience wrapper returning just the effective limit as an int."""
    return read_mimo_limit(cfg, **kwargs)["limit"]


def _write_backoff_record(cfg, record):
    """Write the backoff record under ``cfg.state_dir``; None when unwritable.

    Deliberately no home-directory fallback: with an empty ``state_dir`` the
    caller just gets the suggestion and nothing is written anywhere.
    """
    raw = _cfg_value(cfg, "state_dir", "")
    if not raw:
        return None
    directory = Path(os.path.expanduser(str(raw)))
    path = directory / STATE_FILENAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        return None
    return path


def note_limit_error(cfg=None, source="limit_error", *, project_dir=None,
                     global_path=None):
    """Record a limit/429 backoff event and suggest half the effective limit.

    The event (timestamp, source, previous limit, suggested limit) is stored
    in ``<cfg.state_dir>/mimo-concurrency.json``; when ``cfg.state_dir`` is
    empty nothing is written.  Returns
    ``{"suggested_limit", "recorded_at", "path", "previous"}`` where
    ``suggested_limit`` is ``max(1, limit // 2)`` and ``previous`` is the
    limit that was backed off from.
    """
    previous = read_mimo_limit(
        cfg, project_dir=project_dir, global_path=global_path)["limit"]
    suggested = max(1, previous // 2)
    recorded_at = _iso()
    record = {
        "recorded_at": recorded_at,
        "source": source,
        "previous_limit": previous,
        "suggested_limit": suggested,
    }
    path = _write_backoff_record(cfg, record)
    return {"suggested_limit": suggested, "recorded_at": recorded_at,
            "path": str(path) if path is not None else None,
            "previous": previous}
