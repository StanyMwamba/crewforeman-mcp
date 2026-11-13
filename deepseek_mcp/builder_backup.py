"""Backup-builder configuration, switch gate and run-record helpers (3.12.0 R4).

This module is deliberately free of harness imports (standard library plus the
classified helpers in :mod:`deepseek_mcp.reviewer_backup`) so the switch table
can be unit-tested without a paid job.  The caller owns execution, admission
and settlement; these helpers only answer "may we switch?", "what is the
ceiling?" and "what goes in ``run['backup_builder']``?".

Provider derivation is best-effort: an explicit ``backup_builder_provider``
wins, then a future :mod:`deepseek_mcp.model_policy` target, then a small
model/prefix table.  When nothing matches the provider is ``"unknown"``; no
network or catalog probe is made here.
"""

from __future__ import annotations

import os
import re

try:
    from . import health as _health
except Exception:  # pragma: no cover - broken install only
    _health = None

try:
    from . import reviewer_backup as _reviewer_backup
except Exception:  # pragma: no cover - defensive only
    _reviewer_backup = None

try:
    from .api import redact_secrets as _redact_secrets
except Exception:  # pragma: no cover - defensive only

    def _redact_secrets(text):
        return str(text or "")


DEFAULT_MAX_COST_USD = 1.0
DEFAULT_MIN_ATTEMPTS = 2
SKIP_REASONS = frozenset(("side_effects", "disabled", "cost_unknown", "budget"))
_SWITCH_IMMEDIATELY = frozenset(("credit/budget", "auth"))
_RETRY_AFTER_MIN = frozenset(("service unavailable (5xx)", "stream dropped"))
_DEFAULT_CEILING = 1.0

_PROVIDER_HINTS = (
    ("deepseek", "deepseek"),
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("sonnet", "anthropic"),
    ("gpt", "openai"),
    ("openai", "openai"),
    ("gemini", "google"),
    ("kimi", "moonshot"),
    ("moonshot", "moonshot"),
    ("xiaomi", "mimo"),
    ("mimo", "mimo"),
)
_ABS_PATH_RE = re.compile(
    r"(?<![\w.])/(?:Users|home|tmp|private|var|opt|root)/[^\s'\"]+"
)
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s'\"]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SPACE_RE = re.compile(r"\s+")


def _get(cfg, name, default=None):
    """Read a key from a mapping/Config or a SimpleNamespace-like object."""
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            value = getter(name, default)
        except TypeError:
            value = getter(name)
        return default if value is None else value
    value = getattr(cfg, name, default)
    return default if value is None else value


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    try:
        return str(value)
    except Exception:
        return ""


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _expand(value):
    text = _text(value).strip()
    if not text:
        return ""
    try:
        return os.path.expanduser(os.path.expandvars(text))
    except Exception:
        return text


def _derive_provider(model):
    """Return a provider name from model/prefix hints, else ``"unknown"``."""
    text = _text(model).strip()
    if not text:
        return "unknown"
    if "/" in text:
        prefix = text.split("/", 1)[0].strip().lower()
        if prefix:
            return prefix
    lower = text.lower()
    for marker, provider in _PROVIDER_HINTS:
        if marker in lower:
            return provider
    return "unknown"


def _settings_from_cfg(cfg):
    model = _text(_get(cfg, "backup_builder_model", "")).strip()
    if not model:
        return None
    provider = _text(_get(cfg, "backup_builder_provider", "")).strip()
    cap = _number(_get(cfg, "backup_builder_max_cost_usd",
                        DEFAULT_MAX_COST_USD), DEFAULT_MAX_COST_USD)
    if cap < 0:
        cap = 0.0
    try:
        min_attempts = int(_get(cfg, "backup_builder_min_attempts",
                                DEFAULT_MIN_ATTEMPTS))
    except (TypeError, ValueError):
        min_attempts = DEFAULT_MIN_ATTEMPTS
    min_attempts = max(1, min_attempts)
    return {
        "bin": _expand(_get(cfg, "dsh_bin", "")),
        "provider": provider or _derive_provider(model),
        "model": model,
        "vendor": provider or _derive_provider(model),
        "max_cost_usd": cap,
        "min_attempts": min_attempts,
    }


def configured(cfg):
    """Return the backup-builder settings, or ``None`` when disabled."""
    return _settings_from_cfg(cfg)


def _target_from_model_policy(cfg, settings):
    """Call ``model_policy.resolve_target`` when a compatible one exists."""
    try:
        from . import model_policy as _model_policy
    except Exception:
        return None
    resolver = getattr(_model_policy, "resolve_target", None)
    if not callable(resolver):
        return None
    try:
        import inspect
        params = inspect.signature(resolver).parameters
        kwargs = {}
        if "cfg" in params:
            kwargs["cfg"] = cfg
        if "model" in params:
            kwargs["model"] = settings["model"]
        if "provider" in params:
            kwargs["provider"] = settings["provider"]
        if not kwargs:
            return None
        target = resolver(**kwargs)
    except Exception:
        return None
    if isinstance(target, dict):
        data = target
    else:
        data = {}
        for key in ("provider", "vendor", "model"):
            data[key] = getattr(target, key, "")
    merged = dict(settings)
    for key in ("provider", "vendor", "model"):
        value = _text(data.get(key)).strip()
        if value:
            merged[key] = value
    return merged


