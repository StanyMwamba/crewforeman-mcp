"""Bounded, decaying reviewer-defect notes per task type (F3, 3.11.0).

The store contains only fixed defect bucket labels (or opaque ``sig:<hash>``
labels), opaque run ids and timestamps.  Reviewer prose, prompts, code, file
contents and paths never enter it.  Every public entry point swallows store
errors: a broken notes file must never change the outcome of a run.
"""

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import quality
from . import store

# Locking is delegated to ``platforms.file_lock`` at call time.  This module
# must never import ``fcntl``/``msvcrt`` at import time: the other module is
# absent on the current platform.
from . import platforms

try:
    from .api import redact_secrets
except Exception:  # pragma: no cover - api is available in supported builds
    def redact_secrets(text):
        return str(text or "")

VERSION = 2
TASK_TYPES = ("simple", "code", "creative", "research", "unknown")
_ALIASES = {
    "document": "creative",
    "creative-design": "creative",
    "creative_design": "creative",
}
DEFAULTS = {
    "failure_notes": "on",
    "failure_notes_max": 3,
    "failure_notes_half_life_days": 30,
    "failure_notes_min_occurrences": 2,
    "failure_notes_max_store": 200,
    "failure_notes_store": "",
    "failure_notes_models": "on",
    "failure_notes_max_models": 6,
    "failure_notes_model_min_runs": 2,
}
_SEEN_CAP = 512
_MIN_WEIGHT = 0.05
_NOTE_LIMIT = 400
_HALF_LIFE_MIN = 0.1
_LOCK_TIMEOUT = 2.0
_BUCKET_FALLBACK = (
    "overlap",
    "missing required fact",
    "qr code",
    "overflow or clipping",
    "type size or contrast",
    "crash",
    "empty deliverable",
    "failing test or build",
    "broken artifact",
)
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b")
_ABS_PATH_RE = re.compile(r"^(~|[a-z]:[\\/]|/|\\\\)", re.IGNORECASE)
_SIG_RE = re.compile(r"^sig:[0-9a-f]{12}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
_MODEL_GLOBAL_CAP = 48


def _bucket_labels():
    buckets = getattr(quality, "_DEFECT_BUCKETS", None)
    if not buckets:
        return set(_BUCKET_FALLBACK)
    labels = set()
    for _terms, label in buckets:
        if label:
            labels.add(str(label).strip().lower())
    return labels or set(_BUCKET_FALLBACK)


_BUCKETS = _bucket_labels()


def _empty():
    return {"version": VERSION, "task_types": {}, "model_stats": {},
            "seen": {}, "model_seen": {}, "updated": ""}


def _cfg(cfg, name, default):
    """Read a config value defensively (Config object or plain mapping)."""
    try:
        getter = getattr(cfg, "get", None)
        if callable(getter):
            value = getter(name, default)
            return default if value is None else value
        value = getattr(cfg, name, default)
        return default if value is None else value
    except Exception:
        return default


def _enabled(cfg):
    value = _cfg(cfg, "failure_notes", "on")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("off", "0", "false", "no", "disabled")


def _int_cfg(cfg, name, default, minimum=0):
    try:
        return max(minimum, int(_cfg(cfg, name, default)))
    except (TypeError, ValueError):
        return default


def _models_enabled(cfg):
    value = _cfg(cfg, "failure_notes_models", "on")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("off", "0", "false", "no", "disabled")


def _float_cfg(cfg, name, default, minimum):
    try:
        return max(minimum, float(_cfg(cfg, name, default)))
    except (TypeError, ValueError):
        return default


def _half_life(cfg):
    return _float_cfg(cfg, "failure_notes_half_life_days", 30.0, _HALF_LIFE_MIN)


def _state_dir(cfg):
    value = _cfg(cfg, "state_dir", ".")
    try:
        return Path(os.path.expanduser(str(value)))
    except Exception:
        return Path(".")


def _store_path(cfg):
    override = _cfg(cfg, "failure_notes_store", "")
    if override:
        path = Path(os.path.expanduser(str(override)))
        if not path.is_absolute():
            path = _state_dir(cfg) / path
        return path
    return _state_dir(cfg) / "failure_notes.json"


def _lock_path(path):
    name = path.name
    if name.endswith(".json"):
        return path.with_name(name[:-5] + ".lock")
    return path.with_name(name + ".lock")


def _now(value=None):
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return dt.datetime.now(dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso(value):
    return _now(value).isoformat()


def _parse_time(value):
    try:
        if isinstance(value, dt.datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _task_type(value):
    text = str(value or "").strip().lower().replace("_", "-")
    if text in ("simple", "code", "creative", "research", "unknown"):
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    return "unknown"


def _redacted(text):
    try:
        return redact_secrets(str(text or ""))
    except Exception:
        return str(text or "")


def _normalise_label(raw):
    """Map a raw label to a storable label, or ``None`` when unsafe."""
    text = _redacted(raw).strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    if len(text) > 64:
        return None
    lower = text.lower()
    if lower in _BUCKETS:
        return lower
    if _redacted(str(raw or "")) != str(raw or ""):
        return None
    if len(lower.split()) > 6:
        return None
    if "@" in lower or "/" in lower or "\\" in lower:
        return None
    if _URL_RE.search(lower) or "www." in lower or _EMAIL_RE.search(lower):
        return None
    if _ABS_PATH_RE.match(lower):
        return None
    if re.search(r"[\x00-\x1f\x7f]", lower):
        return None
    return "sig:" + hashlib.sha256(lower.encode("utf-8")).hexdigest()[:12]


def _model_label(raw):
    """Sanitize a provider/model label; return ``None`` when unsafe."""
    try:
        text = _redacted(str(raw or "")).strip()
    except Exception:
        return None
    if not text or len(text) > 80:
        return None
    lower = text.lower()
    if ("://" in lower or lower.startswith("www.") or "@" in text
            or "\\" in text or ".." in text or "\x00" in text):
        return None
    if re.search(r"[\x00-\x1f\x7f\s]", text):
        return None
    if _URL_RE.search(lower) or _EMAIL_RE.search(lower):
        return None
    if _ABS_PATH_RE.match(text) or text.startswith("/") or text.startswith("~"):
        return None
    if text.count("/") > 1 or not _MODEL_RE.match(text):
        return None
    return text


def _coerce_model_entry(entry):
    if not isinstance(entry, dict):
        return None
    try:
        runs = max(0, int(entry.get("runs") or 0))
        weight = float(entry.get("weight") or 0.0)
        scored_runs = max(0, int(entry.get("scored_runs") or 0))
        score_sum = float(entry.get("score_sum") or 0.0)
        blocking = max(0, int(entry.get("blocking") or 0))
        cost_sum = float(entry.get("cost_sum") or 0.0)
        cost_known_runs = max(0, int(entry.get("cost_known_runs") or 0))
    except (TypeError, ValueError):
        return None
    values = (weight, score_sum, cost_sum)
    if any(value < 0 or value != value for value in values):
        return None
    last_seen = _parse_time(entry.get("last_seen"))
    first_seen = _parse_time(entry.get("first_seen")) or last_seen
    if last_seen is None:
        return None
    return {
        "runs": runs, "weight": weight, "scored_runs": scored_runs,
        "score_sum": score_sum, "blocking": blocking,
        "cost_sum": cost_sum, "cost_known_runs": cost_known_runs,
        "first_seen": _iso(first_seen or last_seen),
        "last_seen": _iso(last_seen),
    }


def _prune_models(data, cfg, now):
    """Decay model rows, drop weight < .05, cap per type and globally."""
    half_life = _half_life(cfg)
    per_type = _int_cfg(cfg, "failure_notes_max_models", 6)
    kept = []
    for task_type, models in (data.get("model_stats") or {}).items():
        if not isinstance(models, dict):
            continue
        for label, entry in models.items():
            clean = _stored_model_key(label)
            coerced = _coerce_model_entry(entry)
            if clean is None or coerced is None:
                continue
            decayed = _decay_entry(coerced, now, half_life)
            if decayed.get("weight", 0.0) >= _MIN_WEIGHT:
                kept.append((task_type, clean, decayed))
    kept.sort(key=lambda row: (-float(row[2].get("weight") or 0.0),
                               -int(row[2].get("runs") or 0),
                               -( _parse_time(row[2].get("last_seen"))
                                  or dt.datetime.min.replace(
                                      tzinfo=dt.timezone.utc)).timestamp(),
                               row[0], row[1]))
    clean_types = {}
    for task_type, label, entry in kept[:_MODEL_GLOBAL_CAP]:
        bucket = clean_types.setdefault(task_type, {})
        if len(bucket) < per_type:
            bucket[label] = entry
    data["model_stats"] = clean_types
    return data


def _stored_model_key(value):
    text = str(value or "").strip()
    return text if _MODEL_RE.match(text) and _model_label(text) == text else None


def _stored_key(value):
    text = str(value or "").strip().lower()
    if text in _BUCKETS:
        return text
    if _SIG_RE.match(text):
        return text
    return None


def _opaque_id(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if _redacted(text) != text:
        text = "sig:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    if "/" in text or "\\" in text or "@" in text or len(text) > 64:
        text = "sig:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return text


def _token(run_id, round_no):
    raw = "%s:%s" % ("" if run_id is None else run_id,
                     "" if round_no is None else round_no)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _decay_entry(entry, now, half_life):
    try:
        weight = float(entry.get("weight") or 0.0)
    except (TypeError, ValueError):
        weight = 0.0
    last = _parse_time(entry.get("last_seen")) or _now(now)
    if last > now:
        last = now
    age_days = max(0.0, (now - last).total_seconds() / 86400.0)
    factor = 0.5 ** (age_days / max(_HALF_LIFE_MIN, float(half_life)))
    factor = min(1.0, max(0.0, factor))
    result = dict(entry)
    result["weight"] = max(0.0, weight * factor)
    result["last_seen"] = _iso(now)
    return result


def _coerce_entry(key, entry):
    kind = _stored_key(key)
    if not kind or not isinstance(entry, dict):
        return None
    try:
        count = max(0, int(entry.get("count") or 0))
        weight = float(entry.get("weight") or 0.0)
    except (TypeError, ValueError):
        return None
    if weight < 0 or weight != weight:  # NaN guard
        return None
    last_seen = _parse_time(entry.get("last_seen"))
    first_seen = _parse_time(entry.get("first_seen")) or last_seen
    if last_seen is None:
        return None
    samples = []
    for run_id in entry.get("sample_run_ids") or []:
        opaque = _opaque_id(run_id)
        if opaque and opaque not in samples:
            samples.append(opaque)
    return {
        "count": count,
        "weight": weight,
        "first_seen": _iso(first_seen or last_seen),
        "last_seen": _iso(last_seen),
        "sample_run_ids": samples[-3:],
    }


def _coerce_store(data):
    if not isinstance(data, dict) or data.get("version") not in (1, VERSION):
        raise ValueError("bad failure notes schema")
    task_types = data.get("task_types")
    seen = data.get("seen")
    if not isinstance(task_types, dict) or not isinstance(seen, dict):
        raise ValueError("bad failure notes schema")
    is_v1 = data.get("version") == 1
    clean_types = {}
    for task_type, kinds in task_types.items():
        key = _task_type(task_type)
        if key == "unknown" and str(task_type).strip().lower() != "unknown":
            continue
        if not isinstance(kinds, dict):
            continue
        bucket = clean_types.setdefault(key, {})
        for kind, entry in kinds.items():
            coerced = _coerce_entry(kind, entry)
            if coerced is not None:
                bucket[_stored_key(kind)] = coerced
        if not bucket:
            clean_types.pop(key, None)
    clean_seen = {}
    for token, stamp in seen.items():
        text = str(token or "")
        when = _parse_time(stamp)
        if _TOKEN_RE.match(text) and when is not None:
            clean_seen[text] = _iso(when)
    clean_models = {}
    clean_model_seen = {}
    if not is_v1:
        model_stats = data.get("model_stats")
        if isinstance(model_stats, dict):
            for task_type, models in model_stats.items():
                key = _task_type(task_type)
                if key == "unknown" and str(task_type).strip().lower() != "unknown":
                    continue
                if not isinstance(models, dict):
                    continue
                bucket = clean_models.setdefault(key, {})
                for label, entry in models.items():
                    clean = _stored_model_key(label)
                    coerced = _coerce_model_entry(entry)
                    if clean is not None and coerced is not None:
                        bucket[clean] = coerced
                if not bucket:
                    clean_models.pop(key, None)
        raw_model_seen = data.get("model_seen")
        if isinstance(raw_model_seen, dict):
            for token, stamp in raw_model_seen.items():
                text = str(token or "")
                when = _parse_time(stamp)
                if _TOKEN_RE.match(text) and when is not None:
                    clean_model_seen[text] = _iso(when)
    return {
        "version": VERSION,
        "task_types": clean_types,
        "model_stats": clean_models,
        "seen": clean_seen,
        "model_seen": clean_model_seen,
        "updated": str(data.get("updated") or ""),
    }


def _quarantine(path):
    target = path.with_name(path.name + ".corrupt")
    try:
        os.replace(str(path), str(target))
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def _read(path):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError:
        return _empty()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        _quarantine(path)
        return _empty()
    try:
        return _coerce_store(data)
    except (TypeError, ValueError):
        _quarantine(path)
        return _empty()


def _write(path, data):
    text = json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    store._atomic_write(Path(path), text)


class _LockTimeout(Exception):
    pass


class _Lock(object):
    """Exclusive store lock with a bounded timeout (``platforms.file_lock``).

    The public context-manager API and ``_LockTimeout`` behaviour are kept;
    only the OS-specific locking primitive moved into ``platforms``.
    """

    def __init__(self, path, timeout=_LOCK_TIMEOUT):
        self.path = Path(path)
        self.timeout = timeout
        self._lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock = platforms.file_lock(str(self.path),
                                             timeout=self.timeout)
            self._lock.__enter__()
            return self
        except TimeoutError:
            self._lock = None
            raise _LockTimeout()
        except Exception:
            # A store error must never change the outcome of a run.
            self._lock = None
            raise _LockTimeout()

    def __exit__(self, exc_type, exc, tb):
        lock, self._lock = self._lock, None
        if lock is None:
            return False
        try:
            return bool(lock.__exit__(exc_type, exc, tb))
        except Exception:
            return False


def _sort_items(items):
    ranked = []
    for task_type, kind, entry in items:
        when = _parse_time(entry.get("last_seen")) or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc)
        ranked.append((float(entry.get("weight") or 0.0),
                       int(entry.get("count") or 0), when.timestamp(),
                       task_type, kind, entry))
    ranked.sort(key=lambda row: (-row[0], -row[1], -row[2],
                                 row[3], row[4]))
    return ranked


def _prune_nested(data, cfg, now):
    half_life = _half_life(cfg)
    max_store = _int_cfg(cfg, "failure_notes_max_store", 200)
    kept = []
    for task_type, kinds in (data.get("task_types") or {}).items():
        for kind, entry in kinds.items():
            decayed = _decay_entry(entry, now, half_life)
            if decayed.get("weight", 0.0) >= _MIN_WEIGHT:
                kept.append((task_type, kind, decayed))
    ranked = _sort_items(kept)[:max_store]
    clean = {}
    for _weight, _count, _when, task_type, kind, entry in ranked:
        clean.setdefault(task_type, {})[kind] = entry
    data["task_types"] = clean
    _prune_models(data, cfg, now)
    data["updated"] = _iso(now)
    return data


def _trim_seen(seen, token, stamp):
    seen[token] = _iso(stamp)
    ordered = sorted(
        seen.items(),
        key=lambda item: (_parse_time(item[1]) or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc)).timestamp(),
        reverse=True,
    )
    return dict(ordered[:_SEEN_CAP])


def load(cfg):
    """Return the stored notes mapping; never raises and never creates a file."""
    try:
        if not _enabled(cfg):
            return _empty()
        path = _store_path(cfg)
        if not path.exists():
            return _empty()
        return _read(path)
    except Exception:
        return _empty()


def record(task_type, kinds, run_id, round_no, cfg, now=None):
    """Record one completed review round idempotently by (run_id, round_no)."""
    result = {"written": False, "recorded": 0}
    try:
        if not _enabled(cfg):
            return result
        tt = _task_type(task_type)
        labels = []
        raw_kinds = [kinds] if isinstance(kinds, str) else (kinds or [])
        for raw in raw_kinds:
            label = _normalise_label(raw)
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return result
        when = _now(now)
        path = _store_path(cfg)
        token = _token(run_id, round_no)
        try:
            with _Lock(_lock_path(path)):
                data = _read(path)
                if token in data.get("seen", {}):
                    result["duplicate"] = True
                    return result
                half_life = _half_life(cfg)
                bucket = data["task_types"].setdefault(tt, {})
                for label in labels:
                    entry = bucket.get(label)
                    if entry is None:
                        entry = {"count": 0, "weight": 0.0,
                                 "first_seen": _iso(when),
                                 "last_seen": _iso(when),
                                 "sample_run_ids": []}
                    decayed = _decay_entry(entry, when, half_life)
                    decayed["count"] = int(decayed.get("count") or 0) + 1
                    decayed["weight"] = float(decayed.get("weight") or 0.0) + 1.0
                    samples = list(decayed.get("sample_run_ids") or [])
                    opaque = _opaque_id(run_id)
                    if opaque and opaque not in samples:
                        samples.append(opaque)
                    decayed["sample_run_ids"] = samples[-3:]
                    decayed["first_seen"] = entry.get("first_seen") or _iso(when)
                    bucket[label] = decayed
                _prune_nested(data, cfg, when)
                data["seen"] = _trim_seen(data.get("seen", {}), token, when)
                # Defect-only stores keep the 3.11 version so old readers can
                # still consume them; model learning upgrades the store to v2.
                if not data.get("model_stats") and not data.get("model_seen"):
                    data["version"] = 1
                _write(path, data)
                result["written"] = True
                result["recorded"] = len(labels)
                return result
        except _LockTimeout:
            result["skipped"] = "lock_timeout"
            return result
    except Exception:
        result["written"] = False
        return result


def record_model_run(task_type, model, utility, score, cost, cost_known,
                     run_id, cfg, now=None):
    """Record one model outcome idempotently by ``(run_id, "model")``."""
    result = {"written": False, "recorded": 0}
    try:
        if not (_enabled(cfg) and _models_enabled(cfg)):
            return result
        label = _model_label(model)
        if label is None:
            return result
        tt = _task_type(task_type)
        if tt == "unknown" and str(task_type).strip().lower() != "unknown":
            return result
        when = _now(now)
        path = _store_path(cfg)
        token = _token(run_id, "model")
        try:
            with _Lock(_lock_path(path)):
                data = _read(path)
                if token in data.get("model_seen", {}):
                    result["duplicate"] = True
                    return result
                half_life = _half_life(cfg)
                bucket = data["model_stats"].setdefault(tt, {})
                entry = bucket.get(label)
                if entry is None:
                    entry = {"runs": 0, "weight": 0.0, "scored_runs": 0,
                             "score_sum": 0.0, "blocking": 0, "cost_sum": 0.0,
                             "cost_known_runs": 0, "first_seen": _iso(when),
                             "last_seen": _iso(when)}
                decayed = _decay_entry(entry, when, half_life)
                decayed["runs"] = int(decayed.get("runs") or 0) + 1
                decayed["weight"] = float(decayed.get("weight") or 0.0) + 1.0
                if score is not None:
                    try:
                        decayed["scored_runs"] = int(
                            decayed.get("scored_runs") or 0) + 1
                        decayed["score_sum"] = (float(
                            decayed.get("score_sum") or 0.0) + float(score))
                    except (TypeError, ValueError):
                        pass
                if cost_known and cost is not None:
                    try:
                        decayed["cost_sum"] = (float(
                            decayed.get("cost_sum") or 0.0) + float(cost))
                        decayed["cost_known_runs"] = int(
                            decayed.get("cost_known_runs") or 0) + 1
                    except (TypeError, ValueError):
                        pass
                try:
                    target = float(getattr(quality, "DEFAULT_QUALITY_TARGET", 8.0))
                except (TypeError, ValueError):
                    target = 8.0
                if utility is not None:
                    try:
                        if float(utility) < target:
                            decayed["blocking"] = int(
                                decayed.get("blocking") or 0) + 1
                    except (TypeError, ValueError):
                        pass
                decayed["first_seen"] = entry.get("first_seen") or _iso(when)
                bucket[label] = decayed
                _prune_nested(data, cfg, when)
                data["model_seen"] = _trim_seen(
                    data.get("model_seen", {}), token, when)
                _write(path, data)
                result["written"] = True
                result["recorded"] = 1
                result["model"] = label
                return result
        except _LockTimeout:
            result["skipped"] = "lock_timeout"
            return result
    except Exception:
        result["written"] = False
        return result


def decay(entries, now, half_life_days):
    """Decay ``{kind: entry}`` weights to ``now``; factor is clamped to <= 1."""
    try:
        if not isinstance(entries, dict):
            return {}
        when = _now(now)
        try:
            half_life = max(_HALF_LIFE_MIN, float(half_life_days))
        except (TypeError, ValueError):
            half_life = 30.0
        output = {}
        for kind, entry in entries.items():
            if isinstance(entry, dict):
                output[kind] = _decay_entry(entry, when, half_life)
        return output
    except Exception:
        return {}


def prune(entries, cfg, now):
    """Drop entries with W < 0.05 and enforce the global max-store cap."""
    try:
        if not isinstance(entries, dict):
            return {}
        when = _now(now)
        if "task_types" in entries and isinstance(entries.get("task_types"), dict):
            data = {
                "version": VERSION,
                "task_types": {key: {kind: dict(value) for kind, value in kinds.items()}
                               for key, kinds in entries["task_types"].items()
                               if isinstance(kinds, dict)},
                "seen": dict(entries.get("seen") or {}),
                "updated": str(entries.get("updated") or ""),
            }
            return _prune_nested(data, cfg, when)
        half_life = _half_life(cfg)
        max_store = _int_cfg(cfg, "failure_notes_max_store", 200)
        decayed = decay(entries, when, half_life)
        items = [("", kind, entry) for kind, entry in decayed.items()
                 if entry.get("weight", 0.0) >= _MIN_WEIGHT]
        ranked = _sort_items(items)[:max_store]
        return {kind: entry for _w, _c, _t, _tt, kind, entry in ranked}
    except Exception:
        return {}


def _injectable(tt, cfg, now):
    data = load(cfg)
    if not data:
        return data, []
    half_life = _half_life(cfg)
    min_occurrences = _int_cfg(cfg, "failure_notes_min_occurrences", 2, 1)
    max_bullets = _int_cfg(cfg, "failure_notes_max", 3)
    bucket = (data.get("task_types") or {}).get(tt) or {}
    candidates = []
    for kind, entry in bucket.items():
        if kind not in _BUCKETS:
            continue
        decayed = _decay_entry(entry, now, half_life)
        count = int(decayed.get("count") or 0)
        weight = float(decayed.get("weight") or 0.0)
        if count >= min_occurrences and weight >= _MIN_WEIGHT:
            candidates.append((weight, count, _parse_time(
                decayed.get("last_seen")) or now, kind))
    candidates.sort(key=lambda row: (-row[0], -row[1],
                                     -row[2].timestamp(), row[3]))
    return data, candidates[:max_bullets]


def _store_counts(data, tt):
    total = 0
    observations = 0
    task_count = 0
    for task_type, kinds in (data.get("task_types") or {}).items():
        total += len(kinds)
        observations += sum(int(entry.get("count") or 0)
                            for entry in kinds.values())
        if task_type == tt:
            task_count = len(kinds)
    return {"total": total, "task_type": task_count,
            "observations": observations}


def _model_line(tt, cfg, data, now):
    """Return the single bounded model sentence, or "" when none applies."""
    try:
        if not _models_enabled(cfg):
            return ""
        min_runs = _int_cfg(cfg, "failure_notes_model_min_runs", 2, 1)
        max_models = _int_cfg(cfg, "failure_notes_max_models", 6)
        bucket = (data.get("model_stats") or {}).get(tt) or {}
        rows = []
        for label, entry in bucket.items():
            if _stored_model_key(label) is None:
                continue
            runs = int(entry.get("runs") or 0)
            scored = int(entry.get("scored_runs") or 0)
            if runs < min_runs or scored <= 0:
                continue
            mean_score = float(entry.get("score_sum") or 0.0) / scored
            known = int(entry.get("cost_known_runs") or 0)
            mean_cost = (float(entry.get("cost_sum") or 0.0) / known
                         if known else None)
            rows.append((mean_score, runs, label, mean_cost))
        if not rows or max_models <= 0:
            return ""
        rows.sort(key=lambda row: (-row[0], -row[1], row[2]))
        mean_score, runs, label, mean_cost = rows[0]
        utility = mean_score
        cost_text = ("$%.4f" % mean_cost if mean_cost is not None else "n/a")
        return ("Model: %s mean utility %.2f, mean cost %s (%d runs)."
                % (label, utility, cost_text, runs))
    except Exception:
        return ""


def _model_meta_rows(tt, cfg, data, now):
    try:
        max_models = _int_cfg(cfg, "failure_notes_max_models", 6)
        bucket = (data.get("model_stats") or {}).get(tt) or {}
        half_life = _half_life(cfg)
        rows = []
        for label, entry in bucket.items():
            if _stored_model_key(label) is None:
                continue
            decayed = _decay_entry(entry, now, half_life)
            runs = int(decayed.get("runs") or 0)
            scored = int(decayed.get("scored_runs") or 0)
            known = int(decayed.get("cost_known_runs") or 0)
            rows.append({
                "model": label,
                "runs": runs,
                "mean_utility": (round(float(decayed.get("score_sum") or 0.0)
                                       / scored, 2) if scored else None),
                "mean_score": (round(float(decayed.get("score_sum") or 0.0)
                                     / scored, 2) if scored else None),
                "mean_cost": (round(float(decayed.get("cost_sum") or 0.0)
                                    / known, 4) if known else None),
                "cost_known_runs": known,
            })
        rows.sort(key=lambda row: (-(row["mean_utility"] or -1.0),
                                   row["model"]))
        return rows[:max(0, max_models)]
    except Exception:
        return []


def _build_note(tt, cfg, now):
    data, candidates = _injectable(tt, cfg, now)
    max_bullets = _int_cfg(cfg, "failure_notes_max", 3)
    injected = []
    bullets = []
    for weight, count, _last, kind in candidates[:max_bullets]:
        bullets.append("- %s (seen %dx, weight %.2f)" % (kind, count, weight))
        injected.append({"kind": kind, "weight": round(weight, 2),
                         "runs": count})
    header = "Common failures for this task type (%s)" % tt
    subtitle = ("Recent reviewer blocking defects. "
                "Pre-flight these before returning:")
    text = header + "\n" + subtitle
    if len(text) >= _NOTE_LIMIT:
        text = text[:_NOTE_LIMIT]
        return text, [], data
    for bullet in bullets:
        candidate = text + "\n" + bullet
        if len(candidate) > _NOTE_LIMIT:
            break
        text = candidate
    if injected:
        line = _model_line(tt, cfg, data, now)
        if line:
            candidate = text + "\n" + line
            if len(candidate) <= _NOTE_LIMIT:
                text = candidate
    return text, injected, data


def note_for(task_type, cfg, now=None):
    """Return a <=400-char advisory note, or "" when none applies."""
    try:
        if not _enabled(cfg):
            return ""
        tt = _task_type(task_type)
        if tt == "unknown":
            return ""
        when = _now(now)
        text, injected, _data = _build_note(tt, cfg, when)
        return text if injected else ""
    except Exception:
        return ""


def note_meta(task_type, cfg, now=None):
    """Return run-record metadata (never the note body) for the caller."""
    tt = _task_type(task_type)
    try:
        when = _now(now)
        text, injected, data = _build_note(tt, cfg, when) if (
            _enabled(cfg) and tt != "unknown") else (_empty(), [], _empty())
        return {
            "task_type": tt,
            "injected": injected,
            "note_chars": len(text) if injected else 0,
            "store_counts": _store_counts(data, tt),
            "models": _model_meta_rows(tt, cfg, data, when),
            "updated": _iso(when),
        }
    except Exception:
        return {"task_type": tt, "injected": [], "note_chars": 0,
                "store_counts": {"total": 0, "task_type": 0,
                                 "observations": 0},
                "models": [], "updated": ""}


def _round_number(entry, index):
    for key in ("round_no", "round"):
        if key in entry:
            value = entry.get(key)
            if value is not None:
                return value
    return index + 1


def record_review_run(run, cfg, now=None):
    """Record blocking review rounds from a run, idempotently, incl. sweep."""
    summary = {"rounds": 0, "written": False, "recorded": 0}
    try:
        if not isinstance(run, dict):
            return summary
        task_type = _task_type(run.get("task_type"))
        run_id = run.get("run_id") or run.get("id") or ""
        for index, entry in enumerate(run.get("review_rounds") or []):
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "review":
                continue
            try:
                blocking = int(entry.get("blocking_defects") or 0)
            except (TypeError, ValueError):
                blocking = 0
            if blocking <= 0:
                continue
            kinds = []
            defects = entry.get("defects") or []
            if isinstance(defects, str):
                defects = [defects]
            for defect in defects:
                text = str(defect or "").strip()
                if not text:
                    continue
                try:
                    kind = quality.defect_kind(text)
                except Exception:
                    kind = "unspecified"
                if kind and kind not in kinds:
                    kinds.append(kind)
            if not kinds:
                continue
            result = record(task_type, kinds, run_id,
                            _round_number(entry, index), cfg, now=now)
            summary["rounds"] += 1
            summary["written"] = summary["written"] or bool(
                result.get("written"))
            summary["recorded"] += int(result.get("recorded") or 0)
    except Exception:
        pass
    return summary


def terminal_sweep(run, cfg, now=None):
    """Terminal alias for the idempotent review-round sweep."""
    return record_review_run(run, cfg, now=now)


def store_counts(cfg):
    """Bounded store counters for the run record (never includes labels)."""
    try:
        data = load(cfg)
        task_types = data.get("task_types") or {}
        total = 0
        observations = 0
        for entries in task_types.values():
            if not isinstance(entries, dict):
                continue
            total += len(entries)
            for item in entries.values():
                if isinstance(item, dict):
                    try:
                        observations += int(item.get("count") or 0)
                    except (TypeError, ValueError):
                        pass
        return {"total": total, "observations": observations,
                "task_types": len(task_types)}
    except Exception:
        return {"total": 0, "observations": 0, "task_types": 0}


def reset(cfg):
    """Remove the notes store (disabled configs create/modify nothing)."""
    result = {"reset": False}
    try:
        if not _enabled(cfg):
            return result
        path = _store_path(cfg)
        if not path.exists():
            return result
        with _Lock(_lock_path(path)):
            try:
                path.unlink()
                result["reset"] = True
            except FileNotFoundError:
                pass
        return result
    except Exception:
        return result
