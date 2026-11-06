"""Automatic skill capture, clustering, building and lifecycle decisions.

This module implements PLAN sections 5-7, 9 and 10.2 (contract section B).
It is deliberately defensive: every public entry point catches its own
exceptions and returns a structured empty/error value, because it runs inside
the Harness terminal funnel and must never fail a run.

``SkillRegistry.note_run`` may return either a bare stats dict or
``{"stats": <dict>, "recommendation": "promote|demote|hold", "reason": str}``.
Both shapes are accepted here; the promotion decision itself is recomputed from
the persisted entry so a missing recommendation cannot hide a due change.

Only fixed-taxonomy values, lowercase tokens, counts, scores and run ids are
stored.  Raw prompts, titles, workspaces, filenames, e-mails, tokens and home
paths are never written to the capture, cluster, decision or registry files.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_KEYWORDS = 12
MAX_CAPTURE_RECORDS = 500
MAX_CAPTURE_BYTES = 256 * 1024
MAX_RECORD_BYTES = 2048
MAX_CLUSTERS = 50
MAX_MEMBERS = 20
MAX_CENTROID_KEYWORDS = 16
MAX_DECISIONS = 1000

TASK_TYPES = ("code", "research", "creative", "document")
CANONICAL_TASK_TYPES = frozenset(TASK_TYPES)

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren't as at be
because been before being below between both but by can cannot could did do
does doing don't down during each few for from further had has have having he
her here hers herself him himself his how i if in into is it its itself just
let me more most must my myself no nor not now of off on once only or other
ought our ours ourselves out over own please same she should so some such than
that the their theirs them themselves then there these they this those through
to too under until up very was we were what when where which while who whom why
will with would you your yours yourself yourselves using make need want task
file files work works help like well good best first last next per via etc eg
ie get got also may might must shall
""".split())

COMMON_WORDS = frozenset("""
the a an and or but for with from this that these those when where which while
what who whom whose how why all any both each few more most other some such
than then there here now also just only very can will would should could may
might must not into over under about after before during between through above
below again further once our your their its his her my we you they he she it i
me us them him one two three new use used using get got let make made need
needs want wants task tasks file files work works help like well good best
first last next per via etc eg ie
""".split())

TASK_VERBS = frozenset("""
build create design write draft make produce generate prepare plan analyze
analyse research fix improve convert refactor test review edit rewrite render
publish send calculate visualize visualise compare audit implement develop
""".split())

TECH_ALLOWLIST = frozenset("""
python javascript html css pdf csv json api cli qr seo ui ux sql aws gcp macos
linux windows deepseek harness mcp poster document web code data
""".split())

SEVERE_KINDS = frozenset({
    "missing-deliverable", "data-loss", "security", "privacy",
    "unsafe-instruction", "broken-build",
})

_NAME_ALLOW = COMMON_WORDS | TASK_VERBS | TECH_ALLOWLIST | CANONICAL_TASK_TYPES

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{1,23}")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://\S+|www\.\S+)", re.IGNORECASE)
_USERS_RE = re.compile(r"(?:/Users|/home)/[^\s/]+")
_WIN_USERS_RE = re.compile(r"[A-Za-z]:\\Users\\[^\s\\]+")
_PATH_RE = re.compile(r"(?:(?:\.{0,2}/)?(?:[\w.-]+/){2,}[\w.-]+)")
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|xoxb-[A-Za-z0-9-]{8,}"
    r"|AKIA[A-Z0-9]{12,}|eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2})")
_PEM_RE = re.compile(r"-----BEGIN[^-]{0,40}-----.*?-----END[^-]{0,40}-----",
                     re.DOTALL)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b")
_PHONE_RE = re.compile(r"\+?\d[\d ()-]{7,}\d")
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
_NAME_RUN_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_CAP_WORD_RE = re.compile(r"\b[A-Z][a-zA-Z]+\b")

_SUFFIX_KINDS = {
    "image": ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "tiff"),
    "pdf": ("pdf",),
    "document": ("doc", "docx", "odt", "rtf", "txt", "md"),
    "slides": ("ppt", "pptx", "odp", "key"),
    "spreadsheet": ("xls", "xlsx", "ods"),
    "data": ("csv", "tsv", "json", "parquet"),
    "code": ("py", "js", "ts", "html", "css", "sh", "sql", "rs", "go", "java"),
    "archive": ("zip", "tar", "gz"),
    "audio": ("mp3", "wav", "ogg", "flac", "m4a"),
    "video": ("mp4", "mov", "webm", "avi", "mkv"),
}
_SUFFIX_TO_KIND = {}
for _kind, _suffixes in _SUFFIX_KINDS.items():
    for _suffix in _suffixes:
        _SUFFIX_TO_KIND[_suffix] = _kind

_MISSING = object()


def _now():
    return datetime.now(timezone.utc)


def now_iso():
    """Current UTC time as an ISO-8601 ``Z`` string."""
    try:
        return _now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def parse_iso(value):
    """Parse an ISO timestamp into an aware UTC datetime, or return None."""
    if isinstance(value, datetime):
        dt = value
    else:
        if not value:
            return None
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            try:
                dt = datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cfg_get(cfg, name, default):
    if cfg is not None:
        try:
            value = getattr(cfg, name, _MISSING)
        except Exception:
            value = _MISSING
        if value is not _MISSING and value is not None:
            return value
        try:
            getter = getattr(cfg, "get", None)
            if callable(getter):
                value = getter(name, _MISSING)
                if value is not _MISSING and value is not None:
                    return value
        except Exception:
            pass
    return default


def _cfg_int(cfg, name, default):
    try:
        return int(_cfg_get(cfg, name, default))
    except Exception:
        return int(default)


def _cfg_float(cfg, name, default):
    try:
        return float(_cfg_get(cfg, name, default))
    except Exception:
        return float(default)


