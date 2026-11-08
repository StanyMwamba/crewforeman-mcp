"""Pure helpers for the optional backup reviewer (3.10.0 part A).

The harness integration is owned by the lead.  This module deliberately has no
harness imports and uses the standard library only so it can be unit-tested in
isolation.

``error_message`` attributes a reviewer failure to the vendor that actually
ran.  It derives the label from :func:`identity` and the failure class from
:func:`switch_class`; raw error text is never accepted into the returned
sentence, so a secret cannot be echoed.  A plain HTTP 429 that is not a
credit/quota failure is reported as a rate limit.  The DeepSeek balance
sentence is reserved for a DeepSeek *vendor* identity: an unknown vendor that
merely falls back to a ``dsh_model`` such as ``deepseek-flash`` is never
attributed to DeepSeek, and nothing is inferred from the main reviewer's
config.  If attribution itself raises, a fixed unknown/unknown sentence is
returned rather than a guess.
"""

import os
import re

try:  # The API module is dependency-free, but attribution must never fail.
    from .api import redact_secrets as _redact_secrets
except Exception:  # pragma: no cover - defensive only

    def _redact_secrets(text):
        return str(text or "")


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

_VENDOR_KEYS = (
    ("moonshot", "MOONSHOT_API_KEY"),
    ("kimi", "MOONSHOT_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("claude", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("gpt", "OPENAI_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
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

_UNAFFECTED_CLAUSE = "; the DeepSeek balance is unaffected."
_DEEPSEEK_REMEDY = (
    "; DeepSeek account balance is empty: top up at "
    "https://platform.deepseek.com/top_up."
)
_ATTRIBUTION_UNAVAILABLE = (
    "unknown/unknown reviewer failed (attribution unavailable)"
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
    # 3.12 delegates to the canonical map; keep the 3.11 markers as fallback.
    try:
        from .reviewer_vendor import canonical_vendor
        vendor = canonical_vendor(base)
        if vendor:
            return vendor
    except Exception:
        pass
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


def backup_candidate(cfg):
    """Return the 3.11 backup reviewer as a normalized 3.12 candidate.

    ``None`` when no backup binary is configured.  Vendor inference is
    delegated to :mod:`deepseek_mcp.reviewer_vendor`; the legacy bin only
    becomes a candidate, never an automatic launch.
    """
    settings = configured(cfg)
    if settings is None:
        return None
    raw = {
        "name": "backup",
        "vendor": _text(_value(cfg, "backup_reviewer_vendor")).strip(),
        "model": _text(_value(cfg, "backup_reviewer_model")).strip(),
        "dsh_bin": settings["bin"],
        "legacy_wrapper": True,
    }
    try:
        from .reviewer_vendor import normalize_candidate
        candidate = normalize_candidate(raw, cfg)
    except Exception:
        return None
    if not candidate.get("model"):
        candidate["model"] = _text(_value(cfg, "dsh_model")).strip()
    return candidate


def backup_eligibility(cfg, builder_vendors, chosen_by_user=False):
    """Return ``(candidate, error)`` for the legacy post-failure backup.

    A same-vendor backup is eligible only with ``chosen_by_user=True``; an
    unknown vendor is never eligible.  ``error`` is a bounded code or None.
    """
    candidate = backup_candidate(cfg)
    if candidate is None:
        return None, "not_configured"
    if candidate.get("error"):
        return candidate, _text(candidate.get("error"))
    try:
        from .reviewer_vendor import (ReviewerVendorConflict,
                                      assert_reviewer_eligible,
                                      canonical_vendor)
        same = canonical_vendor(candidate.get("vendor")) in {
            canonical_vendor(item) for item in (builder_vendors or ())
        }
        assert_reviewer_eligible(
            candidate, builder_vendors, chosen_by_user=chosen_by_user,
            same_vendor=bool(same and chosen_by_user))
    except ReviewerVendorConflict as exc:
        return candidate, getattr(exc, "reason", "reviewer_vendor_conflict")
    except Exception:
        return candidate, "reviewer_vendor_unknown"
    return candidate, None


def eligible_backup(cfg, builder_vendors, chosen_by_user=False):
    """Return the backup candidate only when the vendor rule allows it."""
    candidate, error = backup_eligibility(
        cfg, builder_vendors, chosen_by_user=chosen_by_user)
    if candidate is None or error is not None:
        return None
    return candidate


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


def _vendor_key(vendor):
    lower = _text(vendor).lower()
    for marker, key in _VENDOR_KEYS:
        if marker in lower:
            return key
    return ""


def _clean_label(label):
    """Keep the display label single-line and free of secrets/home paths."""
    text = _redact_secrets(label)
    text = " ".join(_text(text).split())
    return text or "unknown/unknown"


def error_message(cfg, execution=None, role="reviewer"):
    """Return one attributed sentence for a reviewer failure.

    ``execution`` is used only for the vendor/model fallbacks and for the
    failure class; its free text is never echoed.  A DeepSeek remedy is used
    only when the resolved *vendor* is DeepSeek, never when an unknown vendor's
    model merely looks like DeepSeek.
    """
    try:
        ident = identity(cfg, execution)
        label = _clean_label(ident.get("label"))
        vendor = _text(ident.get("vendor")).strip()
        reason_class = switch_class(execution)
    except Exception:
        return _ATTRIBUTION_UNAVAILABLE

    deepseek = "deepseek" in vendor.lower()
    role_text = " ".join(_text(role or "reviewer").split()) or "reviewer"
    subject = "%s %s" % (label, role_text)

    if reason_class == "credit/budget":
        message = (
            "%s quota is exhausted: top up or raise the limit with that vendor"
            % subject
        )
        message += _DEEPSEEK_REMEDY if deepseek else _UNAFFECTED_CLAUSE
        return message

    if reason_class == "auth":
        key = _vendor_key(vendor)
        if key:
            message = (
                "%s authentication failed: check the vendor key (%s) and the "
                "vendor console" % (subject, key)
            )
        else:
            message = (
                "%s authentication failed: check the vendor key and the "
                "vendor console" % subject
            )
        message += "." if deepseek else _UNAFFECTED_CLAUSE
        return message

    # A bare 429 is a rate limit; switch_class reserves credit/budget for
    # money/quota 429s, so it is safe to check here.
    if reason_class is None:
        try:
            plain_429 = bool(_HTTP_429_RE.search(_haystack(execution)))
        except Exception:
            plain_429 = False
        if plain_429:
            message = (
                "%s reviewer hit a rate limit (HTTP 429); wait and retry"
                % label
            )
            message += "." if deepseek else _UNAFFECTED_CLAUSE
            return message

    message = (
        "%s reviewer failed (unclassified); see the redacted run record"
        % label
    )
    message += "." if deepseek else _UNAFFECTED_CLAUSE
    return message


def banner(cfg, reason_class, execution=None):
    """Return the exact run-record banner for a backup review."""
    label = identity(cfg, execution)["label"]
    return "Reviewed by: backup (%s) \u2014 main failed: %s" % (label, reason_class)
