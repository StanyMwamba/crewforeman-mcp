"""Skill library: task-type detection, checklist injection and lesson capture.

Skills are plain Markdown files in ``<skills_dir>/<key>.md``. They contain a
checklist plus a ``## Lessons learned`` section that grows as runs are rated or
quality reports are distilled. No personal data belongs in these files.

v3.7.0 keeps the original five skills as immutable seeds and adds an optional
registry-backed view behind ``cfg.auto_skills``. ``cfg=None`` and
``auto_skills="off"`` are the v3.6.0 behaviour: only ``SEED_SKILLS`` are
resolved, detection is the frozen legacy scorer, and no registry is constructed
or saved. The registry is imported lazily so this module never creates an import
cycle and the off path cannot touch registry files.
"""

import datetime as dt
import difflib
import json
import math
import os
import re
import tempfile
from pathlib import Path

from .store import append_run_md, find_run, save_run

MAX_LESSONS = 30
CONSOLIDATED_MAX = 10
INJECT_LESSONS = 8
SIMILARITY_RATIO = 0.97
JACCARD_RATIO = 0.90

SKILLS = [
    {
        "key": "poster",
        "title": "Poster / visual design",
        "aliases": ["visual", "visual-design", "visual_design", "design", "flyer", "print"],
        "keywords": ["poster", "flyer", "banner", "brochure", "thumbnail", "visual",
                     "typography", "print", "qr code", "layout", "logo", "cover"],
        "description": "Print and screen visuals: hierarchy, typography, spacing, print checks.",
    },
    {
        "key": "document",
        "title": "Document / letter",
        "aliases": ["letter", "doc", "docs", "documentation", "report", "essay"],
        "keywords": ["letter", "document", "report", "essay", "proposal", "memo",
                     "resume", "cover letter", "article", "manual", "chapter", "brief"],
        "description": "Letters, reports and long-form documents: structure, completeness, tone.",
    },
    {
        "key": "web",
        "title": "Web page",
        "aliases": ["webpage", "web-page", "html", "site", "website", "landing"],
        "keywords": ["website", "web page", "webpage", "landing page", "html", "css",
                     "responsive", "browser", "frontend", "site", "accessibility"],
        "description": "Web pages: responsive layout, accessibility, real content, browser checks.",
    },
    {
        "key": "code",
        "title": "Code / app",
        "aliases": ["app", "software", "script", "cli", "python", "program"],
        "keywords": ["code", "script", "cli", "app", "application", "python", "api",
                     "server", "bug", "refactor", "build", "test", "tests", "library",
                     "function", "package", "database", "backend"],
        "description": "Code and apps: build, tests, lint, error handling, runnable entry point.",
    },
    {
        "key": "data",
        "title": "Data analysis",
        "aliases": ["analysis", "data-analysis", "analytics", "stats"],
        "keywords": ["data", "dataset", "csv", "analysis", "analyze", "analyse", "chart",
                     "statistics", "dashboard", "spreadsheet", "excel", "metrics",
                     "regression", "visualization", "visualisation"],
        "description": "Data work: reproducible pipeline, validation, uncertainty, clear charts.",
    },
]

# The five built-in skills are seeds; _BY_KEY is kept as a seed-only alias for
# v3.6.0 callers. It must never be treated as the live registry.
SEED_SKILLS = SKILLS
_SEED_BY_KEY = {item["key"]: item for item in SKILLS}
_BY_KEY = _SEED_BY_KEY

_SEED_TASK_TYPES = {
    "poster": "creative",
    "document": "document",
    "web": "creative",
    "code": "code",
    "data": "research",
}

_TASK_VERBS = (
    "build", "create", "design", "write", "draft", "make", "produce", "generate",
    "prepare", "plan", "analyze", "analyse", "research", "fix", "improve", "convert",
    "refactor", "test", "review", "edit", "rewrite", "render", "publish", "send",
    "calculate", "visualize", "visualise", "compare", "audit", "implement", "develop",
)
_TASK_VERB_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(v) for v in _TASK_VERBS))
_MODES = ("auto", "propose", "off")


def _empty_stats():
    return {
        "uses": 0, "score_count": 0, "score_sum": 0.0, "blocking_total": 0,
        "last_score": None, "last_run": None, "baseline_at_use": None,
        "baseline_ref": None, "wins": 0, "losses": 0, "consecutive_bad": 0,
        "window": [], "recent_runs": [],
    }


def _seed_entry(item):
    entry = {
        "key": item["key"],
        "title": item["title"],
        "description": item.get("description", ""),
        "aliases": list(item.get("aliases", [])),
        "keywords": list(item.get("keywords", [])),
    }
    entry.update({
        "status": "seed",
        "source": "builtin",
        "approved": True,
        "version": 1,
        "task_type": _SEED_TASK_TYPES.get(item["key"], "document"),
        "stats": _empty_stats(),
        "review": {},
        "cluster_id": None,
    })
    return entry


def _seed_entries():
    return [_seed_entry(item) for item in SKILLS]


def _mode(cfg):
    """Return auto/propose/off. Bare test configs default to auto."""
    if cfg is None:
        return "off"
    value = getattr(cfg, "auto_skills", "auto")
    if isinstance(value, str):
        value = value.strip().lower()
        if value in _MODES:
            return value
    return "auto"