def _cfg_bool(cfg, name, default):
    value = _cfg_get(cfg, name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _mode(cfg):
    value = str(_cfg_get(cfg, "auto_skills", "auto")).strip().lower()
    return value if value in ("auto", "propose", "off") else "auto"


def _safe(default):
    """Decorator: a public helper never raises into a Harness run."""
    def decorate(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                try:
                    return default() if callable(default) else default
                except Exception:
                    return None
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        return wrapper
    return decorate


def _err(exc):
    text = str(exc) or exc.__class__.__name__
    try:
        from . import api
        redact = getattr(api, "redact_secrets", None)
        if callable(redact):
            text = str(redact(text))
    except Exception:
        pass
    return text.replace("\n", " ")[:300]


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _state_root(cfg):
    raw = _cfg_get(cfg, "state_dir", None)
    if raw:
        try:
            return Path(os.path.expanduser(str(raw)))
        except Exception:
            pass
    return Path(tempfile.gettempdir()) / "deepseek-mcp-state"


def _registry(cfg):
    from . import skill_registry
    return skill_registry.SkillRegistry(cfg)


def _skills_directory(cfg):
    try:
        return Path(_registry(cfg).directory)
    except Exception:
        return _state_root(cfg) / "skills"


def _capture_dir(cfg):
    return _skills_directory(cfg) / ".capture"


def _capture_path(cfg):
    return _capture_dir(cfg) / "unmatched.jsonl"


def _clusters_path(cfg):
    return _capture_dir(cfg) / "clusters.json"


def _decisions_path(cfg):
    return _capture_dir(cfg) / "decisions.jsonl"


def _ensure_dir(path):
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False


def _atomic_text(path, text):
    path = Path(path)
    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, str(path))
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _write_json(path, obj):
    _atomic_text(path, json.dumps(obj, sort_keys=True, indent=2,
                                  ensure_ascii=False) + "\n")


def _rotate_if_needed(path, max_records, max_bytes):
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return
    if len(data) < max_bytes and data.count(b"\n") < max_records:
        return
    backup = Path(str(path) + ".1")
    try:
        os.replace(str(path), str(backup))
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def _append_jsonl(path, record, max_bytes=MAX_RECORD_BYTES):
    path = Path(path)
    _ensure_dir(path.parent)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    if len(line.encode("utf-8")) > max_bytes:
        line = line[:max_bytes]
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _median(values):
    rows = sorted(float(item) for item in values if _is_num(item))
    if not rows:
        return None
    middle = len(rows) // 2
    if len(rows) % 2:
        return round(rows[middle], 4)
    return round((rows[middle - 1] + rows[middle]) / 2.0, 4)


def _mean(values):
    rows = [float(item) for item in values if _is_num(item)]
    if not rows:
        return None
    return round(sum(rows) / len(rows), 4)


def _score_of(run):
    value = run.get("quality_score")
    if _is_num(value):
        return float(value)
    return None


def _run_id(run):
    if not isinstance(run, dict):
        return None
    value = run.get("id") or run.get("run_id")
    return str(value) if value else None


def _last_review(run):
    rounds = run.get("review_rounds") or []
    for item in reversed(rounds):
        if isinstance(item, dict) and item.get("kind") == "review":
            return item
    return None


def _classify(prompt):
    try:
        from . import quality
        result = quality.classify_task(prompt)
        return result.get("task_type") if isinstance(result, dict) else None
    except Exception:
        return None


def _kind(text):
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        from . import quality
        return str(quality.defect_kind(raw))
    except Exception:
        tokens = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", raw.lower())
        return " ".join(tokens[:6]) or "unspecified"


def _severe_kind(kind):
    text = str(kind or "").strip().lower()
    if not text:
        return False
    return text in SEVERE_KINDS or text.replace(" ", "-") in SEVERE_KINDS


def _task_type(run):
    raw = run.get("task_type")
    if raw in CANONICAL_TASK_TYPES:
        return raw
    raw = run.get("complexity")
    mapping = {"creative-design": "creative", "research-data": "research",
               "code-development": "code", "document": "document"}
    if raw in mapping:
        return mapping[raw]
    skill = run.get("skill")
    if skill == "code":
        return "code"
    if skill == "data":
        return "research"
    if skill in ("poster", "web", "document"):
        return "creative"
    prompt = run.get("prompt") or ""
    if prompt:
        value = _classify(prompt)
        if value in CANONICAL_TASK_TYPES:
            return value
    return None


def _strip_pii(text):
    value = str(text or "")[:20000]
    value = _PEM_RE.sub(" ", value)
    value = _URL_RE.sub(" ", value)
    value = _WIN_USERS_RE.sub(" ", value)
    value = _USERS_RE.sub(" ", value)
    value = _EMAIL_RE.sub(" ", value)
    value = _BEARER_RE.sub(" ", value)
    value = _SECRET_RE.sub(" ", value)
    value = _IPV4_RE.sub(" ", value)
    value = _IPV6_RE.sub(" ", value)
    value = _PHONE_RE.sub(" ", value)
    value = _HANDLE_RE.sub(" ", value)
    return value


@_safe(lambda: [])
def extract_keywords(prompt, limit=MAX_KEYWORDS, allow_extra=()):
    """Sanitised lowercase keywords: PII-free, stopwords and names removed."""
    text = str(prompt or "")[:20000]
    cleaned = _strip_pii(text)
    try:
        cap = max(0, min(int(limit if limit is not None else MAX_KEYWORDS),
                         MAX_KEYWORDS))
    except Exception:
        cap = MAX_KEYWORDS
    if cap <= 0:
        return []
    allow = set(_NAME_ALLOW)
    try:
        for item in allow_extra or ():
            token = str(item or "").strip().lower()
            if token:
                allow.add(token)
    except Exception:
        pass
    occurrences = {}
    matches = list(_TOKEN_RE.finditer(cleaned))
    for match in matches:
        raw = match.group(0).strip(".-")
        if len(raw) < 3 or len(raw) > 24:
            continue
        low = raw.lower()
        if low in STOPWORDS:
            continue
        occurrences.setdefault(low, []).append(raw)
    output = []
    seen = set()
    for match in matches:
        raw = match.group(0).strip(".-")
        if len(raw) < 3 or len(raw) > 24:
            continue
        low = raw.lower()
        if low in STOPWORDS or low in seen:
            continue
        originals = occurrences.get(low) or []
        if originals and all(item[:1].isupper() for item in originals):
            if low not in allow:
                continue
        seen.add(low)
        output.append(low)
        if len(output) >= cap:
            break
    return output


def _replace_name_run(match):
    return "[name]"


def _replace_cap_word(match):
    word = match.group(0)
    if word.lower() in _NAME_ALLOW:
        return word
    return "[name]"


@_safe(lambda: "")
def sanitise_evidence(text, limit=200):
    """Lossy sanitisation for the opt-in evidence samples."""
    value = str(text or "")[:20000]
    value = _PEM_RE.sub(" ", value)
    value = _URL_RE.sub("[url]", value)
    value = _WIN_USERS_RE.sub("[path]", value)
    value = _USERS_RE.sub("[path]", value)
    value = _PATH_RE.sub("[path]", value)
    value = _EMAIL_RE.sub("[email]", value)
    value = _BEARER_RE.sub("[secret]", value)
    value = _SECRET_RE.sub("[secret]", value)
    value = _IPV4_RE.sub("[ip]", value)
    value = _IPV6_RE.sub("[ip]", value)
    value = _PHONE_RE.sub("[phone]", value)
    value = _HANDLE_RE.sub("[handle]", value)
    value = _NAME_RUN_RE.sub(_replace_name_run, value)
    value = _CAP_WORD_RE.sub(_replace_cap_word, value)
    value = " ".join(value.split())
    try:
        size = max(0, int(limit if limit is not None else 200))
    except Exception:
        size = 200
    return value[:size]


def _deliverable_kinds(run):
    kinds = set()
    files = run.get("output_files") or []
    try:
        for item in files:
            if isinstance(item, dict):
                item = item.get("path") or item.get("name") or ""
            text = str(item or "")
            suffix = os.path.splitext(text)[1].lower().lstrip(".")
            kinds.add(_SUFFIX_TO_KIND.get(suffix, "other"))
    except Exception:
        pass
    return sorted(kinds) if kinds else ["unknown"]


def _size_bucket(run, prompt):
    raw = run.get("size_estimate")
    label = None
    if isinstance(raw, dict):
        label = str(raw.get("size") or "").strip().lower()
    elif isinstance(raw, str):
        label = raw.strip().lower()
    mapping = {"s": "s", "small": "s", "m": "m", "medium": "m",
               "l": "l", "large": "l"}
    if label in mapping:
        return mapping[label]
    words = len(re.findall(r"\b[\w'-]+\b", str(prompt or "")))
    if words < 300:
        return "s"
    if words <= 1200:
        return "m"
    return "l"


def _registry_entries(cfg):
    try:
        return list(_registry(cfg).entries())
    except Exception:
        return []


def _registry_vocabulary(cfg):
    vocab = set()
    try:
        for entry in _registry_entries(cfg):
            if not isinstance(entry, dict):
                continue
            for value in [entry.get("key")] + list(entry.get("aliases") or []) \
                    + list(entry.get("keywords") or []):
                token = str(value or "").strip().lower()
                if token:
                    vocab.add(token)
    except Exception:
        pass
    return vocab


def _is_severe(status, score, blocking, defect_kinds, write_violations):
    if str(status or "") in ("error", "deadline_exceeded", "budget_exceeded",
                             "no_balance"):
        return True
    if int(blocking or 0) >= 2:
        return True
    if score is not None and score <= 6.0:
        return True
    if write_violations:
        return True
    return any(_severe_kind(kind) for kind in defect_kinds or [])


@_safe(lambda: {})
def build_signature(prompt, run, match, cfg):
    """Build the PLAN section 4.4 capture signature (no raw text)."""
    run = run if isinstance(run, dict) else {}
    match = match if isinstance(match, dict) else {}
    task_type = _task_type(run)
    scores = match.get("scores") if isinstance(match.get("scores"), dict) else {}
    best_key = match.get("key")
    if not best_key and scores:
        best_key = max(scores, key=lambda name: scores[name])
    signature = {
        "v": 1,
        "run_id": _run_id(run),
        "created": now_iso(),
        "at": now_iso(),
        "task_type": task_type,
        "keywords": [],
        "intent": "",
        "deliverable_kinds": _deliverable_kinds(run),
        "size_bucket": _size_bucket(run, prompt),
        "match": {
            "best": best_key,
            "confidence": match.get("confidence"),
            "runner_up": match.get("runner_up"),
            "threshold": match.get("threshold", _cfg_float(
                cfg, "auto_skills_min_confidence", 0.55)),
            "unmatched": bool(match.get("unmatched", True)),
        },
        "outcome": {},
        "severe": False,
        "match_key": match.get("key"),
        "match_confidence": match.get("confidence"),
        "at_unmatched": bool(match.get("unmatched")),
    }
    if task_type is None:
        signature["simple"] = True
        return signature
    limit = _cfg_int(cfg, "auto_skills_max_keywords", MAX_KEYWORDS)
    keywords = extract_keywords(prompt, limit=limit,
                               allow_extra=_registry_vocabulary(cfg))
    signature["keywords"] = list(keywords)
    signature["intent"] = " ".join(list(keywords)[:3])[:80]
    review = _last_review(run)
    try:
        blocking = int((review or {}).get("blocking_defects") or 0)
    except Exception:
        blocking = 0
    defects = []
    fixes = []
    defect_samples = []
    fix_samples = []
    samples = []
    write_violations = False
    if isinstance(review, dict):
        write_violations = bool(review.get("write_violations"))
        for value in review.get("defects") or []:
            kind = _kind(value)
            if kind:
                defects.append(kind)
        for value in review.get("fixes") or []:
            kind = _kind(value)
            if kind:
                fixes.append(kind)
        if _cfg_bool(cfg, "auto_skills_store_evidence_text", False):
            for value in list(review.get("defects") or [])[:3]:
                text = sanitise_evidence(value, 200)
                if text:
                    defect_samples.append(text)
            for value in list(review.get("fixes") or [])[:3]:
                text = sanitise_evidence(value, 200)
                if text:
                    fix_samples.append(text)
            samples = (defect_samples + fix_samples)[:3]
    score = _score_of(run)
    severe = _is_severe(run.get("status"), score, blocking, defects,
                        write_violations)
    signature["outcome"] = {
        "status": str(run.get("status") or "unknown"),
        "score": score,
        "blocking": blocking,
        "defect_kinds": sorted(set(defects))[:6],
        "fix_kinds": sorted(set(fixes))[:6],
        "defect_samples": defect_samples,
        "fix_samples": fix_samples,
        "samples": samples,
        "severe": bool(severe),
    }
    signature["severe"] = bool(severe)
    return signature


@_safe(lambda: [])
def read_captures(cfg, limit=500):
    """Read sanitised capture records, oldest first."""
    output = []
    path = _capture_path(cfg)
    for current in (Path(str(path) + ".1"), path):
        try:
            text = current.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if isinstance(record, dict):
                output.append(record)
    try:
        size = int(limit if limit is not None else 500)
    except Exception:
        size = 500
    if size <= 0:
        return []
    return output[-size:]


@_safe(lambda: False)
def capture_seen(run_id, cfg):
    if not run_id:
        return False
    for record in read_captures(cfg, limit=MAX_CAPTURE_RECORDS * 2):
        if record.get("run_id") == run_id:
            return True
    return False


def _shrink_capture(record):
    if len(_canonical(record).encode("utf-8")) <= MAX_RECORD_BYTES:
        return record
    trimmed = dict(record)
    outcome = dict(trimmed.get("outcome") or {})
    outcome["samples"] = []
    trimmed["outcome"] = outcome
    if len(_canonical(trimmed).encode("utf-8")) <= MAX_RECORD_BYTES:
        return trimmed
    trimmed["keywords"] = list(trimmed.get("keywords") or [])[:8]
    if len(_canonical(trimmed).encode("utf-8")) <= MAX_RECORD_BYTES:
        return trimmed
    trimmed["intent"] = str(trimmed.get("intent") or "")[:40]
    trimmed["keywords"] = list(trimmed.get("keywords") or [])[:4]
    return trimmed


@_safe(lambda: {"captured": False, "reason": "error"})
def append_capture(signature, cfg):
    if not isinstance(signature, dict):
        return {"captured": False, "reason": "invalid"}
    if signature.get("simple") or signature.get("task_type") not in CANONICAL_TASK_TYPES:
        return {"captured": False, "reason": "skipped_simple"}
    run_id = signature.get("run_id")
    if not run_id:
        return {"captured": False, "reason": "no_run_id"}
    if capture_seen(run_id, cfg):
        return {"captured": False, "reason": "duplicate"}
    record = {
        "v": 1,
        "run_id": run_id,
        "created": signature.get("created") or signature.get("at") or now_iso(),
        "at": signature.get("at") or now_iso(),
        "task_type": signature.get("task_type"),
        "keywords": list(signature.get("keywords") or [])[:MAX_KEYWORDS],
        "intent": str(signature.get("intent") or "")[:80],
        "deliverable_kinds": list(signature.get("deliverable_kinds") or [])[:6],
        "size_bucket": signature.get("size_bucket") or "s",
        "match": dict(signature.get("match") or {}),
        "outcome": dict(signature.get("outcome") or {}),
        "severe": bool(signature.get("severe")),
    }
    if signature.get("match_key"):
        record["match_key"] = str(signature.get("match_key"))[:32]
    record = _shrink_capture(record)
    path = _capture_path(cfg)
    _rotate_if_needed(path, _cfg_int(cfg, "auto_skills_capture_limit",
                                     MAX_CAPTURE_RECORDS),
                      MAX_CAPTURE_BYTES)
    _append_jsonl(path, record)
    return {"captured": True, "run_id": run_id,
            "keywords": len(record.get("keywords") or []),
            "severe": record.get("severe", False)}


@_safe(lambda: 0.0)
def jaccard(a, b):
    left = set()
    right = set()
    for value in a or []:
        token = str(value or "").strip().lower()
        if token:
            left.add(token)
    for value in b or []:
        token = str(value or "").strip().lower()
        if token:
            right.add(token)
    if not left or not right:
        return 0.0
    return round(len(left & right) / float(len(left | right)), 4)


def _size_score(left, right):
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    order = {"s": 0, "m": 1, "l": 2}
    if left in order and right in order and abs(order[left] - order[right]) == 1:
        return 0.5
    return 0.0


@_safe(lambda: 0.0)
def cluster_similarity(signature, cluster):
    if not isinstance(signature, dict) or not isinstance(cluster, dict):
        return 0.0
    if signature.get("task_type") != cluster.get("task_type"):
        return 0.0
    keyword_score = jaccard(signature.get("keywords"), cluster.get("keywords"))
    deliverable_score = jaccard(signature.get("deliverable_kinds"),
                                cluster.get("deliverable_kinds"))
    size_score = _size_score(signature.get("size_bucket"),
                             cluster.get("size_bucket"))
    value = (0.50 * keyword_score + 0.20 * deliverable_score
             + 0.20 * 1.0 + 0.10 * size_score)
    return round(value, 4)


def _cluster_id(task_type, keywords):
    first = [str(item).lower() for item in (keywords or [])[:6]]
    seed = "|".join(sorted(first))
    digest = hashlib.sha1(("%s|%s" % (task_type, seed)).encode("utf-8"))
    return "c-" + digest.hexdigest()[:10]


def _member_from_signature(signature):
    outcome = signature.get("outcome") or {}
    score = outcome.get("score")
    defects = list(outcome.get("defect_kinds") or [])
    fixes = list(outcome.get("fix_kinds") or [])
    try:
        blocking = int(outcome.get("blocking") or 0)
    except Exception:
        blocking = 0
    return {
        "run_id": signature.get("run_id"),
        "at": signature.get("at") or now_iso(),
        "keywords": list(signature.get("keywords") or [])[:MAX_KEYWORDS],
        "deliverable_kinds": list(signature.get("deliverable_kinds") or [])[:6],
        "size_bucket": signature.get("size_bucket") or "s",
        "score": score if _is_num(score) else None,
        "blocking": blocking,
        "severe": bool(signature.get("severe")),
        "defect_kinds": defects[:6],
        "fix_kinds": fixes[:6],
        "has_review": bool(_is_num(score) or defects or fixes),
    }


def _recompute_centroid(members):
    counts = {}
    for member in members:
        for token in member.get("keywords") or []:
            token = str(token)
            counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts, key=lambda token: (-counts[token], token))
    return ordered[:MAX_CENTROID_KEYWORDS]


