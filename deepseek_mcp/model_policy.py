"""Model target resolution, validation and model-plan records.

Pure standard-library helpers for CrewForeman 3.12.0 (DESIGN sections 4, 5.1
and 5.2).  Nothing here reads an API key value, performs network I/O or writes
to disk: a model target carries only the *name* of the environment variable
that holds a key.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass

from . import credentials

SERVER_MODEL_VERSION = 1
DEFAULT_PROVIDER = "deepseek-official"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_BASE_URL = "https://api.deepseek.com"
MAX_TEXT = 200

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SLICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_STATUSES = frozenset(("verified", "partial", "mismatch", "unverified", "ignored"))

_VENDOR_MARKERS = (
    ("Anthropic", ("anthropic", "claude", "sonnet", "opus")),
    ("Kimi (Moonshot)", ("moonshot", "kimi")),
    ("OpenAI", ("openai", "gpt")),
    ("Google", ("google", "gemini")),
    ("DeepSeek", ("deepseek",)),
)

_PLAN_TAIL = (
    "Use the workflow agent() options {provider, model} for every agent that writes\n"
    "that slice's files. Do not change a slice's model between waves. If the installed\n"
    "Harness cannot override a child model, write MODEL_PLAN_UNSUPPORTED to\n"
    "wave-1-results.md and continue on the parent model. Never claim a model ran when\n"
    "it did not."
)


class BuilderModelError(Exception):
    """A deterministic, pre-spawn model configuration failure."""

    def __init__(self, code, message):
        self.code = str(code)
        self.message = _text(message)
        super().__init__("%s: %s" % (self.code, self.message))


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    vendor: str
    base_url: str
    api: str
    api_key_env: str
    source: str = "config"

    def label(self):
        return "%s/%s" % (self.provider, self.model)

    def as_dict(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "vendor": self.vendor,
            "base_url": self.base_url,
            "api": self.api,
            "api_key_env": self.api_key_env,
            "source": self.source,
        }


def _text(value, limit=MAX_TEXT):
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    text = text.replace("\x00", "").strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def _cfg_get(cfg, key, default=None):
    if cfg is None:
        return default
    try:
        value = cfg.get(key, default)
    except Exception:
        try:
            value = getattr(cfg, key, default)
        except Exception:
            value = default
    return default if value is None else value


def _cfg_text(cfg, key, default=""):
    return _text(_cfg_get(cfg, key, default), 128)


def _limit(cfg, key, default, maximum):
    try:
        value = int(_cfg_get(cfg, key, default))
    except (TypeError, ValueError):
        value = default
    return max(0, min(value, maximum))


def _bounded(value, depth=0):
    """Return a JSON-safe, size-bounded copy of ``value``."""
    if depth > 4:
        return _text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                break
            out[_text(key, 80)] = _bounded(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in list(value)[:50]]
    return _text(value)


def _model_ids(entry):
    raw = entry.get("models")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    if not raw and entry.get("model"):
        raw = [entry["model"]]
    models = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("id") or item.get("name") or item.get("model")
        text = _text(item, 128)
        if text and text not in models:
            models.append(text)
    return models


def _entry(name, entry):
    if not isinstance(entry, dict):
        return None
    provider = _text(entry.get("name") or entry.get("provider") or entry.get("id"), 64)
    if not provider:
        provider = name
    base_url = _text(entry.get("base_url") or entry.get("baseURL")
                     or entry.get("url"), 300).rstrip("/")
    api = _text(entry.get("api") or entry.get("api_type") or entry.get("apiType"), 80)
    vendor = _text(entry.get("vendor"), 80)
    key_env = _text(entry.get("api_key_env") or entry.get("apiKeyEnv"), 128)
    return {
        "name": provider,
        "base_url": base_url,
        "api": api,
        "vendor": vendor,
        "api_key_env": key_env,
        "api_key_env_explicit": bool(key_env),
        "models": _model_ids(entry),
    }


def _builtin(cfg):
    return {
        "name": DEFAULT_PROVIDER,
        "base_url": (_cfg_text(cfg, "base_url", "") or DEFAULT_BASE_URL).rstrip("/"),
        "api": "openai-chat",
        "vendor": "DeepSeek",
        "api_key_env": DEFAULT_API_KEY_ENV,
        "api_key_env_explicit": False,
        "models": sorted(set([
            _cfg_text(cfg, "dsh_model", "") or DEFAULT_MODEL,
            _cfg_text(cfg, "model", "") or DEFAULT_MODEL,
        ])),
    }


def _provider_entries(cfg):
    entries = [_builtin(cfg)]
    listed = _cfg_get(cfg, "providers", [])
    if not isinstance(listed, list):
        listed = []
    for index, raw in enumerate(listed):
        item = _entry("provider-%d" % index, raw)
        if item:
            entries.append(item)
    return entries


def _find_provider(cfg, name):
    if name == DEFAULT_PROVIDER:
        return _builtin(cfg)
    for item in _provider_entries(cfg):
        if item["name"] == name:
            return item
    return None


def _infer_provider_optional(cfg, model):
    matches = [item["name"] for item in _provider_entries(cfg)
               if model in item["models"]]
    if len(matches) > 1:
        raise BuilderModelError(
            "builder_model_provider_ambiguous",
            "multiple providers list model %s; set builder_provider" % model)
    return matches[0] if matches else ""


def _infer_provider(cfg, model):
    provider = _infer_provider_optional(cfg, model)
    if not provider:
        raise BuilderModelError(
            "builder_model_provider_unknown",
            "no configured provider lists model %s; set builder_provider" % model)
    return provider


def canonical_vendor(*values):
    """Normalize known vendor markers; unknown stays ``unknown``."""
    for value in values:
        text = _text(value, 80)
        if not text:
            continue
        try:
            module = importlib.import_module("deepseek_mcp.reviewer_vendor")
            resolver = getattr(module, "canonical_vendor", None)
            if callable(resolver):
                resolved = resolver(text)
                if isinstance(resolved, str) and resolved.strip():
                    return _text(resolved, 80)
        except Exception:
            pass
        low = text.lower()
        for canonical, markers in _VENDOR_MARKERS:
            if any(marker in low for marker in markers):
                return canonical
    return "unknown"


def _validate_pair(provider, model):
    if not MODEL_RE.match(model) or "=" in model or ";" in model or model.startswith("-"):
        raise BuilderModelError(
            "builder_model_invalid",
            "model must match %s and contain no whitespace, '=', ';', newline or "
            "control characters" % MODEL_RE.pattern)
    if not PROVIDER_RE.match(provider) or "=" in provider or ";" in provider:
        raise BuilderModelError(
            "builder_model_invalid",
            "provider must match %s and contain no whitespace, '=', ';', newline "
            "or control characters" % PROVIDER_RE.pattern)


def _carrier(target):
    if isinstance(target, ModelTarget):
        return target
    if isinstance(target, dict):
        return ModelTarget(
            provider=_text(target.get("provider"), 64),
            model=_text(target.get("model"), 128),
            vendor=_text(target.get("vendor"), 80),
            base_url=_text(target.get("base_url"), 300),
            api=_text(target.get("api"), 80),
            api_key_env=_text(target.get("api_key_env"), 128),
            source=_text(target.get("source", "config"), 20),
        )
    if isinstance(target, (tuple, list)) and len(target) >= 2:
        return ModelTarget(_text(target[0], 64), _text(target[1], 128),
                           "", "", "", "", "config")
    if isinstance(target, str):
        provider, _, model = target.partition("/")
        if not model:
            provider, model = "", provider
        return ModelTarget(_text(provider, 64), _text(model, 128),
                           "", "", "", "", "config")
    return None


def _pair(provider, model, cfg):
    provider = _text(provider, 64)
    model = _text(model, 128)
    if not model:
        raise BuilderModelError("builder_model_missing", "builder model is empty")
    if not provider:
        provider = _infer_provider(cfg, model)
    _validate_pair(provider, model)
    return provider, model


def resolve_target(cfg, provider="", model="", source="config", allow_legacy=False):
    """Resolve the effective builder target using §4.1 precedence.

    Tool arguments beat ``builder_*``, which beats ``dsh_*`` and the built-ins.
    Empty values inherit.  The returned target never carries a key value.
    """
    provider = _text(provider, 64)
    model = _text(model, 128)
    builder_provider = _cfg_text(cfg, "builder_provider", "")
    builder_model = _cfg_text(cfg, "builder_model", "")
    dsh_provider = _cfg_text(cfg, "dsh_provider", "") or DEFAULT_PROVIDER
    dsh_model = _cfg_text(cfg, "dsh_model", "") or DEFAULT_MODEL

    if provider or model:
        chosen_model = model or builder_model or dsh_model
        chosen_provider = provider
        if not chosen_provider:
            chosen_provider = (_infer_provider_optional(cfg, chosen_model)
                               or builder_provider or dsh_provider)
        chosen_source = _text(source, 20) or "job"
    elif builder_provider or builder_model:
        chosen_model = builder_model or dsh_model
        chosen_provider = builder_provider
        if not chosen_provider:
            chosen_provider = (_infer_provider_optional(cfg, chosen_model)
                               or dsh_provider)
        chosen_source = "config"
    else:
        chosen_provider = dsh_provider
        chosen_model = dsh_model
        chosen_source = "default"

    chosen_provider, chosen_model = _pair(chosen_provider, chosen_model, cfg)
    entry = _find_provider(cfg, chosen_provider)
    if entry is None and chosen_provider == dsh_provider:
        # 3.11 compatibility: the historical default pair may name a provider
        # that is not in providers[] (for example a reviewer wrapper's
        # dsh_provider). It is an implicit default target, not a new job
        # override; keep the 3.11 argv working.
        entry = {
            "name": chosen_provider,
            "base_url": (_cfg_text(cfg, "base_url", "") or "").rstrip("/"),
            "api": "openai-chat",
            "vendor": "",
            "api_key_env": DEFAULT_API_KEY_ENV,
            "api_key_env_explicit": False,
            "models": [chosen_model],
        }
    if entry is None:
        raise BuilderModelError(
            "builder_model_provider_unknown",
            "provider %s is neither %s nor a configured providers entry"
            % (chosen_provider, DEFAULT_PROVIDER))

    if not bool(_cfg_get(cfg, "builder_allow_unlisted_models", False)):
        if chosen_model not in entry["models"]:
            raise BuilderModelError(
                "builder_model_unlisted",
                "model %s is not listed by provider %s; add it to providers[].models "
                "or set builder_allow_unlisted_models=true"
                % (chosen_model, chosen_provider))

    key_env = entry.get("api_key_env") or DEFAULT_API_KEY_ENV
    if not key_env or not re.match(r"^[A-Z][A-Z0-9_]*$", str(key_env)):
        raise BuilderModelError(
            "builder_provider_key_missing",
            "no valid api_key_env is configured for provider %s"
            % chosen_provider)
    # Do not require a key for the byte-identical default path: 3.11.0 let the
    # installed Harness resolve its own credential.  An explicit provider entry
    # that names a variable must have that variable set before a paid call.
    if (entry.get("api_key_env_explicit") and not allow_legacy
            and credentials.resolve_key(key_env, cfg) is None):
        raise BuilderModelError(
            "builder_provider_key_missing",
            credentials.missing_message(key_env))

    vendor = _text(entry.get("vendor"), 80)
    vendor = canonical_vendor(vendor, entry.get("name"), chosen_model)
    base_url = entry.get("base_url") or (
        DEFAULT_BASE_URL if chosen_provider == DEFAULT_PROVIDER else "")
    api = entry.get("api") or "openai-chat"
    return ModelTarget(
        provider=chosen_provider,
        model=chosen_model,
        vendor=vendor,
        base_url=_text(base_url, 300),
        api=_text(api, 80),
        api_key_env=_text(key_env, 128),
        source=chosen_source,
    )


def is_default_target(target, cfg):
    """True exactly for ``(cfg.dsh_provider, cfg.dsh_model)`` (stripped)."""
    carried = _carrier(target)
    if carried is None:
        return False
    provider = _cfg_text(cfg, "dsh_provider", "") or DEFAULT_PROVIDER
    model = _cfg_text(cfg, "dsh_model", "") or DEFAULT_MODEL
    return (carried.provider.strip() == provider.strip()
            and carried.model.strip() == model.strip())


def resolve_spec(spec, cfg, source="plan"):
    """Return ``(provider, model)`` from ``provider/model`` or a mapping."""
    if isinstance(spec, dict):
        provider = spec.get("provider", "")
        model = spec.get("model", "")
    else:
        text = _text(spec, 200)
        provider, sep, model = text.partition("/")
        if not sep:
            provider, model = "", text
    if not _text(model, 128):
        raise BuilderModelError("builder_model_missing", "model spec has no model")
    return _text(provider, 64), _text(model, 128)


def _validated_spec(cfg, spec, source):
    provider, model = resolve_spec(spec, cfg, source)
    return resolve_target(cfg, provider, model, source=source, allow_legacy=False)


def validate_slice_models(cfg, raw):
    """Validate a slice-name -> model-spec map.

    Returns ``(normalized, errors)``.  Entries beyond
    ``builder_model_max_slices`` are rejected, never silently truncated.
    """
    limit = _limit(cfg, "builder_model_max_slices", 8, 64)
    normalized = {}
    errors = []
    if raw is None:
        return normalized, errors
    if not isinstance(raw, dict):
        return normalized, [{"code": "builder_model_invalid",
                             "message": "slice_models must be an object"}]
    for index, (name, spec) in enumerate(raw.items()):
        slice_name = _text(name, 64)
        if not SLICE_RE.match(slice_name):
            errors.append({"slice": _text(name, 64), "code": "builder_model_invalid",
                           "message": "invalid slice name"})
            continue
        if len(normalized) >= limit or index >= limit:
            errors.append({"slice": slice_name, "code": "builder_model_max_slices",
                           "message": "at most %d slice_models entries are allowed"
                                      % limit})
            continue
        try:
            target = _validated_spec(cfg, spec, "plan")
        except BuilderModelError as exc:
            errors.append({"slice": slice_name, "code": exc.code,
                           "message": exc.message})
            continue
        normalized[slice_name] = {"provider": target.provider, "model": target.model}
    return normalized, errors


def validate_model_candidates(cfg, raw):
    """Validate slice -> [2..N] model candidates, capping total extras."""
    cap = _limit(cfg, "model_candidates_max", 3, 64)
    normalized = {}
    errors = []
    if raw is None:
        return normalized, errors
    if not isinstance(raw, dict):
        return normalized, [{"code": "builder_model_invalid",
                             "message": "model_candidates must be an object"}]
    extras = 0
    for name, specs in raw.items():
        slice_name = _text(name, 64)
        if not SLICE_RE.match(slice_name):
            errors.append({"slice": slice_name, "code": "builder_model_invalid",
                           "message": "invalid slice name"})
            continue
        if not isinstance(specs, list) or len(specs) < 2:
            errors.append({"slice": slice_name, "code": "model_candidates_invalid",
                           "message": "each slice needs at least two candidates"})
            continue
        accepted = []
        for index, spec in enumerate(specs):
            if extras >= cap:
                errors.append({"slice": slice_name, "index": index,
                               "code": "model_candidates_max",
                               "message": "at most %d extra candidates per job" % cap})
                continue
            try:
                target = _validated_spec(cfg, spec, "plan")
            except BuilderModelError as exc:
                errors.append({"slice": slice_name, "index": index,
                               "code": exc.code, "message": exc.message})
                continue
            if index > 0:
                extras += 1
            accepted.append({"provider": target.provider, "model": target.model})
        if accepted:
            normalized[slice_name] = accepted
    return normalized, errors


def _label(value):
    carried = _carrier(value)
    if carried is not None:
        return "%s/%s" % (carried.provider, carried.model)
    if isinstance(value, dict):
        provider = _text(value.get("provider"), 64)
        model = _text(value.get("model"), 128)
        if provider or model:
            return "%s/%s" % (provider, model)
    return _text(value, 200)


def _slice_label(spec):
    return _label(spec)


def build_model_plan_section(parent, slices, candidates):
    """Return the exact deterministic §5.1 model-plan text block."""
    lines = ["## Model plan (connector)",
             "Parent builder uses %s." % _label(parent)]
    for name in sorted((slices or {})):
        lines.append("- slice %s: %s" % (name, _slice_label(slices[name])))
    for name in sorted((candidates or {})):
        labels = ", ".join(_slice_label(item) for item in candidates[name])
        lines.append("- slice %s candidates: %s" % (name, labels))
    lines.append(_PLAN_TAIL)
    return "\n".join(lines) + "\n"


def _existing_set(existing_files):
    found = set()
    for path in existing_files or []:
        text = _text(path, 300).replace("\\", "/").lstrip("./")
        if text:
            found.add(text)
            found.add(text.rsplit("/", 1)[-1])
    return found


def _candidate_files(record):
    files = []
    for item in (record.get("candidates") or [])[:16]:
        if isinstance(item, dict) and item.get("file"):
            files.append(_text(item["file"], 300))
    if record.get("file"):
        files.append(_text(record["file"], 300))
    return files


def validate_model_audit(audit, cfg, requested_slices, existing_files):
    """Cross-check MODEL_AUDIT.json against the requested plan (§5.1).

    The result is bounded and JSON-safe.  Missing evidence is never
    ``verified``; a requested slice absent from a present audit is ``ignored``.
    """
    requested = requested_slices if isinstance(requested_slices, dict) else {}
    existing = _existing_set(existing_files)
    max_slices = _limit(cfg, "builder_model_max_slices", 8, 64)
    max_candidates = _limit(cfg, "model_candidates_max", 3, 64)
    records = []
    unmatched = []
    if not isinstance(audit, dict):
        for name in sorted(requested)[:max_slices]:
            unmatched.append({"slice": _text(name, 64), "reason": "missing_audit"})
        return {"version": 1,
                "status": "unverified", "slices": [], "unmatched": unmatched}

    audit_slices = {}
    raw_slices = audit.get("slices")
    if isinstance(raw_slices, list):
        for item in raw_slices[:max_slices]:
            if isinstance(item, dict):
                audit_slices[_text(item.get("slice"), 64)] = item
    requested_names = sorted(requested)
    for name in requested_names:
        slice_name = _text(name, 64)
        if len(records) >= max_slices:
            break
        record = audit_slices.get(slice_name)
        if record is None:
            records.append({"slice": slice_name, "status": "ignored",
                            "requested": _slice_label(requested[name]),
                            "winner": ""})
            unmatched.append({"slice": slice_name, "reason": "not_in_audit"})
            continue
        winner = _text(record.get("winner"), 200)
        requested_label = _slice_label(requested[name])
        candidates = []
        verified_file = ""
        state = "mismatch"
        for item in (record.get("candidates") or [])[:max_candidates]:
            if not isinstance(item, dict):
                continue
            label = _label(item)
            file_name = _text(item.get("file"), 300)
            candidates.append({"tag": _text(item.get("tag"), 80),
                               "provider": _text(item.get("provider"), 64),
                               "model": _text(item.get("model"), 128),
                               "file": file_name,
                               "status": _text(item.get("status", "ok"), 40)})
            if (label == requested_label and file_name
                    and _text(item.get("status", "ok"), 40) != "error"):
                base = file_name.replace("\\", "/").lstrip("./")
                if base in existing or base.rsplit("/", 1)[-1] in existing:
                    verified_file = file_name
                    state = "verified"
        if state != "verified":
            if winner and winner != requested_label:
                state = "mismatch"
            elif not _candidate_files(record):
                state = "mismatch"
        if state != "verified" and winner and winner != requested_label:
            unmatched.append({"slice": slice_name, "reason": "winner_mismatch"})
        records.append({"slice": slice_name, "status": state,
                        "requested": requested_label, "winner": winner,
                        "file": verified_file, "candidates": candidates})

    for name in sorted(set(audit_slices) - {_text(n, 64) for n in requested}):
        if len(unmatched) >= 50:
            break
        unmatched.append({"slice": _text(name, 64), "reason": "not_requested"})

    statuses = [item["status"] for item in records]
    if not requested_names:
        status = "unverified"
    elif "mismatch" in statuses:
        status = "mismatch"
    elif statuses and all(item == "verified" for item in statuses):
        status = "verified"
    elif "verified" in statuses:
        status = "partial"
    elif "ignored" in statuses:
        status = "ignored"
    else:
        status = "unverified"
    return {"version": 1, "status": status if status in _STATUSES else "unverified",
            "slices": records, "unmatched": unmatched[:50]}


def _score_map(scores):
    result = {}
    if isinstance(scores, dict):
        for key, value in scores.items():
            if isinstance(value, dict):
                score = value.get("score")
            else:
                score = value
            try:
                result[_text(key, 200)] = float(score)
            except (TypeError, ValueError):
                continue
    elif isinstance(scores, (list, tuple)):
        for index, item in enumerate(scores):
            if not isinstance(item, dict):
                continue
            try:
                number = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            result[_text(item.get("name") or item.get("tag") or index, 200)] = number
    return result


def _cost(candidate):
    for key in ("estimated_cost_usd", "cost_usd", "cost"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value), True
    return None, False


def competition_result(candidates, scores, configured_order):
    """Pick the best candidate: score, then lower known cost, then order."""
    score_map = _score_map(scores)
    ordered = [_text(item, 200) for item in (configured_order or [])]
    rows = []
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        tag = _text(candidate.get("tag") or candidate.get("name")
                    or "candidate-%d" % index, 80)
        score = score_map.get(tag)
        if score is None:
            try:
                score = float(candidate.get("score"))
            except (TypeError, ValueError):
                score = 0.0
        cost, known = _cost(candidate)
        if tag in ordered:
            order = ordered.index(tag)
        else:
            order = len(ordered) + index
        rows.append({
            "tag": tag,
            "provider": _text(candidate.get("provider"), 64),
            "model": _text(candidate.get("model"), 128),
            "score": round(score, 2),
            "estimated_cost_usd": cost,
            "cost_known": known,
            "order": order,
            "_index": index,
        })
    rows.sort(key=lambda item: (-item["score"],
                                item["estimated_cost_usd"] if item["cost_known"] else float("inf"),
                                item["order"], item["_index"]))
    for item in rows:
        item.pop("_index", None)
    winner = _bounded(rows[0]) if rows else None
    applied = bool(winner and winner.get("score", 0) > 0)
    return {"winner": winner, "candidates": _bounded(rows), "applied": applied}
