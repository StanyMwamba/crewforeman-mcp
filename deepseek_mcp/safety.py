"""Permission presets, OS sandbox, env scrub and write guard for DSH.

This module is deliberately dependency-free and import-safe: it never reads
secrets itself and never places environment values into exception text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from . import platforms

PRESETS = {
    "read-only": {"sandbox": "read-only", "approval": "ask"},
    "workspace-write": {"sandbox": "workspace-write", "approval": "ask"},
    "danger": {"sandbox": "danger-full-access", "approval": "never"},
}

_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

_ALIASES = {
    # read-only
    "read-only": "read-only",
    "readonly": "read-only",
    "read_only": "read-only",
    "ro": "read-only",
    # workspace-write
    "workspace-write": "workspace-write",
    "workspace_write": "workspace-write",
    "write": "workspace-write",
    "workspace": "workspace-write",
    "rw": "workspace-write",
    # danger-full-access
    "danger-full-access": "danger-full-access",
    "danger_full_access": "danger-full-access",
    "dangerfullaccess": "danger-full-access",
    "full": "danger-full-access",
    "danger": "danger-full-access",
    "none": "danger-full-access",
}

_SECRET_MARKERS = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
)

_ALWAYS_SCRUB = ("DEEPSEEK_MCP_CONFIG",)
_SECRET_ERROR = "invalid sandbox value; expected read-only, workspace-write or danger-full-access"


def _preset_for_mode(mode):
    if mode == "danger-full-access":
        return "danger"
    return mode


def normalize_sandbox(value, default="workspace-write"):
    """Return one of read-only | workspace-write | danger-full-access.

    Accepts presets and the documented aliases. ``None``/empty falls back to
    ``default``. Unknown values raise ``ValueError`` without echoing the value
    (a caller could have passed a secret by mistake).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None or (isinstance(default, str) and not default.strip()):
            default = "workspace-write"
        return normalize_sandbox(default)
    if not isinstance(value, str):
        raise ValueError(_SECRET_ERROR)
    key = value.strip().lower().replace(" ", "-")
    mode = _ALIASES.get(key)
    if mode is None:
        raise ValueError(_SECRET_ERROR)
    return mode


def _cfg_get(cfg, key, default=None):
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except TypeError:
        try:
            return getter(key)
        except Exception:
            return default
    except Exception:
        return default


_SEATBELT_PROBE = {"done": False, "usable": False, "reason": None}