def _recompute_deliverables(members):
    counts = {}
    for member in members:
        for token in member.get("deliverable_kinds") or []:
            token = str(token)
            counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts, key=lambda token: (-counts[token], token))
    return ordered[:6]


def _baseline_from_members(cluster):
    members = cluster.get("members") or []
    scores = []
    blocking_rows = 0
    scored = 0
    for member in members:
        if not isinstance(member, dict) or not _is_num(member.get("score")):
            continue
        scores.append(float(member["score"]))
        scored += 1
        if int(member.get("blocking") or 0) >= 1:
            blocking_rows += 1
    if len(scores) < 3:
        return None
    return {
        "median": _median(scores),
        "mean": _mean(scores),
        "n": len(scores),
        "blocking_rate": round(blocking_rows / float(scored), 4) if scored else 0.0,
        "source": "members",
        "updated": now_iso(),
    }


def _baseline_from_rows(rows, source, task_type=None):
    scores = []
    blocking_rows = 0
    for row in rows:
        outcome = row.get("outcome") or {}
        score = outcome.get("score")
        if not _is_num(score):
            continue
        scores.append(float(score))
        try:
            if int(outcome.get("blocking") or 0) >= 1:
                blocking_rows += 1
        except Exception:
            pass
    if len(scores) < 3:
        return None
    result = {
        "median": _median(scores),
        "mean": _mean(scores),
        "n": len(scores),
        "blocking_rate": round(blocking_rows / float(len(scores)), 4),
        "source": source,
        "updated": now_iso(),
    }
    if task_type:
        result["task_type"] = task_type
    return result


def _is_recent(value, days):
    when = parse_iso(value)
    if when is None:
        return True
    return (_now() - when) <= timedelta(days=max(1, int(days)))


@_safe(lambda: None)
def baseline_for(task_type, cfg, cluster=None):
    if not task_type:
        return None
    if isinstance(cluster, dict):
        frozen = cluster.get("baseline")
        if isinstance(frozen, dict) and _is_num(frozen.get("median")) \
                and int(frozen.get("n") or 0) >= 3:
            result = dict(frozen)
            result["source"] = "members"
            return result
        computed = _baseline_from_members(cluster)
        if computed:
            return computed
    days = _cfg_int(cfg, "auto_skills_cluster_max_age_days", 90)
    rows = [row for row in read_captures(cfg, limit=MAX_CAPTURE_RECORDS)
            if row.get("task_type") == task_type and _is_recent(row.get("at"), days)]
    result = _baseline_from_rows(rows, "unmatched", task_type=task_type)
    if result:
        return result
    rows = [row for row in read_captures(cfg, limit=MAX_CAPTURE_RECORDS)
            if _is_recent(row.get("at"), days)]
    return _baseline_from_rows(rows, "global")


def _load_clusters(cfg):
    path = _clusters_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("clusters")
    # The on-disk schema is a map keyed by cluster id. A list is accepted for
    # resilience and rewritten as a map on the next save.
    if isinstance(raw, dict):
        source = list(raw.values())
    elif isinstance(raw, list):
        source = raw
    else:
        source = []
    clusters = []
    for item in source:
        if isinstance(item, dict) and item.get("id") and item.get("task_type"):
            clusters.append(item)
    try:
        revision = int(data.get("revision") or 0)
    except Exception:
        revision = 0
    return {"schema": 1, "revision": revision, "clusters": clusters}