def _cfg_float(cfg, name, default):
    value = getattr(cfg, name, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(number) or math.isinf(number):
        return float(default)
    return number


def _cfg_confidence(cfg):
    value = _cfg_float(cfg, "auto_skills_min_confidence", 0.55)
    return min(1.0, max(0.0, value))


def _cfg_min_score(cfg):
    return max(0.0, _cfg_float(cfg, "auto_skills_min_score", 2.0))


def _cfg_tie_margin(cfg):
    return max(0.0, _cfg_float(cfg, "auto_skills_tie_margin", 0.10))


def _registry(cfg):
    """Import and construct the registry lazily (never at module import)."""
    from .skill_registry import SkillRegistry
    return SkillRegistry(cfg)


def _registry_path(registry, key):
    try:
        directory = getattr(registry, "directory", None)
    except Exception:
        directory = None
    if directory:
        try:
            return Path(directory) / (str(key) + ".md")
        except Exception:
            pass
    return None


def _all_names(item):
    return [item["key"], item["key"] + ".md"] + list(item.get("aliases", []))


def _normalise_name_text(name):
    raw = (name or "").strip().lower()
    if raw.endswith(".md"):
        raw = raw[:-3]
    return raw.replace(" ", "-").replace("_", "-")


def _title_slug(title):
    return re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")


def _seed_resolve(raw):
    if not raw:
        return None
    for item in SKILLS:
        names = _all_names(item)
        names.append(_title_slug(item.get("title")))
        for candidate in names:
            if not candidate:
                continue
            candidate = str(candidate).lower()
            if raw == candidate or raw == candidate.replace("_", "-"):
                return item["key"]
    return None


def normalise_skill_name(name, cfg=None):
    """Resolve a key, ``.md`` name, alias or title slug to a canonical key.

    With ``cfg=None`` only the five seeds are consulted and no disk is touched.
    In ``off`` mode the same seed-only rule applies. In ``auto``/``propose`` the
    registry is tried first, with a seed fallback.
    """
    raw = _normalise_name_text(name)
    if not raw:
        return None
    if cfg is not None and _mode(cfg) in ("auto", "propose"):
        try:
            registry = _registry(cfg)
            for candidate in (name, raw):
                try:
                    key = registry.resolve_key(candidate)
                except Exception:
                    key = None
                if key:
                    return key
        except Exception:
            pass
    return _seed_resolve(raw)


def skill_meta(key, cfg=None):
    """Return the old skill metadata shape; add status/version/task_type with cfg."""
    if cfg is None:
        return _SEED_BY_KEY.get(key)
    if _mode(cfg) in ("auto", "propose"):
        try:
            entry = _registry(cfg).get(key)
            if entry:
                result = dict(entry)
                result["aliases"] = list(entry.get("aliases") or [])
                result["keywords"] = list(entry.get("keywords") or [])
                result.setdefault("description", "")
                result.setdefault("status", "seed")
                result.setdefault("version", 1)
                result.setdefault("task_type", "document")
                return result
        except Exception:
            pass
    seed = _SEED_BY_KEY.get(key)
    if not seed:
        return None
    result = _seed_entry(seed)
    return result


def _installed_skill_dirs():
    """Where a wheel/pipx/uvx install may put the shipped skill files."""
    import sys
    import sysconfig
    from .config import PACKAGE_DIR, ROOT_DIR
    dirs = [ROOT_DIR / "skills", PACKAGE_DIR / "skills"]
    try:
        dirs.append(Path(sysconfig.get_path("data")) / "share" /
                    "crewforeman-mcp" / "skills")
        dirs.append(Path(sysconfig.get_path("data")) / "share" /
                    "deepseek-harness-mcp" / "skills")
    except Exception:
        pass
    dirs.append(Path(sys.prefix) / "share" / "crewforeman-mcp" / "skills")
    dirs.append(Path(sys.prefix) / "share" / "deepseek-harness-mcp" / "skills")
    return dirs


def resolved_skills_dir(cfg):
    """Skills directory: config, checkout, or a writable installed copy.

    A system-wide install puts the read-only skill sources under
    ``<data>/share/deepseek-harness-mcp/skills``. Lessons need a writable
    directory, so the first use seeds ``<state_dir>/skills`` from those files.
    """
    if getattr(cfg, "skills_dir", ""):
        return Path(cfg.skills_dir)
    from .config import ROOT_DIR
    checkout = ROOT_DIR / "skills"
    if checkout.is_dir():
        return checkout
    for candidate in _installed_skill_dirs():
        if candidate.is_dir() and os.access(str(candidate), os.W_OK):
            return candidate
    state_dir = Path(str(getattr(cfg, "state_dir", "~/DeepSeek-MCP/state"))).expanduser()
    target = state_dir / "skills"
    if not target.is_dir():
        try:
            target.mkdir(parents=True, exist_ok=True)
            for candidate in _installed_skill_dirs():
                if not candidate.is_dir():
                    continue
                for source in candidate.glob("*.md"):
                    destination = target / source.name
                    if not destination.exists():
                        destination.write_text(
                            source.read_text(encoding="utf-8"), encoding="utf-8")
                break
        except OSError:
            pass
    return target


def skill_path(key, cfg):
    return resolved_skills_dir(cfg) / (key + ".md")


_FRONT_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _fallback_split_front_matter(text):
    match = _FRONT_RE.match(str(text or ""))
    if not match:
        return None, text
    meta = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if value.startswith("[") or value.startswith("{"):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        elif value in ("true", "false"):
            value = value == "true"
        meta[key] = value
    return meta, text[match.end():]


def split_front_matter(text):
    """Backward-compatible re-export of the registry front-matter splitter."""
    try:
        from .skill_registry import split_front_matter as _impl
        return _impl(text)
    except Exception:
        return _fallback_split_front_matter(text)


def _fallback_render_front_matter(meta, body):
    lines = ["---"]
    for key, value in (meta or {}).items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        lines.append("%s: %s" % (key, value))
    lines.append("---")
    return "\n".join(lines) + "\n\n" + str(body or "")


def render_front_matter(meta, body):
    """Backward-compatible re-export of the registry front-matter renderer."""
    try:
        from .skill_registry import render_front_matter as _impl
        return _impl(meta, body)
    except Exception:
        return _fallback_render_front_matter(meta, body)


def _strip_front_matter(text):
    _meta, body = split_front_matter(text)
    return body if body is not None else (text or "")


def load_skill(key, cfg, prompt=None):
    """Load one skill checklist, optionally selecting the injected lessons.

    In auto/propose the registry supplies the current content and metadata; the
    five seed files are only a fallback when the registry is unavailable. In
    off mode only the five seed keys are read.
    """
    mode = _mode(cfg) if cfg is not None else "off"
    if cfg is not None and mode in ("auto", "propose"):
        try:
            registry = _registry(cfg)
            entry = registry.get(key)
            if entry:
                content = registry.content(key) or ""
                content = _strip_front_matter(content)
                path = _registry_path(registry, key) or skill_path(key, cfg)
                if prompt is not None and content:
                    content = with_selected_lessons(content, prompt, limit=INJECT_LESSONS)
                return {
                    "key": key,
                    "title": entry.get("title") or key,
                    "description": entry.get("description") or "",
                    "path": str(path),
                    "content": content,
                    "status": entry.get("status") or "seed",
                    "version": entry.get("version", 1),
                    "task_type": entry.get("task_type") or "document",
                }
        except Exception:
            pass
    meta = _SEED_BY_KEY.get(key)
    if not meta:
        return None
    path = skill_path(key, cfg)
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        content = ""
    if prompt is not None and content:
        content = with_selected_lessons(content, prompt, limit=INJECT_LESSONS)
    return {"key": key, "title": meta["title"], "description": meta["description"],
            "path": str(path), "content": content}


def detect_skill_legacy(prompt):
    """Frozen v3.6.0 detector: no confidence floor, ties by SEED_SKILLS order."""
    text = (prompt or "").lower()
    scores = {}
    for item in SKILLS:
        score = 0
        for keyword in item["keywords"]:
            if " " in keyword:
                if keyword in text:
                    score += 3
            elif re.search(r"\b%s\b" % re.escape(keyword), text):
                score += 1
        if score:
            scores[item["key"]] = score
    if not scores:
        return None
    best = max(scores.values())
    for item in SKILLS:  # stable priority order on ties
        if scores.get(item["key"], 0) == best:
            return item["key"]
    return None


def _term_norm(term):
    return re.sub(r"\s+", " ", str(term or "").lower()).strip()


def _term_present(term, text, flat_text=None):
    term = _term_norm(term)
    if not term:
        return False
    if " " in term:
        if term in text:
            return True
        return bool(flat_text is not None and term in flat_text)
    return re.search(r"\b%s\b" % re.escape(term), text) is not None


def _title_tokens(title):
    tokens = re.findall(r"[a-z0-9]+", str(title or "").lower())
    return [token for token in tokens if len(token) >= 3]


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        text = _term_norm(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(str(item).strip().lower())
    return out


def _score_entries(prompt, entries):
    """Score injectable entries with PLAN section 5.2 rules.

    Returns ``{key: {score, keywords, aliases, title_tokens}}``. This local copy
    is deliberately deterministic and does not need skill_auto to be importable.
    """
    text = re.sub(r"\s+", " ", str(prompt or "").lower()).strip()
    flat_text = re.sub(r"[-_/]+", " ", text)
    has_verb = bool(_TASK_VERB_RE.search(text))
    result = {}
    for item in entries:
        key = str(item.get("key") or "").strip().lower()
        if not key:
            continue
        score = 0
        counted = set()
        keyword_hits, alias_hits = [], []
        keywords = _dedupe(item.get("keywords") or [])
        keyword_norms = {_term_norm(word) for word in keywords}
        for keyword in keywords:
            norm = _term_norm(keyword)
            if _term_present(keyword, text, flat_text):
                score += 3 if " " in norm else 1
                counted.add(norm)
                keyword_hits.append(keyword)
        for alias in _dedupe(item.get("aliases") or []):
            norm = _term_norm(alias)
            if not norm or norm in counted:
                continue
            # An alias that is also a keyword is not counted twice; the keyword
            # pass already covers the same word/phrase.
            if norm in keyword_norms:
                continue
            if _term_present(alias, text, flat_text):
                score += 2
                counted.add(norm)
                alias_hits.append(alias)
        key_hit = _term_present(key, text, flat_text)
        title_hits = [token for token in _title_tokens(item.get("title"))
                      if _term_present(token, text, flat_text)]
        if has_verb and (key_hit or title_hits):
            score += 1
        result[key] = {
            "score": score,
            "keywords": keyword_hits,
            "aliases": alias_hits,
            "title_tokens": title_hits,
        }
    return result


def _confidence(best, second):
    if best <= 0:
        return 0.0
    strength = min(1.0, float(best) / 8.0)
    separation = 1.0 if second <= 0 else max(0.0, (float(best) - float(second)) / float(best))
    return round(0.5 * strength + 0.5 * separation, 3)


def _status_rank(entry):
    status = str(entry.get("status") or "seed").lower()
    return 0 if status in ("seed", "active") else 1


def _detection_entries(cfg):
    """Injectable entries: seed/active, or approved candidates; no disk if cfg=None."""
    if cfg is None or _mode(cfg) == "off":
        return _seed_entries()
    entries = []
    try:
        entries = _registry(cfg).entries()
    except Exception:
        entries = []
    if not entries:
        entries = _seed_entries()
    injectable = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "seed").lower()
        if status in ("demoted", "retired"):
            continue
        if status not in ("seed", "active", "candidate"):
            continue
        if status == "candidate" and not entry.get("approved"):
            continue
        injectable.append(entry)
    return injectable


def _unmatched_result(reason, detail, cfg, prompt):
    return {
        "key": None,
        "status": "unmatched",
        "approved": False,
        "reason": reason,
        "confidence": 0.0,
        "raw_score": 0.0,
        "second_score": 0.0,
        "margin": 0.0,
        "runner_up": None,
        "scores": {},
        "signals": {"keywords": [], "aliases": [], "title_tokens": []},
        "candidates": [],
        "matched_terms": [],
        "threshold": _cfg_confidence(cfg) if cfg is not None else 0.55,
        "unmatched": True,
        "prompt_chars": len(str(prompt or "")),
        "detail": detail,
    }


def detect_skill_match(prompt, cfg=None):
    """Return the 17-key detection result from PLAN sections 5.1-5.3.

    ``cfg=None`` scores the five seeds in memory and never constructs or touches
    the registry. With a real config, registry entries are used when readable;
    any failure falls back to the seeds. Demoted/retired entries and unapproved
    candidates are excluded. The result never contains prompt text.
    """
    if cfg is not None and _mode(cfg) == "off":
        return _unmatched_result("disabled", "auto_skills is off", cfg, prompt)
    min_score = _cfg_min_score(cfg) if cfg is not None else 2.0
    min_confidence = _cfg_confidence(cfg) if cfg is not None else 0.55
    tie_margin = _cfg_tie_margin(cfg) if cfg is not None else 0.10
    prompt_text = prompt if isinstance(prompt, str) else str(prompt or "")
    try:
        entries = _detection_entries(cfg)
        scores = _score_entries(prompt_text, entries)
    except Exception:
        entries = _seed_entries()
        try:
            scores = _score_entries(prompt_text, entries)
        except Exception:
            return _unmatched_result("no_signals", "detection unavailable", cfg, prompt)

    ranked = sorted(
        [entry for entry in entries if scores.get(str(entry.get("key")), {}).get("score", 0) > 0],
        key=lambda entry: (
            -scores[str(entry.get("key"))]["score"],
            _status_rank(entry),
            -len(str(entry.get("key"))),
            str(entry.get("key")),
        ),
    )
    if not ranked:
        return _unmatched_result("no_signals", "no skill signals", cfg, prompt)

    best_entry = ranked[0]
    best_key = str(best_entry.get("key"))
    best = int(scores[best_key]["score"])
    runner_entry = ranked[1] if len(ranked) > 1 else None
    runner_key = str(runner_entry.get("key")) if runner_entry else None
    second = int(scores[runner_key]["score"]) if runner_key else 0
    margin = (float(best) - float(second)) / float(best) if best else 0.0
    confidence = _confidence(best, second)

    if best < min_score:
        reason = "below_threshold"
    elif best >= 2 and margin < tie_margin:
        reason = "ambiguous"
    elif confidence < min_confidence:
        reason = "below_threshold"
    else:
        reason = "matched"

    key = best_key if reason == "matched" else None
    if reason == "matched":
        detail = "%s scored %s" % (best_key, best)
        detail += (" (next %s %s); confidence %s" % (runner_key, second, confidence)
                   if runner_key else "; confidence %s" % confidence)
    elif reason == "ambiguous":
        detail = "%s and %s tied at %s; ambiguous" % (best_key, runner_key or "another skill", best)
    else:
        detail = "%s scored %s" % (best_key, best)
        detail += (" (next %s %s)" % (runner_key, second)) if runner_key else ""
        detail += "; confidence %s; below threshold" % confidence

    display = sorted(
        [entry for entry in entries if scores.get(str(entry.get("key")), {}).get("score", 0) > 0],
        key=lambda entry: (
            -scores[str(entry.get("key"))]["score"],
            len(str(entry.get("key"))),
            str(entry.get("key")),
        ),
    )[:3]
    candidates = []
    for index, entry in enumerate(display):
        candidate_key = str(entry.get("key"))
        candidate_score = int(scores[candidate_key]["score"])
        next_score = (int(scores[str(display[index + 1].get("key"))]["score"])
                      if index + 1 < len(display) else 0)
        candidates.append({"key": candidate_key,
                           "confidence": _confidence(candidate_score, next_score)})

    details = scores.get(best_key, {}) if key else {}
    signals = {
        "keywords": list(details.get("keywords") or []) if key else [],
        "aliases": list(details.get("aliases") or []) if key else [],
        "title_tokens": list(details.get("title_tokens") or []) if key else [],
    }
    matched_terms = []
    seen_terms = set()
    for term in signals["keywords"] + signals["aliases"] + signals["title_tokens"]:
        norm = _term_norm(term)
        if norm and norm not in seen_terms:
            seen_terms.add(norm)
            matched_terms.append(term)

    return {
        "key": key,
        "status": "matched" if key else "unmatched",
        "approved": bool(best_entry.get("approved", True)) if key else False,
        "reason": reason,
        "confidence": confidence,
        "raw_score": float(best),
        "second_score": float(second),
        "margin": round(margin, 3),
        "runner_up": runner_key,
        "scores": {str(entry.get("key")): int(scores[str(entry.get("key"))]["score"])
                   for entry in entries
                   if str(entry.get("key")) in scores
                   and int(scores[str(entry.get("key"))]["score"]) > 0},
        "signals": signals,
        "candidates": candidates,
        "matched_terms": matched_terms,
        "threshold": min_confidence,
        "unmatched": key is None,
        "prompt_chars": len(prompt_text),
        "detail": detail,
    }


def detect_skill(prompt, cfg=None):
    """Return the best matching skill key for a task, or ``None``.

    ``cfg=None`` and ``off`` use the frozen v3.6.0 detector only.
    """
    if cfg is None or _mode(cfg) == "off":
        return detect_skill_legacy(prompt)
    try:
        result = detect_skill_match(prompt, cfg)
    except Exception:
        return detect_skill_legacy(prompt)
    if result.get("unmatched") or not result.get("key"):
        return None
    if not result.get("approved"):
        return None
    return result["key"]


def _row_path(registry, key, cfg):
    path = _registry_path(registry, key) if registry is not None else None
    if path is None and cfg is not None:
        try:
            path = skill_path(key, cfg)
        except Exception:
            path = None
    return path


def list_skills(cfg, status=None):
    """List skill rows; add status/version/stats/source/approved/review."""
    wanted = None if status in (None, "", "all") else str(status)
    registry = None
    entries = []
    if cfg is not None and _mode(cfg) in ("auto", "propose"):
        try:
            registry = _registry(cfg)
            entries = registry.entries(wanted)
        except Exception:
            registry = None
            entries = []
    if not entries:
        seed_views = _seed_entries()
        if wanted and wanted != "seed":
            seed_views = []
        entries = seed_views

    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        if not key:
            continue
        path = _row_path(registry, key, cfg)
        try:
            exists = bool(path and path.is_file())
        except Exception:
            exists = False
        stats = entry.get("stats")
        rows.append({
            "key": key,
            "title": entry.get("title") or key,
            "description": entry.get("description") or "",
            "path": str(path) if path is not None else "",
            "exists": exists,
            "aliases": list(entry.get("aliases") or []),
            "status": entry.get("status") or "seed",
            "version": entry.get("version", 1),
            "stats": dict(stats) if isinstance(stats, dict) else _empty_stats(),
            "source": entry.get("source") or "builtin",
            "approved": bool(entry.get("approved", True)),
            "review": dict(entry.get("review") or {}) if isinstance(entry.get("review"), dict) else {},
            "task_type": entry.get("task_type") or "document",
            "cluster_id": entry.get("cluster_id"),
        })
    return rows


def format_skill_list(cfg, status=None, as_json=False):
    rows = list_skills(cfg, status=status)
    if as_json:
        return json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    try:
        directory = str(resolved_skills_dir(cfg))
    except Exception:
        directory = "(unavailable)"
    lines = ["Skills in %s:" % directory]
    if status not in (None, "", "all"):
        lines.append("status: %s" % status)
    for row in rows:
        lines.append("- %s | %s | %s | %s" % (
            row["key"], row["title"], "present" if row["exists"] else "missing", row["path"]))
    return "\n".join(lines)


def _known_keys(cfg):
    keys = []
    if cfg is not None and _mode(cfg) in ("auto", "propose"):
        try:
            keys = [str(entry.get("key")) for entry in _registry(cfg).entries(None)
                    if entry.get("key")]
        except Exception:
            keys = []
    if not keys:
        keys = [item["key"] for item in SKILLS]
    return keys[:20]


def _meta_prefix(entry, key):
    stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else {}
    score_count = stats.get("score_count") or 0
    mean = None
    try:
        if score_count:
            mean = round(float(stats.get("score_sum") or 0.0) / float(score_count), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        mean = None
    lines = [
        "<!-- skill-meta",
        "key: %s" % key,
        "title: %s" % (entry.get("title") or key),
        "status: %s%s" % (entry.get("status") or "seed",
                          " (approved)" if entry.get("approved") else ""),
        "version: %s" % entry.get("version", 1),
        "task_type: %s" % (entry.get("task_type") or "document"),
        "source: %s" % (entry.get("source") or "builtin"),
        "uses: %s" % (stats.get("uses") or 0),
        "score_count: %s" % score_count,
        "mean_score: %s" % ("n/a" if mean is None else mean),
        "-->",
    ]
    return "\n".join(lines) + "\n\n"


def read_skill(name, cfg, version=None, meta=False):
    """Read a skill body, optionally a historical version and metadata prefix."""
    key = normalise_skill_name(name, cfg=cfg)
    if not key:
        raise RuntimeError("Unknown skill %r. Known skills: %s" % (
            name, ", ".join(_known_keys(cfg))))

    if cfg is not None and _mode(cfg) in ("auto", "propose"):
        try:
            registry = _registry(cfg)
            entry = registry.get(key)
            if entry:
                try:
                    content = registry.content(key, version)
                except Exception as exc:
                    raise RuntimeError(str(exc))
                content = _strip_front_matter(content or "")
                if not content:
                    raise RuntimeError("Skill file for %r is missing: %s" % (
                        key, _registry_path(registry, key) or skill_path(key, cfg)))
                if meta:
                    content = _meta_prefix(entry, key) + content
                return content
        except RuntimeError:
            raise
        except Exception:
            if version is not None:
                raise RuntimeError("Skill registry is unavailable; version history cannot be read.")

    if version is not None:
        raise RuntimeError("Version history is unavailable when auto_skills is off.")
    skill = load_skill(key, cfg)
    if not skill or not skill["content"]:
        path = skill["path"] if skill else str(skill_path(key, cfg))
        raise RuntimeError("Skill file for %r is missing: %s" % (key, path))
    if meta:
        return _meta_prefix(skill, key) + skill["content"]
    return skill["content"]


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SECRET_RE = re.compile(r"\b(?:sk|api|key|token)[-_][A-Za-z0-9_-]{6,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE)
_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s]+")
_WIN_HOME_RE = re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", re.IGNORECASE)
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')]+", re.IGNORECASE)
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def _redact(text):
    cleaned = str(text or "").replace("\r", " ").replace("\n", " ")
    cleaned = _URL_RE.sub("[url]", cleaned)
    cleaned = _WIN_HOME_RE.sub("~", cleaned)
    cleaned = _HOME_RE.sub("~", cleaned)
    cleaned = _EMAIL_RE.sub("[email]", cleaned)
    cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
    cleaned = _SECRET_RE.sub("[redacted]", cleaned)
    cleaned = _LONG_HEX_RE.sub("[redacted]", cleaned)
    return cleaned


def sanitise_lesson(text, limit=500):
    """Remove secrets, URLs and home paths from saved lessons."""
    cleaned = _redact(text).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "..."
    return cleaned


def sanitise_evidence(text, limit=200):
    """Strict, bounded evidence redaction used by capture (PLAN section 6.2)."""
    cleaned = _redact(text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if limit is not None and limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned


_LESSON_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s*")
_LESSON_TAG_RE = re.compile(r"\[(?:auto|rating=[^\]]+|run=[^\]]+)\]\s*")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_lesson(line):
    text = str(line or "").strip()
    text = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", text)
    text = _LESSON_DATE_RE.sub("", text)
    text = _LESSON_TAG_RE.sub("", text)
    return text.strip()


def _lesson_tokens(text):
    return set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower()))


def _normalise_lesson(line):
    text = _strip_lesson(line).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similar_lesson(a, b):
    na, nb = _normalise_lesson(a), _normalise_lesson(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= SIMILARITY_RATIO:
        return True
    ta, tb = _lesson_tokens(na), _lesson_tokens(nb)
    if ta and tb:
        jaccard = len(ta & tb) / float(len(ta | tb))
        if jaccard >= JACCARD_RATIO:
            return True
    return False


def _parse_lessons(content):
    """Split a skill file into (prefix, active lessons, consolidated lessons)."""
    text = content or ""
    match = re.search(r"^##\s+Lessons learned\s*$", text, re.MULTILINE | re.IGNORECASE)
    if not match:
        return text.rstrip(), [], []
    prefix = text[:match.start()].rstrip()
    tail = _COMMENT_RE.sub("", text[match.end():])
    split = re.search(r"^###\s+Consolidated lessons\s*$", tail,
                      re.MULTILINE | re.IGNORECASE)
    active_text = tail[:split.start()] if split else tail
    consolidated_text = tail[split.end():] if split else ""
    active = [line.strip() for line in active_text.splitlines()
              if re.match(r"^\s*[-*+]\s+\S", line)]
    consolidated = [line.strip() for line in consolidated_text.splitlines()
                    if re.match(r"^\s*[-*+]\s+\S", line)]
    return prefix, active, consolidated


def _bullet(line):
    text = _strip_lesson(line)
    return "- " + text if text else ""


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _render_skill(prefix, active, consolidated):
    parts = [prefix.rstrip(), "", "## Lessons learned", ""]
    if active:
        parts.extend(active)
    else:
        parts.append("<!-- Add only verified, reusable lessons as symptom -> cause -> check "
                     "to add. Keep them short and free of names, emails, tokens and personal "
                     "paths. -->")
    if consolidated:
        parts.extend(["", "### Consolidated lessons", ""])
        parts.extend(consolidated[:CONSOLIDATED_MAX])
    return "\n".join(parts).rstrip() + "\n"


def _condense_lessons(items):
    """Keep at most 10 consolidated bullets, summarising older ones."""
    unique = []
    dated = []
    for line in items:
        text = _strip_lesson(line)
        if not text:
            continue
        if any(_similar_lesson(text, existing) for existing, _stamp in unique):
            continue
        match = re.match(r"^\s*-?\s*(\d{4}-\d{2}-\d{2})", str(line or ""))
        stamp = match.group(1) if match else "0000-00-00"
        unique.append((text, stamp))
        dated.append((stamp, text))
    if len(unique) <= CONSOLIDATED_MAX:
        return ["- " + text for text, _stamp in unique]

    dated.sort(key=lambda item: item[0])
    ordered = [text for _stamp, text in dated]
    keep = ordered[-(CONSOLIDATED_MAX - 1):]
    older = ordered[:-(CONSOLIDATED_MAX - 1)]
    summary = "; ".join(older)
    if len(summary) > 300:
        summary = summary[:297].rstrip() + "..."
    bullets = ["- Earlier: " + summary] if summary else []
    bullets.extend("- " + text for text in keep)
    return bullets[:CONSOLIDATED_MAX]


def select_lessons(content, prompt=None, limit=INJECT_LESSONS):
    """Return at most ``limit`` active lessons, most relevant/recent first."""
    _prefix, active, _consolidated = _parse_lessons(content)
    if not active:
        return []
    limit = max(0, int(limit))
    if limit == 0:
        return []
    if len(active) <= limit:
        return active
    if not prompt:
        return active[-limit:]
    prompt_tokens = _lesson_tokens(prompt)
    scored = []
    for index, line in enumerate(active):
        tokens = _lesson_tokens(line)
        relevance = len(prompt_tokens & tokens) if prompt_tokens else 0
        date_match = re.match(r"^-\s+(\d{4}-\d{2}-\d{2})", line)
        recency = date_match.group(1) if date_match else "0000-00-00"
        scored.append((relevance, recency, index, line))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    chosen = sorted(scored[:limit], key=lambda item: (item[1], item[2]))
    return [item[3] for item in chosen]


def with_selected_lessons(content, prompt=None, limit=INJECT_LESSONS):
    """Replace the lessons section with the few lessons injected into a task."""
    prefix, _active, _consolidated = _parse_lessons(content)
    selected = select_lessons(content, prompt, limit)
    parts = [prefix.rstrip(), "",
             "## Lessons learned (most relevant/recent, max %d)" % int(limit), ""]
    if selected:
        parts.extend(selected)
    else:
        parts.append("_No verified lessons yet._")
    return "\n".join(parts).rstrip() + "\n"


def _lesson_line(lesson, run_id=None, rating=None):
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    tag = "[rating=%s]" % rating if rating else "[auto]"
    if run_id:
        tag += " [run=%s]" % run_id
    cleaned = sanitise_lesson(lesson)
    if not cleaned:
        return None, None
    return cleaned, "- %s %s %s" % (stamp, tag, cleaned)


def _append_lesson_direct(key, lesson, cfg, run_id=None, rating=None):
    """Frozen v3.6.0 path: one direct <key>.md write, no registry calls."""
    key = normalise_skill_name(key) or "document"
    path = skill_path(key, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        meta = _BY_KEY[key]
        _atomic_write(path, "# %s\n\n## Lessons learned\n" % meta["title"])
    content = path.read_text(encoding="utf-8")
    prefix, active, consolidated = _parse_lessons(content)
    if not re.search(r"^##\s+Lessons learned\s*$", content, re.MULTILINE | re.IGNORECASE):
        if prefix:
            prefix = prefix.rstrip() + "\n\n## Lessons learned"
        else:
            prefix = "## Lessons learned"
    cleaned, line = _lesson_line(lesson, run_id, rating)
    if not cleaned:
        return str(path)
    if any(_similar_lesson(line, existing) for existing in active + consolidated):
        return str(path)
    active.append(line)
    if len(active) > MAX_LESSONS:
        overflow = active[:-MAX_LESSONS]
        active = active[-MAX_LESSONS:]
        consolidated = _condense_lessons(consolidated + overflow)
    _atomic_write(path, _render_skill(prefix, active, consolidated))
    return str(path)


def _append_lesson_registry(key, lesson, cfg, run_id=None, rating=None):
    """Version-first lesson append through SkillRegistry (auto/propose)."""
    registry = _registry(cfg)
    key = normalise_skill_name(key, cfg=cfg) or "document"
    entry = registry.get(key)
    if not entry:
        raise KeyError("unknown skill %s" % key)
    path = _registry_path(registry, key) or skill_path(key, cfg)
    try:
        content = registry.content(key) or ""
    except Exception:
        content = ""
    prefix, active, consolidated = _parse_lessons(content)
    if not re.search(r"^##\s+Lessons learned\s*$", content, re.MULTILINE | re.IGNORECASE):
        if prefix:
            prefix = prefix.rstrip() + "\n\n## Lessons learned"
        else:
            prefix = "## Lessons learned"
    cleaned, line = _lesson_line(lesson, run_id, rating)
    if not cleaned:
        return str(path)
    if any(_similar_lesson(line, existing) for existing in active + consolidated):
        # A repeated lesson still feeds the registry's lesson counter.
        registry.note_lesson(key, cleaned, run_id=run_id)
        return str(path)
    active.append(line)
    if len(active) > MAX_LESSONS:
        overflow = active[:-MAX_LESSONS]
        active = active[-MAX_LESSONS:]
        consolidated = _condense_lessons(consolidated + overflow)
    new_content = _render_skill(prefix, active, consolidated)
    registry.add_version(key, new_content, reason="lesson", run_id=run_id)
    registry.note_lesson(key, cleaned, run_id=run_id)
    return str(path)


def append_lesson(key, lesson, cfg, run_id=None, rating=None):
    """Append a distilled lesson, deduplicate, and cap the lesson file.

    off: the v3.6.0 direct ``_atomic_write`` path. auto/propose: the registry
    version-first path, with a direct seed fallback if the registry cannot be
    used (migration/read-only failure must not fail the run).
    """
    if cfg is not None and _mode(cfg) in ("auto", "propose"):
        try:
            return _append_lesson_registry(key, lesson, cfg, run_id=run_id, rating=rating)
        except Exception:
            pass
    return _append_lesson_direct(key, lesson, cfg, run_id=run_id, rating=rating)


def lesson_signal(run):
    """True only when a run produced a lesson worth keeping."""
    # A rescued run succeeded in the end; it must not be recorded as a failure.
    if run.get("status") not in ("ok", "ok_rescued"):
        return True
    rounds = run.get("review_rounds") or []
    saw_defect = False
    for entry in rounds:
        if entry.get("kind") == "review" and (entry.get("blocking_defects") or 0) > 0:
            saw_defect = True
        elif entry.get("kind") == "revision" and saw_defect:
            return True
    return False


def distill_lesson(run):
    """Turn a finished run into one short lesson sentence.

    Only called for real signal (fixed independent-review defects, a failed
    run, or a user rating); a clean first-pass run produces no lesson.
    """
    rounds = run.get("review_rounds") or []
    defects, fixes = [], []
    for entry in rounds:
        if entry.get("kind") == "review" and (entry.get("blocking_defects") or 0) > 0:
            defects.extend(entry.get("defects") or [])
        elif entry.get("kind") == "revision":
            fixes.extend(entry.get("fixes") or [])
            if entry.get("status") != "ok" and entry.get("error"):
                defects.append(entry["error"])
    parts = []
    if defects:
        parts.append("independent review found: " + sanitise_lesson("; ".join(defects), 320))
    if fixes:
        parts.append("fixed: " + sanitise_lesson("; ".join(fixes), 320))
    if run.get("status") == "ok_unreviewed":
        # The builder (or its rescue) succeeded; only the independent review
        # failed. Never write the meaningless "run failed: unknown".
        detail = (run.get("review_error") or run.get("review_warning")
                  or run.get("error") or "unknown")
        parts.append("independent review failed: "
                     + sanitise_lesson(detail, 300))
    elif run.get("status") not in ("ok", "ok_rescued"):
        parts.append("run failed: " + sanitise_lesson(run.get("error") or "unknown", 300))
    if not parts:
        if not lesson_signal(run):
            return None
        parts.append("run %s did not pass independent review" % run.get("id"))
    return "; ".join(parts)


def _resolve_rating_key(run, cfg):
    """Resolve a rated run's skill through the registry in auto/propose."""
    if cfg is not None and _mode(cfg) in ("auto", "propose"):
        try:
            key = normalise_skill_name(run.get("skill"), cfg=cfg)
            if key:
                return key
            key = detect_skill(run.get("prompt"), cfg=cfg)
            if key:
                return key
        except Exception:
            pass
    current = run.get("skill")
    if current in _SEED_BY_KEY:
        return current
    return detect_skill_legacy(run.get("prompt"))


def rate_run(run_id, rating, note, cfg):
    """Store user feedback and turn it into a lesson in the matching skill file."""
    rating = (rating or "").strip().lower()
    if rating not in ("up", "down"):
        raise RuntimeError("rating must be 'up' or 'down'")
    run, _json_path, _md_path = find_run(run_id, cfg)
    clean_note = sanitise_lesson(note)
    skill_key = _resolve_rating_key(run, cfg) or "document"
    run["rating"] = rating
    run["rating_note"] = clean_note
    run["rated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    run["skill"] = skill_key
    save_run(run, cfg)
    if rating == "up":
        lesson = "User rated the result up. Keep this approach. " + (clean_note or "")
    else:
        lesson = "User rated the result down. Check and fix: " + (clean_note or "inspect the deliverable against the skill checklist")
    path = append_lesson(skill_key, lesson, cfg, run_id=run_id, rating=rating)
    append_run_md(run, "## User rating\n\n- **Rating:** %s\n- **Note:** %s\n- **Skill updated:** %s\n"
                  % (rating, clean_note or "(none)", path), cfg)
    return ("Saved rating '%s' for run %s. Lesson appended to %s." % (rating, run_id, path))