def resolve_backup_target(cfg):
    """Return the backup target, resolving through model_policy when present."""
    settings = _settings_from_cfg(cfg)
    if settings is None:
        return None
    resolved = _target_from_model_policy(cfg, settings)
    return resolved if resolved is not None else settings


def probe_model(settings, cfg=None):
    """Return the model for a backup preflight/readiness ping.

    A MiMo backup target pings the probe default (``mimo_probe_model``,
    else ``xiaomi/mimo-v2.6-flash``); every other vendor keeps its own
    model, which is the pre-existing preflight behavior.  Delegates to
    :mod:`deepseek_mcp.reviewer_backup` so the vendor rule lives in one
    place.
    """
    if _reviewer_backup is not None:
        try:
            fn = getattr(_reviewer_backup, "probe_model", None)
            if callable(fn):
                return fn(settings, cfg)
        except Exception:
            pass
    if isinstance(settings, dict):
        return _text(settings.get("model")).strip()
    return _text(getattr(settings, "model", "")).strip()


def effective_ceiling(backup_cap, whole_remaining=None, cost_known=True):
    """Return the backup's own cap, reduced by a positive whole-job remainder.

    ``0`` disables the backup and is preserved.  A known whole-job remainder of
    zero (or less) also leaves no room.  Unknown cost returns ``None``: an
    unpriced backup cannot be proven below a ceiling.
    """
    if not cost_known:
        return None
    cap = max(0.0, _number(backup_cap, DEFAULT_MAX_COST_USD))
    if cap == 0:
        return 0.0
    if whole_remaining is None:
        return cap
    remaining = _number(whole_remaining, 0.0)
    if remaining <= 0:
        return 0.0
    return min(cap, remaining)


def _decision(switch, reason_class):
    return {"switch": bool(switch), "reason_class": reason_class}


def _retry_policy(cfg, attempts, min_attempts, retry_policy):
    if retry_policy is not None:
        return retry_policy
    if _health is None:
        return None
    # A switch is allowed only while the retry policy says the main builder
    # still has budget/deadline room; one extra slot keeps the threshold
    # attempt itself "allowed" rather than reported as exhausted.
    max_attempts = max(attempts + 1, min_attempts + 1)
    return _health.RetryPolicy("builder", max_attempts=max_attempts)


def switch_decision(execution, cfg, attempts=1, writes=False, used=False,
                    remaining_seconds=None, ceiling=None, spent=None,
                    cost_known=True, retry_policy=None):
    """Decide whether the main builder may hand off to the backup builder.

    The switch table is exact: successful/quality outcomes (including stalls,
    timeouts, cancellation, low or missing score) never switch; ``credit/budget``
    and ``auth`` switch immediately; 5xx and dropped streams only after
    ``backup_builder_min_attempts`` and only when ``RetryPolicy.should_retry``
    allows it.  Writes always skip with ``side_effects``; a zero cap is
    ``disabled``; unknown cost is ``cost_unknown``; an already-used backup (or a
    spent whole-job ceiling) never switches twice.
    """
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 1

    if used:
        return _decision(False, None)
    if writes:
        return _decision(False, "side_effects")

    settings = _settings_from_cfg(cfg)
    if settings is None:
        return _decision(False, "disabled")
    if settings["max_cost_usd"] <= 0:
        return _decision(False, "disabled")
    if not cost_known:
        return _decision(False, "cost_unknown")
    if ceiling is not None and spent is not None:
        if _number(spent, 0.0) >= _number(ceiling, 0.0):
            return _decision(False, "budget")

    if _reviewer_backup is None:
        return _decision(False, None)
    reason = _reviewer_backup.switch_class(execution)
    if reason is None:
        return _decision(False, None)
    if reason in _SWITCH_IMMEDIATELY:
        return _decision(True, reason)
    if reason not in _RETRY_AFTER_MIN:
        return _decision(False, None)
    if attempts < settings["min_attempts"]:
        return _decision(False, None)

    policy = _retry_policy(cfg, attempts, settings["min_attempts"],
                           retry_policy)
    if policy is None:
        return _decision(False, None)
    failure_class = "provider_5xx" if reason.startswith("service") else "network"
    try:
        allowed, _why = policy.should_retry(
            attempts,
            failure_class,
            remaining_seconds=remaining_seconds,
            spent=spent,
            ceiling=ceiling,
            cost_known=cost_known,
        )
    except Exception:
        allowed = False
    return _decision(bool(allowed), reason if allowed else None)


def _tool_name(item):
    if isinstance(item, dict):
        for key in ("tool", "name", "type", "kind"):
            value = _text(item.get(key)).strip()
            if value:
                return value.lower()
        return ""
    return _text(item).strip().lower()