def seatbelt_usable(force=False):
    """Return True iff a trivial ``sandbox-exec`` profile can actually run.

    ``sandbox-exec`` cannot be nested. A connector hosted by an already
    sandboxed DSH process gets ``sandbox_apply: Operation not permitted``; the
    probe turns that into a clean fallback instead of breaking every run.
    The probe result is cached for the process lifetime.
    """
    if _SEATBELT_PROBE["done"] and not force:
        return bool(_SEATBELT_PROBE["usable"])
    _SEATBELT_PROBE["done"] = True
    _SEATBELT_PROBE["usable"] = False
    if sys.platform != "darwin" or not sandbox_exec_available():
        _SEATBELT_PROBE["reason"] = "sandbox-exec not available"
        return False
    try:
        proc = subprocess.run(
            ["sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=2.0,
        )
        _SEATBELT_PROBE["usable"] = proc.returncode == 0
        if proc.returncode != 0:
            _SEATBELT_PROBE["reason"] = (
                "sandbox-exec cannot be nested in this process (probe exit %s)"
                % proc.returncode)
    except Exception as exc:
        _SEATBELT_PROBE["reason"] = "sandbox-exec probe failed: %s" % exc.__class__.__name__
        _SEATBELT_PROBE["usable"] = False
    return bool(_SEATBELT_PROBE["usable"])


def _os_sandbox_enabled(cfg):
    if sys.platform != "darwin":
        return False
    if not seatbelt_usable():
        return False
    setting = _cfg_get(cfg, "os_sandbox", "auto")
    if setting is False:
        return False
    if isinstance(setting, str) and setting.strip().lower() in (
        "off",
        "false",
        "no",
        "0",
        "disabled",
    ):
        return False
    return True


def resolve(args, cfg=None, reviewer=False):
    """Resolve the effective permission policy for one Harness process."""
    args = args or {}
    if not hasattr(args, "get"):
        raise ValueError("args must be a mapping")

    explicit_raw = args.get("sandbox")
    explicit = explicit_raw is not None and (
        not isinstance(explicit_raw, str) or bool(explicit_raw.strip())
    )

    if explicit:
        requested_raw = explicit_raw
        sandbox = normalize_sandbox(explicit_raw)
    else:
        requested_raw = None
        if reviewer:
            raw_default = _cfg_get(cfg, "reviewer_sandbox", None)
            if raw_default is None or (
                isinstance(raw_default, str) and not raw_default.strip()
            ):
                raw_default = "read-only"
        else:
            raw_default = _cfg_get(cfg, "sandbox", None)
            if raw_default is None or (
                isinstance(raw_default, str) and not raw_default.strip()
            ):
                raw_default = "workspace-write"
        requested_raw = raw_default
        sandbox = normalize_sandbox(raw_default)

    # Dangermode is opt-in only: it can never arrive via a default or config.
    if sandbox == "danger-full-access" and not explicit:
        raise ValueError(
            "danger-full-access requires explicit opt-in via args['sandbox']='danger'"
        )

    approval = args.get("approval")
    if approval is None or (isinstance(approval, str) and not approval.strip()):
        preset = _preset_for_mode(sandbox)
        approval = PRESETS[preset]["approval"]
    elif isinstance(approval, str) and approval.strip().lower() in ("ask", "never"):
        approval = approval.strip().lower()
    else:
        raise ValueError("invalid approval policy; expected 'ask' or 'never'")

    support = platforms.sandbox_support()
    return {
        "preset": _preset_for_mode(sandbox),
        "sandbox": sandbox,
        "approval": approval,
        "os_sandbox": _os_sandbox_enabled(cfg),
        "platform_sandbox": support,
        "requested": requested_raw,
    }


def dsh_flags(policy):
    """Deprecated: the headless profile accepts no permission flags.

    DSH 0.2.1-rc.5's ``--profile headless`` parser accepts only ``--json`` and
    ``--session-id``. Permission is selected with the documented
    ``DSH_PERMISSION_MODE`` deployment override instead; callers use
    :func:`dsh_env`. The function is kept (returning no flags) so older plugin
    code cannot reintroduce unsupported arguments.
    """
    return []


def dsh_env(policy):
    """Return the documented environment override for a resolved policy.

    The headless profile reads ``DSH_PERMISSION_MODE`` for the sandbox mode and
    derives its approval policy from it (``never`` only for full access).
    """
    policy = policy or {}
    sandbox = normalize_sandbox(policy.get("sandbox"), default="workspace-write")
    return {"DSH_PERMISSION_MODE": sandbox}


def add_dir_flags(paths):
    """Deprecated: the headless profile has no ``--add-dir`` option.

    Kept as a compatibility shim that never emits an unsupported flag. Extra
    roots are not available on the headless profile; keep work inside the
    session workspace.
    """
    return []


def scrubbed_env(env=None, keep=("DEEPSEEK_API_KEY",), extra_keep=()):
    """Return a scrubbed copy of ``env`` (default ``os.environ``).

    Secret-looking names are dropped unless explicitly kept. Config pointers and
    provider base URLs are always dropped. The input mapping is never mutated.
    """
    source = os.environ if env is None else env
    result = dict(source)
    keep_names = set()
    for name in tuple(keep or ()) + tuple(extra_keep or ()):
        if isinstance(name, str):
            keep_names.add(name.upper())

    for name in list(result):
        if not isinstance(name, str):
            continue
        upper = name.upper()
        if upper in _ALWAYS_SCRUB or upper.endswith("_BASE_URL"):
            result.pop(name, None)
            continue
        if upper in keep_names:
            continue
        if any(marker in upper for marker in _SECRET_MARKERS):
            result.pop(name, None)
    return result


def sandbox_exec_available():
    """True when the macOS ``sandbox-exec`` binary is available."""
    if os.path.isfile("/usr/bin/sandbox-exec") and os.access(
        "/usr/bin/sandbox-exec", os.X_OK
    ):
        return True
    return shutil.which("sandbox-exec") is not None


def _sbpl_quote(value):
    """Quote a plain SBPL string literal, escaping backslash and quote."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sbpl_regex(pattern):
    """Quote an SBPL ``#\"...\"`` regex literal."""
    escaped = pattern.replace("\\", "\\\\").replace('"', '\\"')
    return '#"' + escaped + '"'


def _canonical(path):
    return os.path.realpath(os.path.expanduser(str(path)))


def seatbelt_profile(workspace, mode="read-only", extra_writes=(), dsh_home=None):
    """Build a Seatbelt SBPL profile for the given workspace and mode.

    ``dsh_home`` is the DSH home the child will actually use. A read-only
    reviewer gets a private home outside the workspace, and no write grant to
    the operator's ``~/.dsh/sessions`` is emitted at all. Credential documents
    are denied after every write grant.
    """
    mode = normalize_sandbox(mode, default="read-only")
    ws = _canonical(workspace or os.getcwd())
    home = os.path.realpath(os.path.expanduser("~"))
    operator_dsh_home = os.path.join(home, ".dsh")
    active_dsh_home = _canonical(dsh_home) if dsh_home else operator_dsh_home
    private_dsh_home = active_dsh_home != _canonical(operator_dsh_home)

    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
    ]

    if mode != "read-only":
        lines.append("(allow file-write* (subpath %s))" % _sbpl_quote(ws))

    # Read-only still needs its report, evidence directory and scratch space.
    lines.append(
        "(allow file-write* (literal %s))"
        % _sbpl_quote(os.path.join(ws, "REVIEW.md"))
    )
    lines.append(
        "(allow file-write* (subpath %s))"
        % _sbpl_quote(os.path.join(ws, ".browser-evidence"))
    )
    tmp_regex = "^" + re.escape(os.path.join(ws, ".dsh-tmp-"))
    lines.append("(allow file-write* (regex %s))" % _sbpl_regex(tmp_regex))

    # The private reviewer home is granted explicitly by the caller through
    # extra_writes. A read-only run never gets a write grant for the operator's
    # ~/.dsh (not even sessions): that was the v3.5.0 over-broad grant, and the
    # reviewer runs with DSH_HOME pointing at its private home instead.
    runtime_dirs = []
    if mode != "read-only":
        runtime_dirs.append(os.path.join(active_dsh_home, "sessions"))
        if not private_dsh_home:
            runtime_dirs.extend([
                os.path.join(active_dsh_home, "profiles"),
                os.path.join(active_dsh_home, "storages"),
                os.path.join(active_dsh_home, "cache"),
            ])
    for runtime_dir in runtime_dirs:
        lines.append(
            "(allow file-write* (subpath %s))" % _sbpl_quote(runtime_dir)
        )

    seen = set()
    for extra in extra_writes or ():
        if not isinstance(extra, str) or not extra.strip():
            continue
        real = _canonical(extra)
        if real in seen:
            continue
        seen.add(real)
        lines.append("(allow file-write* (subpath %s))" % _sbpl_quote(real))

    # Explicitly deny every managed credential document after all grants.
    # Seatbelt uses the last matching rule, so these deny rules must come last.
    for credential in {os.path.join(operator_dsh_home, ".credentials.yaml"),
                       os.path.join(active_dsh_home, ".credentials.yaml")}:
        lines.append("(deny file-write* (literal %s))" % _sbpl_quote(credential))

    lines.append(
        "(allow file-write-data (literal \"/dev/null\") "
        "(literal \"/dev/stdout\") (literal \"/dev/stderr\"))"
    )
    return "\n".join(lines) + "\n"


