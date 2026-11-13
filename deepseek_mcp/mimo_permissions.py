"""Per-run ``mimocode.jsonc`` permission maps for MiMo Code workers.

MiMo Code picks up a ``mimocode.jsonc`` from its ``--dir`` workspace.  This
module renders that file from CrewForeman's sandbox presets so a run is
sandboxed by construction and never needs ``--dangerously-skip-permissions``.

Presets (``PRESETS``), with the same alias normalization as
``deepseek_mcp.safety.normalize_sandbox``:

* ``read-only`` -- inspection only.  ``edit`` is explicitly denied and ``bash``
  only allows a non-mutating allowlist (``ls*``, ``cat*``, ``git log*``, ...)
  behind a ``"*": "deny"`` catch-all.  A read-only reviewer cannot edit files
  or run mutating bash.
* ``workspace-write`` -- normal builder sandbox: everything read/write inside
  the workspace, ``bash`` allowed but denied the documented mutating/escaping
  globs (``git push*``, ``sudo *``, ``rm -rf /*``, ``*--yolo*``, ...).
* ``danger-full-access`` -- everything allowed.  Documented opt-in only.

``external_directory`` is scoped to the workspace in ``read-only`` and
``workspace-write`` (``{"*": "deny", "<workspace>/**": "allow"}``);
``danger-full-access`` allows all directories.

Output format: a single **pure JSON** document (pretty-printed, trailing
newline) even though the file is named ``mimocode.jsonc`` -- deliberately no
comment header, so any strict ``json.loads`` consumer accepts it (the
repo-root ``mimocode.jsonc`` is likewise comment-free).  The shape mirrors the
repo-root file: ``{"$schema": ..., "permission": {...}}`` with permission keys
``read``, ``edit``, ``glob``, ``grep``, ``list``, ``lsp``, ``task``, ``actor``,
``skill``, ``webfetch``, ``external_directory``, ``bash``.  ``webfetch``/``lsp``
are read-only capabilities and are allowed in every preset.

``skip_permissions_args`` returns ``["--dangerously-skip-permissions"]`` only
for an explicit ``cfg.mimo_skip_permissions`` opt-in and **never** returns
``--yolo``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

PRESETS = ("read-only", "workspace-write", "danger-full-access")

SCHEMA_URL = "https://mimo.xiaomi.com/mimocode/config.json"
CONFIG_NAME = "mimocode.jsonc"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

# Mirrors deepseek_mcp.safety._ALIASES so the two modules never disagree.
_ALIASES = {
    "read-only": "read-only",
    "readonly": "read-only",
    "read_only": "read-only",
    "ro": "read-only",
    "workspace-write": "workspace-write",
    "workspace_write": "workspace-write",
    "write": "workspace-write",
    "workspace": "workspace-write",
    "rw": "workspace-write",
    "danger-full-access": "danger-full-access",
    "danger_full_access": "danger-full-access",
    "dangerfullaccess": "danger-full-access",
    "full": "danger-full-access",
    "danger": "danger-full-access",
    "none": "danger-full-access",
}

_PRESET_ERROR = ("invalid sandbox preset; expected read-only, "
                 "workspace-write or danger-full-access")

# Bash allowlist for read-only: only non-mutating inspection commands.
_READ_ONLY_BASH_ALLOW = (
    "ls*", "cat*", "head*", "tail*", "grep*", "find*", "wc*", "pwd",
    "which*", "git status*", "git log*", "git diff*", "git show*",
)

# Bash deny rules layered on top of workspace-write's "*" allow.
_WORKSPACE_WRITE_BASH_DENY = (
    "git push*", "git * push*", "gh *",
    "git config --global*", "git config --system*",
    "git commit --amend*", "git rebase*", "git reset --hard*",
    "git filter-branch*", "git gc*", "git tag*",
    "sudo *", "rm -rf /*", "rm -rf ~*",
    "*--dangerously-skip-permissions*", "*--yolo*",
)

_ROLE_READ_ONLY = frozenset({"reviewer", "audit", "probe", "preflight"})


def normalize_preset(value):
    """Return one of PRESETS, accepting the safety-module aliases.

    ``None``/empty/unknown values raise ``ValueError`` without echoing the
    value (it could be a secret by mistake).
    """
    if not isinstance(value, str):
        raise ValueError(_PRESET_ERROR)
    key = value.strip().lower().replace(" ", "-")
    mode = _ALIASES.get(key)
    if mode is None:
        raise ValueError(_PRESET_ERROR)
    return mode


def _cfg_get(cfg, key, default=None):
    """Read ``key`` from a config object via ``.get`` or ``getattr``."""
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                return default
        except Exception:
            return default
    return getattr(cfg, key, default)


def _truthy(value):
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    if isinstance(value, int):
        return value == 1
    return False


def _deny_globs(extra_deny):
    if extra_deny is None:
        return ()
    if isinstance(extra_deny, str):
        return (extra_deny,)
    extra = tuple(extra_deny)
    if not all(isinstance(g, str) for g in extra):
        raise TypeError("extra_deny must be a string or a sequence of strings")
    return extra


def _workspace_root(workspace):
    """Absolute, separator-normalised workspace path for glob building."""
    raw = os.fspath(workspace) if workspace is not None else ""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace is required")
    root = os.path.abspath(raw).replace("\\", "/").rstrip("/")
    return root or "/"


def _external_directory(preset, root):
    if preset == "danger-full-access":
        return {"*": "allow"}
    return {"*": "deny", root + "/**": "allow"}


def _bash_map(preset, extra):
    if preset == "read-only":
        bash = {"*": "deny"}
        for glob in _READ_ONLY_BASH_ALLOW:
            bash[glob] = "allow"
    else:
        bash = {"*": "allow"}
        if preset == "workspace-write":
            for glob in _WORKSPACE_WRITE_BASH_DENY:
                bash[glob] = "deny"
    for glob in extra:
        bash[glob] = "deny"
    return bash


def _permission_map(preset, root, extra):
    allowed = preset != "read-only"
    return {
        "read": "allow",
        "edit": "allow" if allowed else "deny",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "lsp": "allow",
        "task": "allow",
        "actor": "allow",
        "skill": "allow",
        "webfetch": "allow",
        "external_directory": _external_directory(preset, root),
        "bash": _bash_map(preset, extra),
    }


def render_mimocode_jsonc(preset, workspace, *, extra_deny=None) -> str:
    """Return the ``mimocode.jsonc`` text for one run.

    ``extra_deny`` is an optional string (or sequence of strings) of bash globs
    appended as deny entries -- honoured for every preset, including
    ``danger-full-access``, so an explicit caller constraint always lands.
    """
    mode = normalize_preset(preset)
    root = _workspace_root(workspace)
    extra = _deny_globs(extra_deny)
    doc = {
        "$schema": SCHEMA_URL,
        "permission": _permission_map(mode, root, extra),
    }
    return json.dumps(doc, indent=2) + "\n"


def write_run_config(preset, workspace, *, path=None) -> Path:
    """Atomically write the rendered config and return its absolute path.

    Writes ``<workspace>/mimocode.jsonc`` (or ``path``) and creates missing
    parent directories.  ``path`` must stay inside ``workspace`` -- the user's
    global config (``~/.config``) is never written.
    """
    root = Path(os.path.abspath(os.fspath(workspace)))
    if path is None:
        target = root / CONFIG_NAME
    else:
        given = Path(os.fspath(path))
        target = given if given.is_absolute() else root / given
        target = Path(os.path.abspath(str(target)))
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError(
                "path must stay inside the workspace; global config is never "
                "written") from None
    text = render_mimocode_jsonc(preset, workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mimocode-", suffix=".tmp",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def skip_permissions_args(cfg) -> list:
    """``["--dangerously-skip-permissions"]`` only on explicit opt-in.

    Truthy ``cfg.mimo_skip_permissions`` (bool, ``1``, ``"1"``, ``"true"``)
    opts in; everything else returns ``[]``.  Never returns ``--yolo``.
    """
    if _truthy(_cfg_get(cfg, "mimo_skip_permissions", False)):
        return [SKIP_PERMISSIONS_FLAG]
    return []


def preset_for_role(role, cfg=None) -> str:
    """Map a CrewForeman role to a sandbox preset.

    ``reviewer``/``audit``/``probe``/``preflight`` get ``read-only``;
    ``builder``/``crosscheck``/``finisher``/``revision``/``rescue`` and any
    other role get ``workspace-write``.  An explicit ``cfg.mimo_sandbox`` wins
    when it normalizes to a valid preset; an invalid override falls back to the
    role default.
    """
    configured = _cfg_get(cfg, "mimo_sandbox", "")
    if isinstance(configured, str) and configured.strip():
        try:
            return normalize_preset(configured)
        except ValueError:
            pass
    name = (str(role) if role is not None else "").strip().lower()
    if name in _ROLE_READ_ONLY:
        return "read-only"
    return "workspace-write"