def _write_tool_used(tools_used):
    if not tools_used:
        return False
    if isinstance(tools_used, (str, bytes)):
        tools_used = [tools_used]
    for item in tools_used:
        name = _tool_name(item)
        if not name:
            continue
        if any(token in name for token in ("write", "edit", "patch")):
            return True
    return False


def _snapshot(workspace):
    snapshot = {}
    root = _text(workspace).strip()
    if not root or not os.path.isdir(root):
        return snapshot
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                stat = os.stat(path)
            except OSError:
                continue
            snapshot[rel] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _fallback_guard(builder_before, workspace):
    if builder_before is None:
        return True
    before = builder_before if isinstance(builder_before, dict) else {}
    after = _snapshot(workspace)
    return before == after


def side_effect_guard_result(builder_before, workspace, tools_used=None):
    """Return ``{'clean': bool, 'reason': 'side_effects'|None}``.

    A write/edit tool entry is dirty even when mtimes are coarse.  The shared
    :func:`health.side_effect_guard` is used when available, otherwise a
    mtime+size snapshot is compared.
    """
    if _write_tool_used(tools_used):
        return {"clean": False, "reason": "side_effects"}
    clean = True
    if _health is not None and hasattr(_health, "side_effect_guard"):
        try:
            clean, _changes = _health.side_effect_guard(
                workspace, builder_before, phase="builder-backup", writing=True
            )
        except Exception:
            clean = _fallback_guard(builder_before, workspace)
    else:
        clean = _fallback_guard(builder_before, workspace)
    return {"clean": bool(clean), "reason": None if clean else "side_effects"}


def _identity(candidate):
    if not isinstance(candidate, dict):
        candidate = {
            "vendor": getattr(candidate, "vendor", ""),
            "provider": getattr(candidate, "provider", ""),
            "model": getattr(candidate, "model", ""),
        }
    vendor = _text(candidate.get("vendor")).strip()
    provider = _text(candidate.get("provider")).strip()
    model = _text(candidate.get("model")).strip() or "unknown"
    return vendor or provider or "unknown", model


def header_line(candidate, reason_class):
    """Return the exact ``_result_text`` fragment, leading separator included."""
    vendor, model = _identity(candidate)
    reason = _text(reason_class).strip() or "none"
    return " | backup builder: %s/%s (main failed: %s)" % (vendor, model, reason)


def _redact(text):
    """Redact secrets/paths/emails and bound the result to 500 characters."""
    value = _text(text)
    try:
        value = _redact_secrets(value)
    except Exception:
        pass
    value = _ABS_PATH_RE.sub("<path>", value)
    value = _WIN_PATH_RE.sub("<path>", value)
    value = _EMAIL_RE.sub("<email>", value)
    value = _SPACE_RE.sub(" ", value).strip()
    if len(value) > 500:
        value = value[:500]
    return value


def record(candidate, reason_class, status="skipped", main_error="",
           attempts=0, ceiling_usd=None, cost_usd=None, cost_known=False):
    """Build the fixed ``run['backup_builder']`` object from DESIGN section 7.3.

    ``bin`` is never copied.  Unknown cost keeps ``cost_usd`` null and
    ``cost_known`` false; ``main_error`` is redacted and path-free.
    """
    vendor, model = _identity(candidate)
    status = _text(status).strip().lower() or "skipped"
    if status not in ("ok", "failed", "skipped"):
        status = "skipped"
    known = bool(cost_known)
    settled = float(cost_usd) if known and cost_usd is not None else None
    try:
        attempts = max(0, int(attempts))
    except (TypeError, ValueError):
        attempts = 0
    reason = _text(reason_class).strip() or None
    line = "Built by: backup (%s/%s) \u2014 main failed: %s" % (
        vendor, model, reason or "none")
    return {
        "used": status in ("ok", "failed"),
        "status": status,
        "vendor": vendor,
        "model": model,
        "reason_class": reason,
        "main_error": _redact(main_error),
        "attempts": attempts,
        "ceiling_usd": None if ceiling_usd is None else _number(ceiling_usd, 0.0),
        "settlement": {"cost_usd": settled, "cost_known": known},
        "line": line,
    }


def error_message(execution, cfg):
    """Attribute a backup-builder failure, using the reviewer classifier."""
    if _reviewer_backup is None:
        return "unknown/unknown builder failed (attribution unavailable)"
    model = _text(_get(cfg, "backup_builder_model", "")).strip()
    provider = _text(_get(cfg, "backup_builder_provider", "")).strip()
    if not provider:
        provider = _derive_provider(model)
    # reviewer_backup.identity reads its own key names; adapt rather than
    # changing that module (and never leak a binary path or key value).
    view = {
        "backup_reviewer_vendor": provider,
        "backup_reviewer_model": model,
        "dsh_model": _text(_get(cfg, "dsh_model", "")).strip(),
    }
    try:
        return _reviewer_backup.error_message(view, execution, role="builder")
    except Exception:
        return "unknown/unknown builder failed (attribution unavailable)"


def uses_backup_once(used):
    """True when the caller has already consumed its one backup switch."""
    return bool(used)
