"""Resolve a provider API key by *variable name* (3.12.1).

Lookup order is fixed and small:

1. the process environment (an explicit operator export always wins);
2. the Harness credentials file, ``~/.dsh/.credentials.yaml`` by default,
   overridable with the config key ``credentials_file`` or the environment
   variables ``CREWFOREMAN_CREDENTIALS_FILE`` / ``DSH_CREDENTIALS_FILE``.

The legacy reviewer wrappers read that file with a flat ``NAME: value``
regex.  This reader accepts the same flat form, quoted or bare, and also the
nested ``records`` / ``refs`` layouts when they are present (a record may name
its variable with ``name``/``api_key_env`` and carry the value under
``value``/``literal``/``secret``/``api_key``/``key``/``token``; a ``ref`` of
the form ``env:NAME`` is followed through the process environment only).

Security rules:

* a credentials file that is readable by group or others is **skipped** with a
  one-line warning (the mode, never a value); owner-only means ``mode & 0o077
  == 0``, and the check is disabled on Windows where POSIX modes do not apply;
* the returned value is only ever copied into the child ``dsh`` environment by
  the caller.  This module never writes a value to a file, overlay, run
  record, event, log or error; errors name the variable only.

Standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import os
import re
import sys

DEFAULT_CREDENTIALS_FILE = "~/.dsh/.credentials.yaml"
MAX_FILE_BYTES = 256 * 1024
CREDENTIALS_ENV_NAMES = (
    "CREWFOREMAN_CREDENTIALS_FILE",
    "DSH_CREDENTIALS_FILE",
)
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.*)$")
_VALUE_FIELDS = ("value", "literal", "secret", "api_key", "apikey",
                 "key", "token")
_WARNED = set()


def _cfg_get(cfg, name, default=None):
    """Read ``name`` from a dict or Config-like object without raising."""
    if cfg is None:
        return default
    value = None
    try:
        if isinstance(cfg, dict):
            value = cfg.get(name, default)
        else:
            getter = getattr(cfg, "get", None)
            if callable(getter):
                value = getter(name, default)
            else:
                value = getattr(cfg, name, default)
    except Exception:
        return default
    return default if value is None else value


def credentials_path(cfg=None, env=None):
    """Return the credentials path, expanding ``~`` exactly once.

    No base directory is ever joined to the result: an absolute path stays
    absolute and a ``~`` path is expanded against the current ``HOME``.  A
    missing ``HOME`` leaves the literal ``~`` in place rather than producing a
    doubled ``<cwd>/~/...`` path.
    """
    source = os.environ if env is None else env
    raw = ""
    for name in CREDENTIALS_ENV_NAMES:
        try:
            candidate = source.get(name)
        except Exception:
            candidate = None
        if isinstance(candidate, str) and candidate.strip():
            raw = candidate.strip()
            break
    if not raw:
        configured = _cfg_get(cfg, "credentials_file", "")
        if isinstance(configured, str):
            raw = configured.strip()
    if not raw:
        raw = DEFAULT_CREDENTIALS_FILE
    expanded = os.path.expanduser(os.path.expandvars(str(raw)))
    return expanded


def _owner_only(path):
    """True when no group/other permission bits are set (POSIX)."""
    if os.name == "nt":  # pragma: no cover - Windows has no POSIX mode
        return True
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return False
    return (mode & 0o077) == 0


def _warn_once(path, mode, warn=None):
    message = ("crewforeman: ignoring credentials file %s: mode %04o is not "
               "owner-only (chmod 600 required)\n" % (path, mode))
    if callable(warn):
        try:
            warn(message.rstrip("\n"))
            return
        except Exception:
            pass
    key = (str(path), int(mode))
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        sys.stderr.write(message)
        sys.stderr.flush()
    except Exception:
        pass


def _read_text(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size <= 0 or size > MAX_FILE_BYTES:
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        return ""
    if len(raw) > MAX_FILE_BYTES:
        return ""
    return raw.decode("utf-8", "replace")


def _strip_comment(text):
    """Drop a trailing ``#`` comment that is outside quotes."""
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#":
            return text[:index].strip()
    return text.strip()


def _scalar(raw):
    """Return a clean scalar or ``""`` for null-ish/placeholder-free values."""
    text = _strip_comment(str(raw or "")).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in ("null", "none", "~", "true", "false", "{}", "[]", "|", ">"):
        return ""
    return text


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _flat_value(text, name):
    """The wrappers' flat ``NAME: value`` form, quoted or bare."""
    pattern = re.compile(
        r"^[ \t]*(?:-[ \t]+)?%s[ \t]*:[ \t]*(?P<value>[^\r\n#]+?)[ \t]*$"
        % re.escape(name), re.M)
    for match in pattern.finditer(text):
        value = _scalar(match.group("value"))
        if value:
            return value
    return ""