def _save_clusters(cfg, data):
    data = data if isinstance(data, dict) else {"schema": 1, "clusters": []}
    try:
        data["revision"] = int(data.get("revision") or 0) + 1
    except Exception:
        data["revision"] = 1
    data["schema"] = 1
    rows = data.get("clusters") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    data["clusters"] = dict((str(item.get("id")), item)
                            for item in rows if isinstance(item, dict)
                            and item.get("id"))
    _write_json(_clusters_path(cfg), data)


@_safe(lambda: (False, "error"))
def should_draft(cluster, cfg):
    if not isinstance(cluster, dict):
        return False, "no_cluster"
    status = str(cluster.get("status") or "collecting")
    if status == "building":
        return False, "building"
    if status in ("drafted", "rejected", "exhausted", "skipped"):
        return False, "status_" + status
    try:
        attempts = int(cluster.get("attempts") or 0)
    except Exception:
        attempts = 0
    if attempts >= 2:
        return False, "exhausted"
    max_age = _cfg_int(cfg, "auto_skills_cluster_max_age_days", 90)
    created = parse_iso(cluster.get("created"))
    if created is not None and (_now() - created) > timedelta(days=max_age):
        return False, "expired"
    members = [item for item in (cluster.get("members") or [])
               if isinstance(item, dict)]
    count = len(members)
    if not count:
        return False, "empty"
    severe = sum(1 for member in members if member.get("severe"))
    cluster["severe_count"] = severe
    reviewed = sum(1 for member in members if member.get("has_review"))
    reviewed_min = _cfg_int(cfg, "auto_skills_reviewed_min", 2)
    review_data = (reviewed >= reviewed_min
                   or (severe >= 1 and count >= _cfg_int(
                       cfg, "auto_skills_cluster_min_severe", 2)))
    if not review_data:
        return False, "needs_reviewed_evidence"
    normal_ready = count >= _cfg_int(cfg, "auto_skills_cluster_min", 3)
    severe_ready = (severe >= 1
                    and count >= _cfg_int(cfg, "auto_skills_cluster_min_severe", 2))
    single_ready = (_cfg_bool(cfg, "auto_skills_allow_single_severe", False)
                    and severe >= 1 and count >= 1)
    if not (normal_ready or severe_ready or single_ready):
        return False, "not_enough_runs"
    return True, "ready"


def _due(cluster):
    value = cluster.get("next_attempt_at")
    if not value:
        return True
    when = parse_iso(value)
    return when is None or _now() >= when


def _update_cluster(cluster, signature, cfg):
    members = [item for item in (cluster.get("members") or [])
               if isinstance(item, dict)]
    run_id = signature.get("run_id")
    if not run_id or not any(item.get("run_id") == run_id for item in members):
        members.append(_member_from_signature(signature))
    cluster["members"] = members[-MAX_MEMBERS:]
    members = cluster["members"]
    cluster["count"] = len(members)
    cluster["severe_count"] = sum(1 for item in members if item.get("severe"))
    cluster["keywords"] = _recompute_centroid(members)
    cluster["deliverable_kinds"] = _recompute_deliverables(members)
    if not cluster.get("anchor_keywords"):
        cluster["anchor_keywords"] = list(cluster["keywords"])
    cluster["updated"] = now_iso()
    cluster["last_run_id"] = run_id
    if str(cluster.get("status") or "") in ("drafted", "rejected", "exhausted",
                                            "skipped", "building"):
        cluster["status"] = "collecting"
    ready, _reason = should_draft(cluster, cfg)
    if cluster.get("status") != "building":
        cluster["status"] = "ready" if ready else "collecting"
    frozen = _baseline_from_members(cluster)
    if frozen:
        cluster["baseline"] = frozen
    return cluster


def _new_cluster(signature, cfg):
    task_type = signature.get("task_type")
    keywords = list(signature.get("keywords") or [])[:MAX_KEYWORDS]
    return {
        "id": _cluster_id(task_type, keywords),
        "task_type": task_type,
        "status": "collecting",
        "keywords": list(keywords[:MAX_CENTROID_KEYWORDS]),
        "anchor_keywords": list(keywords[:MAX_CENTROID_KEYWORDS]),
        "deliverable_kinds": list(signature.get("deliverable_kinds") or [])[:6],
        "members": [],
        "count": 0,
        "severe_count": 0,
        "created": now_iso(),
        "updated": now_iso(),
        "attempts": 0,
        "next_attempt_at": None,
        "baseline": None,
        "signature_digest": None,
        "cost_usd": 0.0,
        "seconds": 0.0,
        "last_run_id": None,
        "last_decision": None,
    }


@_safe(lambda: {})
def join_or_create_cluster(signature, cfg):
    if not isinstance(signature, dict) \
            or signature.get("task_type") not in CANONICAL_TASK_TYPES:
        return {}
    data = _load_clusters(cfg)
    clusters = data["clusters"]
    threshold = _cfg_float(cfg, "auto_skills_cluster_similarity", 0.62)
    best = None
    best_similarity = -1.0
    for cluster in clusters:
        if cluster.get("task_type") != signature.get("task_type"):
            continue
        if cluster.get("status") in ("building",):
            continue
        similarity = cluster_similarity(signature, cluster)
        keyword_score = jaccard(signature.get("keywords"),
                                cluster.get("keywords"))
        if (similarity >= threshold or keyword_score >= 0.75) \
                and similarity >= best_similarity:
            best = cluster
            best_similarity = similarity
    if best is not None:
        _update_cluster(best, signature, cfg)
        _save_clusters(cfg, data)
        return best
    candidate = _new_cluster(signature, cfg)
    existing = None
    for cluster in clusters:
        if cluster.get("id") == candidate["id"]:
            existing = cluster
            break
    if existing is not None:
        _update_cluster(existing, signature, cfg)
        _save_clusters(cfg, data)
        return existing
    max_clusters = max(1, _cfg_int(cfg, "auto_skills_max_clusters", MAX_CLUSTERS))
    if len(clusters) >= max_clusters:
        droppable = [item for item in clusters
                     if item.get("status") == "collecting"]
        if not droppable:
            return {}
        droppable.sort(key=lambda item: (str(item.get("updated") or ""),
                                         len(item.get("members") or []),
                                         str(item.get("id") or "")))
        clusters.remove(droppable[0])
    _update_cluster(candidate, signature, cfg)
    clusters.append(candidate)
    _save_clusters(cfg, data)
    return candidate


def _builds_today(cfg):
    today = _now().date().isoformat()
    count = 0
    path = _decisions_path(cfg)
    for current in (Path(str(path) + ".1"), path):
        try:
            text = current.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except Exception:
                continue
            if record.get("action") == "build" \
                    and str(record.get("at") or "").startswith(today):
                count += 1
    return count


def _caps_ok(cfg):
    if _cfg_float(cfg, "auto_skills_max_cost_usd", 1.0) == 0:
        return False, "cost_disabled"
    per_day = _cfg_int(cfg, "auto_skills_max_drafts_per_day", 4)
    if per_day >= 0 and _builds_today(cfg) >= per_day:
        return False, "daily_cap"
    entries = _registry_entries(cfg)
    max_skills = _cfg_int(cfg, "auto_skills_max_skills", 64)
    total = [item for item in entries if item.get("status") != "retired"]
    if max_skills and len(total) >= max_skills:
        return False, "skill_cap"
    max_candidates = _cfg_int(cfg, "auto_skills_max_candidates", 8)
    candidates = [item for item in entries
                  if item.get("status") == "candidate"
                  and not item.get("approved")]
    if max_candidates and len(candidates) >= max_candidates:
        return False, "candidate_cap"
    return True, "ok"


@_safe(lambda: None)
def select_ready_cluster(cfg):
    if _mode(cfg) == "off":
        return None
    allowed, _reason = _caps_ok(cfg)
    if not allowed:
        return None
    data = _load_clusters(cfg)
    rows = []
    for cluster in data["clusters"]:
        ready, _why = should_draft(cluster, cfg)
        if ready and _due(cluster):
            rows.append(cluster)
    if not rows:
        return None
    rows.sort(key=lambda item: (-int(item.get("severe_count") or 0),
                                -len(item.get("members") or []),
                                str(item.get("updated") or ""),
                                str(item.get("id") or "")))
    return rows[0]


@_safe(lambda: {})
def collect_evidence(cluster, cfg):
    if not isinstance(cluster, dict):
        return {}
    members = [item for item in (cluster.get("members") or [])
               if isinstance(item, dict)]
    outcomes = []
    defect_kinds = set()
    fix_kinds = set()
    scores = []
    for member in members[-MAX_MEMBERS:]:
        score = member.get("score")
        if _is_num(score):
            scores.append(float(score))
        for kind in member.get("defect_kinds") or []:
            if kind:
                defect_kinds.add(str(kind))
        for kind in member.get("fix_kinds") or []:
            if kind:
                fix_kinds.add(str(kind))
        outcomes.append({
            "status": "reviewed" if member.get("has_review") else "unreviewed",
            "score": score if _is_num(score) else None,
            "blocking": int(member.get("blocking") or 0),
            "severe": bool(member.get("severe")),
        })
    payload = {
        "cluster_id": str(cluster.get("id") or ""),
        "task_type": str(cluster.get("task_type") or ""),
        "runs": len(members),
        "keywords": list(cluster.get("keywords") or [])[:MAX_CENTROID_KEYWORDS],
        "deliverable_kinds": list(cluster.get("deliverable_kinds") or [])[:6],
        "scores": scores[-20:],
        "severe_count": sum(1 for item in members if item.get("severe")),
        "outcomes": outcomes[-MAX_MEMBERS:],
        "defect_kinds": sorted(defect_kinds)[:12],
        "fix_kinds": sorted(fix_kinds)[:12],
    }
    digest = "sha256:" + hashlib.sha256(
        _canonical(payload).encode("utf-8")).hexdigest()
    payload["signature_digest"] = digest
    payload["runs"] = len(members)
    return payload


