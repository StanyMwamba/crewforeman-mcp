"""Permission presets, OS sandbox, env scrub and write guard for DSH.

This module is deliberately dependency-free and import-safe: it never reads
secrets itself and never places environment values into exception text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

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


_SEATBELT_PROBE = {"done": False, "usable": False, "reason": None,
                   "at": 0.0}
_STATUS_CACHE = {}
_CANARY_CACHE = {}
_PROBE_LOCK = threading.Lock()
_SANDBOX_UNSET = object()
_CREDENTIAL_NAMES = (".credentials.yaml",)


def _probe_ttl(cfg):
    raw = _cfg_get(cfg, "sandbox_probe_ttl", 300.0)
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        ttl = 300.0
    return max(0.0, ttl)


def _run_probe(argv, timeout=3.0):
    """Run one tiny probe; returns (returncode|None, stderr_tail)."""
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=timeout)
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        return int(proc.returncode), tail
    except subprocess.TimeoutExpired:
        return None, "probe timeout"
    except Exception as exc:
        return None, exc.__class__.__name__


def _seatbelt_probe():
    """Probe Seatbelt: the wrapper launches and a deny canary is enforced."""
    if sys.platform != "darwin" or not sandbox_exec_available():
        return False, "sandbox-exec not available"
    code, tail = _run_probe(
        ["sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"])
    if code != 0:
        if code is None:
            return False, "sandbox-exec probe timed out"
        lowered = tail.lower()
        if "operation not permitted" in lowered or "sandbox_apply" in lowered:
            return False, "sandbox-exec cannot be nested in this process"
        return False, "sandbox-exec probe failed (exit %s)" % code
    root = None
    try:
        root = tempfile.mkdtemp(prefix="dsh-seatbelt-probe-")
        target = os.path.join(root, "canary")
        script = 'printf x > "$1"'
        code, _tail = _run_probe(
            ["sandbox-exec", "-p",
             "(version 1) (allow default) (deny file-write*)",
             "/bin/sh", "-c", script, "sh", target])
        if code == 0 or os.path.exists(target):
            return False, "sandbox-exec did not deny the canary write"
        return True, None
    except Exception as exc:
        return False, "sandbox-exec canary failed: %s" % exc.__class__.__name__
    finally:
        if root:
            shutil.rmtree(root, ignore_errors=True)


def seatbelt_usable(force=False):
    """Return True iff ``sandbox-exec`` can launch and deny a canary write.

    ``sandbox-exec`` cannot be nested. A connector hosted by an already
    sandboxed DSH process gets ``sandbox_apply: Operation not permitted``; the
    probe turns that into a clean fallback instead of breaking every run.
    The result is cached for ``sandbox_probe_ttl`` seconds.
    """
    now = time.time()
    if not force and _SEATBELT_PROBE["done"]:
        ttl = _probe_ttl(None)
        if ttl <= 0 or (now - _SEATBELT_PROBE.get("at", 0.0)) < ttl:
            return bool(_SEATBELT_PROBE["usable"])
    usable, reason = _seatbelt_probe()
    _SEATBELT_PROBE.update({"done": True, "usable": usable,
                            "reason": reason, "at": now})
    return bool(usable)


def _bwrap_probe():
    binary = shutil.which("bwrap")
    if not binary:
        return False, "bwrap not installed", None
    try:
        proc = subprocess.run(
            [binary, "--ro-bind", "/", "/", "--dev", "/dev",
             "--", "/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=2.0)
        if proc.returncode == 0:
            return True, None, binary
        return False, "bwrap probe failed (exit %s)" % proc.returncode, binary
    except Exception as exc:
        return False, "bwrap probe failed: %s" % exc.__class__.__name__, binary


def _linux_status(cfg):
    setting = str(_cfg_get(cfg, "linux_sandbox", "auto") or "auto").lower()
    min_abi = _cfg_get(cfg, "linux_min_abi", 3)
    try:
        min_abi = max(1, int(min_abi))
    except (TypeError, ValueError):
        min_abi = 3
    if setting == "off":
        return {"kind": "none", "reason": "linux_sandbox=off",
                "enforcement": "none", "abi": None, "tested": False}
    abi = None
    reason = None
    if setting in ("auto", "landlock"):
        try:
            from . import sandbox_linux
            probe = sandbox_linux.probe_abi()
        except Exception as exc:
            probe = {"ok": False, "reason": "launcher import failed: %s"
                     % exc.__class__.__name__, "abi": None}
        abi = probe.get("abi")
        if probe.get("ok"):
            if int(abi) >= min_abi:
                return {"kind": "landlock", "reason": None,
                        "enforcement": "enforced", "abi": "landlock-%d" % abi,
                        "tested": True, "min_abi": min_abi}
            reason = "abi_lt_%d" % min_abi
            if setting == "landlock":
                return {"kind": "landlock", "reason": reason,
                        "enforcement": "degraded",
                        "abi": "landlock-%d" % abi, "tested": True,
                        "min_abi": min_abi}
        else:
            reason = probe.get("reason") or "landlock_unavailable"
    if setting in ("auto", "bwrap"):
        usable, bwrap_reason, _binary = _bwrap_probe()
        if usable:
            return {"kind": "bwrap", "reason": None,
                    "enforcement": "enforced", "abi": None, "tested": True,
                    "min_abi": min_abi}
        reason = bwrap_reason or reason or "no_linux_boundary"
    return {"kind": "none", "reason": reason or "no_linux_boundary",
            "enforcement": "none", "abi": None, "tested": False,
            "min_abi": min_abi}


def _windows_status(cfg):
    setting = str(_cfg_get(cfg, "windows_sandbox", "auto") or "auto").lower()
    if setting == "off":
        return {"kind": "none", "reason": "windows_sandbox=off",
                "enforcement": "none", "abi": None, "tested": False}
    if setting == "appcontainer":
        # W2 needs a provisioned AppContainer profile and capability work;
        # claiming it here would be a false enforced boundary.
        return {"kind": "none", "reason": "appcontainer_unavailable",
                "enforcement": "none", "abi": None, "tested": False}
    try:
        from . import sandbox_windows
        probe = sandbox_windows.probe_status()
    except Exception as exc:
        probe = {"ok": False, "reason": "launcher import failed: %s"
                 % exc.__class__.__name__}
    diag_keys = ("admin", "admin_elevated", "admin_member", "admin_source",
                 "admin_error", "admin_member_error", "restricting_sid",
                 "logon_sid", "write_restricted", "step", "error")
    diag = {key: probe.get(key) for key in diag_keys if key in probe}
    if probe.get("ok"):
        status = {"kind": "windows-write-restricted", "reason": None,
                  "enforcement": "enforced", "abi": "write-restricted",
                  "tested": True, "min_abi": None}
        status.update(diag)
        status["admin"] = bool(probe.get("admin"))
        return status
    status = {"kind": "none",
              "reason": probe.get("reason") or "windows_unavailable",
              "enforcement": "none", "abi": None, "tested": False,
              "min_abi": None}
    status.update(diag)
    return status


def _status_for(system, cfg=None):
    if system == "Darwin":
        usable = seatbelt_usable(force=bool(_cfg_get(cfg, "_force_probe", False)))
        if usable:
            return {"kind": "seatbelt", "reason": None,
                    "enforcement": "enforced", "abi": "seatbelt",
                    "tested": True, "min_abi": None}
        return {"kind": "none", "reason": _SEATBELT_PROBE.get("reason")
                or "seatbelt_unavailable", "enforcement": "none",
                "abi": None, "tested": False, "min_abi": None}
    if system == "Linux":
        return _linux_status(cfg)
    if system == "Windows":
        return _windows_status(cfg)
    return {"kind": "none", "reason": "unsupported platform",
            "enforcement": "none", "abi": None, "tested": False,
            "min_abi": None}


_DANGER_OPT_IN_REASON = "explicit_danger_full_access_opt_in"
_DANGER_OPT_IN_HEADER = (
    "boundary=none(explicit danger-full-access opt-in); writes=unbounded; "
    "delete=unbounded; network=unrestricted")


def _sandbox_header(status):
    kind = status.get("kind") or "none"
    enforcement = status.get("enforcement") or "none"
    abi = status.get("abi")
    delete = status.get("delete_outside") or "unverified"
    if status.get("reason") == _DANGER_OPT_IN_REASON:
        return _DANGER_OPT_IN_HEADER
    if status.get("blocked"):
        return ("UNSANDBOXED blocked; boundary=%s(%s) unavailable; "
                "writes=unverified; delete=unverified; network=unrestricted"
                % (kind, status.get("reason") or "unavailable"))
    if enforcement == "enforced":
        label = "%s(%s)" % (kind, abi) if abi else kind
        return ("boundary=%s enforced; writes=deny-outside; delete=%s; "
                "network=unrestricted" % (label, delete))
    if kind == "none":
        label = "none"
    else:
        label = ("%s(%s) %s" % (kind, abi, enforcement) if abi
                 else "%s %s" % (kind, enforcement))
    return ("UNSANDBOXED; boundary=%s (%s); writes=unverified; "
            "delete=unverified; network=unrestricted"
            % (label, status.get("reason") or "unavailable"))


def sandbox_status(force=False, cfg=None, system=None):
    """Probe the OS boundary; never spawns ``dsh`` and never raises.

    ``tested`` is true only when a probe actually ran on this host.  The
    result is cached for ``sandbox_probe_ttl`` seconds (0 re-probes).
    """
    system = platforms.normalize_system(system)
    ttl = _probe_ttl(cfg)
    now = time.time()
    with _PROBE_LOCK:
        cached = _STATUS_CACHE.get(system)
        # ``seatbelt_usable`` already caches its probe for the TTL. Keeping a
        # second status cache on macOS would hide a monkeypatched probe from
        # the policy tests, so Darwin re-reads that cached boolean.
        if (system != "Darwin" and not force and cached is not None
                and (ttl <= 0 or now - cached[0] < ttl)):
            return _copy_status(cached[1])
        detail = _status_for(system, cfg)
        status = {
            "schema": 1,
            "kind": detail.get("kind") or "none",
            "enforcement": detail.get("enforcement") or "none",
            "abi": detail.get("abi"),
            "probe": {"ok": bool(detail.get("tested")), "at": now,
                      "ttl": ttl, "reason": detail.get("reason")},
            "canary": {"ok": None, "at": None, "reason": "not_run"},
            "tested": bool(detail.get("tested")),
            "degraded": (detail.get("enforcement") or "none") != "enforced",
            "blocked": False,
            "reason": detail.get("reason"),
            "temp_roots": [],
            "profile_sha256": None,
            "requested": "auto",
            "platform": system,
            "writes_outside": ("denied" if detail.get("enforcement") == "enforced"
                               else "unverified"),
            "delete_outside": ("unverified"
                               if detail.get("kind") == "windows-write-restricted"
                               else ("denied"
                                     if detail.get("enforcement") == "enforced"
                                     else "unverified")),
            "network": "unrestricted",
        }
        for key in ("min_abi", "admin", "admin_elevated", "admin_member",
                    "admin_source", "admin_error", "admin_member_error",
                    "restricting_sid", "logon_sid", "write_restricted",
                    "step", "error"):
            if key in detail:
                status[key] = detail[key]
        status["sandbox_header"] = _sandbox_header(status)
        _STATUS_CACHE[system] = (now, _copy_status(status))
        return status


def _copy_status(status):
    return json.loads(json.dumps(status))


def _sandbox_mode(cfg):
    raw = _cfg_get(cfg, "os_sandbox_mode", None)
    if raw is None:
        raw = _cfg_get(cfg, "os_sandbox", "auto")
    if raw is False:
        return "off"
    if raw is True or raw is None:
        return "auto"
    text = str(raw).strip().lower()
    if text in ("require", "required", "strict"):
        return "require"
    if text in ("off", "false", "no", "0", "disabled"):
        return "off"
    return "auto"


def sandbox_config_warning(cfg):
    """One-line warning when the operator explicitly disabled the boundary."""
    if _sandbox_mode(cfg) == "off":
        return ("WARNING: OS sandbox disabled by config os_sandbox=off; "
                "file writes are unbounded")
    system = platforms.normalize_system()
    key = {"Linux": "linux_sandbox", "Windows": "windows_sandbox"}.get(system)
    if key:
        raw = _cfg_get(cfg, key, "")
        if str(raw or "").strip().lower() in ("off", "false", "no", "0",
                                               "disabled"):
            return ("WARNING: OS sandbox disabled by config %s=off; "
                    "file writes are unbounded" % key)
    return None


def _os_sandbox_enabled(cfg):
    """Legacy boolean used by :func:`resolve`; delegates to the policy."""
    policy = sandbox_policy(cfg)
    return bool(policy.get("os_sandbox"))


def sandbox_policy(cfg, args=None, reviewer=False):
    """Resolve the OS-sandbox portion of a permission policy.

    Keeps the legacy ``os_sandbox`` boolean, adds ``os_sandbox_mode`` and the
    full ``os_sandbox_policy`` status object.  In ``require`` mode with no
    enforced tier the policy carries ``os_sandbox_blocked=True``.
    """
    mode = _sandbox_mode(cfg)
    danger_opt_in = False
    if isinstance(args, dict) and args.get("sandbox") is not None:
        try:
            danger_opt_in = (normalize_sandbox(args.get("sandbox"))
                             == "danger-full-access")
        except Exception:
            danger_opt_in = False
    if danger_opt_in:
        # A per-job danger-full-access request is an explicit opt-in and
        # must be labelled as such, never reported as a silent degradation.
        now = time.time()
        status = {
            "schema": 1, "kind": "none", "enforcement": "none", "abi": None,
            "probe": {"ok": False, "at": now, "ttl": _probe_ttl(cfg),
                      "reason": _DANGER_OPT_IN_REASON},
            "canary": {"ok": None, "at": None, "reason": "not_run"},
            "tested": False, "degraded": False, "blocked": False,
            "reason": _DANGER_OPT_IN_REASON, "temp_roots": [],
            "profile_sha256": None, "requested": "danger-full-access",
            "platform": platforms.normalize_system(),
            "writes_outside": "unbounded", "delete_outside": "unbounded",
            "network": "unrestricted",
        }
    elif mode == "off":
        # An explicit opt-out needs no probe and must not touch the host.
        now = time.time()
        status = {
            "schema": 1, "kind": "none", "enforcement": "none", "abi": None,
            "probe": {"ok": False, "at": now, "ttl": _probe_ttl(cfg),
                      "reason": "policy_opt_out"},
            "canary": {"ok": None, "at": None, "reason": "not_run"},
            "tested": False, "degraded": True, "blocked": True,
            "reason": "policy_opt_out", "temp_roots": [],
            "profile_sha256": None, "requested": mode, "platform":
                platforms.normalize_system(),
            "writes_outside": "not_claimed",
            "delete_outside": "not_claimed", "network": "unrestricted",
        }
    else:
        status = sandbox_status(cfg=cfg)
        status = _copy_status(status)
    if not status.get("kind"):
        status["kind"] = ("seatbelt" if status.get("available")
                          else "none")
    if status.get("kind") != "none" and not status.get("enforcement"):
        status["enforcement"] = "enforced"
    status["requested"] = ("danger-full-access"
                             if danger_opt_in else mode)
    blocked = False
    if danger_opt_in:
        status["blocked"] = False
        status["degraded"] = False
        status["reason"] = _DANGER_OPT_IN_REASON
        status["writes_outside"] = "unbounded"
        status["delete_outside"] = "unbounded"
    elif mode == "off":
        status["blocked"] = True
        status["kind"] = "none"
        status["enforcement"] = "none"
        status["tested"] = False
        status["degraded"] = True
        status["writes_outside"] = "not_claimed"
        status["delete_outside"] = "not_claimed"
        status["reason"] = "policy_opt_out"
    elif status.get("enforcement") != "enforced":
        if mode == "require":
            blocked = True
            status["blocked"] = True
            if not status.get("reason"):
                status["reason"] = "sandbox_unavailable"
            elif "sandbox" not in str(status.get("reason")).lower():
                status["reason"] = "sandbox_unavailable: %s" % status["reason"]
        else:
            status["reason"] = status.get("reason") or "sandbox_unavailable"
    status["sandbox_header"] = _sandbox_header(status)
    if danger_opt_in:
        enabled = False
    elif mode == "off":
        enabled = False
    else:
        enabled = mode == "require" or status.get("enforcement") == "enforced"
    return {
        "os_sandbox": bool(enabled),
        "os_sandbox_mode": mode,
        "os_sandbox_policy": status,
        "os_sandbox_blocked": bool(blocked),
        "blocked": bool(blocked),
        "degraded": status.get("enforcement") != "enforced",
        "enforcement": status.get("enforcement") or "none",
        "kind": status.get("kind") or "none",
        "reason": status.get("reason"),
        "canary": dict(status.get("canary") or {}),
        "sandbox_header": status["sandbox_header"],
    }


def sandbox_header(policy):
    """Return the one-line header for a resolved policy or status object."""
    if isinstance(policy, dict) and "os_sandbox_policy" in policy:
        status = dict(policy.get("os_sandbox_policy") or {})
        if policy.get("blocked"):
            status["blocked"] = True
        return status.get("sandbox_header") or _sandbox_header(status)
    if isinstance(policy, dict):
        return policy.get("sandbox_header") or _sandbox_header(policy)
    return "UNSANDBOXED; no boundary"


def normalize_os_sandbox_record(raw):
    """Normalize a legacy/modern run-record ``os_sandbox`` object; never raises."""
    if not isinstance(raw, dict):
        return {
            "schema": 0, "kind": "none", "enforcement": "none", "abi": None,
            "probe": {"ok": False, "at": None, "ttl": None, "reason": None},
            "canary": {"ok": None, "at": None, "reason": "not_run"},
            "tested": False, "degraded": True, "blocked": False,
            "reason": "not recorded", "temp_roots": [], "profile_sha256": None,
            "requested": "auto", "platform": None, "writes_outside": "unverified",
            "delete_outside": "unverified", "network": "unrestricted",
            "sandbox_header": "UNSANDBOXED; no boundary recorded",
        }
    if raw.get("schema") == 1 and "kind" in raw:
        return _copy_status(raw)
    wrapped = bool(raw.get("wrapped"))
    return {
        "schema": 0,
        "kind": "seatbelt" if wrapped else "none",
        # A legacy ``wrapped`` flag is intent, not proof: schema 0 always
        # reports no enforcement until a probe/canary actually ran.
        "enforcement": "none",
        "degraded": True,
        "abi": None,
        "probe": {"ok": False, "at": None, "ttl": None,
                  "reason": raw.get("reason")},
        "canary": {"ok": None, "at": None, "reason": "not_run"},
        "tested": False,
        "degraded": True,
        "blocked": False,
        "reason": raw.get("reason"),
        "temp_roots": [],
        "profile_sha256": None,
        "requested": "auto",
        "platform": None,
        "writes_outside": "unverified",
        "delete_outside": "unverified",
        "network": "unrestricted",
        "sandbox_header": ("legacy wrapped record; enforcement unverified"
                           if wrapped else
                           "UNSANDBOXED; legacy record"),
    }


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

    sandbox_state = sandbox_policy(cfg, args=args, reviewer=reviewer)
    sandbox_status_obj = sandbox_state["os_sandbox_policy"]
    support = {
        "kind": sandbox_status_obj.get("kind") or "none",
        "available": (sandbox_status_obj.get("kind") not in (None, "none")
                      and sandbox_status_obj.get("enforcement")
                      not in (None, "none")),
        "enforcement": sandbox_status_obj.get("enforcement") or "none",
        "abi": sandbox_status_obj.get("abi"),
        "reason": sandbox_status_obj.get("reason"),
    }
    return {
        "preset": _preset_for_mode(sandbox),
        "sandbox": sandbox,
        "approval": approval,
        "os_sandbox": bool(sandbox_state["os_sandbox"]),
        "os_sandbox_mode": sandbox_state["os_sandbox_mode"],
        "os_sandbox_policy": sandbox_state["os_sandbox_policy"],
        "os_sandbox_blocked": sandbox_state["os_sandbox_blocked"],
        "sandbox_header": sandbox_state["sandbox_header"],
        "sandbox_canary": _cfg_get(cfg, "sandbox_canary", "auto"),
        "sandbox_extra_writes": _cfg_get(cfg, "sandbox_extra_writes", []),
        "linux_min_abi": _cfg_get(cfg, "linux_min_abi", 3),
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


def _credential_paths(workspace):
    home = os.path.realpath(os.path.expanduser("~"))
    return {
        os.path.realpath(os.path.join(home, ".dsh", ".credentials.yaml")),
        os.path.realpath(os.path.join(home, ".dsh", ".credentials.yml")),
        os.path.realpath(os.path.join(workspace or os.getcwd(),
                                      ".credentials.yaml")),
    }


def validate_extra_writes(extra_writes, workspace):
    """Return the safe, canonical, de-duplicated extra write roots (max 32).

    Rejects ``/``, the home directory, the managed ``~/.dsh`` root and any
    directory that is an ancestor of a credential document.  A reviewer's
    private DSH home is outside the workspace and is intentionally allowed.
    """
    ws = _canonical(workspace or os.getcwd())
    home = os.path.realpath(os.path.expanduser("~"))
    forbidden = {os.path.realpath("/"), home,
                 os.path.realpath(os.path.join(home, ".dsh"))}
    credentials = _credential_paths(ws)
    result = []
    seen = set()
    for raw in list(extra_writes or ())[:32]:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            real = _canonical(raw)
        except (OSError, ValueError, TypeError):
            continue
        if real in forbidden or real in seen:
            continue
        unsafe = False
        for credential in credentials:
            try:
                if os.path.commonpath([real, credential]) == real:
                    unsafe = True
                    break
            except (OSError, ValueError):
                continue
        if unsafe:
            continue
        seen.add(real)
        result.append(real)
    return result


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
    for real in validate_extra_writes(extra_writes, ws):
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


def _outside_canary_dir(workspace):
    """Return a writable temp directory outside *workspace*, or ``None``."""
    ws = _canonical(workspace or os.getcwd())
    candidates = []
    try:
        candidates.append(tempfile.mkdtemp(prefix="dsh-sandbox-canary-"))
    except OSError:
        pass
    try:
        candidates.append(tempfile.mkdtemp(
            prefix="dsh-sandbox-canary-", dir=os.path.expanduser("~")))
    except OSError:
        pass
    for candidate in candidates:
        real = _canonical(candidate)
        try:
            inside = os.path.commonpath([real, ws]) == ws
        except ValueError:
            inside = False
        if not inside:
            return real
        shutil.rmtree(real, ignore_errors=True)
    return None


def _run_canary(prefix, workspace, key):
    """Prove the built wrapper denies a write outside the workspace."""
    now = time.time()
    cached = _CANARY_CACHE.get(key)
    if cached is not None and (now - cached["at"]) < _probe_ttl(None):
        return dict(cached)
    root = _outside_canary_dir(workspace)
    if root is None:
        result = {"ok": False, "at": now, "reason": "no_outside_canary_dir"}
        _CANARY_CACHE[key] = result
        return dict(result)
    target = os.path.join(root, "canary.txt")
    code = None
    stderr = ""
    try:
        proc = subprocess.run(
            prefix + [sys.executable, "-c",
                      "import sys; open(sys.argv[1], 'w').write('x')",
                      target],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=15.0)
        code = int(proc.returncode)
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
    except subprocess.TimeoutExpired:
        code = None
        stderr = "canary timeout"
    except Exception as exc:
        code = None
        stderr = exc.__class__.__name__
    exists = os.path.exists(target)
    try:
        shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass
    if exists or code == 0:
        reason = "canary_write_allowed"
        ok = False
    elif (code in (125, 126, 127) or "SANDBOX_" in stderr
          or "No module named" in stderr
          or "ModuleNotFoundError" in stderr):
        ok = False
        reason = "canary_launcher_failed"
    elif code is None:
        ok = False
        reason = "canary_timeout"
    else:
        ok = True
        reason = None
    result = {"ok": ok, "at": now, "reason": reason}
    _CANARY_CACHE[key] = result
    return dict(result)


def _launcher_argv(module, policy_json, argv):
    return [sys.executable, "-m", module, "--policy", policy_json,
            "--"] + list(argv)


def _bwrap_argv(argv, workspace, mode, writes):
    binary = shutil.which("bwrap")
    if not binary:
        return None
    ws = _canonical(workspace or os.getcwd())
    prefix = [binary, "--ro-bind", "/", "/", "--dev", "/dev"]
    if mode != "read-only":
        prefix += ["--bind", ws, ws]
    else:
        review = os.path.join(ws, "REVIEW.md")
        if os.path.exists(review):
            prefix += ["--bind", review, review]
    for extra in writes:
        real = _canonical(extra)
        if os.path.isdir(real):
            prefix += ["--bind", real, real]
        elif os.path.exists(real):
            prefix += ["--bind", real, real]
    prefix += ["--chdir", ws, "--die-with-parent", "--"]
    return prefix + list(argv)


def wrap_command(cmd, policy, workspace, tmpdir=None, extra_writes=(), dsh_home=None):
    """Wrap ``cmd`` with the enforced OS boundary selected by ``policy``.

    Returns ``(argv, meta)``. This function never raises.  Seatbelt on macOS,
    the Landlock launcher on Linux and the write-restricted launcher on
    Windows are selected from the resolved status object.  In ``require`` mode
    a missing or failed wrapper sets ``meta["blocked"]=true`` and the caller
    must not spawn.
    """
    argv = []
    policy = policy or {}
    mode = str(policy.get("os_sandbox_mode") or "auto")
    status = policy.get("os_sandbox_policy") or {}
    kind = status.get("kind") or "none"
    blocked = False
    meta = {
        "wrapped": False,
        "schema": 1,
        "kind": kind,
        "enforcement": status.get("enforcement") or "none",
        "abi": status.get("abi"),
        # The status object carries the accurate request (danger-full-access
        # for an explicit opt-in, otherwise auto/require/off).
        "requested": status.get("requested") or mode,
        "blocked": False,
        "canary": {"ok": None, "at": None, "reason": "not_run"},
        "tested": bool(status.get("tested")),
        "degraded": True,
        "reason": None,
        "network": "unrestricted",
        "writes_outside": status.get("writes_outside") or "unverified",
        "delete_outside": status.get("delete_outside") or "unverified",
        "probe": status.get("probe") or {"ok": False, "reason": None},
    }
    if str(policy.get("sandbox") or "") == "danger-full-access":
        # Explicit per-job opt-in: never wrap, and say so in the record.
        meta.update({
            "wrapped": False, "kind": "none", "enforcement": "none",
            "blocked": False, "degraded": False,
            "reason": _DANGER_OPT_IN_REASON,
            "sandbox_header": _DANGER_OPT_IN_HEADER,
            "writes_outside": "unbounded", "delete_outside": "unbounded",
        })
        return (([cmd] if isinstance(cmd, str) else list(cmd or ())), meta)
    try:
        argv = [cmd] if isinstance(cmd, str) else list(cmd or ())
        if not policy.get("os_sandbox"):
            if mode == "require":
                blocked = True
                meta.update(blocked=True,
                            reason="sandbox_unavailable: os sandbox disabled")
            else:
                # auto with no enforced tier (or an explicit off) still
                # records the honest degraded object and UNSANDBOXED header.
                meta.update({
                    "enforcement": status.get("enforcement") or "none",
                    "kind": status.get("kind") or "none",
                    "abi": status.get("abi"),
                    "degraded": True,
                    "tested": bool(status.get("tested")),
                    "writes_outside": status.get("writes_outside")
                    or "unverified",
                    "delete_outside": status.get("delete_outside")
                    or "unverified",
                    "sandbox_header": status.get("sandbox_header")
                    or "UNSANDBOXED; no boundary",
                    "reason": status.get("reason")
                    or "os sandbox disabled by policy",
                })
            return argv, meta
        if not status:
            status = sandbox_status(force=True)
            kind = status.get("kind") or "none"
            meta["kind"] = kind
            meta["enforcement"] = status.get("enforcement") or "none"
        if kind == "none":
            blocked = mode == "require"
            reason = status.get("reason") or "sandbox_unavailable"
            if blocked and "sandbox" not in str(reason).lower():
                reason = "sandbox_unavailable: %s" % reason
            meta.update(blocked=blocked, reason=reason)
            return argv, meta

        sandbox_mode = policy.get("sandbox") or "read-only"
        writes = validate_extra_writes(
            list(extra_writes or ())
            + list(policy.get("sandbox_extra_writes") or ()), workspace)
        if tmpdir:
            writes.append(_canonical(tmpdir))
        hash_payload = None
        prefix = None
        prefix_includes_argv = False
        if kind == "seatbelt":
            if sys.platform != "darwin" or not seatbelt_usable():
                blocked = mode == "require"
                meta.update(blocked=blocked,
                            reason=_SEATBELT_PROBE.get("reason")
                            or "sandbox-exec not usable")
                return argv, meta
            profile = seatbelt_profile(
                workspace, mode=sandbox_mode, extra_writes=writes,
                dsh_home=dsh_home)
            prefix = ["sandbox-exec", "-p", profile]
            hash_payload = profile
        elif kind == "landlock":
            policy_json = json.dumps({
                "workspace": _canonical(workspace or os.getcwd()),
                "mode": sandbox_mode,
                "tmpdir": _canonical(tmpdir) if tmpdir else None,
                "extra_writes": writes,
                "dsh_home": _canonical(dsh_home) if dsh_home else None,
                "min_abi": int(policy.get("linux_min_abi") or 3),
                "required": mode == "require",
            }, sort_keys=True, separators=(",", ":"))
            prefix = _launcher_argv("deepseek_mcp.sandbox_linux",
                                    policy_json, argv)
            prefix_includes_argv = True
            hash_payload = policy_json
        elif kind == "windows-write-restricted":
            policy_json = json.dumps({
                "workspace": _canonical(workspace or os.getcwd()),
                "mode": sandbox_mode,
                "tmpdir": _canonical(tmpdir) if tmpdir else None,
                "extra_writes": writes,
                "dsh_home": _canonical(dsh_home) if dsh_home else None,
                "required": mode == "require",
            }, sort_keys=True, separators=(",", ":"))
            prefix = _launcher_argv("deepseek_mcp.sandbox_windows",
                                    policy_json, argv)
            prefix_includes_argv = True
            hash_payload = policy_json
        elif kind == "bwrap":
            prefix = _bwrap_argv(argv, workspace, sandbox_mode, writes)
            if prefix is None:
                blocked = mode == "require"
                meta.update(blocked=blocked, reason="bwrap_unavailable")
                return argv, meta
            prefix_includes_argv = True
            hash_payload = json.dumps(prefix[:-len(argv)] if argv else prefix,
                                      sort_keys=True)
        else:
            blocked = mode == "require"
            meta.update(blocked=blocked, reason="unknown_sandbox_kind")
            return argv, meta

        digest = hashlib.sha256(
            str(hash_payload or "").encode("utf-8", "surrogatepass")
        ).hexdigest()
        canary_setting = str(policy.get("sandbox_canary") or "off").lower()
        canary = {"ok": None, "at": None, "reason": "disabled"}
        if canary_setting != "off":
            canary = _run_canary(prefix, workspace, digest)
        else:
            # A direct legacy caller that did not resolve a policy still gets
            # an unverified (not claimed-enforced) wrapper.
            canary = {"ok": None, "at": time.time(),
                      "reason": "canary_disabled"}
        meta.update({
            "wrapped": True,
            "sandbox": sandbox_mode,
            "workspace": _canonical(workspace or os.getcwd()),
            "profile_sha256": digest,
            "canary": canary,
            "temp_roots": [_canonical(tmpdir)] if tmpdir else [],
        })
        if canary.get("ok") is False:
            blocked = mode == "require"
            meta.update(wrapped=False, blocked=blocked, degraded=True,
                        enforcement="degraded",
                        reason="canary_failed: %s" % canary.get("reason"))
            meta["sandbox_header"] = (
                "UNSANDBOXED; canary failed (%s); writes=unverified; "
                "delete=unverified; network=unrestricted"
                % canary.get("reason"))
            return argv, meta
        meta.update({
            "enforcement": "enforced" if canary.get("ok") else "degraded",
            "degraded": not bool(canary.get("ok")),
            "reason": None if canary.get("ok") else "canary_not_run",
        })
        header_status = dict(status)
        header_status.update({
            "kind": kind, "enforcement": meta["enforcement"],
            "abi": meta.get("abi"), "blocked": False,
            "delete_outside": status.get("delete_outside") or "unverified",
        })
        meta["sandbox_header"] = _sandbox_header(header_status)
        if prefix_includes_argv:
            return prefix, meta
        return prefix + list(argv), meta
    except Exception as exc:
        blocked = mode == "require"
        meta.update({
            "wrapped": False,
            "blocked": blocked,
            "degraded": True,
            "enforcement": "none",
            "reason": "os sandbox setup failed: %s" % exc.__class__.__name__,
        })
        return argv, meta


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
