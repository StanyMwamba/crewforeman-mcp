"""Guard for tests that genuinely need the real Seatbelt wrapper.

At 3.9.2 every sandbox test mocks ``safety.seatbelt_usable``; no test spawns a
real nested ``sandbox-exec``.  This helper exists so a future test can fail
closed with an environment-only skip instead of a misleading assertion when the
suite itself runs inside an outer Harness/Seatbelt sandbox.
"""

import unittest

from deepseek_mcp import safety

_UNSET = object()
_CACHE = {"usable": _UNSET, "reason": ""}


def reset_cache():
    """Forget the cached probe (used by the guard's own tests)."""
    _CACHE["usable"] = _UNSET
    _CACHE["reason"] = ""


def _detect():
    if _CACHE["usable"] is not _UNSET:
        return bool(_CACHE["usable"])
    try:
        usable = bool(safety.seatbelt_usable())
        detail = ""
    except Exception as exc:  # pragma: no cover - defensive
        usable = False
        detail = "probe raised %s" % exc.__class__.__name__
    _CACHE["usable"] = usable
    if usable:
        _CACHE["reason"] = ""
    else:
        if not detail:
            probe = getattr(safety, "_SEATBELT_PROBE", None)
            if isinstance(probe, dict):
                detail = str(probe.get("reason") or "")
        _CACHE["reason"] = (
            "environment-only: real nested `sandbox-exec` is unavailable "
            "because this test is running inside an outer Harness/Seatbelt "
            "sandbox (sandbox-exec cannot be nested"
            + ("; %s" % detail if detail else "") + ")")
    return usable


def nested_sandbox_reason():
    """Return '' when a real sandbox can run, else the skip reason."""
    _detect()
    return _CACHE["reason"]


def require_real_sandbox():
    """Skip the calling test unless a real nested sandbox can actually run."""
    if not _detect():
        raise unittest.SkipTest(_CACHE["reason"])
