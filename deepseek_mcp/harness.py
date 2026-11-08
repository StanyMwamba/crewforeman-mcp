"""DeepSeek Harness integration: headless execution, --json event parsing and
session-log cost accounting.

Harness writes a session transcript per run under
``~/.dsh/sessions/<cwd-encoded>/<session-id>/session.v4.jsonl.zstd``. The logs
are compressed with zstd; this module uses the ``zstd`` binary when available
and otherwise reports cost as unknown (never guesses when nothing is readable).
"""

import datetime as dt
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import api
from . import authority
from . import budget as budget_mod
from . import checkpoints
from . import health
from . import concurrency
from . import events as events_mod
from . import observability
from . import platforms
from . import progress
from . import render
from . import review_context
from . import review_progress as review_progress_mod
from . import safety
from . import scaling
from . import spend as spend_mod
from . import status as status_mod
from . import transport as transport_mod
from . import work as work_mod
from .quality import (DEFAULT_QUALITY_TARGET, build_finisher_task, build_harness_task,
                      build_review_task, build_revision_task,
                      build_score_reprompt_task, choose_agents,
                      convergence_decision, extract_quality_score, parse_review,
                      persist_signatures, plan_escalation, quality_default,
                      recurring_lessons)
from .skills import (append_lesson, detect_skill, detect_skill_legacy,
                     detect_skill_match, distill_lesson, lesson_signal,
                     load_skill)
from .store import find_run, load_runs, new_run, save_run

TOOL_TYPES = ("tool_call", "tool/call", "tool-call", "tool_use", "tool-use")
FINAL_TYPES = ("final", "result", "done", "completion", "completed")
ERROR_TYPES = ("error", "failed", "failure")

# Watchdog tuning: how often the monitor checks for silence, and how long a
# terminated Harness process gets to exit before it is killed.
STALL_POLL_SECONDS = 0.25
STOP_GRACE_SECONDS = 2.0
# After the direct child exits, keep draining inherited stdout for at most this
# long: a grandchild may flush one last event, but a silent or flooding
# grandchild must never hold the reader open indefinitely.
EXIT_DRAIN_SECONDS = 1.0
# A rescue finisher is deliberately short, and never gets zero seconds just
# because the builder used the whole per-process max_seconds budget.
FINISHER_MAX_SECONDS = 480.0
FINISHER_FLOOR_SECONDS = 60.0


# --------------------------------------------------------------------- cwd/logs

def encode_workspace(workspace):
    """Encoded session directory for a cwd; one shared Harness-parity helper."""
    return progress.encode_workspace(workspace)


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _filter_session_ids(paths, session_ids):
    if not session_ids:
        return paths
    return [path for path in paths if path.parent.name in session_ids]


def find_session_logs(cfg, workspace, since=None, session_ids=None, session_dirs=None):
    """Find session logs for a workspace, newest first.

    First looks in the encoded workspace directory. If that is missing, scans
    recent logs elsewhere under the sessions root and matches the ``cwd`` in the
    session header (reading ~/.dsh/sessions is explicitly allowed).
    ``session_dirs`` adds extra session roots (for example a reviewer's private
    DSH_HOME) without changing the default single-root behaviour.
    """
    bases = []
    for root in [cfg.dsh_sessions_dir] + list(session_dirs or []):
        try:
            base = Path(root).expanduser()
        except TypeError:
            continue
        if base.is_dir() and base not in bases:
            bases.append(base)
    if not bases:
        return []
    # A session log for this run is created/written after ``since``. Do not
    # include prior runs' logs from the same shared workspace.
    cutoff = (time.time() - 300) if since is None else (float(since) - 5)
    found = []
    for base in bases:
        encoded_dir = base / encode_workspace(workspace)
        if not encoded_dir.is_dir():
            continue
        for pattern in ("*.jsonl.zstd", "*.jsonl.zst", "*.jsonl"):
            for path in encoded_dir.rglob(pattern):
                try:
                    if path.is_file() and path.stat().st_mtime >= cutoff:
                        found.append(path)
                except OSError:
                    continue
    found = _filter_session_ids(found, session_ids)
    if found:
        return sorted(set(found), key=_mtime, reverse=True)

    # Fallback: other workspace folders. Only inspect recent, small-ish files.
    candidates = []
    for base in bases:
        try:
            for pattern in ("*.jsonl.zstd", "*.jsonl.zst", "*.jsonl"):
                for path in base.rglob(pattern):
                    try:
                        if path.is_file() and path.stat().st_mtime >= cutoff:
                            candidates.append(path)
                    except OSError:
                        continue
        except OSError:
            continue
    if not candidates:
        return []
    candidates.sort(key=_mtime, reverse=True)
    targets = {str(workspace), str(Path(workspace).resolve())}
    for path in candidates[:100]:
        text = decompress_log(path, cfg)
        if text is None:
            continue
        first = next((line for line in text.splitlines() if line.strip()), "")
        try:
            header = json.loads(first)
        except Exception:
            continue
        if isinstance(header, dict) and str(header.get("cwd") or "") in targets:
            found.append(path)
    found = _filter_session_ids(found, session_ids)
    return sorted(set(found), key=_mtime, reverse=True)


def resolve_zstd(cfg):
    """Find zstd even when the server was launched with a minimal PATH."""
    binary = shutil.which(cfg.zstd_bin or "zstd")
    if binary:
        return binary
    for candidate in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", "/usr/bin/zstd"):
        if Path(candidate).is_file():
            return candidate
    return None


def decompress_log(path, cfg):
    """Return the log text, or ``None`` if the zstd binary is unavailable."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".json", ".log"):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
    binary = resolve_zstd(cfg)
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "-dc", str(path)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------------- cost parse

def _num(container, names):
    for name in names:
        value = container.get(name)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _model_from_event(obj):
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    source = message.get("source") if isinstance(message.get("source"), dict) else {}
    data_source = data.get("source") if isinstance(data.get("source"), dict) else {}
    candidates = (
        source.get("model"), message.get("model"), data.get("model"),
        data_source.get("model"), obj.get("model"),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _usage_from_event(obj):
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    for candidate in (data.get("usage"), message.get("usage"), obj.get("usage")):
        if isinstance(candidate, dict):
            if any(key in candidate for key in (
                    "inputTokens", "input_tokens", "prompt_tokens", "cacheReadTokens",
                    "outputTokens", "output_tokens", "completion_tokens")):
                return candidate
    return None


def _normalise_prices(entry):
    """Normalise a configured price row to miss/hit/out per million tokens."""
    if not isinstance(entry, dict):
        return None
    miss = entry.get("input", entry.get("miss"))
    hit = entry.get("cache_read", entry.get("hit"))
    out = entry.get("output", entry.get("out"))
    # All three rates are required: turning a partial row into zero would
    # fabricate a low cost and claim cost_known=true.
    if miss is None or hit is None or out is None:
        return None
    try:
        return {"miss": float(miss), "hit": float(hit), "out": float(out)}
    except (TypeError, ValueError):
        return None


def _prices_for(model, prices=None):
    """Resolve model prices, preferring configured reviewer prices.

    Configured rows accept ``input``/``cache_read``/``output`` (or the legacy
    ``miss``/``hit``/``out`` aliases). DeepSeek's built-in HARNESS_PRICES stay
    authoritative whenever the configured table has no row for the model.
    """
    table = prices if isinstance(prices, dict) else {}
    for known, entry in table.items():
        if model == known:
            resolved = _normalise_prices(entry)
            if resolved is not None:
                return resolved, known
    for known, entry in table.items():
        if model and str(model).startswith(str(known)):
            resolved = _normalise_prices(entry)
            if resolved is not None:
                return resolved, known
    if model in api.HARNESS_PRICES:
        return api.HARNESS_PRICES[model], model
    for known, known_prices in api.HARNESS_PRICES.items():
        if model and str(model).startswith(known):
            return known_prices, known
    return None, model or "unknown"


def parse_session_usage(text, default_model="deepseek-flash", prices=None):
    """Sum usage from decoded session JSONL.

    Deduplicates repeated usage attached to the same assistant message. Input
    tokens are uncached input; cache reads are billed at the cache-hit rate.
    """
    input_tokens = cache_read = output_tokens = 0
    messages = 0
    models = {}
    unknown_models = []
    seen = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        usage = _usage_from_event(obj)
        if not usage:
            continue
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        key = message.get("id") or data.get("id") or obj.get("id")
        if not key:
            seq = obj.get("seq", data.get("seq"))
            key = ((obj.get("type"), seq, message.get("role"))
                   if seq is not None else ("no-stable-key", messages))
        if key in seen:
            continue
        seen.add(key)
        inp = _num(usage, ("inputTokens", "input_tokens", "prompt_tokens", "uncachedInputTokens"))
        cache = _num(usage, ("cacheReadTokens", "cache_read_tokens", "prompt_cache_hit_tokens"))
        out = _num(usage, ("outputTokens", "output_tokens", "completion_tokens"))
        input_tokens += inp
        cache_read += cache
        output_tokens += out
        messages += 1
        model = _model_from_event(obj) or default_model
        bucket = models.setdefault(model, {"input": 0, "cache_read": 0, "output": 0})
        bucket["input"] += inp
        bucket["cache_read"] += cache
        bucket["output"] += out
        if _prices_for(model, prices)[0] is None and model not in unknown_models:
            unknown_models.append(model)

    total_cost = 0.0
    for model, bucket in models.items():
        resolved_prices, _resolved = _prices_for(model, prices)
        if not resolved_prices:
            continue
        total_cost += (
            bucket["input"] * resolved_prices["miss"]
            + bucket["cache_read"] * resolved_prices["hit"]
            + bucket["output"] * resolved_prices["out"]
        ) / 1000000.0

    cost_known = bool(messages) and not unknown_models
    return {
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + cache_read + output_tokens,
        "messages": messages,
        "models": models,
        "unknown_models": unknown_models,
        "estimated_cost_usd": round(total_cost, 6) if cost_known else None,
        "cost_known": cost_known,
    }


def read_session_usage(cfg, workspace, since, session_ids=None, exclude_paths=None,
                       session_dirs=None, prices=None, default_model="deepseek-flash"):
    """Aggregate usage for all session logs of a run. Returns ``(usage, paths, reason)``."""
    paths = find_session_logs(cfg, workspace, since, session_ids=session_ids,
                              session_dirs=session_dirs)
    if exclude_paths:
        excluded = {str(path) for path in exclude_paths}
        paths = [path for path in paths if str(path) not in excluded]
    if not paths:
        roots = [str(cfg.dsh_sessions_dir)]
        roots += [str(Path(root).expanduser()) for root in (session_dirs or [])]
        return None, [], "no session log found under %s" % ", ".join(roots)
    aggregate = {
        "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "messages": 0, "models": {}, "unknown_models": [],
        "estimated_cost_usd": 0.0, "cost_known": True,
    }
    readable = []
    unreadable = 0
    for path in paths:
        text = decompress_log(path, cfg)
        if text is None:
            unreadable += 1
            continue
        first = next((line for line in text.splitlines() if line.strip()), "")
        try:
            header = json.loads(first)
        except Exception:
            header = {}
        header_cwd = header.get("cwd") if isinstance(header, dict) else None
        workspace_names = {str(workspace), str(Path(workspace).resolve())}
        if header_cwd and str(header_cwd) not in workspace_names:
            # Encoded directory names are lossy; do not bill another workspace.
            unreadable += 1
            continue
        readable.append(str(path))
        parsed = parse_session_usage(text, default_model=default_model,
                                     prices=prices)
        for key in ("input_tokens", "cache_read_tokens", "output_tokens",
                    "total_tokens", "messages"):
            aggregate[key] += parsed[key]
        aggregate["estimated_cost_usd"] += parsed["estimated_cost_usd"] or 0.0
        for model, bucket in parsed["models"].items():
            target = aggregate["models"].setdefault(model, {"input": 0, "cache_read": 0, "output": 0})
            for field in ("input", "cache_read", "output"):
                target[field] += bucket[field]
        for model in parsed["unknown_models"]:
            if model not in aggregate["unknown_models"]:
                aggregate["unknown_models"].append(model)
    if not readable:
        reason = "session logs found but zstd is unavailable or unreadable"
        if unreadable and resolve_zstd(cfg) is None:
            reason = "zstd binary not found; harness cost unknown"
        return None, [str(p) for p in paths], reason
    if unreadable:
        aggregate["cost_known"] = False
        aggregate["estimated_cost_usd"] = None
        reason = ("%d session log(s) could not be read; cost unknown"
                  % unreadable)
    elif aggregate["unknown_models"] or aggregate["messages"] == 0:
        aggregate["cost_known"] = False
        aggregate["estimated_cost_usd"] = None
        if (prices is not None and aggregate["unknown_models"]
                and all(model == "unknown-reviewer-model"
                        for model in aggregate["unknown_models"])):
            reason = ("reviewer model name missing from session logs; no "
                      "configured price can be applied")
        elif prices is not None and aggregate["unknown_models"]:
            reason = ("no configured reviewer price for %s"
                      % ", ".join(aggregate["unknown_models"]))
        else:
            reason = "unrecognised model or no usage entries in session logs"
    else:
        aggregate["cost_known"] = True
        aggregate["estimated_cost_usd"] = round(aggregate["estimated_cost_usd"], 6)
        reason = None
    return aggregate, readable, reason


def _reviewer_usage_aggregate(entries):
    """Aggregate token usage and known cost across reviewer review entries."""
    input_tokens = cached_tokens = output_tokens = total_tokens = 0
    models = {}
    known = True
    cost = 0.0
    seen = False
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("kind", "review") != "review":
            continue
        seen = True
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        inp = entry.get("tokens_in")
        cached = entry.get("cache_read_tokens")
        out = entry.get("tokens_out")
        total = entry.get("total_tokens")
        if inp is None:
            inp = usage.get("input_tokens")
        if cached is None:
            cached = usage.get("cache_read_tokens")
        if out is None:
            out = usage.get("output_tokens")
        if total is None:
            total = usage.get("total_tokens")
        inp, cached, out = int(inp or 0), int(cached or 0), int(out or 0)
        total = int(total if total is not None else inp + cached + out)
        input_tokens += inp
        cached_tokens += cached
        output_tokens += out
        total_tokens += total
        for model, bucket in (entry.get("models") or {}).items():
            if not isinstance(bucket, dict):
                continue
            target = models.setdefault(model, {"input": 0, "cache_read": 0, "output": 0})
            for field in ("input", "cache_read", "output"):
                target[field] += int(bucket.get(field) or 0)
        if entry.get("cost_known") and isinstance(entry.get("cost"), (int, float)):
            cost += float(entry["cost"])
        else:
            known = False
    known = bool(known and seen)
    return {
        "input": input_tokens,
        "cached": cached_tokens,
        "output": output_tokens,
        "total": total_tokens,
        "models": models,
        "cost_known": known,
        "estimated_cost_usd": round(cost, 6) if known else None,
    }


# ----------------------------------------------------------------- event parse

def _content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return ""


def parse_event(event):
    """Normalise one Harness ``--json`` event. Returns a dict or ``None``."""
    if not isinstance(event, dict):
        return None
    typ = str(event.get("type") or "").lower()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if typ in TOOL_TYPES:
        name = event.get("name") or event.get("tool") or data.get("name")
        if not isinstance(name, str):
            name = None
        if not name:
            for candidate in (event.get("call"), data.get("call"), data.get("tool")):
                if isinstance(candidate, dict) and candidate.get("name"):
                    name = candidate.get("name")
                    break
        return {"kind": "tool", "name": str(name or "tool")}
    if typ in ("session", "session/start"):
        session_id = (event.get("id") or event.get("sessionId")
                      or data.get("id") or data.get("sessionId"))
        return {"kind": "session", "id": str(session_id) if session_id else None}
    if typ in FINAL_TYPES:
        text = (event.get("text") or event.get("answer") or event.get("content")
                or event.get("result") or data.get("text") or data.get("answer")
                or data.get("content") or data.get("result") or data.get("output"))
        return {"kind": "final", "text": _content_text(text)}
    if typ in ERROR_TYPES:
        message = (event.get("message") or event.get("error") or data.get("message")
                   or data.get("error") or json.dumps(event)[:500])
        if not isinstance(message, str):
            message = json.dumps(message)[:500]
        return {"kind": "error", "message": message}
    if typ in ("subagent/catalog", "subagent-catalog"):
        # The parent stream advertises the child ids it spawned.  ChildActivity
        # uses this allow-list so a concurrent run in the same workspace cannot
        # refresh this run's watchdog.
        ids = []
        for key in ("childId", "child_id", "sessionId", "id"):
            value = data.get(key) or event.get(key)
            if isinstance(value, str) and value:
                ids.append(value)
        for value in data.get("childIds") or data.get("child_ids") or []:
            if isinstance(value, str) and value:
                ids.append(value)
        if ids:
            return {"kind": "child", "ids": ids}
    if typ in ("assistant/message", "assistant-message"):
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        text = _content_text(message.get("content"))
        return {"kind": "assistant", "text": text}
    return {"kind": "other", "type": typ}


def parse_events(lines):
    """Parse an iterable of JSON strings/dicts into a run summary."""
    tools = {}
    events = 0
    errors = []
    final = None
    last_assistant = None
    for line in lines:
        if isinstance(line, dict):
            event = line
        else:
            line = str(line).strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
        events += 1
        parsed = parse_event(event)
        if not parsed:
            continue
        if parsed["kind"] == "tool":
            tools[parsed["name"]] = tools.get(parsed["name"], 0) + 1
        elif parsed["kind"] == "final":
            final = parsed["text"]
        elif parsed["kind"] == "error":
            errors.append(parsed["message"])
        elif parsed["kind"] == "assistant" and parsed["text"]:
            last_assistant = parsed["text"]
    if final is None:
        final = last_assistant
    return {"tools": tools, "events": events, "final": final, "errors": errors}


# -------------------------------------------------------------------- harness

def _quality_fixes(workspace):
    path = Path(workspace) / "QUALITY_REPORT.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and re.search(r"\b(?:fixed|resolved|corrected|addressed)\b", line, re.IGNORECASE):
            lines.append(line)
    return "\n".join(lines[:8]) or None


def _quality_defects(workspace):
    path = Path(workspace) / "QUALITY_REPORT.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "[x]" in line.lower():
            continue
        if re.search(r"\b(?:no|none|zero|without)\b", line, re.IGNORECASE) and                 re.search(r"\b(?:defect|issue|fail(?:ed)?|missing|overlap|overflow|clipped)\b",
                          line, re.IGNORECASE):
            continue
        if re.search(r"\b(?:defect|issue|fail(?:ed)?|missing|overlap|overflow|clipped)\b",
                     line, re.IGNORECASE):
            lines.append(line)
    return "\n".join(lines[:12]) or None


def _review_round_usage(record, cfg):
    """Normalise per-round reviewer usage and price it from reviewer_prices.

    Every review/revision entry keeps input (uncached), cache read, output and
    total tokens, the per-model buckets, cost and cost_known. Cost is reported
    only when every model resolves; otherwise ``cost`` stays None and
    ``cost_known`` is False so callers can say why.
    """
    record = record if isinstance(record, dict) else {}
    usage = dict(record.get("usage") or {})
    tokens_in = record.get("tokens_in")
    cache = record.get("cache_read_tokens")
    out = record.get("tokens_out")
    total = record.get("total_tokens")
    if tokens_in is None:
        tokens_in = usage.get("input_tokens")
    if cache is None:
        cache = usage.get("cache_read_tokens")
    if out is None:
        out = usage.get("output_tokens")
    if total is None:
        total = usage.get("total_tokens")
    tokens_in, cache, out = int(tokens_in or 0), int(cache or 0), int(out or 0)
    total = int(total if total is not None else tokens_in + cache + out)
    models = dict(record.get("models") or {})
    model = str(record.get("model") or getattr(cfg, "dsh_model", None)
                or "deepseek-flash")
    if not models:
        models = {model: {"input": tokens_in, "cache_read": cache,
                          "output": out}}
    cost = record.get("cost")
    known = record.get("cost_known")
    if cost is None or known is False:
        priced = 0.0
        resolvable = True
        for name, bucket in models.items():
            bucket = bucket if isinstance(bucket, dict) else {}
            rates, _hit = _prices_for(
                name, getattr(cfg, "reviewer_prices", None))
            if rates is None:
                resolvable = False
                break
            priced += (int(bucket.get("input") or 0) * rates["miss"]
                       + int(bucket.get("cache_read") or 0) * rates["hit"]
                       + int(bucket.get("output") or 0) * rates["out"]) / 1000000.0
        if resolvable:
            cost = round(priced, 6)
            known = True
    if cost is None:
        known = False
    return {
        "tokens_in": tokens_in,
        "cache_read_tokens": cache,
        "tokens_out": out,
        "total_tokens": total,
        "usage": dict(usage, input_tokens=tokens_in,
                      cache_read_tokens=cache, output_tokens=out,
                      total_tokens=total),
        "models": models,
        "model": model,
        "cost": cost,
        "cost_known": bool(known),
    }


def _merge_preflight_usage(record, preflight, cfg):
    """Fold pre-flight reviewer usage into the paid review round record.

    Pre-flight is a near-free call through the same reviewer path, but it is
    still a reviewer call: its tokens/cost must appear in the round entry and
    therefore in ``reviewer_usage`` too.
    """
    merged = dict(record or {})
    preflight_record = dict(preflight or {})
    if ("cost" not in preflight_record
            and "estimated_cost_usd" in preflight_record):
        preflight_record["cost"] = preflight_record.get("estimated_cost_usd")
    usage = _review_round_usage(preflight_record, cfg)
    if not usage.get("tokens_in") and not usage.get("tokens_out"):
        # A failed/empty pre-flight still gets an explicit zero record so the
        # round can say it was charged/checked.
        pass
    merged["preflight_usage"] = usage
    if not any(int(usage.get(field) or 0) for field in
               ("tokens_in", "cache_read_tokens", "tokens_out",
                "total_tokens")):
        # A ping with no readable usage must not perturb the paid round's
        # totals or make a known review cost unknown by adding a zero model.
        return merged
    for field in ("tokens_in", "cache_read_tokens", "tokens_out",
                  "total_tokens"):
        merged[field] = (int(merged.get(field) or 0)
                         + int(usage.get(field) or 0))
    models = dict(merged.get("models") or {})
    for model, bucket in (usage.get("models") or {}).items():
        if not isinstance(bucket, dict):
            continue
        target = models.setdefault(
            model, {"input": 0, "cache_read": 0, "output": 0})
        for field in ("input", "cache_read", "output"):
            target[field] = int(target.get(field) or 0) + int(
                bucket.get(field) or 0)
    merged["models"] = models
    base_usage = dict(merged.get("usage") or {})
    base_usage.update({
        "input_tokens": merged["tokens_in"],
        "cache_read_tokens": merged["cache_read_tokens"],
        "output_tokens": merged["tokens_out"],
        "total_tokens": merged["total_tokens"],
    })
    merged["usage"] = base_usage
    if (merged.get("cost_known") and usage.get("cost_known")
            and merged.get("cost") is not None
            and usage.get("cost") is not None):
        merged["cost"] = round(
            float(merged["cost"]) + float(usage["cost"]), 6)
        merged["cost_known"] = True
    else:
        # Let _review_round_usage try to reprice the combined model buckets;
        # otherwise the combined cost remains explicitly unknown.
        merged["cost"] = None
        merged["cost_known"] = False
    return merged


def _result_text(run, path):
    tools = run.get("tools_used") or {}
    tool_text = ", ".join("%s x%d" % (name, count)
                          for name, count in sorted(tools.items(), key=lambda item: -item[1])) or "none"
    head = "[DeepSeek Harness run %s | %s s" % (run["id"], run.get("seconds", "?"))
    if run.get("rescued"):
        head += " | rescued after %s" % (
            "stall" if (run.get("rescue") or {}).get("reason") == "stalled" else "time limit")
    if run.get("stalled"):
        head += " | stalled: %s" % (run.get("stalled_step") or "no progress event")
    if run.get("child_progress_events"):
        head += " | child activity: %d event(s)" % int(run.get("child_progress_events") or 0)
    retry = run.get("builder_retry") or {}
    if retry and retry.get("status") != "skipped":
        head += " | retried once: %s" % (retry.get("reason") or "transient")
    if run.get("subagents"):
        head += " | subagents %d (%s, concurrency %s)" % (
            run["subagents"], run.get("subagent_reason") or "", run.get("concurrency", "?"))
    else:
        head += " | single agent"
    if run.get("quality_used") or run.get("independent_review"):
        score = run.get("quality_score")
        if score is None:
            head += " | quality not reported (unscored review)"
        else:
            head += " | quality %.2f/10" % score
        if run.get("independent_review"):
            head += " (independent)"
            if run.get("self_score") is not None:
                head += " | self_score %.2f/10" % run["self_score"]
            head += " | review rounds %s" % run.get("quality_rounds_used", 0)
        if run.get("quality_report_missing"):
            head += " (QUALITY_REPORT.md missing)"
        if run.get("review_status") == "error":
            head += " (review error: %s)" % (run.get("review_error") or "unknown")
    head += " | tools: %s" % tool_text
    if run.get("cost_source") == "session_logs":
        cost_text = ("$%s" % api.fmt_cost(run.get("estimated_cost_usd"))
                     if run.get("estimated_cost_usd") is not None else "unknown")
        head += " | tokens: in %s uncached + cache %s / out %s | est. cost %s" % (
            run.get("tokens_in"), run.get("cache_read_tokens"), run.get("tokens_out"), cost_text)
    else:
        head += " | cost: unknown (%s)" % (run.get("usage_unknown_reason") or "no session log")
    head += " | saved: %s]" % path
    auto_summary = (run.get("skill_auto") or {}).get("summary")
    if auto_summary:
        head += "\nSkills: %s" % auto_summary
    files = run.get("output_files") or []
    if files:
        head += "\nFiles written: " + ", ".join(files)
    rounds = run.get("review_rounds") or []
    if rounds:
        head += "\nReview/revision rounds:"
        for entry in rounds:
            label = "Review" if entry.get("kind") == "review" else "Revision"
            bit = "  - %s %s: %s" % (label, entry.get("round", "?"),
                                      "%s s" % entry.get("seconds", "?"))
            if entry.get("score") is not None:
                bit += ", score %.2f/10" % entry["score"]
            elif entry.get("score_missing"):
                bit += ", score_missing"
            if entry.get("kind") == "review":
                bit += ", %d blocking defect(s)" % int(entry.get("blocking_defects") or 0)
            if entry.get("status") and entry.get("status") != "ok":
                bit += ", %s" % entry.get("status")
            bit += ", input %s (uncached) / cache read %s / output %s / total %s" % (
                entry.get("tokens_in", "?"), entry.get("cache_read_tokens", "?"),
                entry.get("tokens_out", "?"), entry.get("total_tokens", "?"))
            if entry.get("preflight_usage"):
                bit += " (incl. pre-flight)"
            if entry.get("cost") is not None:
                bit += ", $%s" % api.fmt_cost(entry["cost"])
            else:
                bit += ", cost unknown (reviewer_prices not configured)"
            head += "\n" + bit
    text = head + "\n\n" + (run.get("answer") or "")
    warning = _log_fallback_warning(run)
    if warning:
        text = "[WARNING] " + warning + "\n" + text
    return text


def _bool_value(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _stash_old(workspace, name, run_id, round_no=None):
    """Move a previous run's report aside so it cannot be mistaken for this one."""
    path = Path(workspace) / name
    if not path.is_file():
        return None
    suffix = ".previous-%s%s.md" % (run_id, "-r%d" % round_no if round_no else "")
    target = Path(workspace) / (name[:-3] + suffix)
    try:
        os.replace(str(path), str(target))
        return str(target)
    except OSError:
        return None


def _noise_output(rel):
    low = str(rel).replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    if "/.dsh-tmp-" in "/" + low or base.startswith(".chrome-probe-"):
        return True
    return (base.startswith(("quality_report.previous-", "review.previous-", ".fake"))
            or base in (".ds_store",) or base.endswith((".pyc", ".pyo", ".tmp")))


def _deliverable_files(workspace, output_files):
    """Filter builder/revision outputs down to reviewable deliverables."""
    excluded_names = {"quality_report.md", "review.md", "review.partial.md",
                      "review.partial.json"}
    excluded_prefixes = ("quality_report.previous-", "review.previous-",
                         "review.partial", ".fake",
                         ".git/", "__pycache__/", ".ds_store")
    files = []
    for raw in output_files or []:
        rel = str(raw).replace("\\", "/")
        low = rel.lower()
        base = low.rsplit("/", 1)[-1]
        if base in excluded_names:
            continue
        if low.startswith(excluded_prefixes) or "/__pycache__/" in low:
            continue
        if base.endswith((".pyc", ".pyo", ".tmp")):
            continue
        if rel not in files:
            files.append(rel)
    return sorted(files)


def _read_review(workspace):
    path = Path(workspace) / "REVIEW.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), str(path)
    except Exception:
        return "", None


