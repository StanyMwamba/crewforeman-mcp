"""Reviewer-vendor policy for CrewForeman 3.12.0 (R5).

Pure standard-library helpers.  They decide which reviewer candidates are
eligible, build the single consent question when no cross-vendor candidate is
available, resolve the user's answer, and run an ordered, bounded preflight.
No network, no key values and no harness imports live here.

``dsh_bin`` and ``patch`` are operator-only config: callers must never copy
them from tool arguments.  Every path is expanded, and a bad path becomes a
per-candidate ``error`` instead of an exception.
"""

import json
import os
import re

try:  # Reuse the 3.11 wrapper-name markers for the last vendor fallback.
    from . import reviewer_backup as _reviewer_backup
except Exception:  # pragma: no cover - standalone/import-defensive
    _reviewer_backup = None

try:  # Public validator; local fallback keeps this module dependency-free.
    from .events import build_reviewer_attention as _validate_attention
except Exception:  # pragma: no cover - defensive only
    _validate_attention = None

VENDORS = ("Anthropic", "Kimi (Moonshot)", "OpenAI", "Google", "DeepSeek")

QUESTION = (
    "No reviewer from a different vendor passed preflight. "
    "Which reviewer should score this job?"
)
DECLINE_OPTION = "Do not review; return the build as unreviewed"
RETRY_OPTION = "Retry reviewer preflight"
SAME_VENDOR_SUFFIX = " (same vendor)"
SAME_VENDOR_MARKER = "same-vendor review (chosen by user)"

# Most specific aliases first.  Matching is case-insensitive and deliberately
# substring-based, exactly like the 3.11 reviewer_backup markers.
_ALIASES = (
    ("anthropic", "Anthropic"),
    ("claude", "Anthropic"),
    ("sonnet", "Anthropic"),
    ("opus", "Anthropic"),
    ("moonshot", "Kimi (Moonshot)"),
    ("kimi", "Kimi (Moonshot)"),
    ("openai", "OpenAI"),
    ("gpt", "OpenAI"),
    ("google", "Google"),
    ("gemini", "Google"),
    ("deepseek", "DeepSeek"),
)

_PROVIDERS = {
    "Anthropic": "anthropic",
    "Kimi (Moonshot)": "moonshot",
    "OpenAI": "openai",
    "Google": "google",
    "DeepSeek": "deepseek",
}

_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUCCESS = frozenset(("ok", "done", "completed", "success", "succeeded"))
_STALL = frozenset((
    "stalled", "timed_out", "timedout", "timeout", "deadline", "time_limit",
    "cancelled", "canceled", "interrupted",
))
_RETRY_CLASSES = frozenset(("service unavailable (5xx)", "stream dropped"))
_MAX_TEXT = 200
_MAX_EVIDENCE_CANDIDATES = 8


class ReviewerVendorConflict(Exception):
    """Raised before a paid review that would violate the vendor rule."""

    def __init__(self, reason="reviewer_vendor_conflict"):
        self.reason = str(reason or "reviewer_vendor_conflict")
        Exception.__init__(self, self.reason)


class CandidateList(list):
    """List of candidates carrying the caller-visible legacy flag."""

    legacy_ignored = False


# ---------------------------------------------------------------------------
# Small coercion helpers
# ---------------------------------------------------------------------------

def _cfg_get(cfg, key, default=None):
    """Read a config key from a mapping or a SimpleNamespace-like object."""
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        return default if value is None else value
    return getattr(cfg, key, default)


def _raw_get(raw, key, default=None):
    if isinstance(raw, dict):
        value = raw.get(key, default)
    else:
        value = getattr(raw, key, default)
    return default if value is None else value


def _text(value, limit=_MAX_TEXT):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        return ""
    text = text.strip()
    return text[:limit]


