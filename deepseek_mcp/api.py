"""DeepSeek HTTP API client and text-cost estimation.

Standard library only. The API key is read from the environment at call time and
is never logged, stored or returned.
"""

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request

PRICES = {
    # USD per 1M tokens. Published DeepSeek off-peak prices; peak hours doubled
    # for the direct text API (kept from v2 for backward-compatible estimates).
    "deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.60},
    "deepseek-v4-pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
}

# Harness session logs are priced with the published per-token rates, no peak
# multiplier. These are the same base rates as above.
HARNESS_PRICES = {
    "deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.60},
    "deepseek-v4-pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
}

PERSONAS = [
    "You are a careful domain expert. Give the most accurate, well-reasoned answer.",
    "You are a skeptic. Look for traps, edge cases and wrong assumptions before answering.",
    "You are a creative problem solver. Consider unusual approaches, then give your best answer.",
    "You are a step-by-step thinker. Break the problem down carefully, then answer.",
    "You are a practical implementer. Focus on what actually works in real life.",
    "You are a critic. Draft an answer, find its weaknesses, then give the corrected version.",
    "You are a simplifier. Find the clearest, most direct correct answer.",
    "You are a senior reviewer with very high standards for correctness and completeness.",
    "You are a contrarian. Check whether the obvious answer is wrong; answer with what is right.",
    "You are a teacher. Make sure the answer is correct and easy to follow.",
]

JUDGE_SYSTEM = (
    "You are the JUDGE of a team of AI agents. You receive a task and several candidate answers. "
    "Evaluate them for correctness, completeness and usefulness. Then write the best possible "
    "final answer: pick the best candidate or merge the strongest parts, and fix any errors. "
    "Reply with ONLY a JSON object, no other text:\n"
    '{"best_candidate": <number>, "confidence": <0..1, how sure you are the final answer is '
    'right and complete>, "agreement": <0..1, how much the candidates agree>, '
    '"reason": "<one or two sentences>", "final_answer": "<the final answer, complete>"}'
)


_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE)
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s\"']+")


def redact_secrets(text):
    """Remove the configured API key and obvious key material from text."""
    text = str(text or "")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        text = text.replace(key, "[redacted-api-key]")
    text = _SECRET_RE.sub("[redacted-api-key]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _HOME_PATH_RE.sub("~", text)
    return text


def fmt_cost(cost):
    return "n/a" if cost is None else "%.6f" % float(cost)


def is_peak(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.weekday() >= 5:
        return False
    return 1 <= now.hour < 4 or 6 <= now.hour < 10


def estimate_cost(model, usage):
    """Estimate a direct text API cost in USD (peak hours doubled)."""
    prices = PRICES.get(model)
    if not prices or not usage:
        return None
    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max((usage.get("prompt_tokens", 0) or 0) - hit, 0)
    out = usage.get("completion_tokens", 0) or 0
    cost = (hit * prices["hit"] + miss * prices["miss"] + out * prices["out"]) / 1000000.0
    return round(cost * (2 if is_peak() else 1), 6)


def call_deepseek(messages, model, max_tokens=None, reasoning_effort=None,
                  temperature=None, timeout=None, base_url=None, api_key=None):
    """POST to /chat/completions. Returns the decoded JSON response."""
    key = (api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set. Add it to the server's env in your MCP config.")
    base = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
    body = {"model": model, "messages": messages, "stream": False}
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if temperature is not None:
        body["temperature"] = float(temperature)
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or 600.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError("DeepSeek API error %s: %s" % (exc.code, redact_secrets(detail)))
    except urllib.error.URLError as exc:
        raise RuntimeError("Could not reach DeepSeek: %s" % (exc.reason,))
    except json.JSONDecodeError:
        raise RuntimeError("DeepSeek returned a response that was not valid JSON")


def one_agent(messages, model, reasoning, max_tokens=None, temperature=None,
              timeout=None, base_url=None, api_key=None):
    """Run one call; return answer, reasoning, usage, estimated cost and seconds."""
    started = time.time()
    response = call_deepseek(messages, model, max_tokens, reasoning, temperature,
                             timeout, base_url, api_key)
    message = (response.get("choices") or [{}])[0].get("message", {}) or {}
    usage = response.get("usage") or {}
    return {
        "answer": message.get("content") or "",
        "reasoning": message.get("reasoning_content"),
        "usage": usage,
        "cost": estimate_cost(model, usage),
        "seconds": round(time.time() - started, 1),
    }