def _cooldown_until(attempts, cfg):
    hours = _cfg_float(cfg, "auto_skills_cluster_cooldown_hours", 24.0)
    try:
        factor = 2 ** max(0, int(attempts) - 1)
    except Exception:
        factor = 1
    return (_now() + timedelta(hours=hours * factor)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _emit_change(change, run, synthetic):
    if synthetic:
        return [change]
    try:
        record_change(run, change.get("action"), change.get("key"),
                      change.get("version"), change.get("reason"),
                      review_score=change.get("review_score"))
    except Exception:
        pass
    return change


@_safe(lambda: {"action": "error", "error": "build_failed"})
def build_cluster(cluster_id, cfg, run=None, trace=None, cancel_check=None,
                  envelope=None):
    synthetic = run is None
    if synthetic:
        run = {"id": "pending-%s" % cluster_id,
               "skill_auto": {"observed": True, "changes": []}}
    auth_env = envelope or (run or {}).get("authority")
    mode = _mode(cfg)
    if mode == "off":
        return _emit_change({"action": "skipped", "reason": "off",
                             "cluster_id": cluster_id}, run, synthetic)
    if callable(cancel_check):
        try:
            if cancel_check():
                return _emit_change({"action": "skipped",
                                     "reason": "cancelled",
                                     "cluster_id": cluster_id}, run, synthetic)
        except Exception:
            pass
    data = _load_clusters(cfg)
    cluster = None
    for item in data["clusters"]:
        if item.get("id") == cluster_id:
            cluster = item
            break
    if cluster is None:
        return _emit_change({"action": "error", "error": "unknown_cluster",
                             "cluster_id": cluster_id}, run, synthetic)
    ready, why = should_draft(cluster, cfg)
    if not ready:
        return _emit_change({"action": "skipped", "reason": why,
                             "cluster_id": cluster_id}, run, synthetic)
    if not _due(cluster):
        return _emit_change({"action": "skipped", "reason": "cooldown",
                             "cluster_id": cluster_id}, run, synthetic)
    allowed, cap_reason = _caps_ok(cfg)
    if not allowed:
        return _emit_change({"action": "skipped", "reason": cap_reason,
                             "cluster_id": cluster_id}, run, synthetic)
    run_id = _run_id(run) or ("pending-%s" % cluster_id)
    cluster["status"] = "building"
    cluster["updated"] = now_iso()
    _save_clusters(cfg, data)
    record_decision("build", cfg, cluster_id=cluster_id, run_id=run_id)
    evidence = collect_evidence(cluster, cfg)
    if not evidence:
        cluster["status"] = "ready"
        cluster["next_attempt_at"] = _cooldown_until(
            int(cluster.get("attempts") or 0) + 1, cfg)
        _save_clusters(cfg, data)
        return _emit_change({"action": "error", "error": "no_evidence",
                             "cluster_id": cluster_id}, run, synthetic)
    cluster["signature_digest"] = evidence.get("signature_digest")
    frozen = _baseline_from_members(cluster)
    if frozen:
        cluster["baseline"] = frozen
    _save_clusters(cfg, data)
    started = time.time()
    try:
        from . import skill_author
        existing = _registry_entries(cfg)
        author_kwargs = {"cancel_check": cancel_check}
        if auth_env is not None:
            author_kwargs["envelope"] = auth_env
        draft = skill_author.author_draft("create", evidence, existing, cfg=cfg,
                                          **author_kwargs)
    except Exception as exc:
        _release(cfg, cluster_id, "ready", _cooldown_until(
            int(cluster.get("attempts") or 0) + 1, cfg))
        record_decision("error", cfg, cluster_id=cluster_id, run_id=run_id,
                        error=_err(exc))
        return _emit_change({"action": "error", "error": _err(exc),
                             "cluster_id": cluster_id}, run, synthetic)
    cost = 0.0
    if isinstance(draft, dict):
        try:
            cost = float(draft.get("estimated_cost_usd") or 0.0)
        except Exception:
            cost = 0.0
    cluster["cost_usd"] = float(cluster.get("cost_usd") or 0.0) + cost
    cluster["seconds"] = float(cluster.get("seconds") or 0.0) + (
        time.time() - started)
    if not isinstance(draft, dict):
        _release(cfg, cluster_id, "ready", _cooldown_until(
            int(cluster.get("attempts") or 0) + 1, cfg))
        record_decision("error", cfg, cluster_id=cluster_id, run_id=run_id,
                        error="author_unavailable")
        return _emit_change({"action": "error",
                             "error": "author_unavailable",
                             "cluster_id": cluster_id}, run, synthetic)
    _trace(trace, "skill/draft", cluster_id=cluster_id,
           key=draft.get("key"), action=draft.get("action"))
    if draft.get("action") == "skip":
        reason = sanitise_evidence(draft.get("reason") or "insufficient_evidence",
                                   120) or "insufficient_evidence"
        if reason == "author_unavailable" or draft.get("error"):
            # A failed author call is a retryable build failure, not evidence
            # that the cluster is too thin to learn from.
            _release(cfg, cluster_id, "ready", _cooldown_until(
                int(cluster.get("attempts") or 0) + 1, cfg))
            record_decision("error", cfg, cluster_id=cluster_id,
                            run_id=run_id, error=reason)
            return _emit_change({"action": "pending",
                                 "reason": "author_unavailable",
                                 "cluster_id": cluster_id}, run, synthetic)
        cluster["status"] = "skipped"
        cluster["last_decision"] = "skip"
        cluster["updated"] = now_iso()
        _save_clusters(cfg, data)
        record_decision("skip", cfg, cluster_id=cluster_id, run_id=run_id,
                        reason=reason)
        _trace(trace, "skill/skip", cluster_id=cluster_id, reason=reason)
        return _emit_change({"action": "skipped", "reason": reason,
                             "cluster_id": cluster_id}, run, synthetic)
    max_cost = _cfg_float(cfg, "auto_skills_max_cost_usd", 1.0)
    if max_cost > 0 and cluster["cost_usd"] > max_cost:
        _release(cfg, cluster_id, "ready", _cooldown_until(
            int(cluster.get("attempts") or 0) + 1, cfg))
        record_decision("cost_cap", cfg, cluster_id=cluster_id, run_id=run_id,
                        cost_usd=round(cluster["cost_usd"], 4))
        return _emit_change({"action": "pending", "reason": "cost_cap",
                             "cluster_id": cluster_id}, run, synthetic)
    try:
        gate_kwargs = {"cancel_check": cancel_check}
        if auth_env is not None:
            gate_kwargs["envelope"] = auth_env
        gate = skill_author.gate(draft, evidence, _registry(cfg), cfg,
                                 **gate_kwargs)
    except Exception as exc:
        _release(cfg, cluster_id, "ready", _cooldown_until(
            int(cluster.get("attempts") or 0) + 1, cfg))
        record_decision("error", cfg, cluster_id=cluster_id, run_id=run_id,
                        error=_err(exc))
        return _emit_change({"action": "error", "error": _err(exc),
                             "cluster_id": cluster_id}, run, synthetic)
    if not isinstance(gate, dict):
        gate = {"accepted": False, "decision": "invalid",
                "errors": ["E_GATE"]}
    if _is_num(gate.get("cost_usd")):
        cluster["cost_usd"] = (float(cluster.get("cost_usd") or 0.0)
                               + float(gate["cost_usd"]))
    if _is_num(gate.get("seconds")):
        cluster["seconds"] = (float(cluster.get("seconds") or 0.0)
                              + float(gate["seconds"]))
    review = gate.get("review") if isinstance(gate.get("review"), dict) else {}
    review_score = review.get("score")
    _trace(trace, "skill/review", cluster_id=cluster_id,
           score=review_score, blocking=len(review.get("blocking_defects") or []),
           accepted=bool(gate.get("accepted")))
    max_cost = _cfg_float(cfg, "auto_skills_max_cost_usd", 1.0)
    if max_cost > 0 and cluster["cost_usd"] > max_cost:
        _release(cfg, cluster_id, "ready", _cooldown_until(
            int(cluster.get("attempts") or 0) + 1, cfg))
        record_decision("cost_cap", cfg, cluster_id=cluster_id, run_id=run_id,
                        cost_usd=round(cluster["cost_usd"], 4))
        return _emit_change({"action": "pending", "reason": "cost_cap",
                             "cluster_id": cluster_id}, run, synthetic)
    if gate.get("accepted"):
        try:
            result = _registry(cfg).create_or_merge(
                gate.get("draft") or draft, review, cluster, mode, run_id)
        except Exception as exc:
            _release(cfg, cluster_id, "ready", _cooldown_until(
                int(cluster.get("attempts") or 0) + 1, cfg))
            record_decision("error", cfg, cluster_id=cluster_id,
                            run_id=run_id, error=_err(exc))
            return _emit_change({"action": "error", "error": _err(exc),
                                 "cluster_id": cluster_id}, run, synthetic)
        result = result if isinstance(result, dict) else {}
        key = result.get("key") or (gate.get("target") or draft.get("key"))
        version = result.get("version")
        action = "merged" if (result.get("merged_into")
                              or result.get("action") in ("merge", "merged")
                              or gate.get("decision") == "merge") else "created"
        cluster["status"] = "drafted"
        cluster["last_decision"] = "accepted"
        cluster["next_attempt_at"] = None
        cluster["updated"] = now_iso()
        cluster["result_key"] = key
        cluster["result_action"] = action
        cluster["version"] = version
        _save_clusters(cfg, data)
        record_decision(action, cfg, cluster_id=cluster_id, run_id=run_id,
                        key=key, version=version, review_score=review_score)
        _trace(trace, "skill/" + action, cluster_id=cluster_id, key=key,
               version=version, review_score=review_score)
        _trace(trace, "skill/saved", key=key, version=version,
               status=cluster.get("status"), action=action)
        return _emit_change({
            "action": action, "key": key, "version": version,
            "cluster_id": cluster_id, "reason": "accepted",
            "review_score": review_score if _is_num(review_score) else None,
        }, run, synthetic)
    attempts = int(cluster.get("attempts") or 0) + 1
    decision = str(gate.get("decision") or "rejected")
    retryable = decision in ("timeout", "cost_cap", "skip", "error",
                             "author_unavailable")
    exhausted = attempts >= 2
    if retryable:
        cluster["status"] = "ready"
        action = "pending"
    else:
        cluster["status"] = "exhausted" if exhausted else "rejected"
        action = "rejected"
    cluster["attempts"] = attempts
    cluster["last_decision"] = decision
    cluster["next_attempt_at"] = _cooldown_until(attempts, cfg)
    cluster["updated"] = now_iso()
    errors = gate.get("errors") if isinstance(gate.get("errors"), list) else []
    _save_clusters(cfg, data)
    record_decision(action, cfg, cluster_id=cluster_id, run_id=run_id,
                    decision=decision, attempts=attempts,
                    review_score=review_score, errors=errors[:6])
    _trace(trace, "skill/reject", cluster_id=cluster_id,
           decision=decision, attempts=attempts)
    return _emit_change({
        "action": action, "cluster_id": cluster_id, "reason": decision,
        "attempts": attempts,
        "review_score": review_score if _is_num(review_score) else None,
    }, run, synthetic)


def _release(cfg, cluster_id, status, next_attempt_at=None):
    try:
        data = _load_clusters(cfg)
        for cluster in data["clusters"]:
            if cluster.get("id") == cluster_id:
                cluster["status"] = status
                cluster["next_attempt_at"] = next_attempt_at
                cluster["updated"] = now_iso()
                break
        _save_clusters(cfg, data)
    except Exception:
        pass


@_safe(lambda: None)
def record_change(run, action, key, version, reason, review_score=None):
    if not isinstance(run, dict):
        return None
    block = run.get("skill_auto")
    if not isinstance(block, dict):
        block = {"observed": True, "changes": []}
        run["skill_auto"] = block
    changes = block.get("changes")
    if not isinstance(changes, list):
        changes = []
        block["changes"] = changes
    change = {"action": str(action or "unknown"), "key": key,
              "version": version, "reason": str(reason or "")[:80]}
    if _is_num(review_score):
        change["review_score"] = round(float(review_score), 3)
    changes.append(change)
    return change


@_safe(lambda: None)
def record_decision(action, cfg, **fields):
    record = {
        "v": 1,
        "at": now_iso(),
        "run_id": _decision_value(fields.pop("run_id", None)),
        "mode": _mode(cfg),
        "action": str(action or "unknown")[:40],
    }
    for name, value in fields.items():
        key = str(name)[:40]
        if key in record:
            continue
        record[key] = _decision_value(value)
    path = _decisions_path(cfg)
    _rotate_if_needed(path, MAX_DECISIONS, MAX_CAPTURE_BYTES)
    _append_jsonl(path, record, max_bytes=4096)
    return None


def _decision_value(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4) if isinstance(value, float) else value
    if isinstance(value, (list, tuple)):
        output = []
        for item in list(value)[:10]:
            if item is None or isinstance(item, (bool, int, float)):
                output.append(item)
            else:
                output.append(str(item)[:120])
        return output
    return str(value).replace("\n", " ")[:120]


@_safe(lambda: {"action": "hold", "reason": "error"})
def promote_or_demote(entry, cfg, run=None, trace=None):
    if not isinstance(entry, dict):
        return {"action": "hold", "reason": "unknown"}
    key = entry.get("key")
    if not key:
        return {"action": "hold", "reason": "no_key"}
    status = str(entry.get("status") or "")
    stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else {}
    uses = int(stats.get("uses") or 0)
    window_rows = [row for row in (stats.get("window") or [])
                   if isinstance(row, dict) and _is_num(row.get("score"))]
    scores = [float(row["score"]) for row in window_rows][-20:]
    scored = len(scores)
    baseline_ref = stats.get("baseline_ref")
    tolerance = _cfg_float(cfg, "auto_skills_score_tolerance", 0.3)
    promote_after = _cfg_int(cfg, "auto_skills_promote_after", 5)
    demote_after = _cfg_int(cfg, "auto_skills_demote_after", 3)
    action = "hold"
    reason = "no_change"
    if status == "candidate":
        if not entry.get("approved"):
            reason = "unapproved"
        elif not (isinstance(baseline_ref, dict)
                  and _is_num(baseline_ref.get("median"))
                  and _is_num(baseline_ref.get("blocking_rate"))):
            reason = "no_baseline"
        elif uses >= 2 * promote_after and scored < 3:
            reason = "unscored"
        elif uses >= promote_after and scored >= 3:
            baseline_median = float(baseline_ref["median"])
            reference_rate = float(baseline_ref["blocking_rate"])
            median = _median(scores)
            rate = _blocking_rate(window_rows)
            losses = int(stats.get("losses") or 0)
            consecutive_bad = int(stats.get("consecutive_bad") or 0)
            if (median is not None and median >= baseline_median - tolerance
                    and rate <= reference_rate + 0.05
                    and losses <= 1 and consecutive_bad == 0):
                action = "promote"
                reason = "baseline_cleared"
            else:
                reason = "below_baseline"
        else:
            reason = "insufficient_uses"
    if action == "hold" and (status in ("active", "seed")
                             or (status == "candidate"
                                 and entry.get("approved"))):
        if uses >= demote_after:
            baseline_at_use = stats.get("baseline_at_use")
            consecutive_bad = int(stats.get("consecutive_bad") or 0)
            median = _median(scores)
            if (median is not None and _is_num(baseline_at_use)
                    and scored >= 3 and median < float(baseline_at_use) - 1.0):
                action = "demote"
                reason = "below_baseline"
            elif consecutive_bad >= 2:
                action = "demote"
                reason = "consecutive_bad"
            elif (scored >= 3 and isinstance(baseline_ref, dict)
                  and _is_num(baseline_ref.get("blocking_rate"))
                  and _blocking_rate(window_rows)
                  > float(baseline_ref["blocking_rate"]) + 0.15):
                action = "demote"
                reason = "blocking_rate"
            elif reason == "no_change":
                reason = "within_tolerance"
        elif reason == "no_change":
            reason = "insufficient_uses"
    elif status == "demoted":
        days = _cfg_int(cfg, "auto_skills_retire_after_days", 30)
        updated = parse_iso(entry.get("updated"))
        if entry.get("source") != "builtin" and updated is not None \
                and (_now() - updated) >= timedelta(days=days):
            action = "retire"
            reason = "inactive"
        else:
            reason = "awaiting_recovery"
    else:
        reason = "status_" + (status or "unknown")
    if action in ("promote", "demote", "retire"):
        new_status = {"promote": "active", "demote": "demoted",
                      "retire": "retired"}[action]
        try:
            _registry(cfg).set_status(key, new_status, reason,
                                      run_id=_run_id(run),
                                      approved=True if action == "promote" else None)
        except Exception as exc:
            return {"action": "hold", "reason": _err(exc)}
        record_decision(action, cfg, key=key, reason=reason,
                        run_id=_run_id(run), version=entry.get("version"))
        _trace(trace, "skill/" + action, key=key, reason=reason,
               status=new_status)
    return {"action": action, "reason": reason}


def _blocking_rate(rows):
    scored = [row for row in rows if _is_num(row.get("score"))]
    if not scored:
        return 0.0
    blocked = 0
    for row in scored:
        try:
            if int(row.get("blocking") or 0) >= 1:
                blocked += 1
        except Exception:
            pass
    return round(blocked / float(len(scored)), 4)


def _refresh_evidence(entry, repeated, lessons, cfg):
    samples = []
    for key in repeated[:6]:
        record = lessons.get(key) or {}
        sample = sanitise_evidence(record.get("sample") or "", 160)
        if sample:
            samples.append(sample)
    payload = {
        "cluster_id": "refresh-%s" % entry.get("key"),
        "task_type": entry.get("task_type") or "document",
        "runs": int((entry.get("stats") or {}).get("uses") or 0),
        "keywords": list(entry.get("keywords") or [])[:MAX_KEYWORDS],
        "repeated_lessons": samples,
        "repeated_count": len(repeated),
        "defect_kinds": [],
        "fix_kinds": [],
    }
    payload["signature_digest"] = "sha256:" + hashlib.sha256(
        _canonical(payload).encode("utf-8")).hexdigest()
    return payload


def _refresh_due(entry, cfg, repeated):
    if not repeated:
        return False, "no_repeated_lesson"
    refresh = entry.get("refresh") if isinstance(entry.get("refresh"), dict) else {}
    attempts = int(refresh.get("attempts") or 0)
    last = parse_iso(refresh.get("last_attempt"))
    cooldown_days = _cfg_float(cfg, "auto_skills_refresh_cooldown_days", 7.0)
    if last is not None:
        wait = timedelta(days=cooldown_days * (2 ** max(0, attempts)))
        if (_now() - last) < wait:
            return False, "cooldown"
    return True, "due"


def _save_refresh(registry, key, repeated=None, version=None, failed=False):
    try:
        data = registry.load()
        entry = None
        for item in (data.get("skills") or {}).values():
            if item.get("key") == key:
                entry = item
                break
        if entry is None:
            return
        refresh = entry.get("refresh")
        if not isinstance(refresh, dict):
            refresh = {"last_attempt": None, "attempts": 0, "last_version": None}
            entry["refresh"] = refresh
        refresh["last_attempt"] = now_iso()
        if failed:
            refresh["attempts"] = int(refresh.get("attempts") or 0) + 1
        else:
            refresh["attempts"] = 0
        if version is not None:
            refresh["last_version"] = version
        lessons = entry.get("lessons")
        if not failed and repeated and isinstance(lessons, dict):
            for digest in repeated:
                record = lessons.get(digest)
                if isinstance(record, dict):
                    record["n"] = 0
        registry.save()
    except Exception:
        pass


@_safe(lambda: {"action": "hold", "reason": "error"})
def maybe_refresh(key, cfg, run=None, trace=None, cancel_check=None,
                  envelope=None):
    auth_env = envelope or (run or {}).get("authority")
    if not key or _mode(cfg) == "off":
        return {"action": "skipped", "reason": "off"}
    if not _cfg_bool(cfg, "auto_skills_refresh", True):
        return {"action": "hold", "reason": "disabled"}
    registry = _registry(cfg)
    entry = registry.get(key)
    if not isinstance(entry, dict):
        return {"action": "skipped", "reason": "unknown"}
    if entry.get("status") not in ("seed", "active", "candidate"):
        return {"action": "hold", "reason": "status"}
    lessons = entry.get("lessons") if isinstance(entry.get("lessons"), dict) else {}
    threshold = _cfg_int(cfg, "auto_skills_refresh_after", 3)
    repeated = [digest for digest, record in lessons.items()
                if isinstance(record, dict)
                and int(record.get("n") or 0) >= threshold]
    due, why = _refresh_due(entry, cfg, repeated)
    if not due:
        return {"action": "hold", "reason": why}
    if callable(cancel_check):
        try:
            if cancel_check():
                return {"action": "hold", "reason": "cancelled"}
        except Exception:
            pass
    try:
        existing_content = registry.content(key)
    except Exception as exc:
        return {"action": "hold", "reason": _err(exc)}
    evidence = _refresh_evidence(entry, repeated, lessons, cfg)
    run_id = _run_id(run)
    try:
        from . import skill_author
        existing = _registry_entries(cfg)
        author_kwargs = {"cancel_check": cancel_check,
                         "previous": existing_content}
        if auth_env is not None:
            author_kwargs["envelope"] = auth_env
        refresh = skill_author.author_draft("refresh", evidence, existing,
                                            cfg=cfg, **author_kwargs)
    except Exception as exc:
        _save_refresh(registry, key, failed=True)
        record_decision("refresh_error", cfg, key=key, run_id=run_id,
                        error=_err(exc))
        return {"action": "error", "reason": _err(exc)}
    if not isinstance(refresh, dict) or refresh.get("action") == "skip":
        _save_refresh(registry, key, failed=True)
        record_decision("refresh_skip", cfg, key=key, run_id=run_id,
                        reason=sanitise_evidence(
                            (refresh or {}).get("reason") or "author_skip", 100))
        return {"action": "skipped", "reason": "author_skip"}
    try:
        gate_kwargs = {"cancel_check": cancel_check}
        if auth_env is not None:
            gate_kwargs["envelope"] = auth_env
        gate = skill_author.gate_refresh(refresh, evidence, existing_content, cfg,
                                         **gate_kwargs)
    except Exception as exc:
        _save_refresh(registry, key, failed=True)
        record_decision("refresh_error", cfg, key=key, run_id=run_id,
                        error=_err(exc))
        return {"action": "error", "reason": _err(exc)}
    if not isinstance(gate, dict) or not gate.get("accepted"):
        _save_refresh(registry, key, failed=True)
        review = (gate or {}).get("review") if isinstance(gate, dict) else {}
        record_decision("refresh_rejected", cfg, key=key, run_id=run_id,
                        review_score=(review or {}).get("score"))
        _trace(trace, "skill/refresh_rejected", key=key)
        return {"action": "rejected", "reason": "gate"}
    preview = None
    if isinstance(gate, dict):
        preview = gate.get("preview") or gate.get("rendered")
    if not preview:
        try:
            from . import skill_author
            preview = skill_author.refresh_preview(existing_content, refresh)
        except Exception as exc:
            _save_refresh(registry, key, failed=True)
            return {"action": "error", "reason": _err(exc)}
    review = gate.get("review") if isinstance(gate.get("review"), dict) else {}
    try:
        result = registry.add_version(key, preview, "refresh", review=review,
                                      run_id=run_id)
    except Exception as exc:
        _save_refresh(registry, key, failed=True)
        record_decision("refresh_error", cfg, key=key, run_id=run_id,
                        error=_err(exc))
        return {"action": "error", "reason": _err(exc)}
    version = result.get("version") if isinstance(result, dict) else None
    _save_refresh(registry, key, repeated=repeated, version=version)
    record_decision("refreshed", cfg, key=key, run_id=run_id, version=version,
                    review_score=review.get("score"))
    _trace(trace, "skill/refresh", key=key, version=version)
    record_change(run, "refreshed", key, version, "refresh",
                  review_score=review.get("score"))
    return {"action": "refreshed", "reason": "accepted", "version": version}


def _should_capture(run, match, cfg):
    skill = run.get("skill")
    explicit_none = (skill == "none"
                     or str(match.get("reason") or "") == "disabled")
    if explicit_none:
        return _cfg_bool(cfg, "auto_skills_capture_explicit_none", False)
    if skill not in (None, ""):
        return False
    if bool(match.get("unmatched")):
        return True
    return False


def _zero_summary():
    return {"captured": 0, "drafted": 0, "saved": 0, "merged": 0,
            "refreshed": 0, "promoted": 0, "demoted": 0, "retired": 0,
            "rolled_back": 0, "errors": 0}


@_safe(lambda: _zero_summary())
def skill_summary(run):
    counts = _zero_summary()
    if not isinstance(run, dict):
        return counts
    block = run.get("skill_auto")
    if not isinstance(block, dict):
        return counts
    capture = block.get("capture")
    if isinstance(capture, dict) and capture.get("captured"):
        counts["captured"] = 1
    changes = block.get("changes")
    if not isinstance(changes, list):
        changes = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        action = str(change.get("action") or "")
        if action in ("created", "merged"):
            counts["drafted"] += 1
            counts["saved"] += 1
            if action == "merged":
                counts["merged"] += 1
        elif action == "rejected":
            counts["drafted"] += 1
        elif action in ("refreshed", "refresh"):
            counts["refreshed"] += 1
        elif action in ("promoted", "promote"):
            counts["promoted"] += 1
        elif action in ("demoted", "demote"):
            counts["demoted"] += 1
        elif action in ("retired", "retire"):
            counts["retired"] += 1
        elif action in ("rolled_back", "rollback"):
            counts["rolled_back"] += 1
        elif action in ("error", "failed"):
            counts["errors"] += 1
    draft = block.get("draft")
    if isinstance(draft, dict) and not changes:
        action = str(draft.get("action") or "")
        if action in ("created", "merged"):
            counts["drafted"] += 1
            counts["saved"] += 1
            if action == "merged":
                counts["merged"] += 1
        elif action == "rejected":
            counts["drafted"] += 1
        elif action == "error":
            counts["errors"] += 1
    if block.get("error"):
        counts["errors"] += 1
    return counts


def _trace(trace, event, **fields):
    if trace is None:
        return
    phase, name = "skill", str(event or "event")
    if "/" in name:
        phase, name = name.split("/", 1)
    for method_name in ("event", "add", "emit"):
        try:
            method = getattr(trace, method_name, None)
        except Exception:
            method = None
        if not callable(method):
            continue
        try:
            method(phase, name, **fields)
            return
        except TypeError:
            try:
                method(name, **fields)
                return
            except Exception:
                return
        except Exception:
            return


def _summary_text(block):
    """One human-readable line for the run record and Markdown."""
    parts = []
    capture = block.get("capture") if isinstance(block.get("capture"), dict) else {}
    if capture.get("captured"):
        parts.append("captured cluster %s" % (capture.get("cluster_id") or "?"))
    draft = block.get("draft") if isinstance(block.get("draft"), dict) else {}
    if draft.get("attempted"):
        label = draft.get("decision") or draft.get("action") or "draft"
        parts.append("%s %s" % (label, draft.get("key") or "?"))
    for change in (block.get("changes") or [])[:4]:
        if isinstance(change, dict):
            parts.append("%s %s v%s" % (change.get("action"),
                                        change.get("key"),
                                        change.get("version")))
    for item in (block.get("pending") or [])[:2]:
        if isinstance(item, dict):
            parts.append("pending %s (%s)" % (item.get("cluster_id"),
                                              item.get("reason")))
    if block.get("error"):
        parts.append("error: %s" % block.get("error"))
    return "; ".join(parts) or "observed"


def _after_run(run, cfg, mode, history_runs, trace, cancel_check):
    block = {
        "mode": mode,
        "observed": True,
        "summary": "observed",
        "summary_counts": _zero_summary(),
        "capture": {"captured": False, "reason": "not_unmatched"},
        "draft": None,
        "changes": [],
        "pending": [],
        "error": None,
    }
    run["skill_auto"] = block
    match = run.get("skill_match")
    if not isinstance(match, dict):
        match = {}
    _trace(trace, "skill/match", key=match.get("key"),
           confidence=match.get("confidence"), status=match.get("status"),
           unmatched=bool(match.get("unmatched")))
    capture = {"captured": False, "reason": "not_unmatched"}
    touched = None
    if _should_capture(run, match, cfg):
        signature = build_signature(run.get("prompt") or "", run, match, cfg)
        if signature.get("simple") or signature.get("task_type") not in CANONICAL_TASK_TYPES:
            capture = {"captured": False, "reason": "skipped_simple"}
        else:
            capture = append_capture(signature, cfg)
            if capture.get("captured"):
                cluster = join_or_create_cluster(signature, cfg)
                if cluster:
                    touched = cluster
                    capture["cluster_id"] = cluster.get("id")
                    capture["severe"] = bool(signature.get("severe"))
                    capture["count"] = int(cluster.get("count") or 0)
                    capture["severe_count"] = int(cluster.get("severe_count") or 0)
                    capture["ready"] = cluster.get("status") == "ready"
                    _trace(trace, "skill/capture",
                           cluster_id=cluster.get("id"),
                           keywords=len(signature.get("keywords") or []),
                           severe=bool(signature.get("severe")))
                    _trace(trace, "skill/cluster", cluster_id=cluster.get("id"),
                           count=capture["count"],
                           severe_count=capture["severe_count"],
                           ready=capture["ready"])
    block["capture"] = capture
    key = run.get("skill")
    if key in (None, "", "none"):
        key = match.get("key") if not match.get("unmatched") else None
    entry = None
    if key:
        registry = _registry(cfg)
        entry = registry.get(key)
        if isinstance(entry, dict):
            baseline = baseline_for(entry.get("task_type")
                                    or run.get("task_type"), cfg, touched)
            try:
                registry.note_run(key, run, baseline=baseline)
            except Exception as exc:
                block["error"] = _err(exc)
            entry = registry.get(key) or entry
            outcome = promote_or_demote(entry, cfg, run=run, trace=trace)
            block["promotion"] = outcome
            if outcome.get("action") in ("promote", "demote", "retire"):
                record_change(run, outcome.get("action"), key,
                              entry.get("version"), outcome.get("reason"))
            if outcome.get("action") not in ("demote", "retire"):
                refresh = maybe_refresh(key, cfg, run=run, trace=trace,
                                        cancel_check=cancel_check)
                block["refresh"] = refresh
    pending = []
    draft = {"attempted": False, "action": "pending", "reason": "not_ready"}
    target = touched
    if target is None:
        target = select_ready_cluster(cfg)
    if isinstance(target, dict):
        ready, why = should_draft(target, cfg)
        status = str(run.get("status") or "")
        status_ok = status in ("ok", "ok_rescued", "ok_below_gate")
        allowed, cap_reason = _caps_ok(cfg)
        cost_ok = _cfg_float(cfg, "auto_skills_max_cost_usd", 1.0) != 0
        if ready and status_ok and allowed and cost_ok:
            draft = build_cluster(target.get("id"), cfg, run=run, trace=trace,
                                  cancel_check=cancel_check)
            if isinstance(draft, dict):
                draft["attempted"] = True
        else:
            if not ready:
                reason = why
            elif not status_ok:
                reason = "run_" + (status or "unknown")
            elif not allowed:
                reason = cap_reason
            else:
                reason = "cost_cap"
            pending.append({"cluster_id": target.get("id"),
                            "reason": reason,
                            "count": len(target.get("members") or [])})
    block["pending"] = pending
    block["draft"] = draft
    block["summary_counts"] = skill_summary(run)
    block["summary"] = _summary_text(block)
    return block


def after_run(run, cfg, history_runs=None, trace=None, cancel_check=None):
    """Single terminal funnel: capture, note stats, promote and maybe draft."""
    mode = _mode(cfg)
    try:
        if not isinstance(run, dict):
            return {}
        if mode == "off":
            return {}
        existing = run.get("skill_auto")
        if isinstance(existing, dict) and existing.get("observed"):
            return existing
        return _after_run(run, cfg, mode, history_runs, trace, cancel_check)
    except Exception as exc:
        block = {
            "mode": mode,
            "observed": True,
            "summary": "auto-skills error",
            "summary_counts": _zero_summary(),
            "capture": {"captured": False, "reason": "error"},
            "draft": None,
            "changes": [],
            "pending": [],
            "error": _err(exc),
        }
        block["summary"] = _summary_text(block)
        try:
            run["skill_auto"] = block
        except Exception:
            pass
        return block


@_safe(lambda: {})
def observe_run(run, cfg, history_runs=None):
    """Convenience alias for callers that only observe a terminal run."""
    return after_run(run, cfg, history_runs=history_runs)


@_safe(lambda: [])
def process_pending(cfg, trace=None, limit=1, cancel_check=None,
                    envelope=None):
    if _mode(cfg) == "off":
        return []
    changes = []
    seen = set()
    try:
        rounds = max(1, int(limit if limit is not None else 1))
    except Exception:
        rounds = 1
    for _index in range(rounds):
        if callable(cancel_check):
            try:
                if cancel_check():
                    break
            except Exception:
                break
        cluster = select_ready_cluster(cfg)
        if not cluster or cluster.get("id") in seen:
            break
        cluster_id = cluster.get("id")
        seen.add(cluster_id)
        result = build_cluster(cluster_id, cfg, run=None, trace=trace,
                               cancel_check=cancel_check, envelope=envelope)
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    changes.append(item)
        elif isinstance(result, dict):
            changes.append(result)
        if isinstance(result, dict) and result.get("action") == "error":
            break
    return changes


@_safe(lambda: {})
def score_entries(prompt, entries):
    """PLAN section 5.2 scoring for injectable entries only."""
    normalized = " ".join(str(prompt or "").lower().split())
    has_verb = any(_has_term(normalized, verb) for verb in TASK_VERBS)
    output = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or not _injectable(entry):
            continue
        key = str(entry.get("key") or "").strip().lower()
        if not key:
            continue
        score = 0
        found_keywords = []
        found_aliases = []
        counted = set()
        for keyword in entry.get("keywords") or []:
            token = str(keyword or "").strip().lower()
            if not token or token in counted:
                continue
            counted.add(token)
            if " " in token:
                if token in normalized:
                    score += 3
                    found_keywords.append(token)
            elif _has_term(normalized, token):
                score += 1
                found_keywords.append(token)
        for alias in entry.get("aliases") or []:
            token = str(alias or "").strip().lower()
            if not token or token in counted:
                continue
            counted.add(token)
            if " " in token:
                if token in normalized:
                    score += 2
                    found_aliases.append(token)
            elif _has_term(normalized, token):
                score += 2
                found_aliases.append(token)
        title_tokens = [str(item).lower() for item in
                        _TOKEN_RE.findall(str(entry.get("title") or ""))
                        if len(str(item)) >= 3]
        title_hits = []
        if has_verb:
            key_hit = bool(key) and _has_term(normalized, key)
            title_hits = [token for token in title_tokens
                          if _has_term(normalized, token)]
            if key_hit or title_hits:
                score += 1
        output[key] = {
            "score": int(score),
            "keywords": found_keywords[:12],
            "aliases": found_aliases[:8],
            "title_tokens": title_hits[:3],
        }
    return output


def _has_term(text, term):
    if not text or not term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _injectable(entry):
    status = entry.get("status")
    if not status:
        return entry.get("approved", True) is not False
    status = str(status)
    if status in ("demoted", "retired"):
        return False
    if status == "candidate":
        return bool(entry.get("approved"))
    return status in ("seed", "active")


@_safe(lambda: 0.0)
def confidence(best, second):
    """PLAN section 5.3 confidence from the top two scores."""
    try:
        best_value = float(best or 0.0)
        second_value = float(second or 0.0)
    except Exception:
        return 0.0
    if best_value <= 0:
        return 0.0
    strength = min(1.0, best_value / 8.0)
    if second_value <= 0:
        separation = 1.0
    else:
        separation = max(0.0, (best_value - second_value) / best_value)
    return round(0.5 * strength + 0.5 * separation, 3)