def _bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value).lower()
    if text in ("1", "true", "yes", "on", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disabled", ""):
        return False
    return default


def _bounded_int(value, default, low=1, high=32):
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _canonical_set(builder_vendors):
    result = set()
    for vendor in builder_vendors or ():
        canonical = canonical_vendor(vendor)
        if canonical:
            result.add(canonical)
    return result


# ---------------------------------------------------------------------------
# Vendor resolution
# ---------------------------------------------------------------------------

def canonical_vendor(value):
    """Return the canonical vendor for *value*, or ``None`` when unknown."""
    text = _text(value, 400).lower()
    if not text:
        return None
    for alias, vendor in _ALIASES:
        if alias in text:
            return vendor
    return None


def _infer_vendor_from_path(path):
    """Last-resort wrapper basename inference, delegated when safe."""
    text = _text(path, 400)
    if not text:
        return None
    if _reviewer_backup is not None:
        try:
            inferred = _reviewer_backup._infer_vendor(os.path.basename(text))
        except Exception:
            inferred = None
        if inferred:
            return inferred
    return canonical_vendor(os.path.basename(text))


def _resolve_vendor(raw):
    """Explicit vendor > provider > model prefix > wrapper basename."""
    explicit = _text(_raw_get(raw, "vendor", ""))
    if explicit:
        # An explicit unknown is never promoted to a guess.
        return canonical_vendor(explicit) or "unknown"
    provider = _text(_raw_get(raw, "provider", ""))
    if provider:
        found = canonical_vendor(provider)
        if found:
            return found
    model = _text(_raw_get(raw, "model", ""))
    if model:
        found = canonical_vendor(model)
        if found:
            return found
    path = _text(_raw_get(raw, "dsh_bin", "")) or _text(_raw_get(raw, "bin", ""))
    return _infer_vendor_from_path(path) or "unknown"


def _expand(path):
    text = _text(path, 1024)
    if not text:
        return ""
    try:
        return os.path.expanduser(text)
    except (TypeError, ValueError, OSError):
        return text


def _bin_error(path):
    if not path:
        return None
    try:
        if not os.path.exists(path):
            return "dsh_bin_missing"
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            return "dsh_bin_not_executable"
    except (OSError, ValueError):
        return "dsh_bin_not_executable"
    return None


def _patch_error(path):
    if not path:
        return None
    try:
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return "patch_unreadable"
    except (OSError, ValueError):
        return "patch_unreadable"
    return None


def normalize_candidate(raw, cfg=None):
    """Return the bounded candidate dict; never raise on bad config."""
    if not isinstance(raw, dict):
        raw = {"model": _text(raw)}
    vendor = _resolve_vendor(raw)
    provider = _text(_raw_get(raw, "provider", ""), 64)
    if not provider:
        provider = _PROVIDERS.get(vendor, "")
    model = _text(_raw_get(raw, "model", ""), 128)
    name = _text(_raw_get(raw, "name", ""), 80) or model or "reviewer"
    dsh_bin = _expand(_raw_get(raw, "dsh_bin", "") or _raw_get(raw, "bin", ""))
    patch = _expand(_raw_get(raw, "patch", ""))
    api_key_env = _text(_raw_get(raw, "api_key_env", ""), 80)

    error = _bin_error(dsh_bin) or _patch_error(patch)
    if not error and api_key_env and not _ENV_RE.match(api_key_env):
        error = "api_key_env_invalid"

    return {
        "name": name,
        "vendor": vendor,
        "provider": provider,
        "model": model,
        "dsh_bin": dsh_bin,
        "patch": patch,
        "api_key_env": api_key_env,
        "enabled": _bool(_raw_get(raw, "enabled", True), True),
        "legacy_wrapper": bool(dsh_bin or patch),
        "error": error,
    }


def legacy_candidates(cfg):
    """Return configured reviewers, or synthesize the 3.11 legacy set.

    The returned :class:`CandidateList` exposes ``legacy_ignored``: true when
    ``cfg.reviewers`` was used and the legacy ``dsh_bin``/``backup_dsh_bin``
    settings were therefore ignored.
    """
    reviewers = _cfg_get(cfg, "reviewers", None)
    result = CandidateList()
    if isinstance(reviewers, list) and reviewers:
        for raw in reviewers[:32]:
            try:
                result.append(normalize_candidate(raw, cfg))
            except Exception:
                result.append(_failed_candidate(raw))
        result.legacy_ignored = True
        return result

    main_bin = _text(_cfg_get(cfg, "dsh_bin", ""), 1024)
    if main_bin:
        result.append(normalize_candidate({
            "name": "main",
            "vendor": _cfg_get(cfg, "reviewer_vendor", ""),
            "provider": _cfg_get(cfg, "reviewer_provider", ""),
            "model": _cfg_get(cfg, "reviewer_model", ""),
            "dsh_bin": main_bin,
        }, cfg))

    backup_bin = _text(_cfg_get(cfg, "backup_dsh_bin", ""), 1024)
    if backup_bin:
        result.append(normalize_candidate({
            "name": "backup",
            "vendor": _cfg_get(cfg, "backup_reviewer_vendor", ""),
            "model": _cfg_get(cfg, "backup_reviewer_model", ""),
            "dsh_bin": backup_bin,
        }, cfg))
    result.legacy_ignored = False
    return result


def _failed_candidate(raw):
    vendor = _resolve_vendor(raw)
    return {
        "name": _text(_raw_get(raw, "name", ""), 80) or "reviewer",
        "vendor": vendor,
        "provider": "",
        "model": _text(_raw_get(raw, "model", ""), 128),
        "dsh_bin": "",
        "patch": "",
        "api_key_env": "",
        "enabled": False,
        "legacy_wrapper": False,
        "error": "candidate_invalid",
    }


def legacy_ignored(cfg):
    """Return True when configured ``reviewers`` wins over the legacy bins."""
    return bool(legacy_candidates(cfg).legacy_ignored)


# ---------------------------------------------------------------------------
# Builder vendor set
# ---------------------------------------------------------------------------

def _split_model_spec(spec):
    text = _text(spec, 256)
    if not text:
        return "", ""
    if "/" in text:
        provider, model = text.split("/", 1)
        return provider.strip(), model.strip()
    return "", text


def _spec_vendor(spec):
    """Resolve one builder model spec; return canonical vendor or ``None``."""
    if isinstance(spec, dict):
        explicit = _text(spec.get("vendor", ""))
        if explicit:
            return canonical_vendor(explicit)
        provider = _text(spec.get("provider", ""))
        if canonical_vendor(provider):
            return canonical_vendor(provider)
        model = _text(spec.get("model", ""))
        if canonical_vendor(model):
            return canonical_vendor(model)
        path = _text(spec.get("dsh_bin", ""))
        return _infer_vendor_from_path(path)
    if isinstance(spec, (list, tuple)):
        return None
    provider, model = _split_model_spec(spec)
    return canonical_vendor(provider) or canonical_vendor(model)


def _cfg_target(cfg):
    provider = _text(
        _cfg_get(cfg, "builder_provider", "")
        or _cfg_get(cfg, "dsh_provider", ""), 64)
    model = _text(
        _cfg_get(cfg, "builder_model", "")
        or _cfg_get(cfg, "dsh_model", ""), 128)
    return {"provider": provider, "model": model}


def _collect_target(spec, vendors, unknown):
    vendor = _spec_vendor(spec)
    if vendor:
        vendors.add(vendor)
    else:
        unknown.append(True)


def builder_vendor_set(cfg, targets=None, run=None):
    """Return ``(sorted canonical builder vendors, any_unknown)``.

    Includes the R1 target, every valid slice assignment, every competing
    candidate (winners and losers), and the configured backup builder.  Any
    unresolved entry makes the second element true so callers fail closed.
    """
    vendors = set()
    unknown = []

    target_specs = []
    if isinstance(targets, (list, tuple)):
        target_specs = list(targets)
    elif isinstance(targets, dict):
        if any(key in targets for key in (
                "builder_model", "builder_provider", "model", "provider",
                "vendor", "dsh_bin")):
            target_specs = [targets]
        elif isinstance(targets.get("targets"), (list, tuple)):
            target_specs = list(targets["targets"])

    cfg_target = _cfg_target(cfg)
    # The R1 target is always part of the set, even when a job override exists.
    _collect_target(cfg_target, vendors, unknown)
    for spec in target_specs:
        if isinstance(spec, dict) and (
                spec.get("builder_model") or spec.get("builder_provider")):
            spec = {
                "provider": spec.get("builder_provider", ""),
                "model": spec.get("builder_model", ""),
            }
        _collect_target(spec, vendors, unknown)

    slices = _cfg_get(cfg, "slice_models", {})
    if isinstance(slices, dict):
        limit = _bounded_int(
            _cfg_get(cfg, "builder_model_max_slices", 8), 8, 0, 64)
        for index, spec in enumerate(slices.values()):
            if index >= limit:
                break
            _collect_target(spec, vendors, unknown)

    candidates = _cfg_get(cfg, "model_candidates", {})
    if isinstance(candidates, dict):
        for entries in candidates.values():
            if isinstance(entries, (list, tuple)):
                for spec in entries[:32]:
                    _collect_target(spec, vendors, unknown)
            elif entries:
                _collect_target(entries, vendors, unknown)

    backup_model = _text(_cfg_get(cfg, "backup_builder_model", ""), 128)
    if backup_model:
        _collect_target({
            "provider": _cfg_get(cfg, "backup_builder_provider", ""),
            "model": backup_model,
        }, vendors, unknown)

    recorded = _cfg_get(run, "builder_models", None) if isinstance(run, dict) else None
    if isinstance(recorded, list):
        for entry in recorded[:16]:
            if isinstance(entry, dict):
                _collect_target(entry, vendors, unknown)

    return sorted(vendors), bool(unknown)


# ---------------------------------------------------------------------------
# Eligibility and labels
# ---------------------------------------------------------------------------

def assert_reviewer_eligible(candidate, builder_vendors, chosen_by_user=False,
                             same_vendor=False):
    """Raise :class:`ReviewerVendorConflict` unless the candidate is allowed."""
    vendor = canonical_vendor(_raw_get(candidate, "vendor", ""))
    if vendor is None:
        raise ReviewerVendorConflict("reviewer_vendor_unknown")
    builders = _canonical_set(builder_vendors)
    if vendor not in builders:
        return True
    if same_vendor and chosen_by_user:
        return True
    raise ReviewerVendorConflict("reviewer_vendor_same_as_builder")


def review_header(candidate, same_vendor_choice=False):
    """Return the exact one-line review label."""
    vendor = canonical_vendor(_raw_get(candidate, "vendor", "")) or "unknown"
    model = _text(_raw_get(candidate, "model", ""), 128) or "unknown"
    header = "Reviewed by: %s/%s" % (vendor, model)
    if same_vendor_choice:
        header += " \u2014 " + SAME_VENDOR_MARKER
    return header


# ---------------------------------------------------------------------------
# ONE attention question
# ---------------------------------------------------------------------------

def _candidate_vendor(candidate):
    return canonical_vendor(_raw_get(candidate, "vendor", "")) or "unknown"


def _candidate_model(candidate):
    return _text(_raw_get(candidate, "model", ""), 128) or "unknown"


def _candidate_label(candidate):
    return "%s/%s" % (_candidate_vendor(candidate), _candidate_model(candidate))


def _is_passing_same_vendor(candidate, builders):
    vendor = _candidate_vendor(candidate)
    if vendor == "unknown" or vendor not in builders:
        return False
    if _raw_get(candidate, "enabled", True) is False:
        return False
    if _raw_get(candidate, "error", None):
        return False
    state = _text(_raw_get(candidate, "state", ""), 64).lower()
    if state in ("same_vendor_preflight_ok", "preflight_ok"):
        return True
    if _raw_get(candidate, "preflight_ok", False) is True:
        return True
    return False


def _evidence_candidate(candidate):
    entry = {
        "vendor": _candidate_vendor(candidate),
        "model": _candidate_model(candidate),
        "name": _text(_raw_get(candidate, "name", ""), 80) or "reviewer",
        "state": _text(_raw_get(candidate, "state", ""), 64) or "configured",
    }
    failure_class = _text(_raw_get(candidate, "class", ""), 64)
    if failure_class:
        entry["class"] = failure_class
    return entry


def _local_validate(payload):
    if not isinstance(payload, dict):
        return None
    question = payload.get("question")
    options = payload.get("options")
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        return None
    if not isinstance(options, list) or not 2 <= len(options) <= 8:
        return None
    clean = []
    for option in options:
        if not isinstance(option, str):
            return None
        option = option.strip()
        if not option or len(option) > 200:
            return None
        clean.append(option)
    evidence = payload.get("evidence")
    try:
        rendered = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(rendered) > 2000:
        return None
    return {"question": question.strip(), "options": clean, "evidence": evidence}


def _validate(payload):
    validator = _validate_attention or _local_validate
    try:
        return validator(payload)
    except Exception:
        return _local_validate(payload)


def build_attention(candidates, builders, round_no=1, policy="ask"):
    """Build and validate the single reviewer-consent question payload."""
    builders = sorted(_canonical_set(builders))
    candidates = [c for c in (candidates or ()) if isinstance(c, dict)]
    passing = []
    for candidate in candidates:
        if _is_passing_same_vendor(candidate, set(builders)):
            passing.append(candidate)

    options = []
    if str(policy or "ask").strip().lower() != "never":
        for candidate in passing[:7]:
            options.append(_candidate_label(candidate) + SAME_VENDOR_SUFFIX)
    if not options:
        options = [RETRY_OPTION, DECLINE_OPTION]
    else:
        options.append(DECLINE_OPTION)

    ordered = passing + [c for c in candidates if c not in passing]
    evidence_candidates = [
        _evidence_candidate(c) for c in ordered[:_MAX_EVIDENCE_CANDIDATES]
    ]
    try:
        round_value = max(1, min(99, int(round_no)))
    except (TypeError, ValueError):
        round_value = 1
    payload = {
        "question": QUESTION,
        "options": options[:8],
        "evidence": {
            "kind": "reviewer_vendor",
            "builders": builders,
            "candidates": evidence_candidates,
            "round": round_value,
        },
    }
    validated = _validate(payload)
    if validated is not None:
        return validated
    # Never return a malformed block: shrink to the two guaranteed options.
    minimal = {
        "question": QUESTION,
        "options": [RETRY_OPTION, DECLINE_OPTION],
        "evidence": {
            "kind": "reviewer_vendor",
            "builders": builders[:8],
            "candidates": evidence_candidates[:4],
            "round": round_value,
        },
    }
    return _validate(minimal) or _local_validate(minimal)


def _attention_payload(attention):
    if isinstance(attention, dict):
        return attention
    if isinstance(attention, str):
        try:
            from .events import validate_attention
            return validate_attention(attention)
        except Exception:
            return None
    return None


def _selectable(candidate):
    state = _text(_raw_get(candidate, "state", ""), 64).lower()
    return state in ("same_vendor_preflight_ok", "preflight_ok")


def _match_answer(answer, passing):
    text = _text(answer, 200)
    if not text:
        return None
    lower = text.lower()
    if lower.endswith(SAME_VENDOR_SUFFIX):
        text = text[:-len(SAME_VENDOR_SUFFIX)].strip()
        lower = text.lower()
    if (lower in ("decline", "declined", "no review", "unreviewed",
                  "return unreviewed")
            or lower.startswith("do not review")
            or lower.startswith("decline")):
        return "decline"
    for candidate in passing:
        name = _text(_raw_get(candidate, "name", ""), 80)
        label = _candidate_label(candidate)
        model = _candidate_model(candidate)
        if text == name or text.lower() == name.lower():
            return candidate
        if lower == label.lower() or lower == model.lower():
            return candidate
    return None


def resolve_choice(attention, answer=None, option=None):
    """Resolve a reviewer attention answer.

    Returns ``("same_vendor", candidate)``, ``("decline", None)`` or
    ``("invalid", None)``.  An exact stored option wins; otherwise *answer* is
    matched to a candidate name or ``<vendor>/<model>``.
    """
    payload = _attention_payload(attention)
    if payload is None:
        return ("invalid", None)
    options = payload.get("options") or []
    evidence = (payload.get("evidence") or {}).get("candidates") or []
    passing = [c for c in evidence
               if isinstance(c, dict) and _selectable(c)]

    if isinstance(option, str):
        chosen = option.strip()
        if chosen in options:
            if chosen == DECLINE_OPTION or chosen.startswith("Do not review"):
                return ("decline", None)
            for candidate in passing:
                if _candidate_label(candidate) + SAME_VENDOR_SUFFIX == chosen:
                    return ("same_vendor", candidate)
            return ("invalid", None)

    matched = _match_answer(answer, passing)
    if matched == "decline":
        return ("decline", None)
    if isinstance(matched, dict):
        return ("same_vendor", matched)
    return ("invalid", None)


# ---------------------------------------------------------------------------
# Preflight and selection
# ---------------------------------------------------------------------------

def _execution_text(execution):
    if isinstance(execution, str):
        return execution
    if not isinstance(execution, dict):
        return ""
    parts = []
    for key in ("answer", "output", "text", "result", "stdout"):
        value = execution.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif value is not None and not isinstance(value, (dict, list)):
            parts.append(str(value))
    return " ".join(parts)


def _execution_status(execution):
    if isinstance(execution, dict):
        return _text(execution.get("status", ""), 64).lower()
    if isinstance(execution, bool):
        return "ok" if execution else "error"
    return ""


def _ready(execution):
    if isinstance(execution, bool):
        return execution
    status = _execution_status(execution)
    if status != "ok":
        return False
    return "READY" in _execution_text(execution).upper()


def _failure_class(execution):
    status = _execution_status(execution)
    if status in _STALL:
        return "stalled"
    if _reviewer_backup is not None:
        try:
            classified = _reviewer_backup.switch_class(execution)
        except Exception:
            classified = None
        if classified:
            return classified
    if status and status not in _SUCCESS and status != "ok":
        return "error"
    return "no_ready"


def _probe(preflight_fn, candidate):
    """Run at most two attempts, retrying only 5xx and dropped streams."""
    result = None
    failure_class = "config"
    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            result = preflight_fn(candidate)
        except Exception:
            result = {"status": "error", "error": "preflight_exception"}
        if _ready(result):
            return True, failure_class, attempts
        failure_class = _failure_class(result)
        if failure_class not in _RETRY_CLASSES:
            break
    return False, failure_class, attempts


def _enriched(candidate, state, failure_class=None, attempts=0):
    entry = dict(candidate)
    entry["state"] = state
    if failure_class:
        entry["class"] = failure_class
    entry["attempts"] = attempts
    return entry


def _candidate_entry(raw, cfg=None):
    if isinstance(raw, dict) and all(
            key in raw for key in ("name", "vendor", "provider", "model",
                                   "dsh_bin", "patch", "api_key_env",
                                   "enabled", "legacy_wrapper", "error")):
        return dict(raw)
    try:
        return normalize_candidate(raw, cfg)
    except Exception:
        return _failed_candidate(raw)


def reviewer_config_errors(candidates):
    """Return bounded records for candidate errors and over-cap entries."""
    errors = []
    for candidate in candidates or ():
        if not isinstance(candidate, dict):
            continue
        error = _text(candidate.get("error", ""), 80)
        state = _text(candidate.get("state", ""), 64)
        if not error and state in ("config_error", "over_cap", "unknown_vendor"):
            error = state
        if error:
            errors.append({
                "name": _text(candidate.get("name", ""), 80) or "reviewer",
                "error": error,
            })
        if len(errors) >= 16:
            break
    return errors


def select_candidate(candidates, builder_vendors, preflight_fn,
                     policy="ask", max_preflights=8):
    """Preflight candidates in configured order and select the first READY one.

    Cross-vendor candidates run first.  When none passes, same-vendor
    candidates are pinged for availability and the result pauses with exactly
    one validated attention payload.  No paid review happens here.
    """
    builders = _canonical_set(builder_vendors)
    limit = _bounded_int(max_preflights, 8, 1, 32)
    policy = _text(policy, 16).lower() or "ask"
    prepared = []
    active = []

    for index, raw in enumerate(candidates or ()):
        entry = _candidate_entry(raw)
        if index >= limit:
            entry = _enriched(entry, "over_cap", "config", 0)
            entry["error"] = "reviewer_cap_exceeded"
            prepared.append(entry)
            continue
        if not entry.get("enabled", True):
            prepared.append(_enriched(entry, "disabled", "config", 0))
            continue
        if entry.get("error"):
            prepared.append(_enriched(entry, "config_error", "config", 0))
            continue
        vendor = canonical_vendor(entry.get("vendor"))
        if vendor is None:
            prepared.append(_enriched(entry, "unknown_vendor", "unknown", 0))
            continue
        entry = dict(entry)
        entry["vendor"] = vendor
        prepared.append(entry)
        active.append(entry)

    for entry in active:
        same = canonical_vendor(entry.get("vendor")) in builders
        if same:
            continue
        passed, failure_class, attempts = _probe(preflight_fn, entry)
        if passed:
            entry["state"] = "preflight_ok"
            entry["attempts"] = attempts
            return {
                "status": "selected",
                "candidate": entry,
                "same_vendor": False,
                "candidates": prepared,
                "attention": None,
            }
        entry["state"] = "preflight_failed"
        entry["class"] = failure_class
        entry["attempts"] = attempts

    same_vendor_entries = [
        entry for entry in active
        if canonical_vendor(entry.get("vendor")) in builders
    ]
    for entry in same_vendor_entries:
        passed, failure_class, attempts = _probe(preflight_fn, entry)
        entry["attempts"] = attempts
        if passed:
            entry["state"] = "same_vendor_preflight_ok"
        else:
            entry["state"] = "same_vendor_preflight_failed"
            entry["class"] = failure_class

    attention = build_attention(prepared, sorted(builders), policy=policy)
    return {
        "status": "paused",
        "candidate": None,
        "same_vendor": False,
        "candidates": prepared,
        "attention": attention,
    }