def wrap_command(cmd, policy, workspace, tmpdir=None, extra_writes=(), dsh_home=None):
    """Wrap ``cmd`` with ``sandbox-exec`` when the policy asks for it.

    Returns ``(argv, meta)``. This function never raises; any setup problem
    degrades to an unwrapped command with a short, redacted reason.
    """
    argv = []
    try:
        argv = [cmd] if isinstance(cmd, str) else list(cmd or ())
        policy = policy or {}
        if not policy.get("os_sandbox"):
            support = platforms.sandbox_support()
            return argv, {
                "wrapped": False,
                "reason": (support.get("reason")
                           if not support.get("available")
                           else "os sandbox disabled by policy"),
            }
        if not seatbelt_usable():
            return argv, {
                "wrapped": False,
                "reason": (_SEATBELT_PROBE.get("reason")
                           or "sandbox-exec not usable"),
            }
        writes = list(extra_writes or ())
        if tmpdir:
            writes.append(tmpdir)
        mode = policy.get("sandbox") or "read-only"
        profile = seatbelt_profile(workspace, mode=mode, extra_writes=writes,
                                   dsh_home=dsh_home)
        wrapped = ["sandbox-exec", "-p", profile] + argv
        return wrapped, {
            "wrapped": True,
            "sandbox": mode,
            "workspace": _canonical(workspace or os.getcwd()),
        }
    except Exception:
        return argv, {"wrapped": False, "reason": "os sandbox setup failed"}