def _materialize_review(workspace, review):
    """Write the reviewer's final answer to REVIEW.md when it carries a report.

    The reviewer runs read-only, and DSH's own fs sandbox rejects *every*
    mutation in that mode. The connector therefore owns the one allowed file:
    the reviewer returns the complete report as its final answer and this
    function persists it. An explicit wider reviewer_sandbox can still write
    the file itself; an existing full report is never replaced by a short
    completion message.
    """
    answer = (review.get("answer") or "").strip()
    if not answer:
        return None
    # REVIEW.md is a shipped artifact in the user's workspace: never persist
    # key material or an operator home path that the reviewer echoed back.
    answer = api.redact_secrets(answer)
    path = Path(workspace) / "REVIEW.md"
    try:
        existing = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        existing = ""
    lowered = answer.lower()
    full_report = "## score" in lowered or "# independent review" in lowered
    existing_is_report = ("## score" in existing.lower()
                          and "## blocking defects" in existing.lower())
    if not full_report and existing_is_report:
        return str(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".review-", suffix=".md",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(answer.rstrip() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        review["report_written_by"] = "connector"
        return str(path)
    except Exception as exc:
        review["report_write_error"] = api.redact_secrets(str(exc))
        return None


def _checkpoint_status(status):
    """Map an execution status to the checkpoint vocabulary (``done`` etc.)."""
    text = str(status or "").strip().lower()
    if text in ("ok", "done", "completed", "accepted"):
        return "done"
    if text in ("cancelled", "canceled", "interrupted"):
        return "interrupted"
    return "failed"


def _restore_review_report(workspace, data):
    """Restore the accepted reviewer report on resume.

    The checkpoint stores the report text, not just its path: the path may have
    been stashed as ``REVIEW.previous-<run>.md`` by the resume itself. Falling
    back to a score-only stub loses the defects and fix list, which the v3.5.0
    review found.
    """
    # Run records (and checkpoint metadata) are stored through
    # ``api.redact_secrets``, which rewrites home paths to ``~/...``. Expand
    # that back before writing, otherwise the report lands in a literal ``~``
    # directory relative to the process CWD instead of the real workspace.
    workspace = os.path.expanduser(str(workspace))
    target = Path(workspace) / "REVIEW.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    text = data.get("report_text")
    if isinstance(text, str) and text.strip():
        try:
            target.write_text(api.redact_secrets(text).rstrip() + "\n",
                              encoding="utf-8")
            try:
                os.chmod(str(target), 0o600)
            except OSError:
                pass
            return str(target)
        except Exception:
            pass
    try:
        source = Path(str(data.get("report") or ""))
        source_real = os.path.realpath(str(source))
        workspace_real = os.path.realpath(str(workspace))
        if (source.is_file()
                and (source_real == workspace_real
                     or source_real.startswith(workspace_real + os.sep))):
            target.write_text(source.read_text(encoding="utf-8", errors="replace"),
                              encoding="utf-8")
            return str(target)
    except Exception:
        pass
    score = data.get("score")
    if score is None:
        return None
    text = ("# Independent review (restored from checkpoint)\n\n"
            "## Score\nFinal quality score: %s/10\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\nNone\n\n"
            "## Fix list\nNone\n" % score)
    try:
        target.write_text(text, encoding="utf-8")
        return str(target)
    except Exception:
        return None


def _sum_executions(executions):
    total_cost = 0.0
    cost_known = True
    tokens_in = cache_read = tokens_out = total_tokens = 0
    models = {}
    for execution in executions:
        if execution.get("estimated_cost_usd") is None:
            cost_known = False
        else:
            total_cost += float(execution["estimated_cost_usd"])
        tokens_in += execution.get("tokens_in") or 0
        cache_read += execution.get("cache_read_tokens") or 0
        tokens_out += execution.get("tokens_out") or 0
        total_tokens += execution.get("total_tokens") or 0
        for model, bucket in (execution.get("models") or {}).items():
            target = models.setdefault(model, {"input": 0, "cache_read": 0, "output": 0})
            for key in ("input", "cache_read", "output"):
                target[key] += bucket.get(key, 0)
    return {
        "estimated_cost_usd": round(total_cost, 6) if cost_known else None,
        "cost_known": cost_known,
        "tokens_in": tokens_in,
        "cache_read_tokens": cache_read,
        "tokens_out": tokens_out,
        "total_tokens": total_tokens,
        "models": models,
    }


_REPROMPT_MIN_SECONDS = 15.0
_REPROMPT_BLOCKING_RE = re.compile(
    r"(?im)^[\s>*_`\-]*blocking\s+defects?\s*[*_`]*\s*[:=]\s*[*_`]*\s*(\d{1,3})\b")


def _reprompt_bounded_limit(limit, remaining, minimum=_REPROMPT_MIN_SECONDS):
    """Return ``(call_limit, has_time)`` for one score re-prompt call."""
    if remaining is None:
        return limit, True
    if remaining < minimum:
        return None, False
    cap = max(10.0, float(remaining) - 5.0)
    if limit is None:
        return cap, True
    try:
        return min(float(limit), cap), True
    except (TypeError, ValueError):
        return cap, True


def _reprompt_reply_fields(answer):
    """Parse the rigid two-line re-prompt answer; never fabricate a field.

    Returns ``(score, blocking)``.  Either element is ``None`` when its
    contract line is missing: a score-only reply must not be recorded as
    ``Blocking defects: 0``.  The caller keeps the report-derived blocking
    count in that case.
    """
    text = str(answer or "")
    if not text.strip():
        return None, None
    score = extract_quality_score(None, text)
    match = _REPROMPT_BLOCKING_RE.search(text)
    blocking = None
    if match:
        try:
            blocking = int(match.group(1))
        except ValueError:
            blocking = None
    return score, blocking


def _reprompt_record(executions, method, session_id, error=None,
                     fallback_used=False, session_error=None,
                     skipped_reason=None):
    """Normalise the one/two re-prompt executions into the journal record."""
    executions = [item for item in executions if isinstance(item, dict)]
    totals = _sum_executions(executions) if executions else {
        "estimated_cost_usd": 0.0, "cost_known": True,
        "tokens_in": 0, "cache_read_tokens": 0, "tokens_out": 0,
        "total_tokens": 0, "models": {},
    }
    last = executions[-1] if executions else {}
    answer = str(last.get("answer") or "")
    score, blocking = _reprompt_reply_fields(answer)
    logs = []
    for item in executions:
        for name in item.get("session_logs") or []:
            if name not in logs:
                logs.append(name)
    record = {
        "attempted": bool(executions),
        "method": method,
        "status": last.get("status") if executions else "skipped",
        "error": api.redact_secrets(str(
            error if error is not None
            else (last.get("error") if executions else "") or "")),
        "seconds": round(sum(float(item.get("seconds") or 0)
                             for item in executions), 3),
        "tokens_in": totals.get("tokens_in") or 0,
        "cache_read_tokens": totals.get("cache_read_tokens") or 0,
        "tokens_out": totals.get("tokens_out") or 0,
        "total_tokens": totals.get("total_tokens") or 0,
        "estimated_cost_usd": totals.get("estimated_cost_usd"),
        "cost_known": totals.get("cost_known", True),
        "models": totals.get("models") or {},
        "usage": last.get("usage"),
        "session_id": (last.get("transport_session_id")
                       or (executions[0].get("transport_session_id")
                           if executions else None)
                       or session_id),
        "session_logs": logs,
        "score": score,
        "blocking": blocking,
        "contract_complete": bool(score is not None and blocking is not None),
        "fallback_used": bool(fallback_used),
        "reply": answer[:400],
    }
    if session_error:
        record["session_error"] = api.redact_secrets(str(session_error))
    if skipped_reason:
        record["skipped_reason"] = skipped_reason
    return record


def _reprompt_review_score(cfg, workspace, review, report_text, round_no=1,
                           policy=None, transport_name=None, limit=None,
                           cancel_check=None, budget=None, used_logs=None):
    """One score re-prompt for one reviewer report.

    Resume the same reviewer session when it has an id; only a non-ok/dead
    session may fall back to ONE short fresh reviewer call carrying the report.
    The caller records the returned dict and appends it to
    ``reviewer_executions`` when ``attempted`` is true.
    """
    review = review if isinstance(review, dict) else {}
    session_id = review.get("transport_session_id")
    report = str(report_text or review.get("answer") or "")
    remaining = None
    if budget is not None:
        try:
            remaining = budget.remaining_seconds()
        except Exception:
            remaining = None
    call_limit, has_time = _reprompt_bounded_limit(limit, remaining)
    if not has_time:
        return _reprompt_record(
            [], "skipped", session_id,
            error="insufficient job budget for a score re-prompt",
            skipped_reason="insufficient job budget for a score re-prompt")
    if policy is None:
        try:
            policy = safety.resolve({}, cfg, reviewer=True)
        except Exception:
            policy = review.get("sandbox")
    transport_name = (transport_name or review.get("transport")
                      or cfg.get("dsh_transport") or "headless")
    logs = used_logs if used_logs is not None else set()
    executions = []
    session_error = None
    fallback_used = False

    if session_id:
        first = _execute_once(
            cfg, workspace,
            build_score_reprompt_task(report, round_no=round_no),
            limit=call_limit,
            label="reviewer score reprompt round %d" % int(round_no or 1),
            exclude_logs=logs, policy=policy, session_id=session_id,
            transport_name=transport_name, cancel_check=cancel_check)
        executions.append(first)
        logs.update(first.get("session_logs") or [])
        stop_reason = str(first.get("stop_reason") or "").lower()
        dead = (str(first.get("status") or "").lower() != "ok"
                and stop_reason not in ("budget", "cancelled", "canceled",
                                        "interrupted"))
        if dead:
            session_error = first.get("error") or "reviewer session unavailable"
            remaining_after = None
            if budget is not None:
                try:
                    remaining_after = budget.remaining_seconds()
                except Exception:
                    remaining_after = None
            fresh_limit, fresh_has_time = _reprompt_bounded_limit(
                limit, remaining_after)
            if fresh_has_time:
                fallback_used = True
                second = _execute_once(
                    cfg, workspace,
                    build_score_reprompt_task(report, round_no=round_no,
                                              fresh=True),
                    limit=fresh_limit,
                    label=("reviewer score reprompt fresh round %d"
                           % int(round_no or 1)),
                    exclude_logs=logs, policy=policy, session_id=None,
                    transport_name=transport_name,
                    cancel_check=cancel_check)
                executions.append(second)
                logs.update(second.get("session_logs") or [])
        method = ("fresh_after_dead_session" if fallback_used
                  else "same_session")
        last = executions[-1]
        return _reprompt_record(
            executions, method, session_id,
            error=(last.get("error") if str(last.get("status")) != "ok"
                   else None),
            fallback_used=fallback_used, session_error=session_error)

    fresh = _execute_once(
        cfg, workspace,
        build_score_reprompt_task(report, round_no=round_no, fresh=True),
        limit=call_limit,
        label="reviewer score reprompt fresh round %d" % int(round_no or 1),
        exclude_logs=logs, policy=policy, session_id=None,
        transport_name=transport_name, cancel_check=cancel_check)
    executions.append(fresh)
    logs.update(fresh.get("session_logs") or [])
    return _reprompt_record(executions, "fresh", None,
                            error=fresh.get("error"))


def _merge_reprompt_usage(entry, record):
    """Fold a re-prompt's usage into the review entry (additive)."""
    if not isinstance(entry, dict) or not isinstance(record, dict):
        return entry
    # Review entries store the priced cost under ``cost``; normalise that view
    # for ``_sum_executions`` so a known review cost is not lost when the
    # re-prompt itself is unpriced, and vice versa.
    entry_view = dict(entry)
    if (entry_view.get("estimated_cost_usd") is None
            and entry_view.get("cost") is not None):
        entry_view["estimated_cost_usd"] = entry_view.get("cost")
    totals = _sum_executions([entry_view, record])
    for key in ("estimated_cost_usd", "cost_known", "tokens_in",
                "cache_read_tokens", "tokens_out", "total_tokens", "models"):
        if key in totals:
            entry[key] = totals[key]
    # Review entries keep the priced cost under ``cost`` (not
    # ``estimated_cost_usd``); keep both views consistent.
    entry["cost"] = totals.get("estimated_cost_usd")
    entry["cost_known"] = totals.get("cost_known", False)
    try:
        entry["seconds"] = round(
            float(entry.get("seconds") or 0)
            + float(record.get("seconds") or 0), 3)
    except (TypeError, ValueError):
        pass
    entry["score_reprompt"] = record
    entry["score_reprompt_method"] = record.get("method")
    entry["score_reprompt_status"] = record.get("status")
    return entry


def _append_score_to_review(workspace, score, blocking, report_text):
    """Append the re-prompted two lines so REVIEW.md stays self-consistent."""
    path = Path(workspace) / "REVIEW.md"
    suffix = ("\n\nFinal quality score: %s/10\nBlocking defects: %d\n"
              % (round(float(score), 2), int(blocking or 0)))
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(suffix)
            handle.flush()
            os.fsync(handle.fileno())
        return str(report_text or "").rstrip() + suffix
    except Exception:
        return report_text


def _apply_execution(run, execution, include_outputs=True):
    run.update(
        answer=execution.get("answer"),
        exit_code=execution.get("exit_code"),
        error=execution.get("error"),
        tools_used=execution.get("tools_used") or {},
        events=execution.get("events") or 0,
        seconds=execution.get("seconds") or 0,
        stderr_tail=execution.get("stderr_tail") or "",
        session_logs=execution.get("session_logs") or [],
        tokens_in=execution.get("tokens_in"),
        cache_read_tokens=execution.get("cache_read_tokens"),
        tokens_out=execution.get("tokens_out"),
        total_tokens=execution.get("total_tokens"),
        estimated_cost_usd=execution.get("estimated_cost_usd"),
        cost_source=execution.get("cost_source") or "unknown",
        cost_known=execution.get("cost_known"),
        usage=execution.get("usage"),
        models=execution.get("models"),
        usage_unknown_reason=execution.get("usage_unknown_reason"),
        status=execution.get("status"),
        stalled=bool(execution.get("stalled")),
        stalled_step=execution.get("stalled_step"),
        stall_elapsed=execution.get("stall_elapsed"),
        stall_max_seconds=execution.get("stall_max_seconds"),
        stall_cap_hard_max_seconds=execution.get("stall_cap_hard_max_seconds"),
        stall_cap_hit=bool(execution.get("stall_cap_hit")),
        timed_out=bool(execution.get("timed_out")),
        stop_reason=execution.get("stop_reason"),
        child_progress_events=execution.get("child_progress_events") or 0,
        child_sessions_active=execution.get("child_sessions_active") or 0,
        tmpdir_isolated=bool(execution.get("tmpdir_isolated")),
    )
    if include_outputs:
        run["output_files"] = execution.get("output_files") or []


def _positive_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _stall_seconds(cfg):
    """Return the positive stall threshold, or ``None`` when disabled."""
    return _positive_float(getattr(cfg, "stall_seconds", None))


def _phase_kind(label):
    text = str(label or "").lower()
    if text.startswith("reviewer"):
        return "reviewer"
    if text.startswith("revision"):
        return "revision"
    if text.startswith("finisher") or text.startswith("rescue"):
        return "finisher"
    if text.startswith("wave"):
        return "builder"
    return "builder"


def _stall_max_seconds(cfg, label=None):
    """Progress-gated wall-clock cap for one Harness process, phase-scaled.

    A reviewer routinely needs longer than a builder because it reads every
    deliverable; a revision is between the two. The finisher is deliberately
    short. The cap is not a hard deadline: :class:`_StallWatch` extends it
    while the process is still producing progress. The whole-job deadline is
    the only unconditional bound.
    """
    base = _positive_float(getattr(cfg, "stall_max_seconds", None))
    if base is None:
        return None
    kind = _phase_kind(label)
    key = {"reviewer": "cap_reviewer_multiplier",
           "revision": "cap_revision_multiplier",
           "finisher": "cap_finisher_multiplier"}.get(kind, None)
    if key:
        try:
            multiplier = float(getattr(cfg, key, 1.0))
        except (TypeError, ValueError):
            multiplier = 1.0
        if multiplier <= 0:
            # Documented behaviour: a non-positive multiplier disables the
            # cap for that phase rather than clamping it to the 60 s floor.
            return None
        base *= multiplier
    return max(60.0, base)


def _cap_grace(cfg):
    return _positive_float(getattr(cfg, "stall_cap_grace_seconds", 120.0)) or 120.0


def _cap_extension(cfg):
    return _positive_float(getattr(cfg, "stall_cap_extension_seconds", 300.0)) or 300.0


def _stall_cap_hard_max(cfg):
    """Absolute ceiling for cap extensions; None/0 disables only the ceiling."""
    return _positive_float(getattr(cfg, "stall_cap_hard_max_seconds", 7200.0))


class _StallWatch(object):
    """Monotonic timestamps for the silence window and the progress-gated cap.

    ``poll()`` is pure with respect to the injected clock, so tests can drive it
    without a process or thread. The cap is *not* an unconditional kill: when
    the cap is reached but progress arrived within ``grace_seconds``, the
    deadline is extended. This is the fix for the v3.5.0 incident where a
    reviewer and both revision rounds were killed at 900.2/900.3 s while their
    child sessions and output files were demonstrably growing.
    """

    def __init__(self, stall_seconds, clock=None, max_seconds=None,
                 grace_seconds=120.0, extension_seconds=300.0,
                 hard_max_seconds=None):
        self.stall_seconds = stall_seconds
        self.max_seconds = max_seconds
        self.grace_seconds = max(0.0, float(grace_seconds or 0.0))
        self.extension_seconds = max(1.0, float(extension_seconds or 300.0))
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._last = self._started
        self._detail = "process start"
        self._stalled = False
        self._cap_hit = False
        self._cap_hard = False
        self._cap_extensions = 0
        self._cap_deadline = (self._started + float(max_seconds)
                              if max_seconds is not None else None)
        # Progress may extend the cap only up to this absolute ceiling.
        self._hard_deadline = (self._started + float(hard_max_seconds)
                               if hard_max_seconds is not None else None)
        self._elapsed = None
        self._last_at = None
        self._lock = threading.Lock()

    @property
    def enabled(self):
        # The cap is useful on its own: ``stall_seconds=0`` disables the
        # silence timer but must not disable ``stall_max_seconds``.
        return (self.stall_seconds is not None
                or self.max_seconds is not None
                or self._hard_deadline is not None)

    def touch(self, kind, detail=None):
        """Record a progress event. Error events deliberately do not call this."""
        with self._lock:
            if self._stalled:
                return
            self._last = self._clock()
            self._detail = detail or kind

    def poll(self):
        """Return a stop reason string exactly once, or ``None``."""
        with self._lock:
            if self._stalled:
                return None
            now = self._clock()
            if self.stall_seconds is not None and (now - self._last) >= float(self.stall_seconds):
                self._stalled = True
                self._elapsed = round(now - self._last, 1)
                self._last_at = round(self._last - self._started, 1)
                self._reason = "stall"
                return "stall"
            cap_due = (self._cap_deadline is not None
                       and now >= self._cap_deadline)
            hard_due = (self._hard_deadline is not None
                        and now >= self._hard_deadline)
            if not cap_due and not hard_due:
                return None
            if hard_due:
                # The ceiling fires even when progress is recent: a phase can
                # never run past the absolute bound.
                return self._fire_cap(now, hard=True)
            if (now - self._last) <= self.grace_seconds:
                # Still progressing: extend instead of destroying the work.
                candidate = now + self.extension_seconds
                if (self._hard_deadline is not None
                        and candidate >= self._hard_deadline):
                    # Clamp the final extension to the ceiling; the poll at the
                    # ceiling fires even though progress was recent.
                    self._cap_deadline = self._hard_deadline
                    self._cap_extensions += 1
                    return None
                self._cap_deadline = candidate
                self._cap_extensions += 1
                return None
            return self._fire_cap(now, hard=False)

    def _fire_cap(self, now, hard=False):
        self._stalled = True
        self._cap_hit = True
        self._cap_hard = bool(hard)
        self._elapsed = round(now - self._started, 1)
        self._last_at = round(self._last - self._started, 1)
        self._reason = "cap"
        return "cap"

    def cap_hit(self):
        return self._cap_hit

    def cap_extensions(self):
        return self._cap_extensions

    def cap_hard(self):
        return self._cap_hard

    def elapsed(self):
        return self._elapsed

    def last_at(self):
        return self._last_at

    def detail(self):
        return self._detail

    def error(self, label, child_events=0):
        if self._cap_hit:
            events = int(child_events or 0)
            if events > 0:
                lead = "child/workspace activity kept %s alive" % label
            else:
                lead = "parent-only activity capped %s" % label
            ceiling = " (hard ceiling)" if self._cap_hard else ""
            return ("absolute watchdog cap%s: %s for %.1f s (last event: %s at "
                    "%.1f s; progress events: %d; extensions: %d); process stopped"
                    % (ceiling, lead, self._elapsed or 0.0, self._detail,
                       self._last_at or 0.0, events, self._cap_extensions))
        return ("stall: no progress event for %.1f s in %s (last event: %s at "
                "%.1f s); process stopped" % (
                    self._elapsed or 0.0, label, self._detail, self._last_at or 0.0))

def _process_group(proc):
    """Return the child's pgid only when it leads its own group, else ``None``.

    The helper may also be called with an arbitrary ``Popen`` from a caller or
    a test; signalling the caller's group would be catastrophic.
    """
    if os.name == "nt":
        return None
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return None
    return pgid if pgid == proc.pid else None


def _signal_harness_group(proc, sig, pgid=None):
    """Signal the whole Harness process group, falling back to the child."""
    if pgid is None:
        pgid = _process_group(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except Exception:
            pass
    try:
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except Exception:
        pass


def _terminate_process(proc, grace=STOP_GRACE_SECONDS):
    """Terminate a Harness process tree, then kill it if it ignores SIGTERM.

    POSIX uses the child's own process group so Chrome/zygote grandchildren do
    not outlive a stall or time-limit stop. Windows has no killpg; the platform
    layer uses ``taskkill /T /F``. The pgid is captured before the first signal:
    the leader may exit while a grandchild that ignores SIGTERM is still in the
    group, and that group must still get SIGKILL after the grace period.
    """
    if os.name == "nt":
        try:
            platforms.terminate_tree(proc.pid, timeout=max(1.0, grace))
        except Exception:
            pass
        return
    pgid = _process_group(proc)
    try:
        if proc.poll() is not None:
            return
        _signal_harness_group(proc, signal.SIGTERM, pgid)
    except Exception:
        pass
    deadline = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except Exception:
            pass
    if proc.poll() is None:
        _signal_harness_group(proc, signal.SIGKILL)



def _reviewer_home(cfg, workspace, policy, label):
    """Create a private DSH home for a read-only reviewer.

    It lives under ``state_dir``, not inside the builder-writable workspace, so
    a builder cannot plant profile patches before the reviewer starts. The
    reviewer's Seatbelt profile grants write access only to this directory (and
    the report/evidence paths), not to the operator's ``~/.dsh``.
    """
    read_only = (_phase_kind(label) == "reviewer"
                 or (policy or {}).get("sandbox") == "read-only")
    if not read_only:
        return None
    try:
        root = Path(cfg.state_dir) / "reviewer-homes"
        root.mkdir(parents=True, exist_ok=True)
        platforms.private_permissions(root, is_dir=True)
        home = Path(tempfile.mkdtemp(prefix="review-", dir=str(root)))
        platforms.private_permissions(home, is_dir=True)
        return home
    except Exception:
        return None


def _sdk_usage_cost(cfg, raw_usage, label=None):
    """Normalise SDK usage and price it; ``(usage, cost_or_None)``.

    Reviewer phases price ``cfg.dsh_model`` through :func:`_prices_for` against
    ``cfg.reviewer_prices``: a configured row wins, DeepSeek's built-in table
    is the fallback, and an unresolvable/partial row leaves cost unknown.
    Builder phases keep the historical built-in pricing.
    """
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    usage = {
        "input_tokens": raw_usage.get("input_tokens"),
        "cache_read_tokens": raw_usage.get("cache_read_tokens"),
        "output_tokens": raw_usage.get("output_tokens"),
    }
    if usage.get("input_tokens") is None:
        return usage, None
    model = str(cfg.get("dsh_model") or "deepseek-flash")
    if _phase_kind(label) == "reviewer":
        rates, _name = _prices_for(
            model, getattr(cfg, "reviewer_prices", None))
        if rates is None:
            return usage, None
        uncached = max(
            (usage.get("input_tokens") or 0)
            - (usage.get("cache_read_tokens") or 0), 0)
        return usage, (
            uncached * rates["miss"]
            + (usage.get("cache_read_tokens") or 0) * rates["hit"]
            + (usage.get("output_tokens") or 0) * rates["out"]
        ) / 1_000_000
    cost = api.estimate_cost(model, {
        "prompt_tokens": usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or 0,
        "prompt_cache_hit_tokens": usage.get("cache_read_tokens") or 0,
        "prompt_cache_miss_tokens": max(
            (usage.get("input_tokens") or 0) - (usage.get("cache_read_tokens") or 0), 0),
    })
    return usage, cost


def _sdk_budget_execution(cfg, policy, sandbox_meta, started, raw_usage,
                          event_count, session_id, label=None):
    """Execution record for an SDK run stopped by the live cost ceiling."""
    usage, cost = _sdk_usage_cost(cfg, raw_usage, label=label)
    return {
        "status": "error", "answer": None, "exit_code": None,
        "error": ("live cost usage crossed the whole-job ceiling; the "
                  "SDK run was stopped at an event boundary"),
        "tools_used": {}, "events": event_count,
        "output_files": [], "seconds": round(time.time() - started, 1),
        "stderr_tail": "", "session_logs": [],
        "tokens_in": usage.get("input_tokens"),
        "cache_read_tokens": usage.get("cache_read_tokens"),
        "tokens_out": usage.get("output_tokens"),
        "total_tokens": sum(
            value for value in usage.values()
            if isinstance(value, (int, float))),
        "estimated_cost_usd": cost,
        "cost_source": "sdk_usage" if cost is not None else "unknown",
        "cost_known": cost is not None,
        "usage_unknown_reason": (
            None if cost is not None else "SDK live usage unavailable"),
        "models": [cfg.get("dsh_model")] if cfg.get("dsh_model") else None,
        "usage": usage if usage.get("input_tokens") is not None else None,
        "stalled": False, "stalled_step": None, "stall_elapsed": None,
        "stall_seconds": _stall_seconds(cfg),
        "stall_max_seconds": _stall_max_seconds(cfg),
        "stall_cap_hard_max_seconds": _stall_cap_hard_max(cfg),
        "stall_cap_hit": False, "timed_out": False,
        "stop_reason": "budget", "child_progress_events": 0,
        "child_sessions_active": 0, "transport": "sdk",
        "sandbox": policy, "os_sandbox": sandbox_meta,
        "os_sandbox_wrapped": bool(sandbox_meta.get("wrapped")),
        "transport_session_id": session_id,
    }


def _sdk_execution(cfg, workspace, task, limit, progress_cb, label, policy,
                   session_id, started, output_slack, cancel_check=None,
                   live_usage_cb=None):
    """Run one task through the documented SDK JSON-RPC transport.

    Returns an execution dict on success/failure, or ``{"_sdk_unavailable": ...}``
    when the SDK could not be established (the caller may fall back to headless).
    The child is wrapped by the same Seatbelt policy as the headless path and
    the caller's cancel check is polled while requests wait; cancellation
    signals the process group through :class:`transport.Cancelled`.
    """
    dsh = Path(os.path.expanduser(cfg.dsh_bin))
    env = safety.scrubbed_env(keep=("DEEPSEEK_API_KEY",))
    env.setdefault("NO_COLOR", "1")
    env.update(safety.dsh_env(policy))
    reviewer_home = _reviewer_home(cfg, workspace, policy, label)
    if reviewer_home is not None:
        env["DSH_HOME"] = str(reviewer_home)
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix=".dsh-tmp-", dir=str(workspace))
    except Exception:
        tmpdir = None
    if tmpdir:
        env["TMPDIR"] = tmpdir
        env["TMP"] = tmpdir
        env["TEMP"] = tmpdir
    extra_writes = [str(reviewer_home)] if reviewer_home else []
    command, sandbox_meta = safety.wrap_command(
        [str(dsh), "--profile", "sdk"], policy, workspace, tmpdir,
        extra_writes=extra_writes,
        dsh_home=str(reviewer_home) if reviewer_home else None)
    client = None
    observed_usage_events = []

    def _observe_sdk_usage(event, _aggregated):
        """Adapt the SDK event/usage observer to the headless callback shape."""
        if live_usage_cb is None:
            return None
        try:
            parsed = parse_event(event)
        except Exception:
            parsed = None
        try:
            reason = live_usage_cb(event, parsed or {})
        except Exception:
            return None
        observed_usage_events.append(1)
        return reason

    try:
        if progress_cb:
            try:
                progress_cb("%s: SDK transport starting" % label)
            except Exception:
                pass
        client = transport_mod.SdkClient(
            str(dsh), str(workspace), env=env, command=command,
            timeout=float(limit) if limit else float(cfg.timeout),
            usage_observer=(_observe_sdk_usage
                            if live_usage_cb is not None else None))
        result = client.run(
            task, timeout=float(limit) if limit else float(cfg.timeout),
            session_id=session_id, resume=bool(session_id),
            provider=str(cfg.get("dsh_provider") or "deepseek-official"),
            model=str(cfg.get("dsh_model") or "deepseek-flash"),
            reasoning_effort=str(cfg.get("reasoning") or "max"),
            cancel_check=cancel_check,
            usage_observer=(_observe_sdk_usage
                            if live_usage_cb is not None else None))
    except transport_mod.BudgetStopped:
        # The reader thread saw a usage event that crossed the whole-job
        # ceiling, signalled the SDK process group and aborted the in-flight
        # request.  Settle exactly what was observed before the stop instead
        # of waiting for a final answer or falling back to another transport.
        return _sdk_budget_execution(
            cfg, policy, sandbox_meta, started,
            dict(getattr(client, "_usage", {}) or {}),
            len(getattr(client, "_events", []) or []),
            client._session_id if client else None, label=label)
    except transport_mod.Cancelled:
        return {
            "status": "cancelled", "answer": None, "exit_code": None,
            "error": "cancelled by client", "tools_used": {}, "events": 0,
            "output_files": [], "seconds": round(time.time() - started, 1),
            "stderr_tail": "", "session_logs": [], "tokens_in": None,
            "cache_read_tokens": None, "tokens_out": None,
            "total_tokens": None, "estimated_cost_usd": None,
            "cost_source": "unknown", "cost_known": False,
            "usage_unknown_reason": "SDK run cancelled",
            "models": None, "usage": None, "stalled": False,
            "stalled_step": None, "stall_elapsed": None,
            "stall_seconds": _stall_seconds(cfg),
            "stall_max_seconds": _stall_max_seconds(cfg),
            "stall_cap_hard_max_seconds": _stall_cap_hard_max(cfg),
            "stall_cap_hit": False, "timed_out": False,
            "stop_reason": "cancelled", "child_progress_events": 0,
            "child_sessions_active": 0, "transport": "sdk",
            "sandbox": policy, "os_sandbox": sandbox_meta,
            "os_sandbox_wrapped": bool(sandbox_meta.get("wrapped")),
            "transport_session_id": client._session_id if client else None,
        }
    except transport_mod.SdkUnavailable as exc:
        return {"_sdk_unavailable": transport_mod.redact(str(exc)),
                "seconds": round(time.time() - started, 1)}
    except Exception as exc:
        return {
            "status": "error", "answer": None, "exit_code": None,
            "error": transport_mod.redact("SDK transport failed: %s" % (exc,)),
            "tools_used": {}, "events": 0, "output_files": [],
            "seconds": round(time.time() - started, 1), "stderr_tail": "",
            "session_logs": [], "tokens_in": None, "cache_read_tokens": None,
            "tokens_out": None, "total_tokens": None,
            "estimated_cost_usd": None, "cost_source": "unknown",
            "cost_known": False, "usage_unknown_reason": "SDK transport failed",
            "models": None, "usage": None, "stalled": False,
            "stalled_step": None, "stall_elapsed": None,
            "stall_seconds": _stall_seconds(cfg),
            "stall_max_seconds": _stall_max_seconds(cfg),
            "stall_cap_hard_max_seconds": _stall_cap_hard_max(cfg),
            "stall_cap_hit": False, "timed_out": False,
            "stop_reason": "transport_error", "child_progress_events": 0,
            "child_sessions_active": 0, "transport": "sdk",
            "sandbox": policy, "os_sandbox": sandbox_meta,
            "os_sandbox_wrapped": bool(sandbox_meta.get("wrapped")),
            "transport_session_id": client._session_id if client else None,
        }
    finally:
        if client is not None:
            try:
                client.close(timeout=2.0)
            except Exception:
                pass
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if reviewer_home is not None:
            shutil.rmtree(str(reviewer_home), ignore_errors=True)
    # Safety net for a client/transport that returns final usage without
    # emitting a session.event notification: consult the live callback once
    # with that aggregate.  When notifications were observed they are
    # authoritative, so this never double-counts a phase.
    if live_usage_cb is not None and not observed_usage_events:
        raw_usage = result.get("usage") or {}
        if raw_usage:
            reason = _observe_sdk_usage(
                {"type": "usage", "data": {"usage": raw_usage}}, raw_usage)
            if reason:
                return _sdk_budget_execution(
                    cfg, policy, sandbox_meta, started, raw_usage,
                    len(result.get("events") or []),
                    result.get("session_id") or (
                        client._session_id if client else None), label=label)
    answer = (result.get("answer") or "").strip()
    usage, cost = _sdk_usage_cost(cfg, result.get("usage") or {},
                                  label=label)
    try:
        outputs = sorted(
            str(path.relative_to(workspace))
            for path in workspace.rglob("*")
            if path.is_file() and path.stat().st_mtime >= started - output_slack
            and not _noise_output(str(path.relative_to(workspace)))
        )[:100]
    except Exception:
        outputs = []
    status = "ok" if answer else "error"
    return {
        "status": status,
        "answer": answer or None,
        "exit_code": 0 if status == "ok" else 1,
        "error": None if status == "ok" else "SDK run produced no final answer",
        "tools_used": {}, "events": len(result.get("events") or []),
        "output_files": outputs,
        "seconds": round(time.time() - started, 1),
        "stderr_tail": "",
        "session_logs": [],
        "tokens_in": usage.get("input_tokens"),
        "cache_read_tokens": usage.get("cache_read_tokens"),
        "tokens_out": usage.get("output_tokens"),
        "total_tokens": None,
        "estimated_cost_usd": cost,
        "cost_source": "sdk_usage" if cost is not None else "unknown",
        "cost_known": cost is not None,
        "usage_unknown_reason": None if cost is not None else "SDK usage unavailable",
        "models": [cfg.get("dsh_model")] if cfg.get("dsh_model") else None,
        "usage": usage if usage.get("input_tokens") is not None else None,
        "stalled": False, "stalled_step": None, "stall_elapsed": None,
        "stall_seconds": _stall_seconds(cfg),
        "stall_max_seconds": _stall_max_seconds(cfg),
        "stall_cap_hard_max_seconds": _stall_cap_hard_max(cfg),
        "stall_cap_hit": False, "timed_out": False,
        "stop_reason": "ok" if status == "ok" else "error",
        "child_progress_events": 0, "child_sessions_active": 0,
        "transport": "sdk",
        "sandbox": policy,
        "os_sandbox": sandbox_meta,
        "os_sandbox_wrapped": bool(sandbox_meta.get("wrapped")),
        "transport_session_id": result.get("session_id") or (
            client._session_id if client else None),
    }


def _workspace_signature(workspace, limit=4000):
    """Cheap fingerprint of model-written files, used as a progress signal.

    A long build/render tool call can run for minutes without a JSON event.
    Files changing on disk prove the process is alive. Noise (temp profiles,
    caches, previous reports) is excluded so a browser probe is not mistaken
    for deliverable progress.
    """
    entries = []
    skip_dirs = {".git", "__pycache__", ".browser-evidence", "node_modules",
                 ".dsh-review-home"}
    try:
        for current, dirs, names in os.walk(str(workspace)):
            dirs[:] = [name for name in dirs
                       if name not in skip_dirs and not name.startswith(".dsh-tmp-")]
            for name in names:
                path = os.path.join(current, name)
                rel = os.path.relpath(path, str(workspace)).replace("\\", "/")
                if _noise_output(rel):
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                entries.append((rel, stat.st_size, stat.st_mtime_ns))
                if len(entries) >= limit:
                    return tuple(sorted(entries))
    except Exception:
        return None
    return tuple(sorted(entries))


def _execute_once(cfg, workspace, task, limit=None, progress_cb=None, label="builder",
                  exclude_logs=None, clock=None, output_slack=2.0,
                  policy=None, session_id=None, cancel_check=None,
                  transport_name=None, add_dirs=None, live_usage_cb=None):
    """Run one Harness process and return its parsed result.

    ``limit`` is a per-process wall-clock limit. ``clock`` is injectable for the
    stall-watch unit test; production uses ``time.monotonic``. ``output_slack``
    is the mtime tolerance for the output scan; the finisher uses 0 so it cannot
    claim the builder's recently written candidate as its own deliverable.
    A stall, a time limit and an explicit cancel share one first-writer-wins
    stop reason, so they can never kill the child twice or mislabel the run.
    """
    dsh = Path(os.path.expanduser(cfg.dsh_bin))
    policy = policy or safety.resolve({}, cfg, reviewer=label.startswith("reviewer"))
    transport_name = (transport_name or cfg.get("dsh_transport") or "headless").strip().lower()
    started = time.time()
    sdk_fallback = None
    if transport_name in ("sdk", "auto"):
        sdk_result = _sdk_execution(cfg, workspace, task, limit, progress_cb,
                                    label, policy, session_id, started,
                                    output_slack, cancel_check=cancel_check,
                                    live_usage_cb=live_usage_cb)
        if "_sdk_unavailable" not in sdk_result:
            sdk_result["sandbox"] = policy
            return sdk_result
        sdk_fallback = sdk_result.get("_sdk_unavailable")
        if progress_cb:
            try:
                progress_cb("%s: SDK unavailable (%s); falling back to headless"
                            % (label, sdk_fallback))
            except Exception:
                pass
    # DSH 0.2.1-rc.5's headless app accepts only --json and --session-id.
    # Permission is selected through the documented DSH_PERMISSION_MODE
    # deployment override; --permission-mode/--approval-policy/--add-dir are
    # rejected by its argument parser.
    cmd = [str(dsh), "--profile", "headless", "--json"]
    if session_id:
        cmd += ["--session-id", str(session_id)]
    cmd.append(task)
    env = safety.scrubbed_env(keep=("DEEPSEEK_API_KEY",))
    env.setdefault("NO_COLOR", "1")
    env.update(safety.dsh_env(policy))
    # Chrome/macOS resolve temporary directories through TMPDIR.  Inside the
    # Harness sandbox the system temp area is not writable, which is what made
    # headless Chrome fail in a live first-wave run.  Give the whole
    # process tree a fresh workspace-local temp dir; HOME is deliberately left
    # alone because dsh needs ~/.dsh for its profile.
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix=".dsh-tmp-", dir=str(workspace))
    except Exception:
        tmpdir = None
    if tmpdir:
        env["TMPDIR"] = tmpdir
        env["TMP"] = tmpdir
        env["TEMP"] = tmpdir
    # A read-only reviewer gets a private DSH home outside the builder-writable
    # workspace, so a denied write can never touch the operator's profiles or
    # credentials. Builder/revision sessions keep the real home so their
    # session history stays resumable across runs.
    reviewer_home = _reviewer_home(cfg, workspace, policy, label)
    if reviewer_home is not None:
        env["DSH_HOME"] = str(reviewer_home)
    extra_writes = [str(reviewer_home)] if reviewer_home else []
    if add_dirs:
        # Not a DSH flag: extra roots cannot be granted to the headless
        # profile. Record the limitation instead of passing an unsupported
        # option that would abort every run.
        sandbox_meta_extra = {"add_dirs_unsupported": list(add_dirs)}
    else:
        sandbox_meta_extra = {}
    cmd, sandbox_meta = safety.wrap_command(cmd, policy, workspace, tmpdir,
                                            extra_writes=extra_writes,
                                            dsh_home=(str(reviewer_home)
                                                      if reviewer_home else None))
    sandbox_meta.update(sandbox_meta_extra)
    tools, events, final, errors = {}, 0, None, []
    stderr_text = ""
    stall_limit = _stall_seconds(cfg)
    stall_cap = _stall_max_seconds(cfg, label)
    errfile = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, cwd=str(workspace), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=errfile, text=False,
                                bufsize=0, **platforms.popen_kwargs())
        try:
            from . import jobs
            jobs.set_child_pid(proc.pid)
        except Exception:
            pass
    except Exception as exc:
        try:
            errfile.close()
        except Exception:
            pass
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return {"status": "error", "error": "Could not start Harness: %s" % exc,
                "answer": None, "seconds": round(time.time() - started, 1),
                "tools_used": {}, "events": 0, "output_files": [], "exit_code": None,
                "stderr_tail": "", "session_logs": [], "estimated_cost_usd": None,
                "cost_source": "unknown", "usage_unknown_reason": str(exc),
                "tokens_in": None, "cache_read_tokens": None, "tokens_out": None,
                "stalled": False, "stalled_step": None, "stall_elapsed": None,
                "stall_seconds": stall_limit, "stall_max_seconds": stall_cap,
                "stall_cap_hard_max_seconds": (_stall_cap_hard_max(cfg)
                                               if stall_cap is not None else None),
                "stall_cap_hit": False, "timed_out": False,
                "stop_reason": "launch_error", "child_progress_events": 0,
                "child_sessions_active": 0}
    session_ids = set()
    main_session_id = {"value": None}
    stop = {"reason": None}
    stop_lock = threading.Lock()
    stall_state = {}
    watch = _StallWatch(
        stall_limit, clock=clock, max_seconds=stall_cap,
        grace_seconds=_cap_grace(cfg),
        extension_seconds=_cap_extension(cfg),
        hard_max_seconds=(_stall_cap_hard_max(cfg)
                          if stall_cap is not None else None))
    activity = (progress.ChildActivity(cfg.dsh_sessions_dir, workspace, since=started)
                if getattr(cfg, "child_progress", False) else None)
    child_state = {"events": 0, "active": 0}
    workspace_state = {"events": 0, "last_scan": 0.0, "signature": None}
    monitor_stop = threading.Event()

    def _request_stop(reason):
        with stop_lock:
            if stop["reason"] is not None:
                return False
            stop["reason"] = reason
            return True

    def _timeout():
        # Do not label a process that already exited on its own.
        if proc.poll() is not None:
            return
        if _request_stop("timeout"):
            _terminate_process(proc)

    def _watch_stall():
        while not monitor_stop.wait(STALL_POLL_SECONDS):
            if stop["reason"] is not None:
                return
            if proc.poll() is not None:
                return
            if cancel_check is not None:
                try:
                    cancelled = bool(cancel_check())
                except Exception:
                    cancelled = False
                if cancelled:
                    if _request_stop("cancelled"):
                        if progress_cb:
                            try:
                                progress_cb("%s: cancelled; stopping" % label)
                            except Exception:
                                pass
                        _terminate_process(proc)
                    return
            # The parent emits nothing while workflow's parallel() barrier is
            # open.  A growing child session log is real progress, so refresh
            # the watch instead of declaring a stall on a healthy wave.
            if activity is not None:
                update = activity.scan()
                # Keep the last observed count fresh even when nothing grew, so
                # the run record does not overstate children active at the end.
                child_state["active"] = update["active"]
                if update["changed"]:
                    child_state["events"] += 1
                    watch.touch("child", "child session activity (%d active)" % update["active"])
                    if progress_cb:
                        try:
                            progress_cb("%s: child activity, %d session(s) active" % (
                                label, update["active"]))
                        except Exception:
                            pass
            if getattr(cfg, "workspace_progress", True):
                now = time.monotonic()
                if now - workspace_state["last_scan"] >= 5.0:
                    workspace_state["last_scan"] = now
                    signature = _workspace_signature(workspace)
                    previous = workspace_state["signature"]
                    if previous is not None and signature != previous:
                        workspace_state["events"] += 1
                        watch.touch("workspace", "workspace file activity")
                        if progress_cb and workspace_state["events"] <= 12:
                            try:
                                progress_cb("%s: workspace files changed; still working"
                                            % label)
                            except Exception:
                                pass
                    workspace_state["signature"] = signature
            if not watch.poll():
                continue
            stalled_by_cap = watch.cap_hit()
            stall_state.update(
                stalled=True,
                step=("%s: absolute watchdog cap reached (last event: %s at %ss)" % (
                    label, watch.detail(), watch.last_at())
                      if stalled_by_cap else
                      "%s: last event was %s at %ss" % (
                          label, watch.detail(), watch.last_at())),
                elapsed=watch.elapsed(),
                cap_hit=stalled_by_cap,
                cap_extensions=watch.cap_extensions(),
                error=watch.error(label, child_state.get("events", 0)
                                  + workspace_state.get("events", 0)),
            )
            if not _request_stop("stalled"):
                # The max_seconds timer won; this is not a stall.
                stall_state.clear()
                return
            if progress_cb:
                try:
                    progress_cb("%s: STALLED - %s; stopping" % (
                        label,
                        "absolute watchdog cap %.0f s reached" % stall_cap
                        if stalled_by_cap else
                        "no event for %.0f s (last: %s)" % (
                            watch.elapsed() or 0, watch.detail())))
                except Exception:
                    pass
            _terminate_process(proc)
            return

    timer = threading.Timer(limit, _timeout) if limit else None
    if timer:
        timer.daemon = True
        timer.start()
    monitor = threading.Thread(target=_watch_stall, daemon=True) if watch.enabled else None
    if monitor:
        monitor.start()

    # ``select`` with a short timeout lets the loop check the stop reason even
    # when a Harness grandchild inherits stdout and keeps the pipe open after
    # the direct child is killed. Read raw bytes and split JSONL ourselves:
    # selecting on a buffered TextIOWrapper misses lines it already read ahead.
    # stderr goes to a temp file, not a pipe, so it can never block a thread.
    line_buffer = b""

    def _handle_line(raw):
        nonlocal events, final
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except Exception:
            return
        events += 1
        parsed = parse_event(event)
        if not parsed:
            return
        # P2-3: live cost enforcement is claimed only when the child actually
        # emits a priced usage event. The callback is consulted at this event
        # boundary; a truthy reason stops the process group. With no usage
        # event the run relies on reserve-at-admission / settle-at-exit.
        if live_usage_cb is not None:
            try:
                live_reason = live_usage_cb(event, parsed)
            except Exception:
                live_reason = None
            if live_reason:
                if _request_stop(str(live_reason)):
                    _terminate_process(proc)
                return
        if parsed["kind"] == "tool":
            tools[parsed["name"]] = tools.get(parsed["name"], 0) + 1
            watch.touch("tool", "tool %r (%d tool call(s))" % (
                parsed["name"], sum(tools.values())))
            if progress_cb:
                progress_cb("%s: %d tool call(s), last: %s, %d s" % (
                    label, sum(tools.values()), parsed["name"],
                    round(time.time() - started)))
        elif parsed["kind"] == "final":
            final = parsed["text"]
            watch.touch("final", "final answer")
        elif parsed["kind"] == "error":
            # Error spam must not mask a hung process: do not reset here.
            errors.append(parsed["message"])
        elif parsed["kind"] == "assistant":
            watch.touch("assistant", "assistant message")
            if parsed["text"]:
                final = parsed["text"] if final is None else final
                # The reviewer's structured checkpoints arrive as assistant
                # text.  Forward only those marker lines (not ordinary prose)
                # so the phase callback can persist them without spamming the
                # human progress stream.
                if progress_cb:
                    for progress_line in parsed["text"].splitlines():
                        if progress_line.lstrip().startswith(
                                review_progress_mod.PROGRESS_PREFIX):
                            progress_cb(progress_line.strip())
        elif parsed["kind"] == "child":
            # Only this run's advertised children may count as progress.
            if activity is not None:
                activity.add_ids(parsed.get("ids"))
            watch.touch("child catalog", "child session catalog")
        elif parsed["kind"] == "session":
            watch.touch("session", "session %s" % (parsed.get("id") or "start"))
            if parsed.get("id"):
                session_ids.add(parsed["id"])
                if main_session_id["value"] is None:
                    # The parent's --json stream emits its own session first;
                    # child sessions do not forward session events here.  The
                    # exclusion is best-effort and degrades to parent-only
                    # (never to a false stall mask) if that changes.
                    main_session_id["value"] = parsed["id"]
                    if activity is not None:
                        activity.exclude_ids = {parsed["id"]}
        else:
            watch.touch("event", parsed.get("type") or "event")

    drain_deadline = None
    try:
        while True:
            if stop["reason"] is not None:
                break
            now = time.monotonic()
            exited = proc.poll() is not None
            if exited and drain_deadline is None:
                # The direct child is gone. A grandchild can still inherit the
                # stdout pipe and flush one last event, so drain for a bounded
                # window instead of waiting for an EOF that may never arrive.
                # Starting the clock here (not once outside the loop) also
                # catches an exit that happened between select iterations.
                drain_deadline = now + EXIT_DRAIN_SECONDS
            if drain_deadline is None:
                timeout = STALL_POLL_SECONDS
            else:
                remaining = drain_deadline - now
                if remaining <= 0:
                    # Bounded even when a grandchild floods the pipe forever.
                    break
                timeout = min(0.1 if exited else STALL_POLL_SECONDS, remaining)
            try:
                ready, _writable, _errors = select.select(
                    [proc.stdout], [], [], timeout)
            except (OSError, ValueError):
                break
            if not ready:
                # Keep draining until EOF, a stop reason, or the post-exit
                # deadline. Do not break on the first idle select after exit:
                # a grandchild may flush the final event shortly afterwards.
                continue
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except OSError:
                break
            if not chunk:
                break
            line_buffer += chunk
            while b"\n" in line_buffer:
                raw, line_buffer = line_buffer.split(b"\n", 1)
                _handle_line(raw)
        if line_buffer.strip():
            _handle_line(line_buffer)
    finally:
        monitor_stop.set()
        if timer:
            timer.cancel()
            # A fired timer may still be inside _terminate_process; joining
            # keeps its daemon thread from outliving this call.
            timer.join(timeout=STOP_GRACE_SECONDS + 0.5)
        if monitor:
            monitor.join(timeout=STOP_GRACE_SECONDS + STALL_POLL_SECONDS * 2)
        reason = stop["reason"]
        if reason and proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # Bound the final wait: a child that closes stdout but stays alive
        # (or ignores SIGTERM) must not pin the reader forever.
        try:
            code = proc.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            try:
                code = proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    code = proc.wait(timeout=1.0)
                except Exception:
                    code = proc.poll()
        except Exception:
            code = proc.poll()
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            errfile.seek(0)
            stderr_text = errfile.read().decode("utf-8", "replace")
        except Exception:
            stderr_text = ""
        finally:
            try:
                errfile.close()
            except Exception:
                pass
        if reason and code == 0 and final:
            # The process completed on its own at the same instant the timer
            # or watchdog fired; do not mislabel a normal completion.
            reason = None
            stop["reason"] = None
            stall_state.clear()
        if reason == "timeout":
            errors.append("time limit reached")
        elif reason == "stalled":
            errors.append(stall_state.get("error") or watch.error(
                label, child_state.get("events", 0)))
    # Read usage before the private reviewer home is removed: its session
    # logs live under that DSH_HOME and are the only record of what the paid
    # reviewer consumed.
    # Refresh the child count now that the direct child is gone.  A nonzero
    # "active" count can simply mean a session file is recent; for a reviewer,
    # require actual post-exit growth before calling the phase in flight.  A
    # reviewer that exited while a background child session was still writing
    # has not finished and must not be recorded as a clean review.
    child_sessions_in_flight = False
    if activity is not None:
        try:
            activity.scan()
            if _phase_kind(label) == "reviewer":
                time.sleep(1.0)
            final_activity = activity.scan()
            child_state["active"] = int(final_activity.get("active") or 0)
            child_sessions_in_flight = bool(final_activity.get("changed"))
        except Exception:
            pass
    reviewer_session_dirs = None
    if reviewer_home is not None:
        reviewer_sessions = Path(reviewer_home) / "sessions"
        if reviewer_sessions.is_dir():
            reviewer_session_dirs = [reviewer_sessions]
    reviewer_pricing = _phase_kind(label) == "reviewer"
    try:
        usage, session_logs, usage_reason = read_session_usage(
            cfg, workspace, started, session_ids=session_ids or None,
            exclude_paths=exclude_logs, session_dirs=reviewer_session_dirs,
            prices=(getattr(cfg, "reviewer_prices", None) or {})
            if reviewer_pricing else None,
            default_model=("unknown-reviewer-model" if reviewer_pricing
                           else "deepseek-flash"))
    except Exception as exc:
        usage, session_logs = None, []
        usage_reason = "session usage read failed: %s" % exc
    if tmpdir:
        # The process tree is gone; browser temp files must not pollute the
        # output scan or the next run's workspace.
        shutil.rmtree(tmpdir, ignore_errors=True)
    if reviewer_home is not None:
        # The private reviewer home is disposable: REVIEW.md is already
        # persisted in the workspace, and no builder/resume path reads it.
        shutil.rmtree(str(reviewer_home), ignore_errors=True)
    seconds = round(time.time() - started, 1)
    stalled = bool(stall_state.get("stalled")) or reason == "stalled"
    stalled_step = stall_state.get("step")
    stall_elapsed = stall_state.get("elapsed")
    if reason == "stalled":
        stalled_step = stalled_step or ("%s: no progress event" % label)
        stall_elapsed = watch.elapsed() if stall_elapsed is None else stall_elapsed
    if reason is None:
        stop_reason = "ok" if code == 0 and final else "error"
    else:
        # Public vocabulary matches the run field: "time_limit", not "timeout".
        stop_reason = "time_limit" if reason == "timeout" else reason

    try:
        outputs = sorted(
            str(path.relative_to(workspace))
            for path in workspace.rglob("*")
            if path.is_file() and path.stat().st_mtime >= started - output_slack
            and not _noise_output(str(path.relative_to(workspace)))
        )[:100]
    except Exception:
        outputs = []

    result = {
        "status": "ok" if code == 0 and final and not stalled else "error",
        "answer": final,
        "exit_code": code,
        "error": "; ".join(errors) or (stderr_text[-1500:] if code else None),
        "tools_used": tools,
        "events": events,
        "output_files": outputs,
        "seconds": seconds,
        "stderr_tail": stderr_text[-3000:],
        "session_logs": session_logs,
        "tokens_in": None,
        "cache_read_tokens": None,
        "tokens_out": None,
        "total_tokens": None,
        "estimated_cost_usd": None,
        "cost_source": "unknown",
        "cost_known": False,
        "usage_unknown_reason": usage_reason,
        "models": None,
        "usage": None,
        "stalled": stalled,
        "stalled_step": stalled_step,
        "stall_elapsed": stall_elapsed,
        "stall_seconds": stall_limit,
        "stall_max_seconds": stall_cap,
        "stall_cap_hard_max_seconds": (_stall_cap_hard_max(cfg)
                                       if stall_cap is not None else None),
        "stall_cap_hit": bool(stall_state.get("cap_hit")),
        "stall_cap_extensions": int(watch.cap_extensions()),
        "stall_cap_grace_seconds": _cap_grace(cfg),
        "workspace_progress_events": int(workspace_state.get("events") or 0),
        "progress_sources": {
            "events": int(events),
            "child_sessions": int(child_state.get("events") or 0),
            "workspace_files": int(workspace_state.get("events") or 0),
        },
        "timed_out": reason == "timeout",
        "stop_reason": stop_reason,
        "child_progress_events": child_state["events"],
        "child_sessions_active": child_state["active"],
        "child_sessions_in_flight": child_sessions_in_flight,
        "tmpdir_isolated": bool(tmpdir),
        "reviewer_home_isolated": bool(reviewer_home),
        "sandbox": policy,
        "os_sandbox": sandbox_meta,
        "os_sandbox_wrapped": bool(sandbox_meta.get("wrapped")),
        "transport": "headless",
        "transport_fallback_reason": sdk_fallback,
        "transport_session_id": main_session_id.get("value"),
    }
    if reason == "cancelled":
        result["error"] = "cancelled by client"
    if usage:
        result.update(
            tokens_in=usage["input_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            tokens_out=usage["output_tokens"],
            total_tokens=usage.get("total_tokens"),
            usage=usage,
            estimated_cost_usd=usage.get("estimated_cost_usd"),
            cost_source="session_logs",
            cost_known=bool(usage.get("cost_known")),
            models=usage.get("models"),
            usage_unknown_reason=usage_reason,
        )
    if session_logs:
        result["session_logs"] = [str(path) for path in session_logs]
    return result


NO_BALANCE_MESSAGE = ("DeepSeek account balance is empty: top up at "
                      "platform.deepseek.com, then run the task again.")


def _is_no_balance(*executions):
    """True when Harness/DeepSeek failed because the account has no credit."""
    import re as _re
    text = " ".join(str(item.get("error") or "") + " " + str(item.get("stderr_tail") or "")
                    for item in executions if item)
    return bool(_re.search(
        r"Insufficient Balance|exceeded_current_quota_error|insufficient_quota|"
        r"quota_exceeded|\bQUOTA\b|HTTP\s*402|\b402 Payment",
        text, _re.IGNORECASE))


_KIMI_RE = re.compile(r"kimi|moonshot", re.IGNORECASE)
_DEEPSEEK_RE = re.compile(r"deepseek", re.IGNORECASE)
_OPENAI_RE = re.compile(r"\bopenai\b|\bgpt[-_ ]?[0-9a-z]", re.IGNORECASE)
_ANTHROPIC_RE = re.compile(r"anthropic|\bclaude\b", re.IGNORECASE)
_QUOTA_RE = re.compile(
    r"exceeded_current_quota_error|insufficient\s+balance|\bQUOTA\b|"
    r"HTTP\s*402|\b402\b|payment required", re.IGNORECASE)
_RATE_LIMIT_RE = re.compile(
    r"rate[\s_-]*limit|HTTP\s*429|\b429\b|too many requests", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"HTTP\s*(?:401|403)|\b(?:401|403)\b|unauthori[sz]ed|forbidden|"
    r"invalid\s+(?:api[\s_-]*)?key|authentication|permission denied",
    re.IGNORECASE)


def _provider_tokens(value):
    """Flatten execution/config values into one lowercase signal string."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_provider_tokens(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_provider_tokens(item) for item in value)
    return str(value)


def provider_family(text=None, cfg=None, execution=None):
    """Classify the paid endpoint as deepseek/kimi/openai/anthropic/provider.

    Hints are read in strict precedence order: the execution's endpoint/model
    fields first, then the configured ``dsh_provider``/``dsh_model``/
    ``providers``, then ``base_url``/``model`` defaults, and only then the free
    error text. A config value must never override what the failed process
    actually ran on; otherwise a DeepSeek execution with a stale Kimi setting
    is reported as a Kimi quota problem (and vice versa). Within one tier Kimi
    still wins over DeepSeek because a Kimi 429 must never be reported as a
    DeepSeek balance problem.
    """
    execution = execution or {}

    def first_match(haystack):
        if _KIMI_RE.search(haystack):
            return "kimi"
        if _DEEPSEEK_RE.search(haystack):
            return "deepseek"
        if _ANTHROPIC_RE.search(haystack):
            return "anthropic"
        if _OPENAI_RE.search(haystack):
            return "openai"
        return None

    def cfg_value(key):
        try:
            return cfg.get(key)
        except Exception:
            return getattr(cfg, key, None)

    # Tier 1: what the failed execution actually ran. provider/model/models
    # identify the endpoint; base_url is included in the same tier because a
    # proxy URL still belongs to the process that failed.
    if isinstance(execution, dict):
        parts = []
        for key in ("provider", "model", "models", "base_url",
                    "dsh_provider", "dsh_model"):
            if execution.get(key) is not None:
                parts.append(_provider_tokens(execution.get(key)))
        family = first_match(" ".join(parts))
        if family:
            return family
    # Tier 2: explicit configuration hints, used only when the execution did
    # not name a vendor.
    if cfg is not None:
        parts = []
        for key in ("dsh_provider", "dsh_model", "providers"):
            value = cfg_value(key)
            if value:
                parts.append(_provider_tokens(value))
        family = first_match(" ".join(parts))
        if family:
            return family
        # Tier 3: config defaults such as the built-in DeepSeek base_url.
        parts = []
        for key in ("base_url", "model"):
            value = cfg_value(key)
            if value:
                parts.append(_provider_tokens(value))
        family = first_match(" ".join(parts))
        if family:
            return family
    # Tier 4: free error text, the weakest signal.
    family = first_match(_provider_tokens(text))
    if family:
        return family
    return "provider"


def _provider_label(family):
    return {"kimi": "Kimi (Moonshot)", "deepseek": "DeepSeek",
            "openai": "OpenAI", "anthropic": "Anthropic"}.get(
                family, "the configured provider")


def provider_error_message(execution=None, cfg=None, role="reviewer"):
    """Name the real vendor and cause for a paid reviewer/builder failure.

    The text is redacted before it is returned and never claims DeepSeek when
    the execution actually used another endpoint.
    """
    execution = execution or {}
    error_text = ""
    if isinstance(execution, dict):
        error_text = " ".join(str(execution.get(key) or "") for key in
                              ("error", "stderr_tail", "status_detail"))
    family = provider_family(text=error_text, cfg=cfg, execution=execution)
    label = _provider_label(family)
    text = error_text

    if _QUOTA_RE.search(text) or (
            _RATE_LIMIT_RE.search(text) and re.search(r"quota|balance", text,
                                                      re.IGNORECASE)):
        if family == "kimi":
            return ("Kimi (Moonshot) %s quota is exhausted: top up or raise "
                    "the limit at platform.moonshot.cn (platform.moonshot.ai "
                    "outside mainland China), then retry." % role)
        if family == "deepseek":
            return NO_BALANCE_MESSAGE
        return ("%s %s quota is exhausted: check the provider plan and billing "
                "in its console, then retry." % (label, role))

    if _RATE_LIMIT_RE.search(text):
        if family == "provider":
            return ("The configured provider %s hit a rate limit (HTTP 429); "
                    "wait and retry." % role)
        return "%s %s hit a rate limit (HTTP 429); wait and retry." % (
            label, role)

    if _AUTH_RE.search(text):
        keys = {"kimi": "MOONSHOT_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
                "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
        key = keys.get(family)
        if key:
            remedy = ("check %s or the %s console" % (key, label))
        else:
            remedy = "check the provider API key and console"
        return "%s %s authentication failed: %s." % (label, role, remedy)

    tail = api.redact_secrets(text.strip())
    if len(tail) > 300:
        tail = tail[:297] + "..."
    if tail:
        return "%s %s failed: %s" % (label, role, tail)
    return "%s %s failed without a provider error message." % (label, role)


_TRANSIENT_ERROR_RE = re.compile(
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|socket hang up|fetch failed|"
    r"network (?:error|failure)|temporarily unavailable|rate limit|"
    r"HTTP\s*(?:429|500|502|503|504)|\b(?:429|502|503|504)\b", re.IGNORECASE)
_PERMANENT_ERROR_RE = re.compile(
    r"Insufficient Balance|\bQUOTA\b|HTTP\s*402|\b402 Payment|401|403|"
    r"unauthor|invalid api key|authentication|permission denied", re.IGNORECASE)


def _transient_failure_reason(execution):
    """Return a short reason for one safe retry, else ``None``.

    A retry is safe only when the failed process wrote nothing, died fast, and
    the error is a network/rate-limit class failure.  Stalls, time limits,
    balance/auth errors and any run with output must never be retried: the
    first attempt may already have spent money or created a partial deliverable.
    """
    if not isinstance(execution, dict):
        return None
    if execution.get("status") == "ok":
        return None
    if execution.get("stop_reason") in ("stalled", "time_limit", "launch_error"):
        return None
    if execution.get("stalled") or execution.get("timed_out"):
        return None
    if execution.get("output_files"):
        return None
    try:
        seconds = float(execution.get("seconds") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0 or seconds > 30:
        return None
    text = (str(execution.get("error") or "") + " " +
            str(execution.get("stderr_tail") or ""))
    if _is_no_balance(execution) or _PERMANENT_ERROR_RE.search(text):
        return None
    matches = [match.group(0) for match in _TRANSIENT_ERROR_RE.finditer(text)]
    if not matches:
        return None
    # Prefer a concrete socket/status marker over the generic "fetch failed"
    # wrapper so the run record says what actually broke.
    marker = next((item for item in matches
                   if "fetch" not in item.lower()), matches[0])
    return "transient %s after %.1f s with no output" % (marker, seconds)


def _retry_budget(limit, elapsed):
    """Bounded retry seconds, or ``None`` when the run has no budget left.

    With no caller ``max_seconds`` the retry gets the fixed 180 s budget; the
    absolute watchdog cap (``stall_max_seconds``, default 900 s) still bounds
    the retry process.
    """
    try:
        elapsed = max(0.0, float(elapsed or 0.0))
    except (TypeError, ValueError):
        elapsed = 0.0
    if limit is None:
        return 180.0
    try:
        remaining = float(limit) - elapsed - 15.0
    except (TypeError, ValueError):
        return 180.0
    if remaining < 30.0:
        return None
    return round(min(180.0, remaining), 1)


def _note_agent_limit(run, executions):
    text = " ".join(str(item.get("error") or "") for item in executions)
    if re.search(r"ACTIVATION_LIMIT_REACHED|subagent limit reached|active child limit|"
                 r"maxActiveSubagents|agent.?limit|capacity", text, re.IGNORECASE):
        run["concurrency_invalidated"] = True
        run["concurrency_error"] = text[:500]
        return True
    return False


def _stop_reason(execution):
    """Return ``stalled``/``time_limit`` for an early stop, else ``None``."""
    if execution.get("status") == "ok":
        return None
    if execution.get("stop_reason") in ("stalled", "time_limit", "cancelled"):
        return execution.get("stop_reason")
    if execution.get("stalled"):
        return "stalled"
    if execution.get("timed_out"):
        return "time_limit"
    text = str(execution.get("error") or "") + " " + str(execution.get("stderr_tail") or "")
    if re.search(r"time limit reached", text, re.IGNORECASE):
        return "time_limit"
    if re.search(r"\bstall|no progress", text, re.IGNORECASE):
        return "stalled"
    return None


def _rescue_candidates(workspace, output_files):
    """Existing, non-empty deliverables written by the failed attempt."""
    candidates = []
    for rel in _deliverable_files(workspace, output_files):
        path = Path(workspace) / rel
        try:
            if path.is_file() and path.stat().st_size > 0:
                candidates.append(rel)
        except OSError:
            continue
    return candidates[:40]


def _union_files(*lists):
    merged = []
    for items in lists:
        for item in items or []:
            if item not in merged:
                merged.append(item)
    return merged


def _merge_tools(*executions):
    merged = {}
    for execution in executions:
        for name, count in (execution.get("tools_used") or {}).items():
            merged[name] = merged.get(name, 0) + int(count or 0)
    return merged


def _rescue_limit(limit, elapsed):
    """Own time limit for the finisher.

    ``max_seconds`` is per Harness process, so a literal
    ``min(FINISHER_MAX_SECONDS, max_seconds - elapsed)`` is zero exactly when
    the builder hit its limit and would make timeout rescue impossible. Give
    the finisher the remaining budget when that is larger, otherwise a small
    capped fresh budget.
    """
    if limit is None:
        return FINISHER_MAX_SECONDS
    remaining = max(0.0, float(limit) - max(0.0, float(elapsed)))
    floor = min(float(limit), FINISHER_FLOOR_SECONDS)
    return round(min(FINISHER_MAX_SECONDS, max(remaining, floor)), 1)


def _rescue_warning(run):
    rescue = run.get("rescue") or {}
    reason = rescue.get("reason")
    what = ("stalled with no progress" if reason == "stalled"
            else "hit its time limit")
    count = len(rescue.get("candidate_files") or [])
    return ("Builder %s; %d existing file(s) were kept and a finisher completed "
            "the deliverables. This run was rescued, not a normal completion."
            % (what, count))


def _log_fallback_warning(run):
    """Model-visible fallback warning, with home path and e-mail redacted."""
    reason = run.get("log_fallback_reason")
    if not reason:
        return None
    display = api.redact_secrets(reason)
    display = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", display)
    return ("Run log was written to the fallback folder, not the configured "
            "log_dir: %s" % display)


def _saved_note(run, path):
    note = "\n(Run saved: %s)" % path
    warning = _log_fallback_warning(run)
    if warning:
        note += "\n[WARNING] " + warning
    return note


def _work_store(cfg):
    return work_mod.WorkStore(Path(cfg.state_dir) / "work")


def _run_event_log(run, cfg):
    work_id = run.get("work_id")
    if not work_id or not work_mod.valid_work_id(str(work_id)):
        return None
    return events_mod.EventLog(Path(cfg.state_dir) / "work" / str(work_id))


def _emit_run_event(run, cfg, event_type, payload=None):
    """Best-effort journal append; a journal failure never breaks a run."""
    log_obj = _run_event_log(run, cfg)
    if log_obj is None:
        return None
    body = dict(payload or {})
    body.setdefault("run_id", run.get("id"))
    try:
        if event_type == "phase":
            # Consecutive saves of the same phase must not grow the journal.
            recent = log_obj.read_since(0)
            if recent and recent[-1].get("type") == "phase":
                prior = recent[-1].get("payload") or {}
                if prior.get("phase") == body.get("phase"):
                    return recent[-1]
        return log_obj.append(event_type, body)
    except Exception:
        return None


def _is_canonical_run(run, cfg):
    """True when this run may write journal/result state for its work item."""
    work_id = run.get("work_id")
    if not work_id:
        return False
    try:
        current = _work_store(cfg).load(str(work_id))
    except Exception:
        return True
    canonical = (current or {}).get("canonical_run_id")
    return not canonical or canonical == run.get("id")


def _ensure_admitted(run, cfg):
    """Append one ``admitted`` journal event for this run, once."""
    if not _is_canonical_run(run, cfg):
        return None
    log_obj = _run_event_log(run, cfg)
    if log_obj is None:
        return None
    try:
        recent = log_obj.read_since(0)
        for event in recent:
            payload = event.get("payload") or {}
            if (event.get("type") == "admitted"
                    and payload.get("run_id") == run.get("id")):
                return event
        return log_obj.append("admitted", {
            "run_id": run.get("id"),
            "attempt_no": run.get("attempt_no"),
            "tool": "harness_deepseek",
        })
    except Exception:
        return None


def _heartbeat_work(run, cfg):
    work_id = run.get("work_id")
    if not work_id:
        return
    try:
        _work_store(cfg).heartbeat(
            str(work_id), run_id=run.get("id"), pid=os.getpid())
    except Exception:
        pass


def _finalize_work(run, cfg):
    """Write the durable result and close the work item; never raise."""
    work_id = run.get("work_id")
    if not work_id:
        return
    if not _is_canonical_run(run, cfg):
        # Guard before _ensure_admitted: a superseded run must not append an
        # admitted event even when journal rotation dropped its old admission.
        # WorkStore.finish would reject the run id, but the journal/result must
        # stay untouched too.
        return
    _ensure_admitted(run, cfg)
    if not _is_canonical_run(run, cfg):
        # Close the narrow TOCTOU window between the first check and the
        # admitted write; a concurrent resume may have started a new attempt.
        return
    log_obj = _run_event_log(run, cfg)
    status = str(run.get("status") or "error")
    text = run.get("answer")
    if text is None:
        text = run.get("error") or ""
    convergence_attention = run.get("convergence_attention")
    if (log_obj is not None and events_mod.validate_attention(text) is None
            and isinstance(convergence_attention, dict)
            and convergence_attention.get("question")):
        # P3-2: a convergence stop already appended its needs_input event in the
        # review loop, but the request text is only fenced by the caller after
        # this save. Without folding the question into *this* text, the
        # terminal result below would clear the pending question and the MCP
        # task would complete instead of asking the human. Serialise the exact
        # payload (question + options + evidence) here so record_attention can
        # dedupe to the already-emitted event and no terminal event is written.
        try:
            block = json.dumps(convergence_attention, ensure_ascii=False,
                               sort_keys=True)
            text = ((text or "").rstrip() + "\n\n```attention\n%s\n```\n"
                    % block).lstrip()
        except (TypeError, ValueError):
            pass
    if log_obj is not None and text:
        question = events_mod.validate_attention(text)
        if question is not None:
            # A human answer is still required: do not write a terminal event
            # or finish the work.  The job wrapper will surface the question.
            try:
                events_mod.record_attention(log_obj, text, source="harness")
                log_obj.write_result(text, status="needs_input",
                                     run_id=run.get("id"))
            except Exception:
                pass
            try:
                _work_store(cfg).hold(
                    str(work_id), run_id=run.get("id"), status="needs_input")
            except Exception:
                pass
            return
    if log_obj is not None:
        try:
            events_mod.record_terminal(log_obj, run=run, text=text)
        except Exception:
            try:
                log_obj.write_result(text, status=status, run_id=run.get("id"))
            except Exception:
                pass
    try:
        _work_store(cfg).finish(str(work_id), status, run_id=run.get("id"))
    except Exception:
        pass


def _persist(run, cfg, phase=None):
    """Write the durable run record mid-flight; never break the run."""
    if phase:
        run["phase"] = phase
    run["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        path = save_run(run, cfg, ledger=False)
    except Exception as exc:
        run["save_error"] = api.redact_secrets(str(exc))
        path = None
    if _is_canonical_run(run, cfg):
        _ensure_admitted(run, cfg)
        if phase:
            _emit_run_event(run, cfg, "phase", {
                "phase": phase, "status": run.get("status"),
                "attempt_no": run.get("attempt_no")})
        _heartbeat_work(run, cfg)
    return path


def _job_spec_for_checkpoint(run, args, workspace):
    """A self-sufficient job spec stored in the checkpoint metadata.

    ``resume_harness`` can rebuild a run from this when ``runs/<id>.json`` is
    missing (for example when the log directory became unwritable after the
    skeleton was written). Checkpoint writes recursively redact home paths.
    """
    keys = ("prompt", "workspace", "title", "subagents", "quality",
            "independent_review", "quality_rounds", "skill", "sandbox",
            "reviewer_sandbox", "transport", "add_dirs", "max_seconds",
            "job_max_seconds", "job_max_cost_usd", "live_scaling",
            "live_scaling_threshold", "live_scaling_max_extra_waves",
            "review_preflight", "review_preflight_timeout")
    spec = {}
    for key in keys:
        if key in args:
            value = args.get(key)
            if isinstance(value, list):
                value = list(value)
            spec[key] = value
    spec.update({
        "prompt": run.get("prompt"),
        "workspace": str(workspace),
        "workspace_configured": str(args.get("workspace") or ""),
        "title": run.get("title"),
        "subagents": run.get("subagents"),
        "quality_used": run.get("quality_used"),
        "independent_review": run.get("independent_review"),
        "quality_rounds": run.get("quality_rounds"),
        "skill": run.get("skill"),
        "builder_sandbox": run.get("builder_sandbox"),
        "reviewer_sandbox": run.get("reviewer_sandbox"),
        "transport": run.get("transport"),
        "add_dirs": list(run.get("add_dirs") or []),
        "task_type": run.get("task_type"),
        "size_estimate": run.get("size_estimate"),
        "model": run.get("model"),
        "work_id": run.get("work_id"),
        "attempt_no": run.get("attempt_no"),
        "request_fingerprint": run.get("request_fingerprint"),
    })
    return spec


def _prior_rounds(manifest):
    """Reconstruct completed review/revision rounds from a checkpoint."""
    rounds = []
    accepted = None
    phases = (manifest or {}).get("phases") or {}
    for name in (manifest or {}).get("plan") or []:
        name = str(name)
        entry = phases.get(name) or {}
        if entry.get("status") != "done":
            continue
        data = dict(entry.get("data") or {})
        try:
            round_no = int(name.split("-", 1)[1])
        except (IndexError, TypeError, ValueError):
            continue
        if name.startswith("review-"):
            record = {
                "round": round_no,
                "kind": "review",
                "score": data.get("score"),
                "blocking_defects": int(data.get("blocking") or 0),
                "defects": list(data.get("defects") or []),
                "fixes": list(data.get("fixes") or []),
                "signatures": list(data.get("signatures") or []),
                "first_seen_round": dict(data.get("first_seen_round") or {}),
                "visual_impact": data.get("visual_impact"),
                "creative_cap": bool(data.get("creative_cap")),
                "seconds": data.get("seconds"),
                "cost": data.get("cost"),
                "tokens_in": data.get("tokens_in"),
                "cache_read_tokens": data.get("cache_read_tokens"),
                "tokens_out": data.get("tokens_out"),
                "total_tokens": data.get("total_tokens"),
                "usage": data.get("usage"),
                "models": data.get("models"),
                "cost_known": data.get("cost_known"),
                "preflight": data.get("preflight"),
                "preflight_usage": data.get("preflight_usage"),
                "status": "ok",
                "error": None,
                "stalled": False,
                "stalled_step": None,
                "report": data.get("report"),
                "report_text": data.get("report_text"),
                "session_logs": [],
                "write_violations": list(data.get("write_violations") or []),
                "resumed": True,
                "accepted": bool(data.get("accepted")),
            }
            rounds.append(record)
            if record["accepted"] and accepted is None:
                accepted = record
        elif name.startswith("revision-"):
            rounds.append({
                "round": round_no,
                "kind": "revision",
                "score": None,
                "blocking_defects": 0,
                "defects": list(data.get("defects") or []),
                "fixes": list(data.get("fixes") or []),
                "seconds": data.get("seconds"),
                "cost": data.get("cost"),
                "tokens_in": data.get("tokens_in"),
                "cache_read_tokens": data.get("cache_read_tokens"),
                "tokens_out": data.get("tokens_out"),
                "total_tokens": data.get("total_tokens"),
                "usage": data.get("usage"),
                "models": data.get("models"),
                "cost_known": data.get("cost_known"),
                "status": data.get("status") or "ok",
                "error": data.get("error"),
                "stalled": False,
                "stalled_step": None,
                "files": list(data.get("files") or []),
                "session_logs": [],
                "escalation": data.get("escalation"),
                "resumed": True,
            })
    rounds.sort(key=lambda item: (int(item.get("round") or 0),
                                  0 if item.get("kind") == "review" else 1))
    return rounds, accepted


def _synthetic_saved_run(manifest):
    """Rebuild the fields ``resume_harness`` needs when run JSON is gone."""
    metadata = (manifest or {}).get("metadata") or {}
    spec = dict(metadata.get("spec") or metadata or {})
    phases = (manifest or {}).get("phases") or {}
    builder = (phases.get("builder") or {}).get("data") or {}
    saved = {
        "id": (manifest or {}).get("run_id"),
        "kind": "harness",
        # ``_md`` and the saved run record require ``created``; the manifest
        # has carried it since v3.6.0, so reuse it (with a fallback for older
        # or hand-written checkpoints).
        "created": ((manifest or {}).get("created")
                    or dt.datetime.now(dt.timezone.utc).isoformat(
                        timespec="seconds")),
        "prompt": spec.get("prompt"),
        "workspace": spec.get("workspace"),
        "title": spec.get("title"),
        "subagents": spec.get("subagents"),
        "quality_used": spec.get("quality_used", spec.get("quality")),
        "independent_review": spec.get("independent_review"),
        "quality_rounds": spec.get("quality_rounds"),
        "skill": spec.get("skill"),
        "builder_sandbox": spec.get("builder_sandbox"),
        "reviewer_sandbox": spec.get("reviewer_sandbox"),
        "transport": spec.get("transport"),
        "add_dirs": spec.get("add_dirs") or [],
        "output_files": builder.get("output_files") or [],
        "answer": None,
        "authority": metadata.get("authority"),
        "authority_version": metadata.get("authority_version"),
    }
    for phase in phases.values():
        data = (phase or {}).get("data") or {}
        if data.get("answer") and not saved["answer"]:
            saved["answer"] = data["answer"]
    return saved


def _running_cost(executions):
    """Return ``(known_cost, cost_known)`` across executions so far."""
    total = 0.0
    known = True
    for execution in executions or []:
        cost = (execution or {}).get("estimated_cost_usd")
        if cost is None:
            known = False
        else:
            try:
                total += float(cost)
            except (TypeError, ValueError):
                known = False
    return round(total, 6), known


def _saved_elapsed_seconds(saved, manifest, limit=None):
    """Wall-clock already spent by a checkpointed run, for resume budgeting.

    A resume is the same job: the saved phase durations (and the run's own
    overall seconds) are carried into the new ``JobBudget`` so a restart cannot
    grant a fresh whole-job deadline.
    """
    total = 0.0
    for entry in ((manifest or {}).get("phases") or {}).values():
        data = (entry or {}).get("data") or {}
        try:
            total += float(data.get("seconds") or 0.0)
        except (TypeError, ValueError):
            pass
    for key in ("job_time_seconds", "seconds"):
        try:
            total = max(total, float(saved.get(key) or 0.0))
        except (TypeError, ValueError):
            pass
    if limit is not None:
        try:
            total = min(total, float(limit))
        except (TypeError, ValueError):
            pass
    return round(max(0.0, total), 1)


def _resume_spend(saved, prior_rounds):
    """Estimated spend already booked by a checkpointed run."""
    executions = saved.get("executions")
    if executions:
        return _running_cost(executions)
    rows = []
    builder = saved.get("builder") or {}
    builder_cost = builder.get("cost")
    if builder_cost is None:
        builder_cost = saved.get("estimated_cost_usd")
    if builder_cost is not None:
        rows.append({"estimated_cost_usd": builder_cost})
    for item in prior_rounds or []:
        rows.append({"estimated_cost_usd": (item or {}).get("cost")})
    return _running_cost(rows)


def _scaling_candidates(workspace, files, answer):
    candidates = []
    for rel in files or []:
        try:
            path = Path(workspace) / rel
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        candidates.append({"name": rel, "files": [{"path": rel, "size": size}],
                           "answer": answer or ""})
    if not candidates:
        candidates.append({"name": "builder answer", "files": [],
                           "answer": answer or ""})
    return candidates


def _stop_reason_from_execution(execution):
    reason = str((execution or {}).get("stop_reason") or "")
    return reason if reason in ("cancelled", "time_limit", "deadline",
                                "budget") else None


def run_agent_once(cfg, task, workspace, *, sandbox="read-only",
                   max_seconds=None, label="skill-author",
                   cancel_check=None, add_dirs=None):
    """One bounded DSH process, no quality loop and no run record.

    Reuses the connector's existing single-execution path and the reviewer's
    read-only policy; returns the execution dict unchanged. A DSH failure or an
    exception is reported in the dict and never raised.
    """
    try:
        path = Path(workspace)
        path.mkdir(parents=True, exist_ok=True)
        policy = safety.resolve({"sandbox": sandbox}, cfg, reviewer=True)
        result = _execute_once(cfg, path, task, limit=max_seconds,
                               policy=policy, label=label,
                               cancel_check=cancel_check, add_dirs=add_dirs)
        if isinstance(result, dict):
            return result
        return {"status": "error", "error": "execution returned no result",
                "answer": "", "seconds": 0}
    except Exception as exc:
        return {"status": "error", "error": api.redact_secrets(str(exc)),
                "answer": "", "seconds": 0}


def _observe_skills(run, cfg, history_runs, trace, cancel_check):
    """Populate run["skill_auto"] once; never raise into the run."""
    if getattr(cfg, "auto_skills", "off") == "off":
        return {}
    pending = run.pop("_skill_pending_changes", None) or []
    existing = run.get("skill_auto") or {}
    if existing.get("observed"):
        block = existing
    else:
        try:
            from . import skill_auto
            with authority.activate(run.get("authority")):
                block = skill_auto.after_run(
                    run, cfg, history_runs=history_runs or [],
                    trace=trace, cancel_check=cancel_check)
        except Exception as exc:
            block = {"observed": True, "summary": "auto-skills unavailable",
                     "changes": [], "pending": [],
                     "error": api.redact_secrets(str(exc))}
    if not isinstance(block, dict):
        block = {}
    block["observed"] = True
    if pending:
        changes = block.get("changes")
        if not isinstance(changes, list):
            changes = []
        changes.extend(item for item in pending if isinstance(item, dict))
        block["changes"] = changes
    try:
        from . import skill_auto
        block["summary_counts"] = skill_auto.skill_summary(run)
    except Exception:
        pass
    run["skill_auto"] = block
    return block


def _save_terminal(run, cfg, history_runs, trace, cancel_check):
    """The single post-detection terminal save; observes before saving."""
    _observe_skills(run, cfg, history_runs, trace, cancel_check)
    path = save_run(run, cfg)
    _finalize_work(run, cfg)
    return path


def _process_pending_skills(cfg, trace=None, limit=1, cancel_check=None,
                            envelope=None):
    """Best-effort wrapper around skill_auto.process_pending; never raises."""
    try:
        from . import skill_auto
        with authority.activate(envelope):
            changes = skill_auto.process_pending(
                cfg, trace=trace, limit=limit, cancel_check=cancel_check,
                envelope=envelope)
        return list(changes) if changes else []
    except Exception as exc:
        if trace is not None:
            try:
                trace.event("skill", "pending_error",
                            error=api.redact_secrets(str(exc)))
            except Exception:
                pass
        return []


def _cost_ledger_for(cfg):
    """Build the shared reservation ledger; a state-dir problem is not fatal."""
    try:
        return budget_mod.CostLedger(
            cfg.state_dir,
            ttl=getattr(cfg, "cost_reservation_ttl_seconds", 900))
    except Exception:
        return None


def _phase_policy(envelope, cfg, origin, run=None):
    """Derive one post-builder phase from ``envelope.continuation``.

    Rescue, live-scaling and revision are not new direct grants.  Deriving
    their policy from the continuation guarantees a direct danger grant can
    never be inherited by them (continuation downgrades it to
    workspace-write).
    """
    phase_env = envelope.continuation(origin, cfg)
    if isinstance(run, dict):
        run.setdefault("phase_authority", {})[origin] = phase_env.to_dict()
    policy = safety.resolve(
        {"sandbox": phase_env.sandbox, "approval": "ask"},
        cfg, reviewer=False)
    if isinstance(run, dict):
        run.setdefault("phase_policies", {})[origin] = {
            "basis": phase_env.basis,
            "sandbox": policy.get("sandbox"),
            "origin": origin,
        }
    return policy, phase_env


def _guard_prior(admission, prior, known):
    """Combine running-cost prior with the ledger's committed snapshot."""
    ledger_prior = (admission or {}).get("prior_committed")
    if ledger_prior is not None:
        try:
            prior = max(float(prior or 0.0), float(ledger_prior))
        except (TypeError, ValueError):
            pass
        known = bool(known) and bool(
            (admission or {}).get("prior_known", True))
    return prior, known


def _cost_admit(run, cfg, ledger, phase, agents, seconds, job_budget):
    """Reserve a conservative phase estimate; return an error dict on refusal.

    A ledger that cannot be opened or read is fail-closed whenever *either* a
    whole-job or a daily ceiling exists; a transient lock error must not turn
    a daily-only run into an unbounded one.
    """
    daily_limit = getattr(cfg, "daily_limit_usd", None)
    has_ceiling = (job_budget.max_cost_usd is not None
                   or daily_limit is not None)
    estimate = budget_mod.estimate_phase_cost(
        getattr(cfg, "dsh_model", None) or getattr(cfg, "model", None),
        phase, agents, seconds)
    if ledger is None:
        if has_ceiling:
            return {"error": "cost ledger unavailable; refusing a paid phase "
                             "under a ceiling", "estimate": estimate}
        return None
    spent_today = (lambda now=None: spend_mod.spend_today(cfg, now=now)) \
        if daily_limit is not None else 0.0
    prior_committed = 0.0
    prior_known = True
    try:
        try:
            before = ledger.snapshot(run["id"], limit=job_budget.max_cost_usd)
            prior_committed = float(before.get("committed") or 0.0)
            prior_known = bool(before.get("cost_known", True))
            first_phase = (prior_committed <= 0.0
                           and float(before.get("reserved") or 0.0) <= 0.0)
        except Exception:
            first_phase = False
            prior_known = False
        reservation = ledger.reserve(
            run["id"], phase, estimate.get("cost_usd"),
            cost_known=bool(estimate.get("cost_known")),
            job_limit=job_budget.max_cost_usd,
            daily_limit=daily_limit,
            spent_today=spent_today,
            allow_overrun=(first_phase and bool(getattr(
                cfg, "allow_first_phase_overrun", False))))
    except (budget_mod.BudgetExceeded, budget_mod.CostUnknown) as exc:
        run.setdefault("cost_ledger_events", []).append({
            "phase": phase, "event": "refused", "reason": str(exc),
            "estimate": estimate})
        return {"error": str(exc), "estimate": estimate}
    except Exception as exc:
        # A lock timeout/corrupt ledger is a disk/state problem. With either
        # ceiling set the phase cannot be proven admissible, so fail closed
        # instead of spending.
        run["cost_ledger_error"] = api.redact_secrets(str(exc))
        if has_ceiling:
            return {"error": "cost ledger unavailable: %s" % exc,
                    "estimate": estimate}
        return None
    if reservation.get("overrun"):
        overrun = {
            "phase": phase,
            "estimate": estimate.get("cost_usd"),
            "limit": job_budget.max_cost_usd,
            "reason": ("first paid phase started above an unusually small "
                       "ceiling; its reservation is recorded and all later "
                       "phases are admitted strictly"),
        }
        run["admission_overrun"] = overrun
        run.setdefault("admission_overruns", []).append(overrun)
    run["cost_ledger"] = ledger.snapshot(
        run["id"], limit=job_budget.max_cost_usd,
        estimated=estimate.get("cost_usd"),
        cost_known=bool(estimate.get("cost_known")))
    run.setdefault("cost_ledger_events", []).append({
        "phase": phase, "event": "reserved",
        "estimate": estimate.get("cost_usd"),
        "cost_known": estimate.get("cost_known"),
        "reservation": reservation.get("id")})
    return {"reservation": reservation, "estimate": estimate,
            "prior_committed": prior_committed, "prior_known": prior_known}


def _cost_settle(run, ledger, phase, execution, job_budget,
                 operation_id=None):
    """Settle a finished phase and refresh the run's cost snapshot."""
    if ledger is None:
        return None
    known = bool((execution or {}).get("cost_known"))
    actual = (execution or {}).get("estimated_cost_usd") if known else None
    try:
        result = ledger.settle(run["id"], actual, phase=phase,
                               cost_known=known, operation_id=operation_id)
    except Exception as exc:
        run["cost_ledger_error"] = api.redact_secrets(str(exc))
        return None
    run["cost_ledger"] = ledger.snapshot(
        run["id"], limit=job_budget.max_cost_usd, estimated=0.0,
        cost_known=known)
    run.setdefault("cost_ledger_events", []).append({
        "phase": phase, "event": "settled",
        "actual": result.get("actual"), "cost_known": known,
        "released": result.get("released")})
    return result


def _cost_release(run, ledger, phase, job_budget):
    """Release a reservation whose phase was admitted but never started."""
    if ledger is None:
        return 0
    try:
        removed = ledger.release(run["id"], phase=phase)
        run["cost_ledger"] = ledger.snapshot(
            run["id"], limit=job_budget.max_cost_usd, estimated=0.0,
            cost_known=True)
        run.setdefault("cost_ledger_events", []).append({
            "phase": phase, "event": "released", "reason": "phase not started"})
        return removed
    except Exception as exc:
        run["cost_ledger_error"] = api.redact_secrets(str(exc))
        return 0


def _cost_budget_stop(run, cfg, checkpoint, message, history_runs=None,
                      trace=None, cancel_check=None):
    """Mark a paid phase refused by the reservation ledger and stop the run."""
    run["status"] = "budget_exceeded"
    run["stop_reason"] = "budget"
    run["status_detail"] = status_mod.STATUS_MEANING["budget_exceeded"]
    run["error"] = message
    run["job_budget"] = run.get("job_budget") or {}
    if checkpoint:
        try:
            checkpoint.interrupted(message)
        except Exception:
            pass
    path = _save_terminal(run, cfg, history_runs or [], trace, cancel_check)
    return path


def _health_record(board, component, execution, label=None):
    """Record one phase outcome; never raise because health is best effort."""
    try:
        if (execution or {}).get("status") == "ok":
            entry = board.record_success(component)
        else:
            entry = board.record_failure(
                component,
                health.classify_execution(execution),
                error=api.redact_secrets(
                    str((execution or {}).get("error") or
                        (execution or {}).get("stop_reason") or "")),
                retry_after=health.retry_after(
                    str((execution or {}).get("error") or "")))
        return entry
    except Exception:
        return None


def _health_attention(run, cfg, component, entry):
    """Emit one structured journal event when a component is quarantined."""
    if not entry or not entry.get("quarantined"):
        return None
    return _emit_run_event(
        run, cfg, "progress",
        health.structured_attention(component, entry, run_id=run.get("id")))


def _emit_convergence_attention(run, cfg, payload):
    """Append one needs_input convergence question (deduped on resume)."""
    log_obj = _run_event_log(run, cfg)
    if log_obj is None or not isinstance(payload, dict):
        return None
    question = payload.get("question")
    try:
        for event in reversed(log_obj.read_since(0)):
            if event.get("type") != "needs_input":
                continue
            prior = event.get("payload") or {}
            if prior.get("question") == question:
                return event
        return log_obj.append("needs_input", payload)
    except Exception:
        return None


def _health_block_attention(run, cfg, board, component, message=None):
    """Attention for a phase refused *before* it runs because it is already
    quarantined.  This branch does not record a new failure; it reports the
    persisted quarantine entry and is best effort."""
    try:
        entry = ((board.snapshot().get("components") or {}).get(component)
                 or {})
    except Exception:
        entry = {}
    entry = dict(entry)
    entry.setdefault("quarantined", True)
    return _emit_run_event(
        run, cfg, "progress",
        health.structured_attention(component, entry, run_id=run.get("id"),
                                    message=message))



def _review_resume_start(rounds, prior_reviews, prior_revisions):
    """Return ``(start_round, skip_review_round, review_status)`` on resume.

    Rounds are a prefix.  A score-missing review that already had real
    blocking defects consumes a revision phase, so a missing revision must
    resume at that revision (``skip_review_round``).  A score-missing review
    with no real blocking defect bought no revision, so it is a consumed
    round: resume moves to the next review instead of resurrecting a revision.
    """
    for number in range(1, int(rounds) + 1):
        review = prior_reviews.get(number)
        if review is None:
            return number, None, "disabled"
        if number in prior_revisions:
            continue
        unscored = (bool(review.get("score_missing"))
                    or (review.get("score") is None
                        and review.get("status") == "ok"))
        try:
            blocking = int(review.get("blocking_defects") or 0)
        except (TypeError, ValueError):
            blocking = 0
        if unscored and blocking == 0:
            continue
        return number, number, "disabled"
    return int(rounds) + 1, None, "stopped"


def run_harness(args, cfg, progress_cb=None, cancel_check=None):
    """Run one Harness task with optional independent review/revision rounds.

    Returns ``(text, run, path)`` exactly as in v3.0; the run record gains
    ``self_score``, ``review_rounds``, ``deliverable_files`` and the official
    ``quality_score`` from the connector-owned reviewer.

    ``cancel_check`` is polled by every child monitor and between phases, so a
    normal (non-task) MCP ``notifications/cancelled`` also stops the process.
    """
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("prompt is required")
    # The binary is operator configuration only; never accept it from a tool
    # argument, where an agent could point this at another executable.
    dsh = Path(os.path.expanduser(cfg.dsh_bin))
    run = new_run("harness", args, prompt, cfg)
    if run.get("work_id"):
        try:
            _work_store(cfg).bind_run(
                str(run["work_id"]), int(run.get("attempt_no") or 1),
                run["id"], pid=os.getpid())
        except Exception as exc:
            run["work_error"] = api.redact_secrets(str(exc))
    run["model"] = "harness default"
    for name in ("attention_answer", "attention_option"):
        if args.get(name) is not None:
            run[name] = api.redact_secrets(str(args.get(name)))
    run["stall_seconds"] = _stall_seconds(cfg)
    run["stall_max_seconds"] = _stall_max_seconds(cfg)
    run["stall_cap_hard_max_seconds"] = _stall_cap_hard_max(cfg)
    if not dsh.is_file() or not os.access(str(dsh), os.X_OK):
        run.update(status="error", error=(
            "DeepSeek Harness CLI not found or not executable at %s "
            "(set DSH_BIN or config dsh_bin)." % dsh), seconds=0)
        path = save_run(run, cfg)
        _finalize_work(run, cfg)
        raise RuntimeError("DeepSeek Harness CLI not found or not executable at %s "
                           "(set DSH_BIN or config dsh_bin).%s"
                           % (dsh, _saved_note(run, path)))
    try:
        workspace_path = authority.resolve_workspace(args, cfg)
    except authority.AuthorityError as exc:
        run.update(status="error", error=str(exc), seconds=0)
        path = save_run(run, cfg)
        _finalize_work(run, cfg)
        raise RuntimeError("%s%s" % (exc, _saved_note(run, path)))
    workspace = Path(workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    run["workspace"] = str(workspace)

    # P2-1: one authority envelope authorizes every phase of this job. A direct
    # grant is opened only here; resume/rescue/reviewer/revision phases inherit
    # it as a continuation. The reviewer is read-only unless an operator
    # override was recorded, so a builder can never widen it.
    try:
        if args.get("_authority_envelope") is not None:
            # A resume passes the revalidated envelope explicitly. It is always
            # a continuation, never a fresh direct grant.
            envelope = authority.AuthorityEnvelope.from_dict(
                args.get("_authority_envelope"))
            envelope = envelope.continuation("run_harness-resume", cfg)
            envelope, resume_drift = envelope.revalidate(
                cfg, workspace=str(workspace))
            run["policy_drift"] = resume_drift
        elif args.get("_resume_from"):
            raise authority.InvalidAuthority(
                "resume is missing its authority envelope; refusing to start")
        else:
            envelope = authority.direct(args, cfg)
    except authority.AuthorityError as exc:
        run.update(status="error", error=str(exc), seconds=0)
        path = save_run(run, cfg)
        _finalize_work(run, cfg)
        raise RuntimeError("%s%s" % (exc, _saved_note(run, path)))
    run["authority"] = envelope.to_dict()
    run["authority_version"] = authority.AUTHORITY_VERSION
    run["policy_digest"] = envelope.policy_digest
    run["reviewer_policy_source"] = (
        "reviewer_override" if envelope.reviewer_override else "forced_read_only")
    builder_args = dict(args)
    builder_args["sandbox"] = envelope.sandbox
    builder_policy = safety.resolve(builder_args, cfg, reviewer=False)
    # The reviewer is a continuation too: a danger direct grant is downgraded,
    # and reviewer_policy_for can only ever return read-only unless the
    # operator recorded a writing override (workspace-write at most).
    reviewer_env = envelope.continuation("reviewer", cfg)
    run.setdefault("phase_authority", {})["reviewer"] = reviewer_env.to_dict()
    reviewer_policy = safety.resolve(
        {"sandbox": authority.reviewer_policy_for(reviewer_env, cfg),
         "approval": "ask"}, cfg, reviewer=True)
    # All post-builder writing phases derive their policy from a continuation.
    # Recording them up front makes the authority trail auditable even when a
    # phase is never reached.
    rescue_policy, _rescue_env = _phase_policy(
        envelope, cfg, "rescue", run)
    scaling_policy, _scaling_env = _phase_policy(
        envelope, cfg, "live-scaling", run)
    revision_policy, _revision_env = _phase_policy(
        envelope, cfg, "revision", run)
    try:
        auto_env = envelope.continuation("auto-skill", cfg).narrow(
            sandbox="read-only")
        run.setdefault("phase_authority", {})["auto-skill"] = auto_env.to_dict()
    except authority.AuthorityError:
        pass
    run["builder_sandbox"] = builder_policy
    run["reviewer_sandbox"] = reviewer_policy
    run["reviewer_override"] = bool(envelope.reviewer_override)
    run["health_gate"] = bool(getattr(cfg, "health_gate", True))

    # P2-2: a component that failed three times in a row is quarantined until
    # an operator clears it. Completed runs are unaffected and remain readable.
    health_board = health.HealthBoard(cfg.state_dir)
    if bool(getattr(cfg, "health_gate", True)):
        blocked = health_board.quarantine_block("builder")
        if blocked:
            run.update(status="error", error=blocked, seconds=0,
                       health_quarantined=True)
            _emit_run_event(run, cfg, "progress",
                            health.structured_attention(
                                "builder", health_board.snapshot().get(
                                    "components", {}).get("builder"),
                                run_id=run["id"]))
            path = save_run(run, cfg)
            _finalize_work(run, cfg)
            raise RuntimeError(blocked + _saved_note(run, path))
    run["transport"] = (args.get("transport") or cfg.get("dsh_transport") or "headless")
    run["add_dirs"] = list(args.get("add_dirs") or cfg.get("add_dirs") or [])

    explicit = args.get("subagents") if "subagents" in args else None
    # A configurable hard ceiling for an explicit count. It is validated before
    # the first DSH process, so an accidental 400 cannot launch a paid team.
    if explicit is not None:
        try:
            hard_max = max(1, int(getattr(cfg, "max_explicit_subagents", 32) or 32))
        except (TypeError, ValueError):
            hard_max = 32
        try:
            requested_agents = int(explicit)
        except (TypeError, ValueError):
            requested_agents = None
        if requested_agents is None or requested_agents < 0:
            run.update(status="error", seconds=0,
                       error="subagents must be a non-negative integer")
            path = save_run(run, cfg)
            _finalize_work(run, cfg)
            raise RuntimeError("subagents must be a non-negative integer%s"
                               % _saved_note(run, path))
        if requested_agents > hard_max:
            message = ("subagents=%d exceeds the configured hard maximum "
                       "max_explicit_subagents=%d" % (requested_agents, hard_max))
            run.update(status="error", seconds=0, error=message,
                       max_explicit_subagents=hard_max)
            path = save_run(run, cfg)
            _finalize_work(run, cfg)
            raise RuntimeError(message + _saved_note(run, path))
    # History is a best-effort input: a missing/unreadable run folder must not
    # stop a new run, it just means no history-based nudge.
    try:
        history_runs = load_runs(cfg)
    except Exception:
        history_runs = []
    # Best-effort retry hook for clusters whose next_attempt_at has passed. It
    # never starts a brand-new build for this run and never raises.
    pending_changes = []
    if (getattr(cfg, "auto_skills", "off") != "off"
            and not args.get("_resume_from")):
        pending_changes = _process_pending_skills(
            cfg, trace=None, limit=1, cancel_check=cancel_check,
            envelope=envelope)
        run["_skill_pending_changes"] = list(pending_changes)
    # A single-agent task needs no child slots; do not pay for a probe just to
    # fill a field that the task text and sizing will not use.
    preliminary = choose_agents(prompt, args.get("files"), explicit,
                                soft_cap=cfg.subagent_soft_cap,
                                default_subagents=cfg.default_subagents,
                                concurrency=concurrency.FALLBACK_CONCURRENCY,
                                runs=history_runs)
    if int(preliminary.get("subagents") or 0) == 0:
        try:
            detected = concurrency.detect(cfg, allow_probe=False)
        except Exception as exc:
            detected = {"limit": concurrency.FALLBACK_CONCURRENCY,
                        "source": "fallback", "probe_error": str(exc)}
        child_concurrency = int(detected.get("limit") or concurrency.FALLBACK_CONCURRENCY)
        choice = preliminary
    else:
        try:
            detected = concurrency.detect(cfg)
        except Exception as exc:
            detected = {"limit": concurrency.FALLBACK_CONCURRENCY, "source": "fallback",
                        "probe_error": str(exc)}
        child_concurrency = int(detected.get("limit") or concurrency.FALLBACK_CONCURRENCY)
        choice = choose_agents(prompt, args.get("files"), explicit,
                               soft_cap=cfg.subagent_soft_cap,
                               default_subagents=cfg.default_subagents,
                               concurrency=child_concurrency,
                               runs=history_runs)
    run["concurrency"] = child_concurrency
    run["concurrency_source"] = detected.get("source") or "fallback"
    run["concurrency_detected"] = detected
    run["subagents"] = choice["subagents"]
    run["subagent_reason"] = choice["reason"]
    run["subagent_signals"] = choice["signals"]
    run["subagent_waves"] = choice.get("waves")
    run["agent_choice_reason"] = choice.get("agent_choice_reason") or choice["reason"]
    run["task_type"] = choice.get("task_type")
    run["subagent_source"] = "explicit" if explicit is not None else "automatic"
    run["size_estimate"] = choice.get("size")
    run["agent_choice"] = {
        "task_type": choice.get("task_type"),
        "size": choice.get("size"),
        "start_subagents": choice.get("start_subagents"),
        "chosen_subagents": choice.get("subagents"),
        "waves": choice.get("waves"),
        "explicit": explicit is not None,
    }
    run["agent_history"] = choice.get("history")
    run["agent_escalations"] = []
    run["lessons_added"] = []

    quality = args.get("quality")
    if quality is None:
        quality = quality_default(choice["subagents"], cfg)
    quality = bool(quality)
    run["quality_used"] = quality

    raw_independent = args.get("independent_review")
    if raw_independent is None:
        independent = bool(quality and choice["subagents"] > 0 and cfg.independent_review)
    else:
        independent = _bool_value(raw_independent)
    run["independent_review"] = independent

    if quality or independent:
        if "quality_rounds" in args and args.get("quality_rounds") is not None:
            rounds_value = args.get("quality_rounds")
        else:
            rounds_value = cfg.quality_max_rounds
        rounds = max(1, int(rounds_value))
    else:
        rounds = 0
    run["quality_rounds"] = rounds
    run["quality_target"] = DEFAULT_QUALITY_TARGET

    # Durable skeleton BEFORE the first DSH process. A SIGKILL after this point
    # leaves a discoverable run record; the checkpoint metadata carries the job
    # spec as a second copy so resume also works if the log dir was unwritable.
    run["status"] = "running"
    run["phase"] = "preparing"
    run["max_explicit_subagents"] = int(getattr(cfg, "max_explicit_subagents", 32) or 32)
    _persist(run, cfg, "preparing")

    # Durable per-phase manifest. It is best-effort: a state_dir permission
    # problem must not stop the run, it only means resume is unavailable.
    checkpoint = None
    if cfg.get("checkpoint", True):
        try:
            plan = ["builder"]
            for index in range(1, rounds + 1):
                plan.extend(["review-%d" % index, "revision-%d" % index])
            checkpoint = checkpoints.Checkpoint(
                Path(cfg.state_dir) / "checkpoints", run["id"])
            if checkpoint.load() is None:
                checkpoint.create(plan, metadata={
                    "task_type": run.get("task_type"),
                    "subagents": run.get("subagents"),
                    "workspace": str(workspace),
                    "resumed_from": args.get("_resume_from"),
                    "authority": run.get("authority"),
                    "authority_version": run.get("authority_version"),
                    "spec": _job_spec_for_checkpoint(run, args, workspace),
                })
            run["checkpoint_path"] = str(checkpoint.path)
        except Exception as exc:
            run["checkpoint_error"] = str(exc)
            checkpoint = None

    skill_key = None
    requested_skill = (args.get("skill") or "").strip().lower()
    off_mode = getattr(cfg, "auto_skills", "off") == "off"
    explicit_none = requested_skill in ("none", "off", "no")
    if requested_skill and not explicit_none:
        from .skills import normalise_skill_name
        if off_mode:
            skill_key = normalise_skill_name(requested_skill)
        else:
            try:
                skill_key = normalise_skill_name(requested_skill, cfg=cfg)
            except TypeError:  # facade predating the cfg-aware signature
                skill_key = normalise_skill_name(requested_skill)
            if skill_key:
                run["skill_match"] = {
                    "key": skill_key, "title": None, "task_type": None,
                    "confidence": 1.0, "score": 0.0, "second": 0.0,
                    "margin": 0.0, "keywords": [], "aliases": [],
                    "title_tokens": [], "status": "matched",
                    "reason": "explicit", "unmatched": False,
                    "approved": True, "mode": "explicit",
                    "threshold": 1.0, "candidates": [skill_key],
                }
        if not skill_key:
            raise RuntimeError("Unknown skill %r" % requested_skill)
    elif off_mode:
        skill_key = detect_skill_legacy(prompt)
    else:
        if explicit_none:
            match = {
                "key": None, "title": None, "task_type": None,
                "confidence": 0.0, "score": 0.0, "second": 0.0,
                "margin": 0.0, "keywords": [], "aliases": [],
                "title_tokens": [], "status": "disabled",
                "reason": "disabled", "unmatched": True,
                "approved": False, "mode": "disabled",
                "threshold": 0.0, "candidates": [],
            }
        else:
            match = detect_skill_match(prompt, cfg=cfg) or {}
        run["skill_match"] = match
        if not match.get("unmatched") and match.get("approved", True):
            skill_key = match.get("key")
    skill = load_skill(skill_key, cfg, prompt=prompt) if skill_key else None
    run["skill"] = skill_key

    # One bounded provider health probe before a visual run.  It prevents the
    # observed 175 s per-child Chrome rabbit hole: builders either get the exact
    # binary/flags or are told to ship source and report the limitation.
    render_probe = None
    if (getattr(cfg, "render_probe", False)
            and render.is_visual(choice.get("task_type"), skill_key)):
        try:
            render_probe = render.probe(cfg, workspace)
        except Exception as exc:
            render_probe = {
                "available": False, "probed": True, "binary": None,
                "version": None, "seconds": 0.0, "attempts": 0,
                "reason": "probe error: %s" % exc,
            }
        run["render_probe"] = render_probe
    render_hint = render.hint(render_probe) if render_probe else None

    task = build_harness_task(prompt, choice["subagents"], choice["reason"], skill,
                              quality=quality, quality_rounds=rounds,
                              concurrency=child_concurrency,
                              independent_review=independent,
                              render_hint=render_hint,
                              task_type=choice.get("task_type"))
    run["harness_task"] = task

    try:
        limit = float(args["max_seconds"]) if args.get("max_seconds") is not None else None
        if limit is not None and limit <= 0:
            limit = None
    except Exception:
        run.update(status="error", error="max_seconds must be a positive number", seconds=0)
        path = _save_terminal(run, cfg, history_runs, None, cancel_check)
        raise RuntimeError("max_seconds must be a positive number%s"
                           % _saved_note(run, path))

    # Whole-job budget: every phase, retry, rescue and live-scaling wave draws
    # from the same deadline and cost ceiling.
    # The envelope already clamped caller values inside the config hard caps;
    # using it here keeps admission and execution on the same numbers.
    job_seconds = envelope.job_max_seconds
    job_cost = envelope.job_max_cost_usd
    # A resume continues the same job. Seed the monotonic clock with the time
    # already booked by the checkpoint instead of giving the restart a fresh
    # whole-job deadline.
    try:
        prior_elapsed = max(0.0, float(args.get("_job_elapsed_seconds") or 0.0))
    except (TypeError, ValueError):
        prior_elapsed = 0.0
    job_budget = budget_mod.JobBudget(
        job_seconds, job_cost, started=time.monotonic() - prior_elapsed)
    run["job_budget_initial"] = job_budget.snapshot(0.0, True)
    run["max_agents"] = envelope.max_agents
    try:
        actual_agents = int(run.get("subagents") or 0)
    except (TypeError, ValueError):
        actual_agents = 0
    if actual_agents > envelope.max_agents:
        run.update(status="error", seconds=0,
                   error=("chosen subagents=%d exceeds the authority envelope "
                          "max_agents=%d" % (actual_agents, envelope.max_agents)))
        path = _save_terminal(run, cfg, history_runs, None, cancel_check)
        raise RuntimeError(run["error"] + _saved_note(run, path))

    # P2-3: one shared ledger for this state dir. A reservation is advisory
    # when no ceiling exists, but it is always recorded so concurrent jobs and
    # the daily preflight see committed + reserved rather than only yesterday's
    # settled spend.
    cost_ledger = None
    try:
        cost_ledger = budget_mod.CostLedger(
            cfg.state_dir, ttl=getattr(cfg, "cost_reservation_ttl_seconds", 900))
    except Exception as exc:
        run["cost_ledger_error"] = api.redact_secrets(str(exc))
    if cost_ledger is not None:
        try:
            # A resume/restart is a new attempt but the same job. Seed the
            # already-booked cost so admission for its remaining phases cannot
            # ignore what the earlier attempt spent.
            prior_spent, prior_known = 0.0, True
            seen_prior = False
            prior_items = list(args.get("_prior_rounds") or [])
            if args.get("_prior_builder") is not None:
                prior_items.append(args.get("_prior_builder"))
            for item in prior_items:
                if not isinstance(item, dict):
                    continue
                seen_prior = True
                cost = item.get("estimated_cost_usd")
                if cost is None:
                    cost = item.get("cost")
                if cost is None:
                    prior_known = False
                else:
                    try:
                        prior_spent += float(cost)
                    except (TypeError, ValueError):
                        prior_known = False
            if seen_prior:
                cost_ledger.record_committed(
                    run["id"], prior_spent if prior_known else None,
                    phase="prior", cost_known=prior_known)
            # Every run record carries the reservation view, even when the
            # builder is reused from a checkpoint and no new reservation is
            # taken in this process.
            run["cost_ledger"] = cost_ledger.snapshot(
                run["id"], limit=job_budget.max_cost_usd)
        except Exception as exc:
            run["cost_ledger_error"] = api.redact_secrets(str(exc))

    trace = None
    if cfg.get("trace", True):
        try:
            trace = observability.TraceWriter(cfg, run["id"])
            trace.event("run", "start", prompt_chars=len(prompt),
                        workspace=api.redact_secrets(str(workspace)),
                        subagents=run.get("subagents"),
                        task_type=run.get("task_type"))
        except Exception:
            trace = None

    # A previous run's report must not be attributed to this run. On a resume
    # whose accepted review is being restored, the active REVIEW.md is the
    # deliverable and must not be stashed away from the run it belongs to.
    _stash_old(workspace, "QUALITY_REPORT.md", run["id"])
    if not args.get("_preserve_review"):
        _stash_old(workspace, "REVIEW.md", run["id"])

    overall_started = time.time()
    executions = []
    used_logs = set()
    # R2: capture a bounded baseline before the builder writes, so the
    # reviewer can start from the connector diff instead of the whole repo.
    review_baseline = None
    review_baseline_root = None
    review_baseline_round = None
    resumed_baseline = False
    if independent:
        try:
            baseline_root = (Path(cfg.state_dir) / "review-baselines" / run["id"])
            saved_baseline = args.get("_review_baseline_dir")
            if saved_baseline:
                # A resume starts from an already-revised workspace. Reload
                # the pre-revision baseline captured before the original
                # revision so the next review still sees that revision's diff.
                candidate = os.path.abspath(os.path.expanduser(
                    str(saved_baseline)))
                allowed = os.path.abspath(os.path.join(
                    str(cfg.state_dir), "review-baselines"))
                if candidate == allowed or candidate.startswith(
                        allowed + os.sep):
                    loaded = review_context.load_baseline(candidate)
                    if loaded is not None:
                        review_baseline = loaded
                        baseline_root = Path(candidate)
                        resumed_baseline = True
                        try:
                            review_baseline_round = int(args.get(
                                "_review_baseline_round"))
                        except (TypeError, ValueError):
                            review_baseline_round = None
                    else:
                        run["review_baseline_resume_error"] = (
                            "saved baseline manifest missing or unreadable: %s"
                            % api.redact_secrets(str(candidate)))
            if review_baseline is None:
                review_baseline = review_context.capture_baseline(
                    workspace, baseline_root)
            review_baseline_root = baseline_root
            run["review_baseline"] = {
                "dir": str(baseline_root),
                "sha256": review_baseline.get("sha256"),
                "files": len(review_baseline.get("files") or []),
            }
            if review_baseline_round is not None:
                run["review_baseline"]["round"] = review_baseline_round
        except Exception as exc:
            run["review_baseline_error"] = api.redact_secrets(str(exc))
            review_baseline = None
    if checkpoint:
        try:
            checkpoint.begin("builder")
        except Exception:
            pass
    _persist(run, cfg, "builder")
    if trace:
        trace.event("builder", "start", limit_seconds=limit,
                    sandbox=(builder_policy or {}).get("sandbox"))
    if cancel_check is not None:
        try:
            cancelled = bool(cancel_check())
        except Exception:
            cancelled = False
        if cancelled:
            run.update(status="cancelled", error="cancelled by client",
                       stop_reason="cancelled", seconds=0)
            if checkpoint:
                try:
                    checkpoint.interrupted("cancelled before builder")
                except Exception:
                    pass
            path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
            raise RuntimeError("cancelled by client" + _saved_note(run, path))
    if job_budget.time_exceeded() and not (
            args.get("_prior_builder") is not None
            and run.get("independent_review")):
        run.update(status="deadline_exceeded", stop_reason="deadline",
                   error="whole-job deadline reached before the builder started",
                   seconds=0)
        run["job_budget"] = job_budget.snapshot(0.0, True)
        path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
        raise RuntimeError(run["error"] + _saved_note(run, path))
    if args.get("_prior_builder") is not None:
        # Durability resume: the saved builder phase is reused as-is. Its
        # session logs are intentionally not re-read (they were already priced
        # into the saved run), so cost totals remain the saved ones.
        builder = dict(args.get("_prior_builder") or {})
        builder.setdefault("status", "ok")
        executions.append(builder)
        run["resumed"] = True
        run["resumed_from"] = args.get("_resume_from")
    else:
        builder_before = safety.snapshot_workspace(workspace)
        builder_writing = ((builder_policy or {}).get("sandbox")
                           != "read-only")
        admission = _cost_admit(run, cfg, cost_ledger, "builder",
                                run.get("subagents") or 1,
                                job_budget.phase_limit(limit), job_budget)
        if admission and admission.get("error"):
            message = ("cost admission refused the builder: %s"
                       % admission["error"])
            path = _cost_budget_stop(run, cfg, checkpoint, message,
                                     history_runs, trace, cancel_check)
            raise RuntimeError(message + _saved_note(run, path))
        live_guard = None
        live_cb = None
        if job_budget.max_cost_usd is not None:
            live_guard = budget_mod.LiveCostGuard(
                limit=job_budget.max_cost_usd,
                reserved=(admission or {}).get("estimate", {}).get(
                    "cost_usd") or 0.0,
                prior_committed=(admission or {}).get(
                    "prior_committed") or 0.0,
                prior_known=(admission or {}).get("prior_known", True),
                model=getattr(cfg, "dsh_model", None))

            def live_cb(event, parsed, _guard=live_guard):
                usage = _usage_from_event(event)
                if not usage:
                    return None
                if _guard.observe(usage, _model_from_event(event)):
                    return "budget"
                return None
        builder = _execute_once(cfg, workspace, task,
                                limit=job_budget.phase_limit(limit),
                                progress_cb=progress_cb, label="builder",
                                exclude_logs=used_logs, policy=builder_policy,
                                add_dirs=run["add_dirs"],
                                transport_name=run["transport"],
                                session_id=args.get("resume_session_id"),
                                cancel_check=cancel_check,
                                live_usage_cb=live_cb)
        executions.append(builder)
        used_logs.update(builder.get("session_logs") or [])
        _cost_settle(run, cost_ledger, "builder", builder, job_budget,
                     operation_id=(admission or {}).get(
                         "reservation", {}).get("id"))

        retry_reason = (_transient_failure_reason(builder)
                        if getattr(cfg, "transient_retry", False) else None)
        builder_retry_policy = None
        if retry_reason:
            # P2-2: the same policy object that governs reviewer retries also
            # gates the legacy builder transient retry. Hard-stop classes and
            # the whole-job deadline/cost gates win over the transient regex.
            builder_retry_policy = health.RetryPolicy(
                "builder",
                base=getattr(cfg, "retry_backoff_base_seconds", 1.0),
                cap=getattr(cfg, "retry_backoff_cap_seconds", 60.0))
            spent_gate, known_gate = _running_cost(executions)
            allowed, why = builder_retry_policy.should_retry(
                1, health.classify_execution(builder),
                remaining_seconds=job_budget.remaining_seconds(),
                spent=spent_gate, ceiling=job_budget.max_cost_usd,
                cost_known=known_gate)
            if not allowed:
                run["builder_retry"] = {
                    "reason": retry_reason, "status": "skipped",
                    "skip_reason": why,
                }
                retry_reason = None
        # A retry is another paid phase. The first attempt may already have
        # crossed the whole-job ceiling (or produced no priceable log, which
        # cannot be proven under budget), so check the cost gate before it.
        retry_spent, retry_known = _running_cost(executions)
        retry_blocked = bool(
            retry_reason
            and job_budget.max_cost_usd is not None
            and job_budget.cost_exceeded(retry_spent, retry_known))
        if retry_blocked:
            run["builder_retry"] = {
                "reason": retry_reason,
                "status": "skipped",
                "skip_reason": ("whole-job cost budget reached (or cost unknown) "
                                "before the retry"),
                "cost_usd": retry_spent if retry_known else None,
                "cost_known": retry_known,
            }
        retry_admission = None
        if retry_reason and not retry_blocked and not job_budget.time_exceeded():
            retry_limit = _retry_budget(job_budget.phase_limit(limit),
                                        time.time() - overall_started)
            if retry_limit:
                retry_admission = _cost_admit(
                    run, cfg, cost_ledger, "builder-retry",
                    run.get("subagents") or 1, retry_limit, job_budget)
                if retry_admission and retry_admission.get("error"):
                    run["builder_retry"] = {
                        "reason": retry_reason, "status": "skipped",
                        "skip_reason": ("cost admission refused the retry: %s"
                                        % retry_admission["error"]),
                    }
                else:
                    # P2-2 side-effect guard: a writing builder may have
                    # already changed files before it failed. Retrying it would
                    # duplicate those effects.
                    guard_ok, guard_changes = health.side_effect_guard(
                        workspace, builder_before, phase="builder",
                        writing=builder_writing)
                    if not guard_ok:
                        _cost_release(run, cost_ledger, "builder-retry",
                                      job_budget)
                        run["builder_retry"] = {
                            "reason": retry_reason, "status": "skipped",
                            "skip_reason": ("workspace changed outside allowed "
                                            "evidence before the retry"),
                            "changes": guard_changes[:8],
                        }
                    else:
                        if progress_cb:
                            try:
                                progress_cb(
                                    "builder: %s; retrying once (<=%.0f s)" % (
                                        retry_reason, retry_limit))
                            except Exception:
                                pass
                        first = builder
                        # A retry is still the same failed attempt for health
                        # strike purposes; record the retry timestamp only.
                        try:
                            health_board.record_retry(
                                "builder", health.classify_execution(first),
                                error=api.redact_secrets(str(
                                    first.get("error") or "")),
                                retry_after=health.retry_after(str(
                                    first.get("error") or "")))
                        except Exception:
                            pass
                        delay = builder_retry_policy.delay(
                            1, health.retry_after(str(
                                builder.get("error") or "")))
                        builder_retry_policy.sleep(delay)
                        retry_guard = None
                        retry_live_cb = None
                        if job_budget.max_cost_usd is not None:
                            retry_prior, _retry_known = _guard_prior(
                                retry_admission,
                                *_running_cost(executions))
                            retry_guard = budget_mod.LiveCostGuard(
                                limit=job_budget.max_cost_usd,
                                reserved=(retry_admission or {}).get(
                                    "estimate", {}).get("cost_usd") or 0.0,
                                prior_committed=retry_prior,
                                prior_known=_retry_known,
                                model=getattr(cfg, "dsh_model", None))

                            def retry_live_cb(event, parsed,
                                              _guard=retry_guard):
                                usage = _usage_from_event(event)
                                if not usage:
                                    return None
                                if _guard.observe(usage,
                                                  _model_from_event(event)):
                                    return "budget"
                                return None
                        retried = _execute_once(
                            cfg, workspace, task, limit=retry_limit,
                            progress_cb=progress_cb, label="builder retry",
                            exclude_logs=used_logs, policy=builder_policy,
                            add_dirs=run["add_dirs"],
                            transport_name=run["transport"],
                            cancel_check=cancel_check,
                            live_usage_cb=retry_live_cb)
                        executions.append(retried)
                        used_logs.update(retried.get("session_logs") or [])
                        _cost_settle(run, cost_ledger, "builder-retry",
                                     retried, job_budget,
                                     operation_id=(retry_admission or {}).get(
                                         "reservation", {}).get("id"))
                        run["builder_retry"] = {
                            "reason": retry_reason,
                            "first_seconds": first.get("seconds"),
                            "retry_seconds": retried.get("seconds"),
                            "status": retried.get("status"),
                            "limit_seconds": retry_limit,
                        }
                        builder = retried
    builder_health = _health_record(health_board, "builder", builder,
                                    label="builder")
    _health_attention(run, cfg, "builder", builder_health)
    builder_stop = _stop_reason_from_execution(builder)
    spent, cost_known = _running_cost(executions)
    run["job_budget"] = job_budget.snapshot(spent, cost_known)
    if builder_stop == "cancelled":
        run.update(status="cancelled", error="cancelled by client",
                   stop_reason="cancelled",
                   seconds=round(time.time() - overall_started, 1))
        if checkpoint:
            try:
                checkpoint.interrupted("cancelled by client")
            except Exception:
                pass
        _persist(run, cfg, "cancelled")
        if trace:
            trace.finish({"status": "cancelled", "seconds": run.get("seconds")})
            run["observability"] = trace.summary()
        path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
        raise RuntimeError("cancelled by client" + _saved_note(run, path))
    _persist(run, cfg, "builder_done")
    if trace:
        trace.event("builder", "finish", status=builder.get("status"),
                    seconds=builder.get("seconds"),
                    cost=builder.get("estimated_cost_usd"),
                    stop_reason=builder.get("stop_reason"),
                    output_files=len(builder.get("output_files") or []))
    _apply_execution(run, builder)
    # A live budget stop is an interruption.  Do not finish the builder phase
    # first: ``finish`` would mark it failed/done and ``interrupted()`` only
    # rewrites phases that are still running.  The budget branch below calls
    # ``checkpoint.interrupted`` so resume sees the phase as interrupted.
    builder_budget_stop = (str(builder.get("stop_reason")) == "budget"
                           or builder_stop == "budget")
    if checkpoint and not builder_budget_stop:
        try:
            checkpoint.finish(
                "builder", status=_checkpoint_status(builder.get("status")),
                session_id=builder.get("transport_session_id"),
                output_files=builder.get("output_files") or [],
                transport=builder.get("transport") or "headless",
                seconds=builder.get("seconds"),
                error=(builder.get("error") or "")[:500] or None,
            )
        except Exception:
            pass
    run["builder_sandbox_used"] = builder.get("sandbox") or builder_policy
    run["builder_transport"] = builder.get("transport") or "headless"
    run["transport_fallback_reason"] = builder.get("transport_fallback_reason")
    run["os_sandbox"] = builder.get("os_sandbox") or {"wrapped": False,
                                                       "reason": "not evaluated"}
    run["seconds"] = round(time.time() - overall_started, 1)
    run["builder"] = {
        "seconds": builder.get("seconds"),
        "cost": builder.get("estimated_cost_usd"),
        "status": builder.get("status"),
        "stalled": bool(builder.get("stalled")),
        "stalled_step": builder.get("stalled_step"),
        "stall_elapsed": builder.get("stall_elapsed"),
        "stall_max_seconds": builder.get("stall_max_seconds"),
        "stall_cap_hard_max_seconds": builder.get("stall_cap_hard_max_seconds"),
        "stall_cap_hit": bool(builder.get("stall_cap_hit")),
        "stall_cap_extensions": int(builder.get("stall_cap_extensions") or 0),
        "stop_reason": builder.get("stop_reason"),
        "child_progress_events": builder.get("child_progress_events") or 0,
        "child_sessions_active": builder.get("child_sessions_active") or 0,
        "tmpdir_isolated": bool(builder.get("tmpdir_isolated")),
    }
    stop_reason = _stop_reason(builder)
    run["stalled"] = bool(builder.get("stalled"))
    run["stalled_step"] = builder.get("stalled_step")
    run["stop_reason"] = stop_reason
    run["rescued"] = False
    review_scope = list(builder.get("output_files") or [])

    if stop_reason == "budget" or str(builder.get("stop_reason")) == "budget":
        # The live cost guard stopped the phase at an event boundary. The
        # workspace may contain a partial write, so the phase is treated as
        # interrupted and resumed/repaired from the checkpoint rather than
        # accepted as a complete builder.
        message = ("live cost usage crossed the whole-job ceiling; the builder "
                   "was stopped at an event boundary")
        run["status"] = "budget_exceeded"
        run["stop_reason"] = "budget"
        run["status_detail"] = status_mod.STATUS_MEANING["budget_exceeded"]
        run["error"] = message
        run["job_budget"] = job_budget.snapshot(*_running_cost(executions))
        if checkpoint:
            try:
                checkpoint.interrupted(message)
            except Exception:
                pass
        path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
        raise RuntimeError(message + _saved_note(run, path))

    if builder.get("status") != "ok" and _is_no_balance(builder):
        totals = _sum_executions(executions)
        run.update(totals)
        run["status"] = "no_balance"
        run["error"] = provider_error_message(builder, cfg, role="builder")
        path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
        raise RuntimeError("%s%s" % (run["error"], _saved_note(run, path)))

    if builder.get("status") != "ok":
        candidates = (_rescue_candidates(workspace, review_scope)
                      if stop_reason in ("stalled", "time_limit") else [])
        rescue = {
            "attempted": False,
            "reason": stop_reason or "not_stopped_early",
            "candidate_files": candidates,
            "builder_error": (builder.get("error") or "")[:1000] or None,
        }
        if stop_reason in ("stalled", "time_limit") and candidates:
            elapsed = time.time() - overall_started
            rescue_limit = _rescue_limit(limit, elapsed)
            remaining = job_budget.remaining_seconds()
            if remaining is not None:
                rescue_limit = round(min(rescue_limit, remaining), 1)
            if rescue_limit <= 0 or job_budget.time_exceeded():
                rescue_limit = 0.0
            rescue.update(
                attempted=True,
                reason=stop_reason,
                limit_seconds=rescue_limit,
                remaining_seconds=remaining,
            )
            run["rescue"] = rescue
            if checkpoint:
                try:
                    checkpoint.begin("rescue")
                except Exception:
                    pass
            if rescue_limit <= 0:
                rescue["status"] = "budget_exhausted"
                run["status"] = "deadline_exceeded"
                run["error"] = ("Builder stopped early (%s) and the whole-job "
                                "deadline left no time for a finisher" % stop_reason)
                run["stop_reason"] = "deadline"
                run["job_budget"] = job_budget.snapshot(*_running_cost(executions))
                if checkpoint:
                    try:
                        checkpoint.interrupted(run["error"])
                    except Exception:
                        pass
                path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
                raise RuntimeError("%s%s" % (run["error"], _saved_note(run, path)))
            # A finisher is another paid phase: do not start it when the
            # builder already crossed the whole-job cost budget.
            spent, cost_known = _running_cost(executions)
            if (job_budget.max_cost_usd is not None
                    and job_budget.cost_exceeded(spent, cost_known)):
                rescue["status"] = "budget_exhausted"
                run["status"] = "budget_exceeded"
                run["error"] = ("whole-job cost budget reached before the "
                                "finisher; no rescue was started")
                run["stop_reason"] = "budget"
                run["status_detail"] = status_mod.STATUS_MEANING["budget_exceeded"]
                run["job_budget"] = job_budget.snapshot(spent, cost_known)
                if checkpoint:
                    try:
                        checkpoint.interrupted(run["error"])
                    except Exception:
                        pass
                path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
                raise RuntimeError("%s%s" % (run["error"], _saved_note(run, path)))
            # The partial builder report is not this run's final report.
            _stash_old(workspace, "QUALITY_REPORT.md", run["id"], round_no=0)
            finisher_task = build_finisher_task(
                prompt, candidates, stop_reason=stop_reason,
                stopped_after=builder.get("seconds"), skill=skill,
                quality=quality, quality_target=DEFAULT_QUALITY_TARGET,
                limit_seconds=rescue_limit)
            rescue_block = (health_board.quarantine_block("rescue")
                            if getattr(cfg, "health_gate", True) else None)
            if rescue_block:
                rescue["status"] = "health_blocked"
                rescue["reason"] = rescue_block
                run["rescue"] = rescue
                run["status"] = "error"
                run["error"] = rescue_block
                _health_block_attention(run, cfg, health_board, "rescue",
                                        rescue_block)
                if checkpoint:
                    try:
                        checkpoint.interrupted(rescue_block)
                    except Exception:
                        pass
                path = _save_terminal(run, cfg, history_runs, trace,
                                      cancel_check)
                raise RuntimeError(rescue_block + _saved_note(run, path))
            rescue_admission = _cost_admit(
                run, cfg, cost_ledger, "rescue",
                max(1, int(run.get("subagents") or 1)), rescue_limit,
                job_budget)
            if rescue_admission and rescue_admission.get("error"):
                rescue["status"] = "budget_exhausted"
                run["status"] = "budget_exceeded"
                run["error"] = ("cost admission refused the finisher: %s"
                                % rescue_admission["error"])
                run["stop_reason"] = "budget"
                run["status_detail"] = status_mod.STATUS_MEANING[
                    "budget_exceeded"]
                if checkpoint:
                    try:
                        checkpoint.interrupted(run["error"])
                    except Exception:
                        pass
                path = _save_terminal(run, cfg, history_runs, trace,
                                      cancel_check)
                raise RuntimeError(run["error"] + _saved_note(run, path))
            rescue_guard = None
            rescue_live_cb = None
            if job_budget.max_cost_usd is not None:
                rescue_prior, _rescue_known = _guard_prior(
                    rescue_admission, *_running_cost(executions))
                rescue_guard = budget_mod.LiveCostGuard(
                    limit=job_budget.max_cost_usd,
                    reserved=(rescue_admission or {}).get(
                        "estimate", {}).get("cost_usd") or 0.0,
                    prior_committed=rescue_prior,
                    prior_known=_rescue_known,
                    model=getattr(cfg, "dsh_model", None))

                def rescue_live_cb(event, parsed, _guard=rescue_guard):
                    usage = _usage_from_event(event)
                    if not usage:
                        return None
                    if _guard.observe(usage, _model_from_event(event)):
                        return "budget"
                    return None
            finisher = _execute_once(
                cfg, workspace, finisher_task, limit=rescue_limit,
                progress_cb=progress_cb, label="finisher", exclude_logs=used_logs,
                output_slack=0.0, policy=rescue_policy,
                add_dirs=run["add_dirs"], transport_name=run["transport"],
                cancel_check=cancel_check, live_usage_cb=rescue_live_cb)
            executions.append(finisher)
            used_logs.update(finisher.get("session_logs") or [])
            _cost_settle(run, cost_ledger, "rescue", finisher, job_budget,
                         operation_id=(rescue_admission or {}).get(
                             "reservation", {}).get("id"))
            finisher_health = _health_record(
                health_board, "rescue", finisher)
            _health_attention(run, cfg, "rescue", finisher_health)
            if (str(finisher.get("stop_reason")) == "budget"
                    or (rescue_guard is not None and rescue_guard.exceeded)):
                message = ("live cost usage crossed the whole-job ceiling; "
                           "the finisher was stopped at an event boundary")
                run.update(status="budget_exceeded", stop_reason="budget",
                           error=message,
                           status_detail=status_mod.STATUS_MEANING[
                               "budget_exceeded"],
                           job_budget=job_budget.snapshot(
                               *_running_cost(executions)))
                rescue["status"] = "budget_exhausted"
                run["rescue"] = rescue
                if checkpoint:
                    try:
                        checkpoint.interrupted(message)
                    except Exception:
                        pass
                path = _save_terminal(run, cfg, history_runs, trace,
                                      cancel_check)
                raise RuntimeError(message + _saved_note(run, path))
            run["finisher"] = {
                "seconds": finisher.get("seconds"),
                "cost": finisher.get("estimated_cost_usd"),
                "status": finisher.get("status"),
            }
            rescue.update(
                finisher_status=finisher.get("status"),
                finisher_error=(finisher.get("error") or "")[:1000] or None,
            )
            primary_outputs = _union_files(review_scope, finisher.get("output_files"))
            if finisher.get("status") == "ok":
                # Only non-empty files written by the finisher count. The
                # builder's existing candidate must not make a no-op (or
                # empty-file) finisher look like a successful rescue.
                finisher_deliverables = _rescue_candidates(
                    workspace, finisher.get("output_files"))
                review_scope = finisher_deliverables or candidates
                if finisher_deliverables:
                    run["rescued"] = True
                    rescue["status"] = "ok"
                else:
                    rescue["status"] = "no_deliverable"
            else:
                rescue["status"] = "finisher_error"
            run["rescue"] = rescue
            if checkpoint:
                try:
                    checkpoint.finish(
                        "rescue",
                        status=_checkpoint_status(finisher.get("status")),
                        files=finisher.get("output_files") or [],
                        rescued=bool(run.get("rescued")),
                        seconds=finisher.get("seconds"),
                        cost=finisher.get("estimated_cost_usd"),
                        session_id=finisher.get("transport_session_id"),
                        error=(finisher.get("error") or "")[:500] or None,
                    )
                except Exception:
                    pass

            if not run["rescued"]:
                totals = _sum_executions(executions)
                run.update(totals)
                run["output_files"] = sorted(primary_outputs)
                if _is_no_balance(finisher):
                    run["status"] = "no_balance"
                    run["error"] = provider_error_message(
                        finisher, cfg, role="builder")
                else:
                    run["status"] = "error"
                    run["error"] = (
                        "Builder stopped early (%s); finisher failed: %s"
                        % (stop_reason, finisher.get("error") or "no deliverable"))
                if _note_agent_limit(run, executions):
                    try:
                        concurrency.invalidate(cfg)
                    except Exception:
                        pass
                if checkpoint:
                    try:
                        checkpoint.interrupted(run.get("error") or "builder failed")
                    except Exception:
                        pass
                path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
                if skill_key and lesson_signal(run):
                    try:
                        lesson = distill_lesson(run)
                        if lesson:
                            append_lesson(skill_key, lesson, cfg, run_id=run["id"])
                    except Exception:
                        pass
                raise RuntimeError("%s%s" % (run["error"], _saved_note(run, path)))

            # Rescue succeeded: the finisher's answer is this run's answer, and
            # the review loop below continues exactly as for a normal builder.
            _apply_execution(run, finisher, include_outputs=False)
            run.update(
                status="ok_rescued",
                output_files=sorted(_union_files(primary_outputs,
                                                 finisher.get("output_files") or [])),
                tools_used=_merge_tools(builder, finisher),
                events=int(builder.get("events") or 0) + int(finisher.get("events") or 0),
                session_logs=_union_files(builder.get("session_logs"),
                                          finisher.get("session_logs")),
                stalled=bool(builder.get("stalled")),
                stalled_step=builder.get("stalled_step"),
                stall_elapsed=builder.get("stall_elapsed"),
                timed_out=bool(builder.get("timed_out")),
                stop_reason=stop_reason,
                seconds=round(time.time() - overall_started, 1),
            )
            if not finisher.get("answer") and builder.get("answer"):
                run["answer"] = builder.get("answer")
        else:
            run["rescue"] = rescue
            if _note_agent_limit(run, executions):
                try:
                    concurrency.invalidate(cfg)
                except Exception:
                    pass
            totals = _sum_executions(executions)
            run.update(totals)
            run["status"] = "error"
            if job_budget.time_exceeded():
                run["status"] = "deadline_exceeded"
                run["stop_reason"] = "deadline"
                run["error"] = "whole-job deadline reached during the builder"
            elif job_budget.cost_exceeded(
                    *_running_cost(executions)) and job_budget.max_cost_usd is not None:
                run["status"] = "budget_exceeded"
                run["stop_reason"] = "budget"
                run["error"] = ("whole-job cost budget reached (or cost unknown) "
                                "during the builder")
            elif stop_reason in ("stalled", "time_limit") and not candidates:
                run["error"] = ("%s; no usable output files were left to rescue"
                                % (builder.get("error") or "builder stopped early"))
            if checkpoint:
                try:
                    checkpoint.interrupted(run.get("error") or "builder stopped")
                except Exception:
                    pass
            path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
            if skill_key and lesson_signal(run):
                try:
                    lesson = distill_lesson(run)
                    if lesson:
                        append_lesson(skill_key, lesson, cfg, run_id=run["id"])
                except Exception:
                    pass
            raise RuntimeError("Harness run failed (exit %s): %s%s"
                               % (builder.get("exit_code"),
                                  run.get("error") or builder.get("error"),
                                  _saved_note(run, path)))

    # ---------------------------------------------------------------- live scaling
    # After the first builder wave, score the candidates against the brief. If
    # they are weak and the whole-job time/cost budget allows it, add a bounded
    # extra wave with new approaches. The decision and reason are recorded.
    try:
        threshold = float(args.get("live_scaling_threshold",
                                   getattr(cfg, "live_scaling_threshold", 7.0)))
    except (TypeError, ValueError):
        threshold = 7.0
    try:
        max_extra = int(args.get("live_scaling_max_extra_waves",
                                 getattr(cfg, "live_scaling_max_extra_waves", 2)))
    except (TypeError, ValueError):
        max_extra = 2
    scaling_enabled = bool(
        args.get("live_scaling", getattr(cfg, "live_scaling", True))
        and (quality or independent)
        and int(run.get("subagents") or 0) > 0
        and int(run.get("subagents") or 0) <= int(run.get("max_explicit_subagents") or 32))
    scaling_state = {
        "enabled": scaling_enabled,
        "threshold": threshold,
        "max_extra_waves": max(0, max_extra),
        "extra_waves_used": 0,
        "decisions": [],
    }
    try:
        if scaling_enabled:
            wave_number = 1
            while True:
                current_files = _deliverable_files(workspace, review_scope)
                candidates = _scaling_candidates(workspace, current_files,
                                                 run.get("answer"))
                scored = scaling.score_candidates(candidates, prompt,
                                                  threshold=threshold)
                spent_now, known_now = _running_cost(executions)
                decision = scaling.plan_extra_wave(
                    wave_number, scaling_state["max_extra_waves"], candidates, prompt,
                    threshold=threshold,
                    elapsed_seconds=job_budget.elapsed(),
                    deadline_seconds=job_budget.max_seconds,
                    cost_usd=spent_now, budget_usd=job_budget.max_cost_usd,
                    cost_known=known_now,
                    concurrency=child_concurrency)
                entry = {
                    "wave": wave_number,
                    "score": scored.get("score"),
                    "weak": bool(scored.get("weak")),
                    "threshold": threshold,
                    "added": False,
                    "reason": decision.get("reason"),
                    "approaches": list(decision.get("approaches") or []),
                    "per_candidate": scored.get("per_candidate"),
                }
                scaling_state["decisions"].append(entry)
                if progress_cb:
                    try:
                        progress_cb("live scaling wave %d: %s" % (
                            wave_number, decision.get("reason")))
                    except Exception:
                        pass
                if trace:
                    trace.event("scaling", "decision", wave=wave_number,
                                score=scored.get("score"),
                                weak=bool(scored.get("weak")),
                                add=bool(decision.get("add")),
                                reason=decision.get("reason"))
                if not decision.get("add"):
                    break
                wave_number = int(decision.get("wave") or wave_number + 1)
                extra_prompt = (prompt + "\n\n"
                                + scaling.extra_wave_instructions(
                                    prompt, candidates, wave_number))
                extra_task = build_harness_task(
                    extra_prompt, int(run.get("subagents") or 0),
                    "live-scaling extra wave with new approaches",
                    skill=skill, quality=quality, quality_rounds=rounds,
                    concurrency=child_concurrency,
                    independent_review=independent, render_hint=render_hint,
                    task_type=run.get("task_type"))
                extra_limit = job_budget.phase_limit(limit)
                if extra_limit is not None and extra_limit <= 0:
                    entry["reason"] = ("live scaling stopped: whole-job deadline "
                                       "left no time for wave %d" % wave_number)
                    break
                # Begin the wave checkpoint before admission.  A refusal is a
                # real interruption of this wave: with no running phase,
                # ``interrupted()`` would be a no-op and the phase would never
                # appear in the resume manifest at all.
                if checkpoint:
                    try:
                        checkpoint.begin("live-wave-%d" % wave_number)
                    except Exception:
                        pass
                wave_admission = _cost_admit(
                    run, cfg, cost_ledger, "live-wave",
                    max(1, int(run.get("subagents") or 1)), extra_limit,
                    job_budget)
                if wave_admission and wave_admission.get("error"):
                    entry["reason"] = (
                        "live scaling stopped: cost admission refused wave %d "
                        "(%s)" % (wave_number, wave_admission["error"]))
                    run["status"] = "budget_exceeded"
                    run["stop_reason"] = "budget"
                    run["status_detail"] = status_mod.STATUS_MEANING[
                        "budget_exceeded"]
                    run["error"] = entry["reason"]
                    if checkpoint:
                        try:
                            checkpoint.interrupted(entry["reason"])
                        except Exception:
                            pass
                    break
                entry["added"] = True
                entry["limit_seconds"] = extra_limit
                _persist(run, cfg, "live-wave-%d" % wave_number)
                if trace:
                    trace.event("live-wave-%d" % wave_number, "start",
                                limit_seconds=extra_limit,
                                approaches=entry.get("approaches"))
                wave_guard = None
                wave_live_cb = None
                wave_prior, _wave_known = _guard_prior(
                    wave_admission, *_running_cost(executions))
                if job_budget.max_cost_usd is not None:
                    wave_guard = budget_mod.LiveCostGuard(
                        limit=job_budget.max_cost_usd,
                        reserved=(wave_admission or {}).get(
                            "estimate", {}).get("cost_usd") or 0.0,
                        prior_committed=wave_prior,
                        prior_known=_wave_known,
                        model=getattr(cfg, "dsh_model", None))

                    def wave_live_cb(event, parsed, _guard=wave_guard):
                        usage = _usage_from_event(event)
                        if not usage:
                            return None
                        if _guard.observe(usage, _model_from_event(event)):
                            return "budget"
                        return None
                extra = _execute_once(
                    cfg, workspace, extra_task, limit=extra_limit,
                    progress_cb=progress_cb, label="wave %d builder" % wave_number,
                    exclude_logs=used_logs, policy=scaling_policy,
                    add_dirs=run["add_dirs"], transport_name=run["transport"],
                    cancel_check=cancel_check, live_usage_cb=wave_live_cb)
                executions.append(extra)
                used_logs.update(extra.get("session_logs") or [])
                _cost_settle(run, cost_ledger, "live-wave", extra, job_budget,
                             operation_id=(wave_admission or {}).get(
                                 "reservation", {}).get("id"))
                scaling_state["extra_waves_used"] += 1
                run["live_scaling"] = scaling_state
                # A live-scaling wave extends the builder; it must not replace
                # the run's terminal status. In particular a failed extra wave
                # after a successful rescue used to overwrite ``ok_rescued``
                # with ``error`` even though the finisher's deliverables and the
                # independent review were fine.
                if extra.get("status") == "ok":
                    merged = sorted(_union_files(review_scope,
                                                 extra.get("output_files") or []))
                else:
                    merged = list(review_scope)
                review_scope = merged
                run["output_files"] = sorted(_union_files(
                    run.get("output_files") or [], merged))
                entry["error"] = (api.redact_secrets(extra.get("error") or "")
                                  or None)
                if extra.get("status") == "ok" and extra.get("answer"):
                    run["answer"] = extra.get("answer")
                entry["status"] = extra.get("status")
                entry["seconds"] = extra.get("seconds")
                entry["cost"] = extra.get("estimated_cost_usd")
                spent_now, known_now = _running_cost(executions)
                run["job_budget"] = job_budget.snapshot(spent_now, known_now)
                if trace:
                    trace.event("live-wave-%d" % wave_number, "finish",
                                status=extra.get("status"),
                                seconds=extra.get("seconds"),
                                output_files=len(extra.get("output_files") or []))
                _persist(run, cfg, "live-wave-%d_done" % wave_number)
                if (str(extra.get("stop_reason")) == "budget"
                        or (wave_guard is not None and wave_guard.exceeded)):
                    message = ("live cost usage crossed the whole-job ceiling; "
                               "the extra wave was stopped at an event boundary")
                    run["status"] = "budget_exceeded"
                    run["stop_reason"] = "budget"
                    run["status_detail"] = status_mod.STATUS_MEANING[
                        "budget_exceeded"]
                    run["error"] = message
                    entry["reason"] = message
                    if checkpoint:
                        try:
                            checkpoint.interrupted(message)
                        except Exception:
                            pass
                    break
                if checkpoint:
                    try:
                        checkpoint.finish(
                            "live-wave-%d" % wave_number,
                            status=_checkpoint_status(extra.get("status")),
                            files=extra.get("output_files") or [],
                            seconds=extra.get("seconds"),
                            cost=extra.get("estimated_cost_usd"),
                            session_id=extra.get("transport_session_id"),
                            error=(extra.get("error") or "")[:500] or None,
                        )
                    except Exception:
                        pass
                if extra.get("status") != "ok":
                    entry["reason"] = ("extra wave failed: %s"
                                       % (extra.get("error") or "no final answer"))
                    break
                if _stop_reason_from_execution(extra) == "cancelled":
                    run["cancelled"] = True
                    break
                if job_budget.time_exceeded() or job_budget.cost_exceeded(
                        *_running_cost(executions)):
                    break
    except Exception as exc:
        scaling_state["error"] = api.redact_secrets(str(exc))
    run["live_scaling"] = scaling_state

    if (run.get("status") == "budget_exceeded"
            or run.get("stop_reason") == "budget"):
        # Admission refusal and a live budget stop are terminal for the job.
        # Do not fall through to the reviewer/revision loop: both are paid
        # phases and the ceiling is already crossed (or unprovable).
        message = (run.get("error")
                   or "live scaling crossed the whole-job budget; stopping")
        run["status"] = "budget_exceeded"
        run["stop_reason"] = "budget"
        run["status_detail"] = status_mod.STATUS_MEANING["budget_exceeded"]
        run["error"] = message
        run["job_budget"] = job_budget.snapshot(*_running_cost(executions))
        if checkpoint:
            try:
                checkpoint.interrupted(message)
            except Exception:
                pass
        path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
        raise RuntimeError(message + _saved_note(run, path))

    deliverable_files = _deliverable_files(workspace, review_scope)
    # Reviewer-facing browser evidence: console errors, failed requests and a
    # DOM summary. No Chrome means a recorded limitation, never a failed run.
    browser_evidence = None
    if (getattr(cfg, "browser_evidence", False)
            and render.is_visual(run.get("task_type"), skill_key)):
        try:
            browser_evidence = render.collect_evidence(cfg, workspace)
        except Exception as exc:
            browser_evidence = {
                "available": False,
                "reason": "evidence error: %s" % api.redact_secrets(str(exc)),
            }
        run["browser_evidence"] = browser_evidence
    if quality:
        # ``run["answer"]`` is the finisher's answer on a rescued run, not the
        # stopped builder's final text.
        run["self_score"] = extract_quality_score(workspace, run.get("answer"))
        run["quality_defects"] = _quality_defects(workspace)
        run["quality_fixes"] = _quality_fixes(workspace)
        report = workspace / "QUALITY_REPORT.md"
        if report.is_file():
            run["quality_report"] = str(report)
        else:
            run["quality_report_missing"] = True
    else:
        run["self_score"] = None

    review_rounds = []
    review_error = None
    review_status = "disabled"
    reviewer_executions = []
    escalations = []
    prior_rounds = list(args.get("_prior_rounds") or [])
    prior_reviews, prior_revisions = {}, {}
    for item in prior_rounds:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if item.get("kind") == "review":
            prior_reviews[number] = item
        elif item.get("kind") == "revision":
            prior_revisions[number] = item
    accepted_review = next((item for number in sorted(prior_reviews)
                            for item in [prior_reviews[number]]
                            if item.get("accepted")), None)
    # Backward-compatible single accepted review argument (v3.5.0 callers).
    legacy = args.get("_accepted_review")
    if isinstance(legacy, dict) and not prior_reviews:
        number = int(legacy.get("round") or 1)
        prior_reviews[number] = {
            "round": number, "kind": "review",
            "score": legacy.get("score"),
            "blocking_defects": int(legacy.get("blocking") or 0),
            "defects": list(legacy.get("defects") or []),
            "fixes": list(legacy.get("fixes") or []),
            "seconds": legacy.get("seconds"), "cost": legacy.get("cost"),
            "status": "ok", "error": None, "stalled": False,
            "stalled_step": None, "report": legacy.get("report"),
            "report_text": legacy.get("report_text"),
            "session_logs": [], "write_violations": [],
            "accepted": True, "resumed": True,
        }
        accepted_review = prior_reviews[number]

    # A manual convergence answer reopens exactly one revision at the round
    # whose review triggered the stop. The historical repeated signature is
    # allowed to pass this one decision so the scoped revision can run; the
    # following review still applies normal convergence.
    scope_resume = args.get("_scope_resume")
    scope_round = None
    scope_note = ""
    if isinstance(scope_resume, dict) and accepted_review is None:
        try:
            scope_round = int(scope_resume.get("round") or 0)
        except (TypeError, ValueError):
            scope_round = None
        scope_note = str(scope_resume.get("note") or "").strip()
    if scope_round is None or not scope_note:
        scope_resume = None
        scope_round = None
        scope_note = ""
    if scope_resume:
        run["manual_scope_resume"] = {
            "round": scope_round,
            "note": api.redact_secrets(scope_note),
        }

    if independent:
        # Reuse every finished phase. Rounds are a prefix: the first phase
        # that is not ``done`` is where this attempt starts.
        review_rounds = [dict(prior_reviews[number])
                         for number in sorted(prior_reviews)]
        review_rounds += [dict(prior_revisions[number])
                          for number in sorted(prior_revisions)]
        review_rounds.sort(key=lambda item: (int(item.get("round") or 0),
                                             0 if item.get("kind") == "review" else 1))
        for item in review_rounds:
            reviewer_executions.append({
                "status": "ok", "answer": None, "seconds": item.get("seconds"),
                "estimated_cost_usd": item.get("cost"), "session_logs": [],
                "transport_session_id": item.get("session_id"),
                "transport": item.get("transport") or "headless",
                "resumed": True,
            })
            if item.get("kind") == "revision":
                files = list(item.get("files") or [])
                run["output_files"] = sorted(_union_files(
                    run.get("output_files") or [], files))
                deliverable_files = _deliverable_files(
                    workspace, _union_files(deliverable_files, files))
        start_round = 1
        skip_review_round = None
        if accepted_review is not None:
            start_round = rounds + 1
            review_status = "accepted"
            _restore_review_report(workspace, accepted_review)
        else:
            start_round, skip_review_round, review_status = _review_resume_start(
                rounds, prior_reviews, prior_revisions)
        if prior_rounds and start_round > 1:
            run["resumed_rounds"] = start_round - 1
        # Whole-job cost gate before the first paid reviewer call. The builder
        # (and any live-scaling waves) may already have crossed the ceiling;
        # in that case no further paid phase is started.
        if accepted_review is None:
            spent, cost_known = _running_cost(executions)
            if (job_budget.max_cost_usd is not None
                    and job_budget.cost_exceeded(spent, cost_known)):
                review_status = "stopped"
                review_error = ("whole-job cost budget reached before the "
                                "independent review; reviewer was not started")
                run["status"] = "budget_exceeded"
                run["stop_reason"] = "budget"
                run["status_detail"] = status_mod.STATUS_MEANING["budget_exceeded"]
                start_round = rounds + 1
        for round_no in range(start_round, rounds + 1):
            score_missing = False
            reused = prior_reviews.get(round_no) if round_no == skip_review_round else None
            if reused is not None:
                score = reused.get("score")
                score_missing = bool(reused.get("score_missing")) or (
                    score is None and reused.get("status") == "ok")
                blocking = int(reused.get("blocking_defects") or 0)
                defects = list(reused.get("defects") or [])
                fixes = list(reused.get("fixes") or [])
                report_path = reused.get("report")
                review = {"status": "ok", "error": None, "seconds": reused.get("seconds"),
                          "estimated_cost_usd": reused.get("cost"),
                          "session_logs": [], "transport": reused.get("transport")}
                review_status = "stopped"
            else:
                # No paid phase starts after either whole-job bound is crossed.
                # The deadline check after the reviewer (below) is too late for
                # a run that was already at/past the ceiling when the review
                # round began (notably a resume): check before spawning.
                if job_budget.time_exceeded():
                    run["status"] = "deadline_exceeded"
                    run["stop_reason"] = "deadline"
                    run["status_detail"] = status_mod.STATUS_MEANING[
                        "deadline_exceeded"]
                    review_status = "stopped"
                    review_error = ("whole-job deadline reached before the "
                                    "independent review; reviewer was not started")
                    break
                spent_before, known_before = _running_cost(
                    executions + reviewer_executions)
                if (job_budget.max_cost_usd is not None
                        and job_budget.cost_exceeded(spent_before, known_before)):
                    run["status"] = "budget_exceeded"
                    run["stop_reason"] = "budget"
                    run["status_detail"] = status_mod.STATUS_MEANING[
                        "budget_exceeded"]
                    review_status = "stopped"
                    review_error = ("whole-job cost budget reached before the "
                                    "independent review; reviewer was not started")
                    break
                if checkpoint:
                    try:
                        checkpoint.begin("review-%d" % round_no)
                    except Exception:
                        pass
                _stash_old(workspace, "REVIEW.md", run["id"], round_no=round_no)
                partial_arg = args.get("_review_partial")
                if not isinstance(partial_arg, dict):
                    partial_arg = None
                # A resume is a new run record id but the same logical job.  A
                # partial checkpoint is written under the id of the attempt
                # that was interrupted, so accept either identity.
                partial_ids = {str(run["id"])}
                if args.get("_resume_from"):
                    partial_ids.add(str(args.get("_resume_from")))
                if partial_arg is not None:
                    try:
                        if (str(partial_arg.get("run_id") or "") not in partial_ids
                                or int(partial_arg.get("round") or 0) != int(round_no)):
                            partial_arg = None
                    except (TypeError, ValueError):
                        partial_arg = None
                if partial_arg is None:
                    try:
                        saved_partial = review_progress_mod.load(workspace)
                        if (isinstance(saved_partial, dict)
                                and str(saved_partial.get("run_id") or "") in partial_ids
                                and int(saved_partial.get("round") or 0) == int(round_no)):
                            partial_arg = saved_partial
                    except Exception:
                        pass
                review_areas = [
                    {"key": "brief",
                     "label": "Original brief and acceptance criteria",
                     "status": "pending"},
                    {"key": "deliverables",
                     "label": "Deliverable files and artifacts",
                     "status": "pending"},
                    {"key": "correctness",
                     "label": "Correctness, required facts and calculations",
                     "status": "pending"},
                    {"key": "tests",
                     "label": "Tests, build and signed evidence",
                     "status": "pending"},
                    {"key": "safety",
                     "label": "Safety, secrets and policy",
                     "status": "pending"},
                ]
                if not deliverable_files:
                    review_areas.append(
                        {"key": "answer",
                         "label": "Chat-only answer (no deliverable files)",
                         "status": "pending"})
                review_progress = review_progress_mod.ReviewProgress(
                    workspace, run_id=run["id"], round_no=round_no,
                    areas=review_areas, partial=partial_arg)
                try:
                    review_progress.start()
                    run["review_partial"] = review_progress.snapshot()
                    if checkpoint:
                        checkpoint.begin("review-%d" % round_no,
                                         partial=review_progress.snapshot())
                except Exception:
                    pass

                def _reviewer_progress_cb(message, _rp=review_progress):
                    try:
                        text = str(message)
                        # _handle_line forwards one complete marker line at a
                        # time (without its newline); terminate it so the
                        # checkpoint is durable immediately, not only when the
                        # next chunk happens to include a newline.
                        if text.lstrip().startswith(
                                review_progress_mod.PROGRESS_PREFIX):
                            text += "\n"
                        _rp.feed_progress(text)
                    except Exception:
                        pass
                    try:
                        _rp.heartbeat(note=str(message))
                        match = re.search(r"last:\s*([A-Za-z0-9_.-]+)",
                                          str(message))
                        if match:
                            _rp.note_tool(match.group(1))
                    except Exception:
                        pass
                    if progress_cb:
                        try:
                            progress_cb(message)
                        except Exception:
                            pass
                # R1: the connector, not the builder, runs the full suite on
                # the exact tree and signs the result for the reviewer.
                test_evidence = None
                try:
                    suite_mode = cfg.get("review_test_suite", "auto")
                    disabled = (suite_mode is False or suite_mode is None
                                or str(suite_mode).strip().lower() in ("", "off",
                                                                       "none",
                                                                       "false"))
                    if not disabled:
                        command = cfg.get("review_test_command") or None
                        mode_text = str(suite_mode).strip().lower()
                        if mode_text in ("auto", "true", "1", "on"):
                            suite_arg = command
                        elif isinstance(suite_mode, (list, tuple)):
                            suite_arg = list(suite_mode)
                        else:
                            suite_arg = command or suite_mode
                        suite_timeout = float(cfg.get(
                            "review_test_timeout_seconds", 300.0) or 300.0)
                        remaining = job_budget.remaining_seconds()
                        if remaining is not None:
                            suite_timeout = min(suite_timeout,
                                                max(0.0, remaining))
                        if suite_timeout <= 0:
                            raise RuntimeError(
                                "no whole-job time left for connector tests")
                        test_evidence = review_context.run_test_suite(
                            workspace, command=suite_arg,
                            timeout=suite_timeout,
                            key_store=str(cfg.state_dir),
                            metadata={"round": round_no})
                        # Keep the exact signed payload (including key_id) so a
                        # later connector pass can verify the evidence it
                        # handed to the reviewer; tampering must fail.
                        evidence_record = dict(test_evidence)
                        run.setdefault("review_test_evidence", []).append(
                            evidence_record)
                        try:
                            review_progress.add_test(
                                evidence_record.get("command")
                                or "connector test suite",
                                status=str(evidence_record.get("status")
                                           or "unknown"),
                                seconds=evidence_record.get("seconds"))
                            review_progress.note_area(
                                "tests",
                                "checked" if evidence_record.get("status") == "ok"
                                else "in_progress")
                        except Exception:
                            pass
                except Exception as exc:
                    test_evidence = None
                    run["review_test_error"] = api.redact_secrets(str(exc))
                    try:
                        review_progress.add_test("connector test suite",
                                                 status="error")
                    except Exception:
                        pass
                review_diff = ""
                prior_defects = []
                prior_signatures = []
                if round_no > 1:
                    prior_entry = next(
                        (item for item in reversed(review_rounds)
                         if item.get("kind") == "review"), None)
                    if prior_entry:
                        prior_defects = [str(item) for item in
                                         (prior_entry.get("defects") or [])
                                         if str(item).strip()]
                        prior_signatures = [str(item) for item in
                                            (prior_entry.get("signatures") or [])
                                            if str(item).strip()]
                if review_baseline is not None:
                    try:
                        review_diff = review_context.review_focus_text(
                            review_baseline, workspace,
                            prior_defects=prior_defects,
                            prior_signatures=prior_signatures,
                            max_chars=int(cfg.get("review_context_max_chars",
                                                  60000) or 60000),
                            round_no=round_no,
                            focus_paths=deliverable_files)
                    except Exception as exc:
                        run["review_context_error"] = api.redact_secrets(str(exc))
                review_task = build_review_task(
                    prompt, deliverable_files, skill=skill,
                    quality_target=DEFAULT_QUALITY_TARGET, round_no=round_no,
                    task_type=run.get("task_type"),
                    browser_evidence=render.evidence_hint(browser_evidence),
                    review_context=review_diff,
                    prior_defects=prior_defects,
                    prior_signatures=prior_signatures,
                    partial=partial_arg,
                    test_evidence=(review_context.format_test_evidence(
                        test_evidence) if test_evidence else None))
                # P3-2: near-free pre-flight through the SAME reviewer path.
                # A provider that cannot answer a one-word prompt must not be
                # handed the paid review.
                review_preflight_record = None
                _preflight_requested = args.get("review_preflight")
                if _preflight_requested is None:
                    _preflight_requested = cfg.get("review_preflight", True)
                if bool(_preflight_requested):
                    preflight_limit = float(cfg.get("review_preflight_timeout",
                                                   45.0) or 45.0)
                    _remaining = job_budget.remaining_seconds()
                    if _remaining is not None:
                        preflight_limit = max(1.0, min(preflight_limit,
                                                       float(_remaining)))
                    preflight = _execute_once(
                        cfg, workspace,
                        ("Reply with exactly READY and nothing else. Do not use "
                         "tools, do not read or write files, do not run commands."),
                        limit=preflight_limit, progress_cb=progress_cb,
                        label="reviewer preflight %d" % round_no,
                        exclude_logs=used_logs, policy=reviewer_policy,
                        add_dirs=run["add_dirs"],
                        transport_name=run["transport"],
                        cancel_check=cancel_check)
                    reviewer_executions.append(preflight)
                    used_logs.update(preflight.get("session_logs") or [])
                    _preflight_text = " ".join(
                        str(preflight.get(key) or "")
                        for key in ("error", "stderr_tail", "status_detail"))
                    # A vendor name in the failed process's own output is
                    # stronger evidence than a default config model.  This is
                    # what makes a Kimi/Moonshot quota error say Moonshot even
                    # when dsh_model still names the DeepSeek default.
                    if _KIMI_RE.search(_preflight_text):
                        _family = "kimi"
                        _family_execution = dict(preflight, provider="kimi")
                    elif _ANTHROPIC_RE.search(_preflight_text):
                        _family = "anthropic"
                        _family_execution = dict(preflight, provider="anthropic")
                    elif _OPENAI_RE.search(_preflight_text):
                        _family = "openai"
                        _family_execution = dict(preflight, provider="openai")
                    else:
                        _family = provider_family(execution=preflight, cfg=cfg)
                        _family_execution = preflight
                    _answered = (
                        preflight.get("status") == "ok"
                        and not preflight.get("stalled")
                        and not preflight.get("timed_out")
                        and "READY" in str(preflight.get("answer") or "").upper())
                    if not _answered:
                        _message = provider_error_message(
                            _family_execution, cfg, role="reviewer")
                        _raw = api.redact_secrets(
                            str(preflight.get("error") or ""))[:500]
                        run["reviewer_preflight"] = {
                            "status": "error",
                            "error": _raw or "pre-flight did not answer READY",
                            "message": _message,
                            "vendor": _provider_label(_family),
                            "seconds": preflight.get("seconds"),
                        }
                        run["review_error"] = _message
                        review_status = "error"
                        review_error = _message
                        run["review_in_flight"] = False
                        try:
                            review_progress.interrupt(
                                "reviewer pre-flight failed: %s" % _message)
                            run["review_partial"] = review_progress.snapshot()
                            if checkpoint:
                                checkpoint.finish(
                                    "review-%d" % round_no,
                                    status="interrupted",
                                    partial=review_progress.snapshot())
                        except Exception:
                            pass
                        _persist(run, cfg, "review-%d_preflight" % round_no)
                        review = preflight
                        break
                    review_preflight_record = preflight
                    run["reviewer_preflight"] = {
                        "status": "ok",
                        "seconds": preflight.get("seconds"),
                        "model": getattr(cfg, "dsh_model", None),
                        "checked_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "tokens_in": preflight.get("tokens_in"),
                        "cache_read_tokens": preflight.get("cache_read_tokens"),
                        "tokens_out": preflight.get("tokens_out"),
                        "total_tokens": preflight.get("total_tokens"),
                        "cost": preflight.get("estimated_cost_usd"),
                        "cost_known": preflight.get("cost_known"),
                    }
                    _persist(run, cfg, "review-%d_preflight_ok" % round_no)
                # Durable in-flight marker: the detached job wrapper reads
                # this record after the handler returns and refuses to close
                # the job while the paid review phase is still running.
                run["review_in_flight"] = True
                run["review_in_flight_round"] = int(round_no)
                _persist(run, cfg, "review-%d" % round_no)
                if trace:
                    trace.event("review-%d" % round_no, "start",
                                deliverables=len(deliverable_files))
                review_before = safety.snapshot_workspace(workspace)
                retry_policy = health.RetryPolicy(
                    "reviewer",
                    base=getattr(cfg, "retry_backoff_base_seconds", 1.0),
                    cap=getattr(cfg, "retry_backoff_cap_seconds", 60.0),
                    jitter=0.0)
                review = None
                violations = []
                review_attempt = 1
                reviewer_blocked = (health_board.quarantine_block("reviewer")
                                    if getattr(cfg, "health_gate", True)
                                    else None)
                if reviewer_blocked:
                    review = {"status": "error", "error": reviewer_blocked,
                              "seconds": 0, "estimated_cost_usd": None,
                              "cost_known": False, "session_logs": []}
                    _emit_run_event(run, cfg, "progress",
                                    health.structured_attention(
                                        "reviewer", health_board.snapshot().get(
                                            "components", {}).get("reviewer"),
                                        run_id=run["id"]))
                while review is None or (
                        review.get("status") != "ok"
                        and not reviewer_blocked):
                    admission = _cost_admit(
                        run, cfg, cost_ledger, "reviewer",
                        max(1, int(run.get("subagents") or 1)),
                        job_budget.phase_limit(limit), job_budget)
                    if admission and admission.get("error"):
                        review = {
                            "status": "error",
                            "stop_reason": "budget",
                            "error": ("cost admission refused the reviewer: %s"
                                      % admission["error"]),
                            "seconds": 0, "estimated_cost_usd": None,
                            "cost_known": False, "session_logs": [],
                        }
                        run["status"] = "budget_exceeded"
                        run["stop_reason"] = "budget"
                        run["status_detail"] = status_mod.STATUS_MEANING[
                            "budget_exceeded"]
                        if checkpoint:
                            try:
                                checkpoint.interrupted(review["error"])
                            except Exception:
                                pass
                        break
                    live_guard = None
                    review_prior, _review_known = _guard_prior(
                        admission,
                        *_running_cost(executions + reviewer_executions))
                    if job_budget.max_cost_usd is not None:
                        live_guard = budget_mod.LiveCostGuard(
                            limit=job_budget.max_cost_usd,
                            reserved=(admission or {}).get("estimate", {}).get(
                                "cost_usd") or 0.0,
                            prior_committed=review_prior,
                            prior_known=_review_known,
                            model=getattr(cfg, "dsh_model", None))

                    def review_live_cb(event, parsed, _guard=live_guard):
                        if _guard is None:
                            return None
                        usage = _usage_from_event(event)
                        if not usage:
                            return None
                        if _guard.observe(usage, _model_from_event(event)):
                            return "budget"
                        return None

                    review = _execute_once(
                        cfg, workspace, review_task,
                        limit=job_budget.phase_limit(limit),
                        progress_cb=_reviewer_progress_cb,
                        label="reviewer round %d" % round_no,
                        exclude_logs=used_logs, policy=reviewer_policy,
                        add_dirs=run["add_dirs"],
                        transport_name=run["transport"],
                        cancel_check=cancel_check,
                        live_usage_cb=review_live_cb)
                    reviewer_executions.append(review)
                    used_logs.update(review.get("session_logs") or [])
                    _review_answer_text = str(review.get("answer") or "")
                    if _review_answer_text:
                        try:
                            review_progress.feed_progress(
                                _review_answer_text, final=True)
                        except Exception:
                            pass
                        review["answer"] = (
                            review_progress_mod.strip_progress_lines(
                                _review_answer_text))
                    if review.get("child_sessions_in_flight"):
                        # A reviewer process that exited while a background
                        # child session was still writing has not completed
                        # the phase. Never let that review pass the gate.
                        review["status"] = "error"
                        review["error"] = (
                            "reviewer child session was still writing when the "
                            "reviewer process exited")
                        review["review_phase_in_flight"] = True
                        run["review_in_flight"] = True
                    _cost_settle(run, cost_ledger, "reviewer", review,
                                 job_budget,
                                 operation_id=(admission or {}).get(
                                     "reservation", {}).get("id"))
                    if str(review.get("stop_reason")) == "budget":
                        # A live budget kill is an interruption, not a normal
                        # completed review phase.
                        if checkpoint:
                            try:
                                checkpoint.interrupted(
                                    "live cost usage crossed the whole-job "
                                    "ceiling during the reviewer")
                                try:
                                    review_progress.interrupt(
                                        "whole-job budget stopped the reviewer")
                                    run["review_partial"] = review_progress.snapshot()
                                    checkpoint.finish(
                                        "review-%d" % round_no,
                                        status="interrupted",
                                        partial=review_progress.snapshot())
                                except Exception:
                                    pass
                            except Exception:
                                pass
                    review_after = safety.snapshot_workspace(workspace)
                    violations = safety.diff_workspace(
                        review_before, review_after,
                        allowed=("REVIEW.md", "REVIEW.partial.md",
                                 "REVIEW.partial.json",
                                 ".browser-evidence/", "EVIDENCE.json"))
                    if violations:
                        review["status"] = "error"
                        review["error"] = (
                            "reviewer modified files outside REVIEW.md: %s"
                            % ", ".join(
                                "%s (%s)" % (item["path"], item["change"])
                                for item in violations[:8]))
                        review["write_violations"] = violations
                        _review_health = _health_record(
                            health_board, "reviewer", review)
                        _health_attention(run, cfg, "reviewer",
                                          _review_health)
                        break
                    if review.get("status") == "ok":
                        _health_record(health_board, "reviewer", review)
                        break
                    _review_health = _health_record(
                        health_board, "reviewer", review)
                    _health_attention(run, cfg, "reviewer", _review_health)
                    spent_now, known_now = _running_cost(
                        executions + reviewer_executions)
                    allowed, reason = retry_policy.should_retry(
                        review_attempt,
                        health.classify_execution(review),
                        remaining_seconds=job_budget.remaining_seconds(),
                        spent=spent_now,
                        ceiling=job_budget.max_cost_usd,
                        cost_known=known_now)
                    if not allowed:
                        review["retry"] = {
                            "attempt": review_attempt,
                            "stopped": reason,
                        }
                        break
                    writing = ((reviewer_policy or {}).get("sandbox")
                               != "read-only")
                    safe, changes = health.side_effect_guard(
                        workspace, review_before, phase="reviewer",
                        writing=writing)
                    if not safe:
                        review["retry"] = {
                            "attempt": review_attempt,
                            "stopped": ("previous reviewer attempt changed "
                                        "files outside allowed evidence"),
                            "changes": changes[:8],
                        }
                        break
                    delay = retry_policy.delay(
                        review_attempt,
                        health.retry_after(
                            str(review.get("error") or "")))
                    review["retry"] = {
                        "attempt": review_attempt,
                        "next_delay_seconds": delay,
                        "class": health.classify_execution(review),
                    }
                    try:
                        health_board.record_retry(
                            "reviewer", health.classify_execution(review),
                            error=api.redact_secrets(str(
                                review.get("error") or "")),
                            retry_after=health.retry_after(str(
                                review.get("error") or "")))
                    except Exception:
                        pass
                    retry_policy.sleep(delay)
                    review_attempt += 1
                # Read-only DSH cannot write REVIEW.md; the connector persists
                # the reviewer's final report after checking the write guard.
                if review is None:
                    review = {"status": "error", "error": "reviewer not run",
                              "seconds": 0, "estimated_cost_usd": None,
                              "cost_known": False, "session_logs": []}
                _materialize_review(workspace, review)
                review["write_violations"] = violations
                if review.get("status") != "ok":
                    try:
                        interrupted_text, _ = _read_review(workspace)
                    except Exception:
                        interrupted_text = ""
                    interrupted_combined = "\n".join(
                        part for part in (interrupted_text or "",
                                          str(review.get("answer") or ""))
                        if part)
                    try:
                        # Flush any final protocol line, then harvest the
                        # interrupted report with its section severity intact.
                        review_progress.feed_progress(
                            interrupted_combined, final=True)
                        for interrupted_defect in (
                                review_progress_mod.extract_defect_records(
                                    interrupted_combined)):
                            review_progress.add_defect(
                                interrupted_defect["text"],
                                evidence=(interrupted_defect.get("evidence")
                                          or "interrupted reviewer output"),
                                severity=interrupted_defect.get("severity")
                                or "blocking",
                                carried_over=True)
                        review_progress.interrupt(
                            str(review.get("error") or "review interrupted"))
                        run["review_partial"] = review_progress.snapshot()
                    except Exception:
                        pass
                report_text, report_path = _read_review(workspace)
                carried_defects = []
                if partial_arg:
                    try:
                        merged_text, carried_defects = review_progress_mod.merge_review(
                            report_text, partial_arg)
                        if merged_text and merged_text != report_text:
                            review_progress_mod.write_atomic(
                                Path(workspace) / "REVIEW.md", merged_text)
                            report_text = merged_text
                            report_path = str(Path(workspace) / "REVIEW.md")
                    except Exception:
                        carried_defects = []
                parsed = parse_review(report_text, review.get("answer") or "",
                                      task_type=run.get("task_type"),
                                      prefer_sections=bool(carried_defects))
                score = parsed.get("score")
                blocking = int(parsed.get("blocking_defects") or 0)
                defects = list(parsed.get("defects") or [])
                if carried_defects:
                    # ``merge_review`` writes carried findings into the new
                    # report, so parse_review already counted them.  Match on
                    # (normalised text, severity group): a finding repeated
                    # under the wrong heading must not be silently suppressed.
                    known = set()
                    for parsed_defect in (
                            review_progress_mod.extract_defect_records(
                                report_text or "")):
                        parsed_key = review_progress_mod.normalise_text(
                            parsed_defect.get("text"))
                        if parsed_key:
                            # The actual heading decides placement; an inline
                            # tag must not hide a wrong-heading repeat.
                            known.add((parsed_key,
                                       review_progress_mod.severity_group(
                                           parsed_defect.get("section")
                                           or parsed_defect.get("severity"))))
                    for carried_item in carried_defects:
                        carried_text = (carried_item.get("text")
                                        if isinstance(carried_item, dict)
                                        else str(carried_item))
                        carried_key = review_progress_mod.normalise_text(
                            carried_text)
                        carried_severity = (
                            carried_item.get("severity")
                            if isinstance(carried_item, dict) else None
                        ) or "blocking"
                        carried_group = review_progress_mod.severity_group(
                            carried_severity)
                        if not carried_key:
                            continue
                        if (carried_key, carried_group) in known:
                            continue
                        defects.append(carried_text)
                        known.add((carried_key, carried_group))
                        if carried_group == "blocking":
                            blocking += 1
                fixes = list(parsed.get("fixes") or [])
                score_missing = False
                reprompt_record = None
                if (score is None and review.get("status") == "ok"
                        and reused is None):
                    # 3.9.4: one score re-prompt before declaring the review
                    # unscored.  The same reviewer session is resumed when it
                    # has an id; only a dead session gets a fresh short call.
                    reprompt_record = _reprompt_review_score(
                        cfg, workspace, review, report_text,
                        round_no=round_no, policy=reviewer_policy,
                        transport_name=run["transport"],
                        limit=job_budget.phase_limit(limit),
                        cancel_check=cancel_check, budget=job_budget,
                        used_logs=used_logs)
                    if reprompt_record.get("attempted"):
                        reviewer_executions.append(reprompt_record)
                        for _log in reprompt_record.get("session_logs") or []:
                            used_logs.add(_log)
                    if reprompt_record.get("score") is not None:
                        recovered_score = float(reprompt_record["score"])
                        # The reviewer's own report sections decide the
                        # blocking count.  The follow-up score is recorded for
                        # audit, but it can never add a blocking defect the
                        # reviewer never listed (no invented fix demand).  A
                        # score-only reply (missing "Blocking defects:")
                        # likewise keeps the report count instead of a
                        # fabricated zero.
                        reply_blocking = reprompt_record.get("blocking")
                        if reply_blocking is not None:
                            reply_blocking = int(reply_blocking)
                            if reply_blocking > blocking:
                                reprompt_record["blocking_disagreement"] = {
                                    "report": blocking,
                                    "reprompt": reply_blocking}
                        report_text = _append_score_to_review(
                            workspace, recovered_score, blocking, report_text)
                        report_path = str(Path(workspace) / "REVIEW.md")
                        # Reparse the augmented report with the original task
                        # type and carried-section preference: the creative
                        # visual-impact cap and the section-derived review
                        # fields must apply exactly as on the normal scored
                        # path, never the raw re-prompt number.
                        rescored = parse_review(
                            report_text, review.get("answer") or "",
                            task_type=run.get("task_type"),
                            prefer_sections=bool(carried_defects))
                        if rescored.get("score") is not None:
                            score = float(rescored["score"])
                            blocking = int(
                                rescored.get("blocking_defects") or 0)
                            defects = list(rescored.get("defects") or [])
                            fixes = list(rescored.get("fixes") or [])
                            parsed = rescored
                        else:
                            score = recovered_score
                if score is None and review.get("status") == "ok":
                    # Unscored review: score stays None and the reviewer's
                    # real blocking count/defects are untouched.  Never
                    # fabricate 0.0 or a synthetic blocking defect.
                    score_missing = True
                entry = {
                    "round": round_no,
                    "kind": "review",
                    "score": score,
                    "score_missing": bool(score_missing),
                    "score_status": ("score_missing" if score_missing else
                                     ("scored" if score is not None
                                      else "review_failed")),
                    "blocking_defects": blocking,
                    "defects": defects,
                    "fixes": fixes,
                    "visual_impact": parsed.get("visual_impact"),
                    "creative_cap": bool(parsed.get("creative_cap")),
                    "seconds": review.get("seconds"),
                    "cost": review.get("estimated_cost_usd"),
                    "tokens_in": review.get("tokens_in"),
                    "cache_read_tokens": review.get("cache_read_tokens"),
                    "tokens_out": review.get("tokens_out"),
                    "total_tokens": review.get("total_tokens"),
                    "usage": review.get("usage"),
                    "cost_known": review.get("cost_known"),
                    "models": review.get("models"),
                    "usage_unknown_reason": review.get("usage_unknown_reason"),
                    "status": review.get("status"),
                    "error": review.get("error"),
                    "stalled": bool(review.get("stalled")),
                    "stalled_step": review.get("stalled_step"),
                    "report": report_path,
                    "report_text": report_text,
                    "session_logs": review.get("session_logs") or [],
                    "write_violations": violations,
                    "sandbox": reviewer_policy,
                    "transport": review.get("transport"),
                    "accepted": bool(not score_missing
                                     and score is not None
                                     and score >= DEFAULT_QUALITY_TARGET
                                     and blocking == 0),
                }
                if reprompt_record is not None:
                    # Record skipped/budget-refused attempts too: the run and
                    # journal should say the connector tried, and why it did
                    # not launch a paid call.
                    if reprompt_record.get("attempted"):
                        entry = _merge_reprompt_usage(entry, reprompt_record)
                    run.setdefault("review_score_reprompts", []).append(
                        reprompt_record)
                    _emit_run_event(run, cfg, "score_reprompt", {
                        "round": round_no,
                        "method": reprompt_record.get("method"),
                        "status": reprompt_record.get("status"),
                        "fallback_used": reprompt_record.get("fallback_used"),
                        "score": reprompt_record.get("score"),
                        "blocking": reprompt_record.get("blocking"),
                        "contract_complete": reprompt_record.get(
                            "contract_complete"),
                        "seconds": reprompt_record.get("seconds"),
                        "tokens_in": reprompt_record.get("tokens_in"),
                        "cache_read_tokens": reprompt_record.get(
                            "cache_read_tokens"),
                        "tokens_out": reprompt_record.get("tokens_out"),
                        "total_tokens": reprompt_record.get("total_tokens"),
                        "cost": reprompt_record.get("estimated_cost_usd"),
                        "cost_known": reprompt_record.get("cost_known"),
                        "session_id": reprompt_record.get("session_id"),
                    })
                if score_missing:
                    run["review_score_missing"] = True
                    _unscored = run.setdefault("review_unscored_rounds", [])
                    if int(round_no) not in _unscored:
                        _unscored.append(int(round_no))
                if review_preflight_record is not None:
                    entry = _merge_preflight_usage(
                        entry, review_preflight_record, cfg)
                    entry["preflight"] = {
                        "status": "ok",
                        "seconds": review_preflight_record.get("seconds"),
                        "tokens_in": entry["preflight_usage"]["tokens_in"],
                        "cache_read_tokens": entry["preflight_usage"][
                            "cache_read_tokens"],
                        "tokens_out": entry["preflight_usage"]["tokens_out"],
                        "total_tokens": entry["preflight_usage"]["total_tokens"],
                        "cost": entry["preflight_usage"]["cost"],
                        "cost_known": entry["preflight_usage"]["cost_known"],
                    }
                entry.update(_review_round_usage(entry, cfg))
                try:
                    persist_signatures(review_rounds, entry)
                except Exception:
                    entry.setdefault("signatures", [])
                    entry.setdefault("first_seen_round", {})
                review_rounds.append(entry)
                if not review.get("review_phase_in_flight"):
                    run["review_in_flight"] = False
                run["reviewer_usage"] = _reviewer_usage_aggregate(review_rounds)
                run["reviewer_cost_known"] = run["reviewer_usage"]["cost_known"]
                try:
                    if review.get("status") == "ok":
                        # Only a completed reviewer may close the checkpoint.
                        # An error/timeout must leave the partial interrupted so
                        # the next resume can hand its findings to a new
                        # reviewer instead of treating them as finished.
                        completed_defects = (
                            review_progress_mod.extract_defect_records(
                                report_text or review.get("answer") or ""))
                        if not completed_defects and defects:
                            completed_defects = [
                                {"text": item,
                                 "severity": ("blocking" if index < blocking
                                              else "non-blocking")}
                                for index, item in enumerate(defects)]
                        review_progress.complete(
                            report_text, defects=completed_defects)
                    run["review_partial"] = review_progress.snapshot()
                except Exception:
                    pass
                if checkpoint and str(review.get("stop_reason")) != "budget":
                    try:
                        checkpoint.finish(
                            "review-%d" % round_no,
                            status=_checkpoint_status(review.get("status")),
                            score=score, blocking=blocking,
                            accepted=entry["accepted"],
                            defects=defects, fixes=fixes,
                            signatures=entry.get("signatures"),
                            first_seen_round=entry.get("first_seen_round"),
                            visual_impact=entry["visual_impact"],
                            creative_cap=entry["creative_cap"],
                            seconds=review.get("seconds"),
                            cost=review.get("estimated_cost_usd"),
                            tokens_in=entry.get("tokens_in"),
                            cache_read_tokens=entry.get("cache_read_tokens"),
                            tokens_out=entry.get("tokens_out"),
                            total_tokens=entry.get("total_tokens"),
                            usage=entry.get("usage"),
                            models=entry.get("models"),
                            cost_known=entry.get("cost_known"),
                            preflight=entry.get("preflight"),
                            preflight_usage=entry.get("preflight_usage"),
                            report=report_path,
                            report_text=report_text,
                            partial=review_progress.snapshot(),
                            session_id=review.get("transport_session_id"),
                            write_violations=violations,
                        )
                    except Exception:
                        pass
                if trace:
                    trace.event("review-%d" % round_no, "finish",
                                status=review.get("status"), score=score,
                                blocking=blocking,
                                seconds=review.get("seconds"))
                _persist(run, cfg, "review-%d_done" % round_no)
            entry = next((item for item in reversed(review_rounds)
                          if item.get("kind") == "review"
                          and int(item.get("round") or 0) == round_no), None)
            if review.get("status") != "ok":
                review_status = "error"
                review_error = (
                    provider_error_message(review, cfg, role="reviewer")
                    if _is_no_balance(review) or review.get("error")
                    else "reviewer run failed")
                if str(review.get("stop_reason")) == "budget":
                    run["status"] = "budget_exceeded"
                    run["stop_reason"] = "budget"
                    run["status_detail"] = status_mod.STATUS_MEANING[
                        "budget_exceeded"]
                break
            # Budget is a whole-job ceiling: check it before accepting, because
            # the reviewer itself is a paid phase and can take the run over the
            # limit. No further paid phase starts once either bound is crossed.
            if job_budget.time_exceeded():
                run["status"] = "deadline_exceeded"
                run["stop_reason"] = "deadline"
                run["status_detail"] = status_mod.STATUS_MEANING["deadline_exceeded"]
                review_error = ("whole-job deadline reached after the independent "
                                "review; no revision was started")
                review_status = "stopped"
                break
            if job_budget.cost_exceeded(*_running_cost(executions + reviewer_executions)):
                run["status"] = "budget_exceeded"
                run["stop_reason"] = "budget"
                run["status_detail"] = status_mod.STATUS_MEANING["budget_exceeded"]
                review_error = ("whole-job cost budget reached after the "
                                "independent review; no revision was started")
                review_status = "stopped"
                break
            if score is not None and score >= DEFAULT_QUALITY_TARGET and blocking == 0:
                review_status = "accepted"
                if entry is not None:
                    entry["accepted"] = True
                break
            if score_missing and blocking == 0:
                # The reviewer produced no parsable score but also reported no
                # real blocking defect.  Do not buy a revision for a defect
                # that does not exist: mark the round stopped and let the next
                # review round re-review (or stop cleanly at the round cap).
                review_status = "stopped"
                if round_no >= rounds:
                    break
                continue
            # P3-2: stop buying the same fix. The decision is computed from
            # blocking signatures; observe records it without enforcing.
            convergence_mode = str(cfg.get("quality_convergence", "observe")
                                   or "observe").strip().lower()
            if convergence_mode not in ("observe", "auto"):
                convergence_mode = "observe"
            decision = convergence_decision(
                review_rounds, mode=convergence_mode,
                target=DEFAULT_QUALITY_TARGET)
            run["quality_convergence"] = dict(decision, mode=convergence_mode)
            if entry is not None:
                entry["convergence"] = {
                    "action": decision.get("action"),
                    "reason": decision.get("reason"),
                    "signatures": decision.get("signatures") or [],
                    "first_seen_round": decision.get("first_seen_round"),
                    "cosmetic_only": bool(decision.get("cosmetic_only")),
                }
            if decision.get("action") == "stop" and not score_missing:
                if scope_resume is not None and int(round_no) == scope_round:
                    # One manual scoped cycle is allowed. Record that the
                    # historical signature was intentionally overridden once.
                    run["manual_scope_resume"]["suppressed_stop_round"] = (
                        int(round_no))
                else:
                    run["convergence_stopped"] = True
                    attention = {
                        "question": decision.get("question"),
                        "options": list(decision.get("options") or []),
                        "evidence": list(decision.get("evidence") or []),
                    }
                    run["convergence_attention"] = attention
                    try:
                        _emit_convergence_attention(run, cfg, attention)
                    except Exception:
                        pass
                    review_status = "stopped"
                    review_error = None
                    break
            if decision.get("action") == "observe_stop":
                run["convergence_observed"] = True
            if round_no >= rounds:
                review_status = "stopped"
                break
            # Revision is its own fresh process; it gets only the fix list and
            # the original brief, never the builder's quality conclusions. A
            # failed review may escalate the agent count (automatic choices
            # only) and/or require a different approach; the reason is recorded.
            current_agents = (escalations[-1]["to"] if escalations
                              else int(run.get("subagents") or 0))
            escalation = plan_escalation(
                run.get("task_type") or "creative", current_agents, score, blocking,
                round_no, concurrency=child_concurrency,
                target=DEFAULT_QUALITY_TARGET, explicit=explicit is not None,
                soft_cap=getattr(cfg, "subagent_soft_cap", None),
                default_subagents=getattr(cfg, "default_subagents", None))
            if escalation:
                escalations.append(escalation)
            _stash_old(workspace, "REVIEW.md", run["id"], round_no=round_no)
            round_scope = (scope_note if (scope_resume is not None
                                          and int(round_no) == scope_round)
                           else None)
            revision_task = build_revision_task(
                prompt, deliverable_files, fixes, defects, skill=skill,
                quality_target=DEFAULT_QUALITY_TARGET, round_no=round_no,
                escalation=escalation, task_type=run.get("task_type"),
                scope_note=round_scope, score_missing=score_missing)
            if checkpoint:
                try:
                    checkpoint.begin("revision-%d" % round_no)
                except Exception:
                    pass
            _persist(run, cfg, "revision-%d" % round_no)
            if trace:
                trace.event("revision-%d" % round_no, "start",
                            fixes=len(fixes), blocking=blocking)
            revision_block = (health_board.quarantine_block("revision")
                              if getattr(cfg, "health_gate", True) else None)
            if revision_block:
                review_status = "stopped"
                review_error = revision_block
                run["revision_health_block"] = revision_block
                _health_block_attention(run, cfg, health_board, "revision",
                                        revision_block)
                if checkpoint:
                    try:
                        checkpoint.interrupted(revision_block)
                    except Exception:
                        pass
                break
            rev_admission = _cost_admit(
                run, cfg, cost_ledger, "revision",
                max(1, int(run.get("subagents") or 1)),
                job_budget.phase_limit(limit), job_budget)
            if rev_admission and rev_admission.get("error"):
                review_status = "stopped"
                review_error = ("cost admission refused the revision: %s"
                                % rev_admission["error"])
                run["status"] = "budget_exceeded"
                run["stop_reason"] = "budget"
                run["status_detail"] = status_mod.STATUS_MEANING[
                    "budget_exceeded"]
                if checkpoint:
                    try:
                        checkpoint.interrupted(review_error)
                    except Exception:
                        pass
                break
            revision_guard = None
            revision_live_cb = None
            if job_budget.max_cost_usd is not None:
                revision_prior, _rev_known = _guard_prior(
                    rev_admission,
                    *_running_cost(executions + reviewer_executions))
                revision_guard = budget_mod.LiveCostGuard(
                    limit=job_budget.max_cost_usd,
                    reserved=(rev_admission or {}).get(
                        "estimate", {}).get("cost_usd") or 0.0,
                    prior_committed=revision_prior,
                    prior_known=_rev_known,
                    model=getattr(cfg, "dsh_model", None))

                def revision_live_cb(event, parsed, _guard=revision_guard):
                    usage = _usage_from_event(event)
                    if not usage:
                        return None
                    if _guard.observe(usage, _model_from_event(event)):
                        return "budget"
                    return None
            # R3: capture the tree *before* the revision writes, so the next
            # review's connector diff is exactly what this revision changed.
            # Capturing after it made the next diff empty and discarded the
            # very fix the reviewer must inspect.
            if (review_baseline_root is not None
                    and not (resumed_baseline
                             and review_baseline_round == round_no)):
                try:
                    review_baseline = review_context.capture_baseline(
                        workspace, review_baseline_root)
                    review_baseline_round = round_no
                    run["review_baseline"] = {
                        "dir": str(review_baseline_root),
                        "sha256": review_baseline.get("sha256"),
                        "files": len(review_baseline.get("files") or []),
                        "round": round_no,
                    }
                    run.setdefault("review_baseline_refreshes", []).append(
                        round_no)
                    run.pop("review_baseline_refresh_error", None)
                    # Make the current round's baseline durable *before* the
                    # revision writes. A kill during the revision then leaves
                    # a resume that can reuse this exact pre-revision tree
                    # instead of recapturing the partially revised workspace.
                    _persist(run, cfg, "revision-%d_baseline" % round_no)
                except Exception as exc:
                    # Keep the previous baseline: the next diff is then a
                    # cumulative superset, never an empty one.
                    run["review_baseline_refresh_error"] = (
                        api.redact_secrets(str(exc)))
            revision = _execute_once(
                cfg, workspace, revision_task,
                limit=job_budget.phase_limit(limit),
                progress_cb=progress_cb,
                label="revision round %d" % round_no,
                exclude_logs=used_logs, policy=revision_policy,
                add_dirs=run["add_dirs"],
                transport_name=run["transport"],
                cancel_check=cancel_check,
                live_usage_cb=revision_live_cb)
            reviewer_executions.append(revision)
            used_logs.update(revision.get("session_logs") or [])
            _cost_settle(run, cost_ledger, "revision", revision, job_budget,
                         operation_id=(rev_admission or {}).get(
                             "reservation", {}).get("id"))
            revision_health = _health_record(
                health_board, "revision", revision)
            _health_attention(run, cfg, "revision", revision_health)
            rev_entry = {
                "round": round_no,
                "kind": "revision",
                "score": None,
                "blocking_defects": 0,
                "defects": defects,
                "fixes": fixes,
                "seconds": revision.get("seconds"),
                "cost": revision.get("estimated_cost_usd"),
                "status": revision.get("status"),
                "error": revision.get("error"),
                "stalled": bool(revision.get("stalled")),
                "stalled_step": revision.get("stalled_step"),
                "tokens_in": revision.get("tokens_in"),
                "cache_read_tokens": revision.get("cache_read_tokens"),
                "tokens_out": revision.get("tokens_out"),
                "total_tokens": revision.get("total_tokens"),
                "usage": revision.get("usage"),
                "models": revision.get("models"),
                "cost_known": revision.get("cost_known"),
                "files": revision.get("output_files") or [],
                "session_logs": revision.get("session_logs") or [],
                "escalation": escalation,
            }
            rev_entry.update(_review_round_usage(rev_entry, cfg))
            review_rounds.append(rev_entry)
            if (str(revision.get("stop_reason")) == "budget"
                    or (revision_guard is not None and revision_guard.exceeded)):
                message = ("live cost usage crossed the whole-job ceiling; "
                           "the revision was stopped at an event boundary")
                run["status"] = "budget_exceeded"
                run["stop_reason"] = "budget"
                run["status_detail"] = status_mod.STATUS_MEANING[
                    "budget_exceeded"]
                run["error"] = message
                review_status = "stopped"
                review_error = message
                rev_entry["status"] = "budget_exceeded"
                rev_entry["error"] = message
                if checkpoint:
                    try:
                        checkpoint.interrupted(message)
                    except Exception:
                        pass
                break
            if checkpoint:
                try:
                    checkpoint.finish(
                        "revision-%d" % round_no,
                        status=_checkpoint_status(revision.get("status")),
                        files=revision.get("output_files") or [],
                        fixes=fixes, defects=defects,
                        escalation=escalation,
                        seconds=revision.get("seconds"),
                        cost=revision.get("estimated_cost_usd"),
                        session_id=revision.get("transport_session_id"),
                        error=(revision.get("error") or "")[:500] or None,
                    )
                except Exception:
                    pass
            if trace:
                trace.event("revision-%d" % round_no, "finish",
                            status=revision.get("status"),
                            seconds=revision.get("seconds"))
            _persist(run, cfg, "revision-%d_done" % round_no)
            if revision.get("status") != "ok":
                review_status = "error"
                review_error = (
                    provider_error_message(revision, cfg, role="revision")
                    if _is_no_balance(revision) or revision.get("error")
                    else "revision run failed")
                break
            deliverable_files = _deliverable_files(
                workspace, _union_files(run.get("output_files"),
                                        revision.get("output_files")))
        run["quality_rounds_used"] = len([item for item in review_rounds
                                           if item.get("kind") == "review"])
    else:
        run["quality_rounds_used"] = 0
    run["agent_escalations"] = escalations

    if independent:
        last_review = next((item for item in reversed(review_rounds)
                            if item.get("kind") == "review"), None)
        run["quality_score"] = last_review.get("score") if last_review else None
        run["review_status"] = review_status
        # A pre-flight failure carries the vendor-correct remedy and must not
        # be replaced by the generic "no review round recorded" text.
        if not ((run.get("reviewer_preflight") or {}).get("status") == "error"
                and run.get("review_error")):
            run["review_error"] = review_error
        run["review_rounds"] = review_rounds
        run["reviewer_usage"] = _reviewer_usage_aggregate(review_rounds)
        run["reviewer_cost_known"] = run["reviewer_usage"]["cost_known"]
        _report_text, report_path = _read_review(workspace)
        if report_path:
            run["review_report"] = report_path
    else:
        run["quality_score"] = run.get("self_score") if quality else None
        run["review_status"] = "disabled"
        run["review_rounds"] = []
        run["quality_rounds_used"] = 0
        run["reviewer_usage"] = _reviewer_usage_aggregate([])
        run["reviewer_cost_known"] = False

    run["deliverable_files"] = deliverable_files
    all_executions = executions + reviewer_executions
    totals = _sum_executions(all_executions)
    run.update(totals)
    if totals.get("models"):
        run["models"] = totals["models"]
        models = sorted(totals["models"])
        run["model"] = models[0] if len(models) == 1 else "harness: " + ", ".join(models)
    if not totals.get("cost_known"):
        run["usage_unknown_reason"] = (
            "one or more builder/reviewer/revision processes had missing or "
            "unreadable session logs")
    run["seconds"] = round(time.time() - overall_started, 1)
    execution_rows = [
        {"label": "builder", "seconds": builder.get("seconds"),
         "cost": builder.get("estimated_cost_usd"), "status": builder.get("status")}
    ]
    if run.get("finisher"):
        execution_rows.append({
            "label": "finisher",
            "seconds": (run.get("finisher") or {}).get("seconds"),
            "cost": (run.get("finisher") or {}).get("cost"),
            "status": (run.get("finisher") or {}).get("status"),
        })
    execution_rows += [
        {"label": ("review" if item.get("kind") == "review" else "revision")
         + " %s" % item.get("round"), "seconds": item.get("seconds"),
         "cost": item.get("cost"), "status": item.get("status")}
        for item in review_rounds
    ]
    run["executions"] = execution_rows
    # Merge output files from the builder, the finisher (if any) and every
    # revision (review only adds REVIEW.md, intentionally not a deliverable).
    output_files = list(run.get("output_files") or [])
    if not output_files:
        output_files = list(builder.get("output_files") or [])
    for item in review_rounds:
        if item.get("kind") == "revision":
            for name in item.get("files") or []:
                if name not in output_files:
                    output_files.append(name)
    run["output_files"] = sorted(output_files)[:200]
    review_warning = None
    last_review = next((item for item in reversed(review_rounds)
                        if item.get("kind") == "review"), None)
    final_blocking = int((last_review or {}).get("blocking_defects") or 0)
    current_status = run.get("status")
    hard_stop = current_status in ("cancelled", "deadline_exceeded",
                                   "budget_exceeded", "no_balance", "error")
    review_entries = [item for item in review_rounds
                      if item.get("kind") == "review"]
    review_recorded = bool(review_entries)
    run["review_recorded"] = review_recorded if independent else None
    forced_review_error = False
    if independent and not hard_stop:
        in_flight = bool(run.get("review_in_flight"))
        unrecorded = (review_status != "disabled" and not review_recorded)
        if in_flight or unrecorded:
            # Never return a success-shaped status for a review that was
            # requested but had no durable round, or whose phase was still
            # marked in flight.  ``ok_unreviewed`` is for a *recorded*
            # reviewer failure; an unrecorded review is a run error.
            review_status = "error"
            preflight_error = run.get("reviewer_preflight") or {}
            if (isinstance(preflight_error, dict)
                    and preflight_error.get("status") == "error"
                    and str(preflight_error.get("message") or "").strip()):
                # Fail fast with the vendor-correct remedy from the pre-flight.
                # The generic "no round recorded" message would hide whether
                # the account has credit and what the operator must do.
                review_error = str(preflight_error["message"]).strip()
            elif in_flight:
                review_error = (
                    "independent review phase was still in flight when the "
                    "run tried to finish; no terminal review round was recorded")
            else:
                review_error = (
                    "independent review was requested but no review round was "
                    "recorded")
            run["status"] = "error"
            run["status_detail"] = review_error
            run["review_status"] = review_status
            run["review_error"] = review_error
            run["review_in_flight"] = False
            run["review_passed"] = False
            forced_review_error = True
    if not hard_stop and not forced_review_error:
        run["status"], status_detail, review_passed = status_mod.final_status(
            builder_status="ok_rescued" if run.get("rescued") else "ok",
            review_status=review_status,
            score=(last_review or {}).get("score"),
            blocking_defects=final_blocking,
            target=float(run.get("quality_target") or DEFAULT_QUALITY_TARGET),
            rescued=bool(run.get("rescued")),
            cancelled=bool(run.get("cancelled")) or run.get("stop_reason") == "cancelled",
            stop_reason=run.get("stop_reason"),
            review_recorded=review_recorded if independent else True)
        run["status_detail"] = status_detail
        run["review_passed"] = review_passed
    if review_status == "error":
        # The builder succeeded: keep its work, but say plainly it is unverified.
        run.setdefault("status", "ok_unreviewed")
        run["quality_score"] = None
        run["review_passed"] = False
        review_warning = ("Independent review failed: %s; deliverables were not "
                          "independently verified." % (review_error or "unknown"))
        run["review_warning"] = review_warning
    elif run.get("status") == "ok_below_gate":
        if (last_review or {}).get("score") is None:
            review_warning = (
                "Independent review finished without a parseable 0-10 score "
                "(review unscored, round %s); the run did not pass the quality "
                "gate and is not release-ready. The reviewer reported %d real "
                "blocking defect(s). Read REVIEW.md and the fix list; the score "
                "is missing, not 0/10."
                % ((last_review or {}).get("round"),
                   int(final_blocking or 0)))
        else:
            review_warning = (
                "Independent review did not pass: score %s/10, %d blocking defect(s). "
                "Read REVIEW.md and the fix list; this result is not release-ready."
                % ((last_review or {}).get("score"), final_blocking))
        run["review_warning"] = review_warning
    run["status_vocabulary"] = "3.6"
    if _note_agent_limit(run, all_executions):
        try:
            concurrency.invalidate(cfg)
        except Exception:
            pass

    # Recurring reviewer findings: only when the same defect kind appeared in
    # at least two runs of this task type.  append_lesson sanitises/dedupes.
    lessons_added = []
    try:
        for item in recurring_lessons(run, history_runs):
            lesson_path = append_lesson(item["skill"], item["lesson"], cfg,
                                        run_id=run["id"])
            entry = dict(item)
            entry["path"] = lesson_path
            lessons_added.append(entry)
    except Exception:
        pass
    run["lessons_added"] = lessons_added

    spent, cost_known = _running_cost(executions + reviewer_executions)
    run["job_budget"] = job_budget.snapshot(spent, cost_known)
    run["job_time_seconds"] = job_budget.elapsed()
    run["job_cost_usd"] = spent
    run["job_cost_known"] = cost_known

    # Auto-skills observes AFTER status/review/quality/lessons/output_files are
    # final and BEFORE trace.finish, so the run record and trace agree.
    skills_summary = None
    if getattr(cfg, "auto_skills", "off") != "off":
        _observe_skills(run, cfg, history_runs, trace, cancel_check)
        try:
            from . import skill_auto
            skills_summary = skill_auto.skill_summary(run)
        except Exception:
            skills_summary = {}

    if trace:
        try:
            payload = {
                "status": run.get("status"),
                "seconds": run.get("seconds"),
                "quality_score": run.get("quality_score"),
                "review_status": run.get("review_status"),
                "extra_waves": (run.get("live_scaling") or {}).get("extra_waves_used"),
            }
            if skills_summary is not None:
                payload["skills"] = skills_summary
            summary = trace.finish(payload)
            run["observability"] = summary
            if skills_summary is not None and isinstance(summary, dict):
                run["observability"]["skills"] = skills_summary
            run["observability"]["export"] = observability.export_run(
                cfg, run, summary.get("timeline"))
        except Exception as exc:
            run["observability"] = {"error": api.redact_secrets(str(exc))}

    if checkpoint:
        try:
            if (run.get("status") == "budget_exceeded"
                    and run.get("stop_reason") == "budget"):
                # A budget kill is an interruption, not a finished manifest:
                # leave the phase/manifest resumable instead of finalizing.
                checkpoint.interrupted(
                    run.get("error") or "whole-job budget exceeded")
                run["checkpoint_status"] = "interrupted"
            else:
                checkpoint.finalize({
                    "status": run.get("status"),
                    "status_detail": run.get("status_detail"),
                    "quality_score": run.get("quality_score"),
                    "review_status": run.get("review_status"),
                    "review_passed": run.get("review_passed"),
                    "deliverable_files": deliverable_files,
                })
                run["checkpoint_status"] = "finished"
        except Exception as exc:
            run["checkpoint_error"] = str(exc)
    path = _save_terminal(run, cfg, history_runs, trace, cancel_check)
    if skill_key and lesson_signal(run):
        try:
            lesson = distill_lesson(run)
            if lesson:
                append_lesson(skill_key, lesson, cfg, run_id=run["id"])
        except Exception:
            pass
    text = _result_text(run, path)
    convergence_attention = run.get("convergence_attention")
    if isinstance(convergence_attention, dict) and convergence_attention.get("question"):
        text = text.rstrip() + "\n\n```attention\n%s\n```\n" % json.dumps(
            convergence_attention, ensure_ascii=False, sort_keys=True)
    warnings = []
    if run.get("rescued"):
        warnings.append(_rescue_warning(run))
    if review_warning:
        warnings.append(review_warning)
    if warnings:
        text = "".join("[WARNING] %s\n" % item for item in warnings) + text
    return text, run, path


def _resume_final_status(result, prior_rounds, spec=None,
                         accepted_review=None):
    """Finalize a resumed run that has no phase left to execute.

    A crash between checkpoint/review-finish and the final run-record write can
    leave a record with no review round even though an independent review was
    requested.  Such a record must not become ``ok`` on resume.  This pure
    helper is also the regression point for that window.
    """
    result = result if isinstance(result, dict) else {}
    entries = [item for item in (prior_rounds or [])
               if isinstance(item, dict) and item.get("kind") == "review"]
    expected = result.get("independent_review")
    if expected is None:
        expected = (spec or {}).get("independent_review")
    expected = bool(expected)
    saved_review_status = str(result.get("review_status") or "").lower()
    phase = str(result.get("phase") or "")
    in_flight = bool(
        result.get("review_in_flight")
        or result.get("review_phase_in_flight")
        or (phase.startswith("review-") and not phase.endswith("_done"))
        or saved_review_status in ("pending", "running", "in_progress"))
    recorded = bool(entries) or accepted_review is not None
    result["review_recorded"] = recorded if expected else None
    if expected and (in_flight or not recorded):
        result["status"] = "error"
        result["review_status"] = "error"
        result["review_error"] = (
            "independent review phase was still in flight when the run was "
            "resumed; no terminal review round was recorded"
            if in_flight else
            "independent review was requested but no review round was recorded")
        result["review_passed"] = False
        result["status_detail"] = result["review_error"]
        result["review_rounds"] = list(prior_rounds or [])
        return result
    last_review = next(
        (item for item in reversed(entries) if item.get("kind") == "review"),
        None)
    review_status = result.get("review_status")
    if review_status in (None, "", "disabled") and recorded:
        review_status = ("accepted" if accepted_review is not None
                         else "stopped")
    elif review_status in (None, ""):
        review_status = "disabled"
    score = (accepted_review or {}).get("score")
    if score is None and last_review is not None:
        score = last_review.get("score")
    blocking = int((accepted_review or last_review or {}).get(
        "blocking_defects") or 0)
    result["quality_score"] = score
    result["review_rounds"] = list(prior_rounds or [])
    result["status"], status_detail, review_passed = status_mod.final_status(
        builder_status="ok_rescued" if result.get("rescued") else "ok",
        review_status=review_status,
        score=score,
        blocking_defects=blocking,
        target=float(result.get("quality_target") or DEFAULT_QUALITY_TARGET),
        rescued=bool(result.get("rescued")),
        cancelled=bool(result.get("cancelled")),
        stop_reason=result.get("stop_reason"),
        review_recorded=recorded if expected else True)
    result["status_detail"] = status_detail
    result["review_passed"] = review_passed
    return result


def _resume_review_partial(saved, manifest, prior_rounds, workspace, run_id):
    """Return the freshest usable partial review for ``run_id``, or None.

    A hard kill leaves the run record at the last explicit persist, while the
    workspace JSON is rewritten on every heartbeat.  Pick the newest snapshot
    by ``updated`` instead of trusting list order, so a resumed reviewer gets
    the findings recorded closest to the interruption.
    """
    candidates = [saved.get("review_partial")]
    for item in reversed(prior_rounds or []):
        if isinstance(item, dict):
            candidates.append(item.get("partial"))
    for name in sorted((manifest or {}).get("phases") or {}):
        if not str(name).startswith("review-"):
            continue
        entry = ((manifest.get("phases") or {}).get(name) or {})
        candidates.append((entry.get("data") or {}).get("partial"))
    try:
        candidates.append(review_progress_mod.load(workspace))
    except Exception:
        pass
    usable = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("run_id") or run_id) != str(run_id):
            continue
        try:
            number = int(candidate.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if number < 1:
            continue
        if candidate.get("status") == "completed":
            recorded = False
            for item in prior_rounds or []:
                if not isinstance(item, dict) or item.get("kind") != "review":
                    continue
                try:
                    recorded = int(item.get("round") or 0) == number
                except (TypeError, ValueError):
                    recorded = False
                if recorded:
                    break
            if recorded:
                # That round is already durably recorded; do not replay it.
                continue
        stamp = str(candidate.get("updated") or candidate.get("heartbeat_at")
                    or candidate.get("started") or "")
        usable.append((stamp, number, candidate))
    if not usable:
        return None
    usable.sort(key=lambda item: (item[0], item[1]))
    return dict(usable[-1][2])


def resume_harness(args, cfg, progress_cb=None, cancel_check=None):
    """Resume a checkpointed run from its last finished phase.

    Works with or without ``runs/<id>.json``: the checkpoint metadata carries a
    self-sufficient job spec, so a killed run whose log write was lost is still
    discoverable. A completed builder is reused, finished review/revision
    rounds are reused (including a rejected review plus its revision), and an
    accepted review report is restored verbatim instead of being replaced by a
    stub.

    Returns ``(text, run, path)`` exactly like :func:`run_harness`.
    """
    run_id = str(args.get("resume_run_id") or "").strip()
    if not run_id:
        raise RuntimeError("resume_run_id is required")
    root = Path(cfg.state_dir) / "checkpoints"
    manifest = checkpoints.Checkpoint.find(root, run_id)
    if not manifest:
        raise RuntimeError("No durable checkpoint for run %r; start a new run" % run_id)
    try:
        saved, _json_path, md_path = find_run(run_id, cfg)
    except RuntimeError:
        saved, md_path = _synthetic_saved_run(manifest), None
    metadata = manifest.get("metadata") or {}
    spec = dict(metadata.get("spec") or {})
    # P2-1: revalidate before any phase. A pre-authority 3.8.0 record gets a
    # conservative legacy envelope; a record that advertises authority but has
    # none/invalid/newer authority fails closed.
    try:
        effective_authority, policy_drift = authority.revalidate_run(
            saved, cfg, metadata)
    except authority.AuthorityError as exc:
        raise RuntimeError(
            "Authority revalidation failed for run %s: %s" % (run_id, exc))
    saved["authority"] = effective_authority.to_dict()
    saved["authority_version"] = authority.AUTHORITY_VERSION
    saved["policy_drift"] = policy_drift
    saved["reviewer_override"] = bool(effective_authority.reviewer_override)
    saved["reviewer_policy_source"] = (
        "reviewer_override" if effective_authority.reviewer_override
        else "forced_read_only")
    # Keep the checkpoint copy in step with the revalidated envelope so a later
    # resume starts from the same stricter intersection.
    try:
        cp = checkpoints.Checkpoint(root, run_id)
        current = cp.load() or {}
        current_metadata = dict(current.get("metadata") or {})
        current_metadata["authority"] = effective_authority.to_dict()
        current_metadata["authority_version"] = authority.AUTHORITY_VERSION
        current_metadata["policy_drift"] = policy_drift
        cp.update(metadata=current_metadata)
    except Exception:
        pass
    # Fill gaps in an early skeleton (or a synthetic record) from the spec.
    for key in ("prompt", "workspace", "title", "subagents", "quality_used",
                "independent_review", "quality_rounds", "skill",
                "builder_sandbox", "reviewer_sandbox", "transport", "add_dirs",
                "output_files", "answer"):
        if saved.get(key) in (None, "", []) and spec.get(key) is not None:
            saved[key] = spec.get(key)
    # Saved run records are redacted on write, so a home workspace is stored
    # as ``~/DeepSeek-MCP/harness-workspace``. Expand it before restoring an
    # accepted REVIEW.md or passing the workspace to a resumed phase.
    workspace = os.path.expanduser(
        str(saved.get("workspace") or cfg.harness_workspace))
    prompt = saved.get("prompt") or ""
    if not prompt:
        raise RuntimeError("Saved run %r has no prompt to resume" % run_id)
    prior_rounds, accepted_review = _prior_rounds(manifest)
    saved_review_partial = _resume_review_partial(
        saved, manifest, prior_rounds, workspace, run_id)
    next_phase = None
    for name in manifest.get("plan") or []:
        if ((manifest.get("phases") or {}).get(str(name)) or {}).get("status") != "done":
            next_phase = str(name)
            break

    # P3-2: a convergence stop finalized the run with the repeated defect
    # signature unanswered. ``resume_with_scope`` must reopen one
    # revision/review cycle before the "nothing left to execute" return;
    # otherwise the advertised remedy is a no-op.
    scope_option = str(args.get("option") or "").strip().lower()
    scope_answer = str(args.get("answer") or "").strip()
    last_review_round = max(
        [int(item.get("round") or 0) for item in prior_rounds
         if isinstance(item, dict) and item.get("kind") == "review"] or [0])
    scope_resume = bool(
        scope_option == "resume_with_scope"
        and accepted_review is None
        and last_review_round > 0
        and (saved.get("convergence_attention")
             or saved.get("convergence_stopped")))
    scope_round = last_review_round if scope_resume else None
    scope_note = ""
    if scope_resume:
        scope_note = scope_answer or (
            "Address only the blocking defect(s) repeated in the last review; "
            "keep the rest of the deliverable unchanged and verify the fix.")
    if scope_resume:
        resume_quality_rounds = int(
            saved.get("quality_rounds") or spec.get("quality_rounds") or 1)
        resume_quality_rounds = max(resume_quality_rounds,
                                    last_review_round + 1)
        saved["quality_rounds"] = resume_quality_rounds
    # The convergence question advertises accept_below_gate and stop as
    # terminal answers as well as resume_with_scope.  They must resolve the
    # pending question instead of copying it into the no-op resume and letting
    # _finalize_work hold the work as needs_input forever.
    convergence_pending = bool(saved.get("convergence_attention")
                               or saved.get("convergence_stopped"))
    terminal_option = (
        scope_option if (scope_option in ("accept_below_gate", "stop")
                         and accepted_review is None and convergence_pending)
        else "")
    if not scope_resume and (accepted_review is not None or next_phase is None
                             or terminal_option):
        # Nothing left to execute: the accepted review (or the last phase) is
        # the result. Never launch a DSH process just to re-print it.
        result = dict(saved)
        result["resumed"] = True
        result["resumed_from"] = run_id
        saved_work_id = saved.get("work_id") or spec.get("work_id")
        if saved_work_id and not result.get("work_id"):
            result["work_id"] = saved_work_id
        if result.get("work_id") and result.get("attempt_no") is None:
            try:
                current = _work_store(cfg).load(str(result["work_id"]))
                if isinstance(current, dict):
                    result["attempt_no"] = current.get("attempt_no")
            except Exception:
                pass
        # A durable "accepted" checkpoint is written as soon as the reviewer
        # returns, before run_harness applies the post-review budget gates. A
        # kill in that window must not let resume relabel an over-budget run as
        # a clean "ok": re-evaluate both whole-job bounds from saved state.
        max_cost = (spec.get("job_max_cost_usd")
                    if spec.get("job_max_cost_usd") is not None
                    else saved.get("job_max_cost_usd"))
        max_seconds = (spec.get("job_max_seconds")
                       if spec.get("job_max_seconds") is not None
                       else saved.get("job_max_seconds"))
        elapsed = _saved_elapsed_seconds(saved, manifest, max_seconds)
        spent, cost_known = _resume_spend(saved, prior_rounds)
        overrun = None
        if max_seconds is not None:
            try:
                if float(max_seconds) > 0 and elapsed >= float(max_seconds):
                    overrun = "deadline"
            except (TypeError, ValueError):
                pass
        if overrun is None and max_cost is not None:
            if budget_mod.JobBudget(None, max_cost).cost_exceeded(
                    spent, cost_known):
                overrun = "budget"
        if overrun and result.get("status") in (None, "", "running"):
            result["status"] = ("deadline_exceeded" if overrun == "deadline"
                                else "budget_exceeded")
            result["stop_reason"] = ("deadline" if overrun == "deadline"
                                     else "budget")
            result["status_detail"] = status_mod.STATUS_MEANING[
                result["status"]]
            result["review_status"] = "stopped"
            result["review_passed"] = False
        if accepted_review is not None:
            if result.get("status") not in ("deadline_exceeded",
                                            "budget_exceeded"):
                result["review_status"] = "accepted"
                result["review_passed"] = True
            result["quality_score"] = accepted_review.get("score")
            result["review_rounds"] = prior_rounds
            _restore_review_report(workspace, accepted_review)
            _report_text, report_path = _read_review(workspace)
            if report_path:
                result["review_report"] = report_path
        # A saved skeleton still says "running" because it was written before
        # the first spawn. Once every phase is done (or the review was already
        # accepted) there is nothing left that could change the outcome, so
        # finalize instead of returning a permanently-running record.
        for name in ("answer", "option"):
            if args.get(name) is not None:
                result["attention_" + name] = api.redact_secrets(
                    str(args.get(name)))
        if result.get("status") in (None, "", "running"):
            _resume_final_status(result, prior_rounds, spec=spec,
                                 accepted_review=accepted_review)
        else:
            result.setdefault(
                "status_detail",
                status_mod.STATUS_MEANING.get(result.get("status"), ""))
        if terminal_option:
            attention = result.get("convergence_attention")
            question = ((attention or {}).get("question")
                        if isinstance(attention, dict) else None)
            result["attention_resolution"] = terminal_option
            result["attention_question"] = (
                api.redact_secrets(str(question))[:500] if question else None)
            result["attention_resolved"] = True
            result["accepted_below_gate"] = (
                terminal_option == "accept_below_gate")
            result["convergence_resolution"] = {
                "option": terminal_option,
                "resolved": True,
            }
            result["review_status"] = "stopped"
            result["review_passed"] = False
            # Clearing this is what lets _finalize_work record a terminal
            # result and finish the work item instead of asking again.
            result.pop("convergence_attention", None)
        result["status_vocabulary"] = "3.6"
        try:
            history_runs = load_runs(cfg)
        except Exception:
            history_runs = []
        path = _save_terminal(result, cfg, history_runs, None, cancel_check)
        try:
            checkpoints.Checkpoint(
                Path(cfg.state_dir) / "checkpoints", run_id).finalize({
                    "status": result.get("status"),
                    "status_detail": result.get("status_detail"),
                    "quality_score": result.get("quality_score"),
                    "review_status": result.get("review_status"),
                    "resumed_from": run_id,
                })
        except Exception:
            pass
        text = _result_text(result, path)
        if progress_cb:
            try:
                progress_cb("run %s already finished; nothing to resume" % run_id)
            except Exception:
                pass
        return text, result, path

    builder_phase = (manifest.get("phases") or {}).get("builder") or {}
    builder_data = dict(builder_phase.get("data") or {})
    # P1-1: an unfinished resume is a new attempt of the same work item, never
    # a new work identity.  This happens only after the no-op early return so a
    # completed run does not leave a leaking active lease.
    work_id = saved.get("work_id") or spec.get("work_id")
    resume_attempt = None
    if work_id:
        try:
            work_doc = _work_store(cfg).begin_attempt(
                str(work_id), reason="resume", pid=os.getpid())
            resume_attempt = work_doc.get("attempt_no")
            try:
                manifest_cp = checkpoints.Checkpoint(root, run_id)
                current = manifest_cp.load() or {}
                metadata = dict(current.get("metadata") or {})
                spec_copy = dict(metadata.get("spec") or {})
                spec_copy["work_id"] = str(work_id)
                spec_copy["attempt_no"] = resume_attempt
                metadata["spec"] = spec_copy
                manifest_cp.update(metadata=metadata)
            except Exception:
                pass
        except Exception as exc:
            saved["work_error"] = api.redact_secrets(str(exc))
    saved_baseline_dir = None
    saved_baseline_round = None
    saved_baseline = saved.get("review_baseline")
    if isinstance(saved_baseline, dict) and saved_baseline.get("dir"):
        saved_baseline_dir = str(saved_baseline["dir"])
        saved_baseline_round = saved_baseline.get("round")
    resume_args = {
        "prompt": prompt,
        "workspace": workspace,
        "title": saved.get("title") or "resumed %s" % run_id,
        "subagents": saved.get("subagents"),
        "quality": saved.get("quality_used"),
        "independent_review": saved.get("independent_review"),
        "quality_rounds": saved.get("quality_rounds"),
        "skill": saved.get("skill"),
        "sandbox": effective_authority.sandbox,
        "reviewer_sandbox": authority.reviewer_policy_for(
            effective_authority, cfg),
        "transport": saved.get("transport"),
        "review_preflight": saved.get(
            "review_preflight", spec.get("review_preflight")),
        "review_preflight_timeout": saved.get(
            "review_preflight_timeout", spec.get("review_preflight_timeout")),
        "add_dirs": list(saved.get("add_dirs") or []),
        "job_max_seconds": effective_authority.job_max_seconds,
        "job_max_cost_usd": effective_authority.job_max_cost_usd,
        "_authority_envelope": effective_authority.to_dict(),
        "live_scaling": saved.get("live_scaling", spec.get("live_scaling")),
        "live_scaling_threshold": saved.get("live_scaling_threshold",
                                            spec.get("live_scaling_threshold")),
        "live_scaling_max_extra_waves": saved.get(
            "live_scaling_max_extra_waves", spec.get("live_scaling_max_extra_waves")),
        "_resume_from": run_id,
        "_job_elapsed_seconds": _saved_elapsed_seconds(saved, manifest),
        "_prior_rounds": prior_rounds,
        "_preserve_review": bool(prior_rounds and accepted_review is None),
    }
    if saved_review_partial:
        resume_args["_review_partial"] = saved_review_partial
    if saved_baseline_dir:
        resume_args["_review_baseline_dir"] = saved_baseline_dir
        if saved_baseline_round is not None:
            resume_args["_review_baseline_round"] = saved_baseline_round
    if scope_resume:
        resume_args["quality_rounds"] = int(saved["quality_rounds"])
        resume_args["_scope_resume"] = {
            "round": int(scope_round),
            "note": scope_note,
        }
    if work_id:
        resume_args["_work_id"] = str(work_id)
        resume_args["_attempt_no"] = resume_attempt
        resume_args["_fingerprint"] = (saved.get("request_fingerprint")
                                       or spec.get("request_fingerprint"))
    for name in ("answer", "option"):
        if args.get(name) is not None:
            saved["attention_" + name] = args.get(name)
            resume_args["attention_" + name] = args.get(name)
    if builder_phase.get("status") == "done":
        resume_args["_prior_builder"] = {
            "status": "ok",
            "answer": saved.get("answer"),
            "seconds": builder_data.get("seconds"),
            "output_files": builder_data.get("output_files")
            or saved.get("output_files") or [],
            "session_logs": [],
            "estimated_cost_usd": saved.get("estimated_cost_usd"),
            "tokens_in": saved.get("tokens_in"),
            "cache_read_tokens": saved.get("cache_read_tokens"),
            "tokens_out": saved.get("tokens_out"),
            "transport": builder_data.get("transport") or "headless",
            "transport_session_id": builder_data.get("session_id"),
            "sandbox": saved.get("builder_sandbox"),
        }
    else:
        session_id = builder_data.get("session_id")
        if session_id:
            resume_args["resume_session_id"] = session_id
        resume_args["prompt"] = (
            prompt
            + "\n\n[CONNECTOR RESUME] A previous attempt was interrupted. "
              "Continue the same task from the existing workspace state; do not "
              "redo completed work. Finish the deliverables and report.")
    if progress_cb:
        try:
            progress_cb("resuming run %s from checkpoint (builder=%s, next=%s, "
                        "reused=%d phase(s))"
                        % (run_id, builder_phase.get("status") or "unknown",
                           next_phase, len(prior_rounds)))
        except Exception:
            pass
    return run_harness(resume_args, cfg, progress_cb=progress_cb,
                       cancel_check=cancel_check)