def _following_value(lines, index):
    """First value-ish field in the indented block that follows ``index``."""
    base = _indent(lines[index])
    for offset in range(index + 1, len(lines)):
        line = lines[offset]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        current = _indent(line)
        if current <= base:
            break
        match = _LINE_RE.match(line)
        if not match:
            continue
        key = match.group("key").lower()
        if key == "ref":
            value = _scalar(match.group("value"))
            if value:
                return value
        if key in _VALUE_FIELDS:
            value = _scalar(match.group("value"))
            if value:
                return value
    return ""


def _block_value(lines, index):
    """Scan the record block that contains ``index`` for a value field."""
    indent = _indent(lines[index])
    start = index
    while start > 0:
        previous = lines[start - 1]
        if previous.strip() and _indent(previous) < indent:
            break
        start -= 1
    end = index
    while end + 1 < len(lines):
        following = lines[end + 1]
        if following.strip() and _indent(following) < indent:
            break
        end += 1
    for line in lines[start:end + 1]:
        match = _LINE_RE.match(line)
        if not match:
            continue
        if match.group("key").lower() in _VALUE_FIELDS:
            value = _scalar(match.group("value"))
            if value:
                return value
    return ""


def _nested_value(text, name):
    """Find ``name`` in flat or records/refs layouts; return the raw scalar."""
    lines = text.splitlines()
    # 1. The flat form wins (same parsing as the wrappers).
    flat = _flat_value(text, name)
    if flat:
        return flat
    # 2. A key at any indentation: NAME: <value> or NAME:\n  value: ...
    for index, line in enumerate(lines):
        match = _LINE_RE.match(line)
        if not match:
            continue
        key = match.group("key").strip().strip("'\"")
        value = _scalar(match.group("value"))
        if key == name:
            if value:
                return value
            following = _following_value(lines, index)
            if following:
                return following
            block = _block_value(lines, index)
            if block:
                return block
        # 3. A record that names the variable indirectly.
        if key.lower() in ("name", "id", "api_key_env", "key_env") and value == name:
            block = _block_value(lines, index)
            if block:
                return block
            following = _following_value(lines, index)
            if following:
                return following
    return ""


def _interpret(raw, env):
    """Resolve a raw file scalar: literal, ``env:NAME`` ref, or a name ref."""
    value = _scalar(raw)
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith("env:"):
        ref = value.split(":", 1)[1].strip()
        return str(env.get(ref) or "") if ref else ""
    if lowered.startswith(("file:", "cmd:", "cmdline:", "keychain:")):
        return ""
    if value.startswith(("${", "{{", "<", "%", "*")):
        return ""
    if value.startswith("$"):
        ref = value.lstrip("$").strip("{}")
        return str(env.get(ref) or "") if ref else ""
    return value


def resolve_key(name, cfg=None, env=None, warn=None):
    """Return the key named ``name`` or ``None`` when it is unavailable.

    The process environment wins.  A credentials file that is not owner-only
    is skipped with a warning; an unreadable/missing file is not an error.
    The value is returned to the caller and never logged here.
    """
    if not isinstance(name, str) or not _KEY_RE.match(name.strip()):
        return None
    name = name.strip()
    source = os.environ if env is None else env
    try:
        direct = source.get(name)
    except Exception:
        direct = None
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    path = credentials_path(cfg, source)
    if not path or not os.path.isfile(path):
        return None
    if not _owner_only(path):
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:  # pragma: no cover - vanished between calls
            mode = 0
        _warn_once(path, mode, warn=warn)
        return None
    text = _read_text(path)
    if not text:
        return None
    raw = _nested_value(text, name)
    value = _interpret(raw, source)
    return value or None


def key_source(name, cfg=None, env=None, warn=None):
    """Return ``"environment"``, ``"credentials"`` or ``None`` (for docs/tests)."""
    if not isinstance(name, str) or not _KEY_RE.match(name.strip()):
        return None
    name = name.strip()
    source = os.environ if env is None else env
    try:
        direct = source.get(name)
    except Exception:
        direct = None
    if isinstance(direct, str) and direct.strip():
        return "environment"
    return "credentials" if resolve_key(name, cfg, source, warn=warn) else None


def missing_message(name):
    """A value-free error message naming only the variable."""
    return ("environment variable %s is not set (checked the process "
            "environment and the Harness credentials file)" % name)