_SKIP_DIRS = {".git", ".chrome-profile", "node_modules", "__pycache__",
              ".browser-evidence", ".dsh-review-home"}

# Only known runtime scratch is ignored by the write guard. Dot paths in
# general (.env, .github, ...) are deliverables and must be reported.
_RUNTIME_SCRATCH = (".dsh-tmp-", ".chrome-profile", ".browser-evidence",
                    ".dsh-review-home", ".fake-dsh-state.json")


def snapshot_workspace(workspace):
    """Snapshot regular files under ``workspace`` as ``{relpath: (mtime, size)}``."""
    if not workspace or not os.path.isdir(workspace):
        return {}
    root = os.path.abspath(workspace)
    result = {}
    try:
        walker = os.walk(root, followlinks=False)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SKIP_DIRS and not name.startswith(".dsh-tmp-")
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    stat = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                result[rel] = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    return result


def _allowed(path, allowed):
    # Runtime scratch is deliberately hidden at the workspace root: dsh temp
    # dirs, Chrome profiles and the reviewer's private DSH home. Those are not
    # deliverables; the write guard is about the artifact files.
    first = path.split("/", 1)[0]
    for scratch in _RUNTIME_SCRATCH:
        if first == scratch or first.startswith(scratch):
            return True
    for entry in allowed or ():
        if not isinstance(entry, str):
            continue
        entry = entry.replace(os.sep, "/")
        while entry.startswith("./"):
            entry = entry[2:]
        if not entry:
            continue
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def diff_workspace(before, after, allowed=("REVIEW.md",)):
    """Return sorted ``[{'path','change'}]`` describing created/modified/deleted."""
    before = before or {}
    after = after or {}
    changes = []
    for path in sorted(set(before) | set(after)):
        if _allowed(path, allowed):
            continue
        if path not in before:
            changes.append({"path": path, "change": "created"})
        elif path not in after:
            changes.append({"path": path, "change": "deleted"})
        elif before[path] != after[path]:
            changes.append({"path": path, "change": "modified"})
    return changes
