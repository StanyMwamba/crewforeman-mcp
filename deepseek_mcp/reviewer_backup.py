"""Pure helpers for the optional backup reviewer (3.10.0 part A).

The harness integration is owned by the lead.  This module deliberately has no
harness imports and uses the standard library only so it can be unit-tested in
isolation.
"""

import os
import re

CONFIG_KEYS = (
    "backup_dsh_bin",
    "backup_reviewer_vendor",
    "backup_reviewer_model",
)

# Most specific wrapper-name markers first.
_VENDOR_MARKERS = (
    ("anthropic", "Anthropic"),
    ("claude", "Anthropic"),
    ("sonnet", "Anthropic"),
    ("moonshot", "Kimi (Moonshot)"),
    ("kimi", "Kimi (Moonshot)"),
    ("openai", "OpenAI"),
    ("gpt", "OpenAI"),
    ("deepseek", "DeepSeek"),
    ("gemini", "Google"),
)

_SUCCESS_STATUSES = frozenset(("ok", "done", "completed", "success", "succeeded"))
_NO_SWITCH_STATUSES = frozenset(
    (
        "stalled",
        "timed_out",
        "timedout",
        "timeout",
        "deadline",
        "time_limit",
        "cancelled",
        "canceled",
        "interrupted",
    )
)

# Deadlines/stalls/cancellations are not backup-reviewer switch reasons.
_DEADLINE_RE = re.compile(
    r"deadline|time[_ ]limit|timed[_ ]?out|cancelled|canceled|stalled|interrupted"
)

# credit / budget
_CREDIT_RE = re.compile(
    r"insufficient[_ ](?:balance|quota|funds|credit)"
    r"|insufficient_quota"
    r"|no[_ ]balance"
    r"|out of credit"
    r"|credit balance"
    r"|exceeded your current quota"
    r"|payment required"
    r"|\bbilling\b"
    r"|\bbudget\b"
    r"|\bquota\b"
    r"|balance (?:is )?(?:too low|exhausted|zero|empty)"
)
_HTTP_402_RE = re.compile(r"\b402\b")
_HTTP_429_RE = re.compile(r"\b429\b")
_429_MONEY_RE = re.compile(r"quota|budget|balance|credit|billing|insufficient|payment")

# auth / invalid key
_AUTH_RE = re.compile(
    r"\b(?:401|403)\b"
    r"|invalid (?:api )?key"
    r"|incorrect api key"
    r"|expired (?:api )?key"
    r"|unauthenticated"
    r"|\bauthentication\b"
    r"|\bunauthorized\b"
    r"|\bforbidden\b"
)

# service unavailable / 5xx
_SERVICE_RE = re.compile(
    r"\b5\d\d\b"
    r"|service unavailable"
    r"|\boverloaded\b"
    r"|internal server error"
    r"|bad gateway"
    r"|gateway timeout"
)

# dropped stream / closed connection
_STREAM_RE = re.compile(
    r"flush on a closed handle"
    r"|closed handle"
    r"|stream_closed"
    r"|econnreset"
    r"|socket hang up"
    r"|connection reset"
    r"|stream (?:dropped|drop|closed)"
)

_TRANSPORT_FIELDS = (
    "error",
    "error_message",
    "message",
    "detail",
    "reason",
    "stderr",
    "stdout",
    "output",
    "result",
    "text",
)


def _value(cfg, name, default=""):
    """Read a config key from a Config object or a plain mapping."""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        value = cfg.get(name, default)
    else:
        value = getattr(cfg, name, default)
    return default if value is None else value


def _text(value):
    """Flatten execution/config values to searchable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    return str(value)


def _execution_model(execution):
    models = _value(execution, "models", None)
    if isinstance(models, str):
        return models.strip()
    if isinstance(models, (list, tuple)):
        for item in reversed(models):
            text = _text(item).strip()
            if text:
                return text
        return ""
    if isinstance(models, dict):
        # ``models`` maps model name -> usage bucket; the names are the
        # identity signal, never the numeric buckets.
        for item in reversed(list(models.keys())):
            text = _text(item).strip()
            if text:
                return text
        return ""
    return _text(models).strip()


def _infer_vendor(path):
    base = os.path.basename(_text(path)).lower()
    if not base:
        return None
    for marker, vendor in _VENDOR_MARKERS:
        if marker in base:
            return vendor
    return None


def configured(cfg):
    """Return the backup-reviewer settings, or None when no backup is set."""
    raw_bin = _text(_value(cfg, "backup_dsh_bin")).strip()
    if not raw_bin:
        return None
    expanded = os.path.expanduser(os.path.expandvars(raw_bin))
    vendor = _text(_value(cfg, "backup_reviewer_vendor")).strip() or None
    model = _text(_value(cfg, "backup_reviewer_model")).strip() or None
    return {"bin": expanded, "vendor": vendor, "model": model}


def identity(cfg, execution=None):
    """Return the display identity for the backup reviewer."""
    vendor = _text(_value(cfg, "backup_reviewer_vendor")).strip()
    if not vendor:
        vendor = _infer_vendor(_value(cfg, "backup_dsh_bin")) or "unknown"

    model = _text(_value(cfg, "backup_reviewer_model")).strip()
    if not model:
        model = _execution_model(execution)
    if not model:
        model = _text(_value(cfg, "dsh_model")).strip()
    if not model:
        model = "unknown"

    return {"vendor": vendor, "model": model, "label": "%s/%s" % (vendor, model)}


def _haystack(execution):
    if isinstance(execution, str):
        return execution
    if not isinstance(execution, dict):
        return _text(execution)
    parts = [_text(execution.get("status"))]
    for field in _TRANSPORT_FIELDS:
        parts.append(_text(execution.get(field)))
    return " ".join(parts)


def switch_class(execution, retries_exhausted=False):
    """Classify a non-quality main-reviewer failure.

    Returns one of ``"credit/budget"``, ``"auth"``,
    ``"service unavailable (5xx)"``, ``"stream dropped"`` or ``None``.
    ``retries_exhausted`` is advisory only: the caller decides whether the
    bounded retries have been spent.
    """
    if execution is None:
        return None

    status = _text(_value(execution, "status", "")).strip().lower()
    # Successful executions are quality outcomes, never transport switches.
    if status in _SUCCESS_STATUSES:
        return None
    if status in _NO_SWITCH_STATUSES:
        return None

    haystack = _haystack(execution).lower()
    if _DEADLINE_RE.search(haystack):
        return None

    # Money/quota failures win over generic auth/5xx wording.
    if _CREDIT_RE.search(haystack):
        return "credit/budget"
    if _HTTP_402_RE.search(haystack):
        return "credit/budget"
    if _HTTP_429_RE.search(haystack) and _429_MONEY_RE.search(haystack):
        return "credit/budget"

    if _AUTH_RE.search(haystack):
        return "auth"

    # A dropped stream can be reported alongside an HTTP 5xx; the stream marker
    # is the more precise retry-exhaustion signal.
    if _STREAM_RE.search(haystack):
        return "stream dropped"

    if _SERVICE_RE.search(haystack):
        return "service unavailable (5xx)"

    return None


def banner(cfg, reason_class, execution=None):
    """Return the exact run-record banner for a backup review."""
    label = identity(cfg, execution)["label"]
    return "Reviewed by: backup (%s) \u2014 main failed: %s" % (label, reason_class)
