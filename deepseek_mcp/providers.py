"""Config-only OpenAI-compatible providers with ordered fallback and consult.

No SDKs, no network at import time: everything goes through ``urllib``.  API
keys are read from environment variables named by config only; they are never
stored in provider dicts, manifests, or exception text.

Python 3.9 compatible; standard library only.
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"

# USD per 1M tokens (published DeepSeek off-peak prices).
PRICES = {
    "deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.60},
}

_TRANSIENT_STATUSES = frozenset([402, 408, 409, 425, 429])
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_.\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
)


class ProviderError(Exception):
    def __init__(self, message, provider=None, status=None, transient=False):
        super().__init__(_redact(message))
        self.provider = provider
        self.status = status
        self.transient = bool(transient)
        self.attempts = []


def _redact(value, limit=500):
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        text = "<unprintable>"
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def _cfg_get(cfg, key, default=None):
    try:
        value = cfg.get(key, default)
    except Exception:
        value = getattr(cfg, key, default)
    return default if value is None else value


def _derive_key_env(name):
    env_name = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").upper()
    return env_name + "_API_KEY" if env_name else ""


def _base(value):
    return str(value or "").strip().rstrip("/")


def _provider(entry, index):
    if not isinstance(entry, dict):
        return None
    base_url = _base(entry.get("base_url") or entry.get("url"))
    if not base_url:
        return None
    name = str(entry.get("name") or "provider-%d" % index)
    models = entry.get("models")
    if isinstance(models, str):
        models = [models]
    if not isinstance(models, list) or not models:
        models = [entry["model"]] if entry.get("model") else []
    models = [str(model) for model in models if str(model or "").strip()]
    try:
        priority = int(entry.get("priority", 100 + index))
    except (TypeError, ValueError):
        priority = 100 + index
    # Deliberately no 'api_key' passthrough: env var name only.
    return {
        "name": name,
        "base_url": base_url,
        "api_key_env": str(entry.get("api_key_env") or _derive_key_env(name)),
        "models": models,
        "priority": priority,
    }


def load_providers(cfg, env=None):
    """Return ordered provider dicts, always including built-in DeepSeek."""
    env = os.environ if env is None else env
    listed = _cfg_get(cfg, "providers", [])
    if not isinstance(listed, list):
        listed = []
    providers = []
    for index, entry in enumerate(listed):
        item = _provider(entry, index)
        if item:
            providers.append(item)

    base_url = _base(
        _cfg_get(cfg, "base_url", None) or env.get("DEEPSEEK_BASE_URL", "")
    ) or DEEPSEEK_BASE_URL
    model = _cfg_get(cfg, "model", "deepseek-flash")
    builtin = {
        "name": "deepseek",
        "base_url": base_url,
        "api_key_env": DEEPSEEK_API_KEY_ENV,
        "models": [str(model or "deepseek-flash")],
        "priority": 0,
    }
    equivalent = {
        (p["base_url"] or "").lower().rstrip("/") for p in providers
    }
    if builtin["base_url"].lower().rstrip("/") not in equivalent:
        providers.append(builtin)
    providers.sort(key=lambda p: p.get("priority", 1000))
    return providers


def resolve_key(provider, env=None):
    """Read the key from the configured env var only; never return a default."""
    env = os.environ if env is None else env
    name = str((provider or {}).get("api_key_env") or "")
    key = env.get(name) if name else None
    if not key:
        raise ProviderError(
            "environment variable %s is not set" % (name or "<unknown>"),
            provider=(provider or {}).get("name"),
            transient=False,
        )
    return str(key)


def _endpoint(base_url):
    base = _base(base_url)
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _content(raw):
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts).strip()
    return ""


def _usage(raw):
    if not isinstance(raw, dict):
        return {}
    mapping = {
        "inputTokens": "input_tokens",
        "cacheReadTokens": "cache_read_tokens",
        "outputTokens": "output_tokens",
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "prompt_cache_hit_tokens": "cache_read_tokens",
        "prompt_cache_miss_tokens": "cache_miss_tokens",
    }
    result = {}
    for source, target in mapping.items():
        value = raw.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[target] = int(value)
    if "total_tokens" not in result and result:
        result["total_tokens"] = (
            result.get("input_tokens", 0) + result.get("output_tokens", 0)
        )
    return result


def _cost(model, usage):
    prices = PRICES.get(str(model or ""))
    if not prices or not usage:
        return None
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    miss = max(input_tokens - cache_read, 0)
    output = int(usage.get("output_tokens", 0) or 0)
    total = (cache_read * prices["hit"] + miss * prices["miss"]
             + output * prices["out"]) / 1000000.0
    return round(total, 6)


def _post(url, key, payload, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return int(getattr(response, "status", 200)), json.loads(body.decode("utf-8"))


def _attempt(provider, status, error=None):
    return {
        "provider": provider.get("name") if provider else None,
        "status": status,
        "error": None if error is None else _redact(error),
    }


def complete(messages, model=None, providers=None, timeout=60.0, env=None,
             max_tokens=None, temperature=None, reasoning_effort=None):
    """Try providers in order; fall back only on transient failures."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    env = os.environ if env is None else env
    ordered = list(providers or load_providers({}, env))
    ordered.sort(key=lambda p: p.get("priority", 1000))

    requested = model
    if isinstance(model, str) and ":" in model:
        prefix, _, rest = model.partition(":")
        matched = [p for p in ordered if p.get("name") == prefix]
        if matched:
            ordered = matched
            requested = rest

    started = time.monotonic()
    attempts = []
    last = None
    for provider in ordered:
        name = provider.get("name")
        try:
            key = resolve_key(provider, env)
        except ProviderError as exc:
            exc.provider = name
            last = exc
            attempts.append(_attempt(provider, None, str(exc)))
            continue
        chosen = requested or (provider.get("models") or ["deepseek-flash"])[0]
        payload = {
            "model": chosen,
            "messages": messages,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if reasoning_effort:
            payload["reasoning_effort"] = str(reasoning_effort)
        url = _endpoint(provider.get("base_url"))
        try:
            status, data = _post(url, key, payload, timeout)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            error = ProviderError(
                "HTTP %s from %s%s" % (
                    code, name, (": " + detail) if detail else ""),
                provider=name,
                status=code,
                transient=code in _TRANSIENT_STATUSES or code >= 500,
            )
            attempts.append(_attempt(provider, code, str(error)))
            last = error
            if not error.transient:
                error.attempts = attempts
                raise error from None
            continue
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            error = ProviderError(
                "request to %s failed: %s" % (name, _redact(exc)),
                provider=name,
                transient=True,
            )
            attempts.append(_attempt(provider, None, str(error)))
            last = error
            continue
        except ValueError as exc:
            error = ProviderError(
                "invalid JSON from %s: %s" % (name, _redact(exc)),
                provider=name,
                transient=True,
            )
            attempts.append(_attempt(provider, None, str(error)))
            last = error
            continue

        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if choices else None
        answer = _content((message or {}).get("content"))
        if not answer and choices:
            answer = _content(choices[0].get("text"))
        if not answer:
            error = ProviderError(
                "empty completion from %s" % name,
                provider=name, status=status, transient=True,
            )
            attempts.append(_attempt(provider, status, str(error)))
            last = error
            continue
        actual_model = str(data.get("model") or chosen)
        usage = _usage(data.get("usage"))
        attempts.append(_attempt(provider, status or 200))
        return {
            "answer": answer,
            "provider": name,
            "model": actual_model,
            "usage": usage,
            "cost": _cost(actual_model, usage),
            "seconds": round(time.monotonic() - started, 3),
            "attempts": attempts,
        }

    error = ProviderError(
        "all providers failed" + (": " + str(last) if last else ""),
        provider=getattr(last, "provider", None),
        status=getattr(last, "status", None),
        transient=True,
    )
    error.attempts = attempts
    raise error


def consult(prompt, models=None, providers=None, timeout=60.0, env=None,
            max_workers=4, system=None, reasoning_effort=None,
            max_tokens=None, temperature=None):
    """Ask each requested model once in parallel; never synthesise answers."""
    env = os.environ if env is None else env
    ordered = list(providers or load_providers({}, env))
    ordered.sort(key=lambda p: p.get("priority", 1000))

    requested = [str(m) for m in (models or []) if str(m or "").strip()]
    if not requested:
        requested = [
            p["models"][0] for p in ordered if p.get("models")
        ][:3]
    if not requested:
        raise ProviderError("no models to consult", transient=False)

    jobs = []
    for spec in requested:
        target = ordered
        chosen = spec
        if ":" in spec:
            prefix, _, rest = spec.partition(":")
            matched = [p for p in ordered if p.get("name") == prefix]
            if matched:
                target = matched
                chosen = rest
        jobs.append((spec, chosen, target))

    def _one(job):
        spec, chosen, target = job
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            result = complete(
                messages, model=chosen, providers=target, timeout=timeout,
                env=env, reasoning_effort=reasoning_effort,
                max_tokens=max_tokens, temperature=temperature,
            )
            result["model_requested"] = spec
            return result
        except Exception as exc:
            return {
                "model_requested": spec,
                "error": _redact(exc),
                "provider": getattr(exc, "provider", None)
                or (target[0].get("name") if target else None),
            }

    workers = max(1, min(int(max_workers or 1), len(jobs)))
    results = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, job): index
                   for index, job in enumerate(jobs)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results
