"""Authoritative skill registry for the v3.7.0 automatic-skills wave.

The registry owns ``<skills_dir>/index.json``, version snapshots, migration,
repair, statistics and the small set of lifecycle operations used by
``skill_auto`` and the MCP facade.

``note_run`` returns ``{"stats": {...}, "recommendation": "promote"|"demote"|
"hold", "reason": "<short reason>"}`` (it never returns the bare stats dict,
because a caller cannot distinguish "hold" from a missing recommendation).

Standard library only; import-safe on Python 3.9.
"""

import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:  # package import
    from .config import PACKAGE_DIR, ROOT_DIR
except Exception:  # pragma: no cover - direct script / partial checkout
    try:
        from config import PACKAGE_DIR, ROOT_DIR  # type: ignore
    except Exception:
        ROOT_DIR = Path(__file__).resolve().parent.parent

SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 512 * 1024
MAX_SKILL_CHARS = 24000
MAX_CAPTURE_RECORDS = 500
MAX_VERSIONS = 10

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
RESERVED_KEYS = {
    "index", "skills", "readme", "none", "auto", "off", "all", "default",
    "meta", "init", "__init__",
}
WINDOWS_DEVICE_NAMES = {"con", "prn", "aux", "nul"}
WINDOWS_DEVICE_NAMES.update("com%d" % i for i in range(1, 10))
WINDOWS_DEVICE_NAMES.update("lpt%d" % i for i in range(1, 10))

LIFECYCLE_STATUSES = ("seed", "active", "candidate", "demoted", "retired")
TASK_TYPES = ("code", "research", "creative", "document")
VERSION_REASONS = ("seed", "create", "merge", "refresh", "rollback", "lesson", "manual")

_PROBE_CACHE: Dict[str, bool] = {}
_UTC = dt.timezone.utc

_FALLBACK_SEEDS = [
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


class RegistryError(RuntimeError):
    """A registry invariant or I/O operation failed."""


class RegistryBusy(RegistryError):
    """The writer lock could not be acquired before the timeout."""


class RegistryConflict(RegistryError):
    """The on-disk revision moved while a caller held an expected revision."""


# ---------------------------------------------------------------------------
# small helpers


def _now_iso(clock=None) -> str:
    value = None
    if clock is not None:
        try:
            value = clock() if callable(clock) else clock
        except Exception:
            value = None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_UTC)
        return value.astimezone(_UTC).replace(microsecond=0).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dt.datetime.fromtimestamp(float(value), _UTC).replace(
            microsecond=0).isoformat()
    return dt.datetime.now(_UTC).replace(microsecond=0).isoformat()


def _num(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        if value != value or value in (float("inf"), float("-inf")):
            return default
    except Exception:
        return default
    return float(value)


def _int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _get(cfg, name, default):
    if cfg is None:
        return default
    value = getattr(cfg, name, default)
    return default if value is None else value


def _max_versions(cfg) -> int:
    value = _int(_get(cfg, "auto_skills_max_versions", MAX_VERSIONS), MAX_VERSIONS)
    return max(1, min(100, value))


def _max_chars(cfg) -> int:
    value = _int(_get(cfg, "auto_skills_max_skill_chars", MAX_SKILL_CHARS), MAX_SKILL_CHARS)
    return max(4000, min(MAX_SKILL_CHARS, value))


def sha256_text(text) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def validate_key(key) -> str:
    """Return the canonical key or raise ``RegistryError``."""
    if not isinstance(key, str):
        raise RegistryError("skill key must be a string")
    value = key.strip().lower()
    if value.endswith(".md"):
        value = value[:-3]
    if not value or value in RESERVED_KEYS or value in WINDOWS_DEVICE_NAMES:
        raise RegistryError("reserved skill key: %r" % (key,))
    if not KEY_RE.match(value):
        raise RegistryError("invalid skill key: %r" % (key,))
    if value.startswith(".") or "/" in value or "\\" in value or ".." in value:
        raise RegistryError("invalid skill key: %r" % (key,))
    return value


def safe_skill_path(directory, key) -> Path:
    key = validate_key(key)
    root = Path(directory).expanduser()
    path = root / (key + ".md")
    try:
        if path.is_symlink():
            raise RegistryError("refusing symlinked skill path")
        root_resolved = root.resolve()
        path_resolved = path.resolve()
        if path_resolved.parent != root_resolved:
            raise RegistryError("skill path escapes the skills directory")
    except RegistryError:
        raise
    except OSError as exc:
        raise RegistryError("cannot resolve skill path: %s" % exc)
    return path


def split_front_matter(text) -> Tuple[Optional[dict], str]:
    """Split the exact ``<!-- skill-meta ... -->`` block; body unchanged on error."""
    if not isinstance(text, str):
        return None, text
    source = text[1:] if text.startswith("\ufeff") else text
    opener = "<!-- skill-meta"
    if not source.startswith(opener):
        return None, text
    end = source.find("-->", len(opener))
    if end < 0:
        return None, text
    payload = source[len(opener):end].strip()
    if not payload:
        return None, text
    try:
        meta = json.loads(payload)
    except Exception:
        return None, text
    if not isinstance(meta, dict):
        return None, text
    body = source[end + 3:]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return meta, body


def render_front_matter(meta, body) -> str:
    if not isinstance(meta, dict) or not meta:
        return body or ""
    body = split_front_matter(body or "")[1]
    body = (body or "").lstrip("\r\n")
    payload = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    return "<!-- skill-meta\n%s\n-->\n\n%s" % (payload, body)


def atomic_write_text(path, text, mode=0o600) -> None:
    """Write UTF-8 text through a same-directory temp file and ``os.replace``."""
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-skill-", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text if isinstance(text, str) else str(text))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(temp_name, mode)
        except OSError:
            pass
        os.replace(temp_name, str(target))
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_text(path) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None


def _loads(text):
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _infer_task_type(*parts) -> str:
    blob = " ".join(str(p or "") for p in parts).lower()
    tokens = set(re.findall(r"[a-z0-9+#.-]{2,24}", blob))
    if tokens & {"code", "python", "javascript", "api", "cli", "app", "server",
                 "refactor", "bug", "database", "backend", "frontend", "library"}:
        return "code"
    if tokens & {"data", "dataset", "csv", "analysis", "analyze", "analyse",
                 "statistics", "metrics", "dashboard", "chart", "spreadsheet"}:
        return "research"
    if tokens & {"letter", "document", "report", "essay", "proposal", "memo",
                 "resume", "article", "manual", "chapter"}:
        return "document"
    return "creative"


def _infer_keywords(*parts, limit=12) -> List[str]:
    blob = " ".join(str(p or "") for p in parts).lower()
    out = []
    for token in re.findall(r"[a-z][a-z0-9+#.-]{2,23}", blob):
        if token not in out:
            out.append(token)
        if len(out) >= limit:
            break
    return out


def _normalise_alias(value) -> str:
    text = str(value or "").strip().lower()
    if text.endswith(".md"):
        text = text[:-3]
    text = re.sub(r"[\s_]+", "-", text)
    return text[:32]


def _default_stats() -> dict:
    return {
        "uses": 0, "score_count": 0, "score_sum": 0.0, "blocking_total": 0,
        "last_score": None, "last_run": None, "baseline_at_use": None,
        "baseline_ref": None, "wins": 0, "losses": 0, "consecutive_bad": 0,
        "window": [], "recent_runs": [],
    }


def _normalise_stats(value) -> dict:
    stats = _default_stats()
    if isinstance(value, dict):
        for key, default in stats.items():
            if key in value:
                stats[key] = value[key]
        if not isinstance(stats.get("window"), list):
            stats["window"] = []
        if not isinstance(stats.get("recent_runs"), list):
            stats["recent_runs"] = []
        stats["window"] = stats["window"][-20:]
        stats["recent_runs"] = stats["recent_runs"][-50:]
        stats["baseline_ref"] = (stats.get("baseline_ref")
                                 if isinstance(stats.get("baseline_ref"), dict) else None)
    return stats


def _normalise_review(value) -> dict:
    value = value if isinstance(value, dict) else {}
    score = _num(value.get("score"))
    return {
        "score": score,
        "rounds": _int(value.get("rounds"), 0),
        "blocking": _int(value.get("blocking"), 0),
        "reviewer": str(value.get("reviewer") or "skill-review")[:40],
        "at": value.get("at"),
    }


def _normalise_refresh(value) -> dict:
    value = value if isinstance(value, dict) else {}
    return {
        "last_attempt": value.get("last_attempt"),
        "attempts": _int(value.get("attempts"), 0),
        "last_version": value.get("last_version"),
    }


def _canonical_task_type(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in TASK_TYPES else "creative"


def _seed_specs() -> list:
    try:
        from .skills import SEED_SKILLS as seeds
        return list(seeds or [])
    except Exception:
        pass
    try:
        from .skills import SKILLS as seeds
        return list(seeds or [])
    except Exception:
        return list(_FALLBACK_SEEDS)


def _seed_task_type(key) -> str:
    return {"poster": "creative", "document": "document", "web": "creative",
            "code": "code", "data": "research"}.get(key, _infer_task_type(key))


def _fallback_seed_body(spec) -> str:
    return "# %s\n\n%s\n" % (spec.get("title") or spec.get("key"),
                             spec.get("description") or "")


def _seed_entry(spec: dict, directory) -> dict:
    key = validate_key(spec.get("key"))
    title = str(spec.get("title") or key)
    description = str(spec.get("description") or "")
    aliases = []
    for alias in (spec.get("aliases") or []):
        value = _normalise_alias(alias)
        if value and value not in aliases:
            aliases.append(value)
    keywords = [str(k).strip().lower() for k in (spec.get("keywords") or []) if str(k).strip()]
    text = _read_text(Path(directory) / (key + ".md"))
    if text is None:
        text = _read_text(Path(directory) / ".versions" / key / "v0001.md")
    if text is None:
        text = _fallback_seed_body(spec)
    now = _now_iso()
    return {
        "key": key, "title": title, "description": description,
        "task_type": _seed_task_type(key), "aliases": aliases[:12],
        "keywords": keywords[:30], "status": "seed", "source": "builtin",
        "approved": True, "version": 1, "created": now, "updated": now,
        "content_sha256": sha256_text(text), "stats": _default_stats(),
        "review": _normalise_review(None), "lessons": {},
        "refresh": _normalise_refresh(None), "history": [],
        "merged_into": None, "retired_reason": None,
    }


def seed_entries(directory) -> dict:
    """Five built-in seed entries, reading ``<key>.md`` when it exists."""
    result = {}
    for spec in _seed_specs():
        try:
            entry = _seed_entry(spec, directory)
        except Exception:
            continue
        result[entry["key"]] = entry
    if not result:
        for spec in _FALLBACK_SEEDS:
            entry = _seed_entry(spec, directory)
            result[entry["key"]] = entry
    return result


def _entry_from_text(key: str, text: str, source="imported") -> dict:
    meta, body = split_front_matter(text)
    meta = meta or {}
    heading = ""
    for line in (body or "").splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            break
    title = str(meta.get("title") or heading or key)
    description = str(meta.get("description") or "")
    now = _now_iso()
    meta_task = _canonical_task_type(meta.get("task_type")) if meta.get("task_type") else None
    return {
        "key": key, "title": title[:200], "description": description[:500],
        "task_type": meta_task or _infer_task_type(title, body, key),
        "aliases": [_normalise_alias(a) for a in (meta.get("aliases") or [])][:12],
        "keywords": [str(k).strip().lower() for k in
                     (meta.get("keywords") or _infer_keywords(title, body, key, limit=12))][:30],
        "status": str(meta.get("status") or ("active" if source == "imported" else "candidate")),
        "source": str(meta.get("source") or source), "approved": True,
        "version": max(1, _int(meta.get("version"), 1)), "created": meta.get("created") or now,
        "updated": meta.get("updated") or now, "content_sha256": sha256_text(text),
        "stats": _default_stats(), "review": _normalise_review(None), "lessons": {},
        "refresh": _normalise_refresh(None), "history": [],
        "merged_into": None, "retired_reason": None,
    }


def _empty_index() -> dict:
    now = _now_iso()
    return {
        "schema": SCHEMA_VERSION, "revision": 0, "created": now, "updated": now,
        "seeds": [s.get("key") for s in _seed_specs()][:5],
        "skills": {}, "baselines": {}, "builds": {}, "cluster_builds": {},
        "warnings": [],
    }


def _warn(data: dict, message: str) -> None:
    try:
        warnings = list(data.get("warnings") or [])
        text = str(message).replace(str(Path.home()), "~")[:300]
        warnings.append(text)
        data["warnings"] = warnings[-20:]
    except Exception:
        pass


def _normalise_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return entry
    key = entry.get("key")
    entry.setdefault("title", str(key or "skill"))
    entry.setdefault("description", "")
    entry["task_type"] = _canonical_task_type(entry.get("task_type"))
    aliases = entry.get("aliases")
    if isinstance(aliases, list):
        cleaned = []
        for alias in aliases:
            value = _normalise_alias(alias)
            if value and value not in cleaned:
                cleaned.append(value)
        entry["aliases"] = cleaned[:12]
    else:
        entry["aliases"] = []
    keywords = entry.get("keywords")
    entry["keywords"] = [str(k).strip().lower() for k in keywords if str(k).strip()][:30] \
        if isinstance(keywords, list) else []
    entry["status"] = str(entry.get("status") or "candidate").lower()
    if entry["status"] not in LIFECYCLE_STATUSES:
        entry["status"] = "candidate"
    entry.setdefault("source", "generated")
    entry["approved"] = bool(entry.get("approved", entry["source"] == "builtin"))
    entry["version"] = max(1, _int(entry.get("version"), 1))
    now = _now_iso()
    entry.setdefault("created", now)
    entry.setdefault("updated", now)
    entry.setdefault("content_sha256", "")
    entry["stats"] = _normalise_stats(entry.get("stats"))
    entry["review"] = _normalise_review(entry.get("review"))
    entry["lessons"] = entry.get("lessons") if isinstance(entry.get("lessons"), dict) else {}
    entry["refresh"] = _normalise_refresh(entry.get("refresh"))
    entry["history"] = entry.get("history") if isinstance(entry.get("history"), list) else []
    entry.setdefault("merged_into", None)
    entry.setdefault("retired_reason", None)
    return entry


def rebuild_from_files(directory) -> dict:
    """Recover an index from an existing index/versions/front-matter/files."""
    directory = Path(directory)
    data = _empty_index()
    old = _loads(_read_text(directory / "index.json") or "")
    if isinstance(old, dict):
        data["revision"] = max(0, _int(old.get("revision"), 0))
        data["created"] = old.get("created") or data["created"]
        data["seeds"] = old.get("seeds") if isinstance(old.get("seeds"), list) else data["seeds"]
        for key, entry in (old.get("skills") or {}).items() if isinstance(old.get("skills"), dict) else []:
            try:
                valid = validate_key(key)
            except RegistryError:
                continue
            if isinstance(entry, dict):
                entry.setdefault("key", valid)
                data["skills"][valid] = _normalise_entry(entry)
        for name in ("baselines", "builds", "cluster_builds", "warnings"):
            value = old.get(name)
            if isinstance(value, dict if name != "warnings" else list):
                data[name] = value

    seeds = seed_entries(directory)
    for key, entry in seeds.items():
        if key not in data["skills"]:
            data["skills"][key] = entry

    versions_root = directory / ".versions"
    if versions_root.is_dir():
        for key_dir in sorted(versions_root.iterdir()):
            if not key_dir.is_dir():
                continue
            try:
                key = validate_key(key_dir.name)
            except RegistryError:
                continue
            best = None
            for meta_path in sorted(key_dir.glob("v*.json")):
                meta = _loads(_read_text(meta_path) or "")
                if not isinstance(meta, dict):
                    continue
                version = _int(meta.get("version"), 0)
                if version > 0 and (best is None or version > best[0]):
                    best = (version, meta)
            if best is None:
                continue
            version, meta = best
            content = _read_text(key_dir / ("v%04d.md" % version))
            if content is None or sha256_text(content) != meta.get("sha256"):
                continue
            current = data["skills"].get(key)
            if current and _int(current.get("version"), 1) >= version:
                continue
            entry = _entry_from_text(key, content, source=str(meta.get("source") or "generated"))
            entry["version"] = version
            entry["created"] = meta.get("created") or entry["created"]
            entry["updated"] = meta.get("created") or entry["updated"]
            entry["content_sha256"] = meta.get("sha256")
            entry["history"] = [{
                "version": version, "path": ".versions/%s/v%04d.md" % (key, version),
                "sha256": meta.get("sha256"), "created": meta.get("created"),
                "reason": meta.get("reason") or "refresh",
                "review_score": meta.get("review_score"), "run_id": meta.get("run_id"),
            }]
            data["skills"][key] = _normalise_entry(entry)

    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        key = path.stem
        try:
            key = validate_key(key)
        except RegistryError:
            continue
        if key in data["skills"]:
            continue
        text = _read_text(path)
        if text is None or len(text) > MAX_SKILL_CHARS:
            continue
        body = split_front_matter(text)[1]
        first = next((line for line in (body or "").splitlines() if line.strip()), "")
        if not first.startswith("# "):
            _warn(data, "ignored %s: missing H1" % path.name)
            continue
        data["skills"][key] = _normalise_entry(_entry_from_text(key, text, "imported"))
    return data


# ---------------------------------------------------------------------------
# registry


class SkillRegistry:
    def __init__(self, cfg, directory=None, clock=None):
        self.cfg = cfg
        self._explicit_directory = directory
        self._clock = clock
        self._data = None
        self._read_only = False
        self._directory = None
        self._tls = threading.local()
        self._index_stat = None

    # -- directory / locking -------------------------------------------------

    @property
    def directory(self) -> Path:
        if self._directory is None:
            self._directory = self._resolve_directory()
        return self._directory

    def _resolve_directory(self) -> Path:
        explicit = self._explicit_directory
        if explicit is None:
            explicit = _get(self.cfg, "skills_dir", "")
        if explicit:
            path = Path(str(explicit)).expanduser()
            if path.exists():
                self._read_only = not _probe_writable(path)
            else:
                self._read_only = not _probe_writable(path)
            return path
        candidate = None
        try:
            from .skills import resolved_skills_dir
            candidate = Path(resolved_skills_dir(self.cfg)).expanduser()
        except Exception:
            candidate = None
        if candidate is not None and _probe_writable(candidate):
            return candidate
        state = _get(self.cfg, "state_dir", "") or os.path.join(str(ROOT_DIR), "state")
        target = Path(str(state)).expanduser() / "skills"
        target.mkdir(parents=True, exist_ok=True)
        if candidate is not None and candidate.is_dir() and candidate != target:
            for source in candidate.glob("*.md"):
                destination = target / source.name
                if not destination.exists():
                    try:
                        destination.write_text(source.read_text(encoding="utf-8"),
                                                encoding="utf-8")
                    except Exception:
                        pass
        self._read_only = not _probe_writable(target)
        return target

    def _can_write(self) -> bool:
        if self._read_only:
            return False
        return _probe_writable(self.directory)

    @contextlib.contextmanager
    def _locked(self, timeout: float = 2.0):
        tls = self._tls
        depth = getattr(tls, "depth", 0)
        if depth > 0:
            tls.depth = depth + 1
            try:
                yield self
            finally:
                tls.depth -= 1
            return
        lock_dir = self.directory / ".locks"
        lock_path = lock_dir / "registry.lock"
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RegistryError("cannot create lock directory: %s" % exc)
        fd = None
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        for attempt in range(40):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, ("%d\n%d\n" % (os.getpid(), int(time.time()))).encode("ascii"))
                break
            except FileExistsError:
                if _lock_is_stale(lock_path):
                    _take_over_lock(lock_path)
                if attempt >= 39 or time.monotonic() >= deadline:
                    break
                time.sleep(0.05 + random.random() * 0.01)
            except OSError as exc:
                raise RegistryError("cannot acquire registry lock: %s" % exc)
        if fd is None:
            raise RegistryBusy("skills registry is busy")
        tls.depth = 1
        try:
            yield self
        finally:
            tls.depth = 0
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass

    # -- loading -------------------------------------------------------------

    def _index_path(self) -> Path:
        return self.directory / "index.json"

    def _bak_path(self) -> Path:
        return self.directory / "index.json.bak"

    def load(self, refresh: bool = False) -> dict:
        if self._data is not None and not refresh:
            # Cross-process writers (the server, a background job, a tool call)
            # replace index.json atomically. Re-read when the on-disk stat
            # moved instead of serving a stale in-memory copy forever.
            try:
                stat = self._index_path().stat()
                current = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                current = None
            if current is None or current == self._index_stat:
                return self._data
            refresh = True
        path = self._index_path()
        text = _read_text(path)
        if text is None:
            return self._load_missing()
        data = _loads(text)
        if data is None:
            return self._recover_corrupt()
        schema = _int(data.get("schema"), 1)
        if schema > SCHEMA_VERSION:
            self._read_only = True
            data.setdefault("skills", {})
            data.setdefault("warnings", [])
            _warn(data, "index schema %s is newer than %s; read-only" %
                  (schema, SCHEMA_VERSION))
            self._data = self._normalise_index(data)
            self._remember_stat()
            return self._data
        if not isinstance(data.get("skills"), dict):
            return self._recover_corrupt()
        data = self._normalise_index(data)
        self._data = data
        self._remember_stat()
        try:
            self._repair_live()
        except Exception:
            pass
        return self._data

    def _load_missing(self) -> dict:
        if self._can_write():
            try:
                return self.migrate()
            except Exception as exc:
                self._read_only = True
                data = _empty_index()
                data["skills"] = seed_entries(self.directory)
                _warn(data, "migration failed: %s" % type(exc).__name__)
                self._data = data
                return data
        data = _empty_index()
        data["skills"] = seed_entries(self.directory)
        _warn(data, "read-only skills directory; seed view only")
        self._data = data
        return data

    def _normalise_index(self, data: dict) -> dict:
        data.setdefault("schema", SCHEMA_VERSION)
        data.setdefault("revision", 0)
        data.setdefault("created", _now_iso())
        data.setdefault("updated", data.get("created"))
        data.setdefault("seeds", [s.get("key") for s in _seed_specs()][:5])
        data.setdefault("skills", {})
        for name in ("baselines", "builds", "cluster_builds"):
            if not isinstance(data.get(name), dict):
                data[name] = {}
        if not isinstance(data.get("warnings"), list):
            data["warnings"] = []
        clean = {}
        for key, entry in list(data["skills"].items()):
            try:
                valid = validate_key(key)
            except RegistryError:
                _warn(data, "dropped invalid skill key")
                continue
            if isinstance(entry, dict):
                entry.setdefault("key", valid)
                if entry.get("task_type") not in TASK_TYPES:
                    _warn(data, "skill %s: unknown task type, using creative" % valid)
                clean[valid] = _normalise_entry(entry)
        data["skills"] = clean
        return data

    def _recover_corrupt(self) -> dict:
        warnings = []
        if self._can_write():
            warnings.append(self._quarantine_corrupt())
        backup = _loads(_read_text(self._bak_path()) or "")
        if isinstance(backup, dict) and isinstance(backup.get("skills"), dict):
            data = self._normalise_index(backup)
            _warn(data, "recovered index from index.json.bak")
            self._data = data
            self._remember_stat()
            return data
        data = rebuild_from_files(self.directory)
        _warn(data, "rebuilt index from files")
        self._data = data
        self._remember_stat()
        if self._can_write():
            try:
                self.save(data=data)
            except Exception:
                pass
        return self._data

    def _quarantine_corrupt(self) -> str:
        stamp = dt.datetime.now(_UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.directory / ("index.corrupt-%s.json" % stamp)
        try:
            os.replace(str(self._index_path()), str(target))
        except OSError:
            return "index.json is corrupt"
        try:
            old = sorted(self.directory.glob("index.corrupt-*.json"))
            for path in old[:-2]:
                try:
                    path.unlink()
                except OSError:
                    pass
        except Exception:
            pass
        return "quarantined index.json"

    def _remember_stat(self) -> None:
        try:
            stat = self._index_path().stat()
            self._index_stat = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._index_stat = None

    def reload_if_changed(self) -> bool:
        try:
            stat = self._index_path().stat()
            current = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            current = None
        if self._data is None:
            self.load(refresh=True)
            return True
        if current != self._index_stat:
            self.load(refresh=True)
            return True
        return False

    # -- saving --------------------------------------------------------------

    def save(self, data=None, expected_revision=None) -> None:
        if data is None:
            if self._data is None:
                self.load()
            data = self._data
        if not self._can_write():
            raise RegistryError("skills registry is read-only")
        with self._locked():
            work = copy.deepcopy(data if isinstance(data, dict) else self._data)
            if not isinstance(work, dict):
                raise RegistryError("index data must be an object")
            if expected_revision is not None:
                disk = _loads(_read_text(self._index_path()) or "")
                if isinstance(disk, dict):
                    disk_rev = _int(disk.get("revision"), -1)
                    if disk_rev != _int(expected_revision, -1):
                        raise RegistryConflict("registry revision changed")
                if _int(work.get("revision"), 0) != _int(expected_revision, 0):
                    raise RegistryConflict("registry revision changed")
            work["schema"] = SCHEMA_VERSION
            work["revision"] = _int(work.get("revision"), 0) + 1
            work["updated"] = _now_iso(self._clock)
            work.setdefault("created", work["updated"])
            dropped = []
            if _index_bytes(work) > MAX_INDEX_BYTES:
                work, dropped = _trim_index(work)
            if _index_bytes(work) > MAX_INDEX_BYTES:
                _warn(work, "index exceeds %d bytes; save refused" % MAX_INDEX_BYTES)
                self._data = work
                raise RegistryError("index.json exceeds the size cap")
            payload = json.dumps(work, sort_keys=True, indent=2, ensure_ascii=False)
            if not payload.endswith("\n"):
                payload += "\n"
            old_text = _read_text(self._index_path())
            if old_text is not None:
                try:
                    bak = json.dumps(json.loads(old_text), sort_keys=True, indent=2,
                                     ensure_ascii=False)
                    atomic_write_text(self._bak_path(), bak + "\n")
                except Exception:
                    _warn(work, "could not rotate index.json.bak")
            atomic_write_text(self._index_path(), payload)
            self._data = work
            self._remember_stat()
            for key, path_text, meta_text in dropped:
                _unlink_quiet(self.directory / path_text)
                _unlink_quiet(self.directory / meta_text)

    # -- queries -------------------------------------------------------------

    def entries(self, status=None) -> list:
        data = self.load()
        wanted = status
        if isinstance(wanted, str):
            wanted = [wanted]
        result = []
        for key, entry in data.get("skills", {}).items():
            if wanted and entry.get("status") not in wanted:
                continue
            result.append(entry)
        return result

    def resolve_key(self, name) -> Optional[str]:
        if isinstance(name, str):
            candidate = name.strip().lower()
            if candidate.endswith(".md"):
                candidate = candidate[:-3]
            candidate = re.sub(r"[\s_]+", "-", candidate)
        else:
            return None
        data = self.load()
        skills = data.get("skills", {})
        for _ in range(5):
            if candidate in skills:
                entry = skills[candidate]
                target = entry.get("merged_into")
                if target and target in skills and target != candidate:
                    candidate = target
                    continue
                return candidate
            found = None
            for key, entry in skills.items():
                aliases = entry.get("aliases") or []
                if candidate == key or candidate in [_normalise_alias(a) for a in aliases]:
                    found = key
                    break
            if found is None:
                return None
            entry = skills[found]
            target = entry.get("merged_into")
            if target and target in skills and target != found:
                candidate = target
                continue
            return found
        return candidate if candidate in skills else None

    def get(self, key_or_alias) -> Optional[dict]:
        key = self.resolve_key(key_or_alias)
        if key is None:
            return None
        return self.load().get("skills", {}).get(key)

    def content(self, key, version=None) -> str:
        key = self.resolve_key(key)
        if key is None:
            return ""
        entry = self.load().get("skills", {}).get(key) or {}
        if version is not None:
            try:
                return self.read_version(key, version)[1]
            except RegistryError:
                return ""
        path = safe_skill_path(self.directory, key)
        text = _read_text(path)
        recorded = entry.get("content_sha256")
        if text is not None:
            if not recorded or sha256_text(text) == recorded:
                return text
        try:
            return self.read_version(key, _int(entry.get("version"), 1))[1]
        except RegistryError:
            return text or ""

    # -- migration / repair --------------------------------------------------

    def migrate(self) -> dict:
        with self._locked():
            data = _empty_index()
            if not self._can_write():
                data["skills"] = seed_entries(self.directory)
                self._data = data
                return data
            self.directory.mkdir(parents=True, exist_ok=True)
            seeds = seed_entries(self.directory)
            for spec in _seed_specs():
                key = spec.get("key")
                try:
                    key = validate_key(key)
                except RegistryError:
                    continue
                entry = seeds.get(key)
                if entry is None:
                    continue
                live = self.directory / (key + ".md")
                if not live.exists():
                    # A missing seed file is registered but not recreated: the
                    # shipped files are preserved and read_skill keeps its clear
                    # missing-file error.
                    entry["content_sha256"] = ""
                    data["skills"][key] = entry
                    continue
                version_md = self.directory / ".versions" / key / "v0001.md"
                version_json = self.directory / ".versions" / key / "v0001.json"
                content = _read_text(live) or ""
                if not version_md.exists():
                    atomic_write_text(version_md, content)
                if not version_json.exists():
                    meta = _version_meta(key, 1, content, "seed", None, None, None,
                                         "seed", None, None, None)
                    atomic_write_text(version_json, json.dumps(
                        meta, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
                if version_md.exists():
                    version_text = _read_text(version_md) or content
                    entry["history"] = [{
                        "version": 1,
                        "path": ".versions/%s/v0001.md" % key,
                        "sha256": sha256_text(version_text),
                        "created": entry.get("created"),
                        "reason": "seed", "review_score": None, "run_id": None,
                    }]
                data["skills"][key] = entry
            self._adopt_files(data)
            self._data = data
            self.save(data=data)
            return self._data

    def _adopt_files(self, data: dict) -> None:
        try:
            paths = sorted(self.directory.glob("*.md"))
        except OSError:
            return
        for path in paths:
            key = path.stem
            try:
                key = validate_key(key)
            except RegistryError:
                _warn(data, "ignored %s" % path.name)
                continue
            if key in data["skills"]:
                continue
            text = _read_text(path)
            if text is None:
                _warn(data, "unreadable %s" % path.name)
                continue
            if len(text) > MAX_SKILL_CHARS:
                _warn(data, "ignored %s: too large" % path.name)
                continue
            body = split_front_matter(text)[1]
            first = next((line for line in (body or "").splitlines() if line.strip()), "")
            if not first.startswith("# "):
                _warn(data, "ignored %s: missing H1" % path.name)
                continue
            entry = _normalise_entry(_entry_from_text(key, text, "imported"))
            entry["status"] = "active"
            entry["source"] = "imported"
            entry["approved"] = True
            self._write_migrated_version(key, entry, text, "create")
            data["skills"][key] = entry

    def _write_migrated_version(self, key, entry, content, reason) -> None:
        version = 1
        version_md = self.directory / ".versions" / key / ("v%04d.md" % version)
        version_json = self.directory / ".versions" / key / ("v%04d.json" % version)
        if not version_md.exists():
            try:
                atomic_write_text(version_md, content)
            except OSError:
                return
        meta = _version_meta(key, version, content, reason, entry.get("review", {}).get("score"),
                             None, None, entry.get("status"), None, None, None)
        if not version_json.exists():
            try:
                atomic_write_text(version_json, json.dumps(
                    meta, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
            except OSError:
                return
        if version_md.exists():
            entry["history"] = [{
                "version": 1, "path": ".versions/%s/v%04d.md" % (key, version),
                "sha256": sha256_text(content), "created": entry.get("created"),
                "reason": reason, "review_score": None, "run_id": None,
            }]

    def _repair_live(self) -> None:
        if self._read_only or not self._can_write():
            return
        data = self._data or {}
        changed = False
        for key, entry in (data.get("skills") or {}).items():
            path = self.directory / (key + ".md")
            text = _read_text(path)
            recorded = entry.get("content_sha256")
            expected = None
            try:
                expected = self.read_version(key, _int(entry.get("version"), 1))[0].get("sha256")
            except RegistryError:
                expected = recorded
            if text is not None and recorded and sha256_text(text) == recorded:
                continue
            if text is not None:
                self._preserve_conflict(key, text)
            if expected:
                try:
                    version_text = self.read_version(
                        key, _int(entry.get("version"), 1))[1]
                    atomic_write_text(path, version_text)
                    changed = True
                except Exception:
                    _warn(data, "skill %s: live file mismatch" % key)
                    changed = True
            else:
                _warn(data, "skill %s: live file mismatch" % key)
                changed = True
        if changed and self._can_write():
            try:
                self.save(data=data)
            except Exception:
                pass

    def _preserve_conflict(self, key, text) -> None:
        try:
            folder = self.directory / ".conflicts"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(_UTC).strftime("%Y%m%dT%H%M%SZ")
            target = folder / ("%s-%s.md" % (key, stamp))
            atomic_write_text(target, text)
            old = sorted(folder.glob("%s-*.md" % key))
            for path in old[:-2]:
                _unlink_quiet(path)
        except Exception:
            pass

    # -- versioning ----------------------------------------------------------

    def read_version(self, key, version) -> Tuple[dict, str]:
        key = self.resolve_key(key)
        if key is None:
            raise RegistryError("unknown skill key")
        number = _int(version, 0)
        if number < 1:
            raise RegistryError("invalid version")
        folder = self.directory / ".versions" / key
        content = _read_text(folder / ("v%04d.md" % number))
        meta = _loads(_read_text(folder / ("v%04d.json" % number)) or "")
        if content is None or not isinstance(meta, dict):
            raise RegistryError("version %d is not available for %s" % (number, key))
        if meta.get("sha256") != sha256_text(content):
            raise RegistryError("version %d checksum mismatch for %s" % (number, key))
        return meta, content

    def add_version(self, key, content, reason, review=None, run_id=None,
                    cluster_id=None, restored_from=None) -> dict:
        with self._locked():
            self.load()
            key = self.resolve_key(key) or validate_key(key)
            entry = self._data["skills"].get(key)
            if entry is None:
                raise RegistryError("unknown skill key")
            if reason not in VERSION_REASONS:
                raise RegistryError("invalid version reason")
            text = content if isinstance(content, str) else str(content or "")
            current = _int(entry.get("version"), 0)
            number = current + 1
            meta, body = split_front_matter(text)
            if entry.get("source") in ("generated", "imported"):
                body = body if meta is not None else text
                text = render_front_matter(_entry_meta(entry, number), body)
            if len(text) > _max_chars(self.cfg):
                raise RegistryError("skill content exceeds the size cap")
            sha = sha256_text(text)
            meta_dict = _version_meta(
                key, number, text, reason,
                (review or {}).get("score") if isinstance(review, dict) else None,
                (review or {}).get("rounds") if isinstance(review, dict) else None,
                run_id, entry.get("status"), current, restored_from, cluster_id)
            self._write_version(key, number, text, meta_dict)
            history = list(entry.get("history") or [])
            history.append({
                "version": number, "path": ".versions/%s/v%04d.md" % (key, number),
                "sha256": sha, "created": meta_dict["created"], "reason": reason,
                "review_score": meta_dict.get("review_score"), "run_id": run_id,
            })
            entry["history"] = history
            entry["version"] = number
            entry["content_sha256"] = sha
            entry["updated"] = meta_dict["created"]
            dropped = self._prune_history(entry)
            self.save()
            self._write_live(key, text)
            for path_text, meta_text in dropped:
                _unlink_quiet(self.directory / path_text)
                _unlink_quiet(self.directory / meta_text)
            return copy.deepcopy(entry)

    def _write_version(self, key, number, content, meta) -> None:
        folder = self.directory / ".versions" / key
        atomic_write_text(folder / ("v%04d.md" % number), content)
        atomic_write_text(folder / ("v%04d.json" % number),
                          json.dumps(meta, sort_keys=True, indent=2,
                                     ensure_ascii=False) + "\n")

    def _write_live(self, key, content) -> None:
        atomic_write_text(safe_skill_path(self.directory, key), content)

    def _prune_history(self, entry) -> list:
        keep = _max_versions(self.cfg)
        history = list(entry.get("history") or [])
        seed_like = []
        normal = []
        for item in history:
            if item.get("reason") == "seed" or (
                    _int(item.get("version"), 0) == 1 and entry.get("source") == "builtin"):
                seed_like.append(item)
            else:
                normal.append(item)
        normal.sort(key=lambda row: _int(row.get("version"), 0))
        dropped_rows = normal[:-keep] if len(normal) > keep else []
        keep_ids = set(id(row) for row in seed_like + normal[-keep:])
        entry["history"] = [row for row in history if id(row) in keep_ids]
        dropped = []
        for row in dropped_rows:
            version = _int(row.get("version"), 0)
            if version <= 1 and entry.get("source") == "builtin":
                continue
            dropped.append((".versions/%s/v%04d.md" % (entry["key"], version),
                            ".versions/%s/v%04d.json" % (entry["key"], version)))
        return dropped

    def rollback(self, key, version=None, run_id=None, reactivate=False,
                 baseline=None) -> dict:
        with self._locked():
            self.load()
            key = self.resolve_key(key)
            if key is None:
                raise RegistryError("unknown skill key")
            entry = self._data["skills"][key]
            current = _int(entry.get("version"), 1)
            target = _int(version, 0) if version is not None else current - 1
            if target < 1 or target >= current:
                if version is None:
                    raise RegistryError(
                        "no previous version; current version is %d" % current)
                raise RegistryError("version %d is not rollback-able" % target)
            meta, content = self.read_version(key, target)
            new_entry = self.add_version(key, content, "rollback", run_id=run_id,
                                         restored_from=target)
            entry = self._data["skills"][key]
            status = "candidate" if (reactivate or entry.get("source") != "builtin") else "seed"
            entry["status"] = status
            if status == "candidate":
                entry["approved"] = bool(entry.get("approved"))
            entry["merged_into"] = None
            entry["retired_reason"] = None
            stats = entry["stats"]
            stats["window"] = []
            stats["wins"] = 0
            stats["losses"] = 0
            stats["consecutive_bad"] = 0
            stats["baseline_at_use"] = None
            if isinstance(baseline, dict):
                ref = copy.deepcopy(baseline)
                ref.setdefault("frozen", _now_iso(self._clock))
                stats["baseline_ref"] = ref
            else:
                stats["baseline_ref"] = None
            entry["updated"] = _now_iso(self._clock)
            self.save()
            return {
                "key": key, "from_version": current,
                "to_version": _int(entry.get("version"), new_entry.get("version", 0)),
                "restored_from": target, "status": status,
            }

    # -- creation / merging --------------------------------------------------

    def create_or_merge(self, draft, review, cluster, mode, run_id) -> dict:
        draft = draft if isinstance(draft, dict) else {}
        review = review if isinstance(review, dict) else {}
        cluster = cluster if isinstance(cluster, dict) else {}
        with self._locked():
            self.load()
            target = draft.get("_merge_target")
            if target:
                target = self.resolve_key(target) or target
            key = draft.get("key")
            try:
                key = validate_key(key)
            except RegistryError:
                key = None
            if target is None and key and key in self._data["skills"]:
                target = key
            if target is None:
                try:
                    from .skill_author import find_duplicate
                    found = find_duplicate(draft, self)
                except Exception:
                    found = None
                if isinstance(found, dict) and found.get("action") == "merge":
                    target = found.get("target")
            if target:
                return self.merge_locked(target, draft, review, run_id)
            return self._create_locked(key, draft, review, cluster, mode, run_id)

    def _create_locked(self, key, draft, review, cluster, mode, run_id) -> dict:
        if key is None:
            raise RegistryError("draft has no valid key")
        if key in self._data["skills"]:
            return self.merge_locked(key, draft, review, run_id)
        body = _draft_body(draft)
        entry = {
            "key": key, "title": str(draft.get("title") or key)[:200],
            "description": str(draft.get("description") or "")[:500],
            "task_type": _canonical_task_type(draft.get("task_type")),
            "aliases": [_normalise_alias(a) for a in (draft.get("aliases") or [])][:12],
            "keywords": [str(k).strip().lower() for k in
                         (draft.get("keywords") or [])][:30],
            "status": "candidate", "source": "generated",
            "approved": str(mode or "auto") != "propose", "version": 1,
            "created": _now_iso(self._clock), "updated": _now_iso(self._clock),
            "content_sha256": "", "stats": _default_stats(),
            "review": _review_block(review, self._clock),
            "lessons": {}, "refresh": _normalise_refresh(None), "history": [],
            "merged_into": None, "retired_reason": None,
            "cluster_id": cluster.get("id") or cluster.get("cluster_id"),
        }
        if entry["status"] not in LIFECYCLE_STATUSES:
            entry["status"] = "candidate"
        ref = cluster.get("baseline")
        if isinstance(ref, dict):
            frozen = copy.deepcopy(ref)
            frozen.setdefault("frozen", _now_iso(self._clock))
            entry["stats"]["baseline_ref"] = frozen
        else:
            entry["stats"]["baseline_ref"] = None
        content = render_front_matter(_entry_meta(entry, 1), split_front_matter(body)[1])
        if len(content) > _max_chars(self.cfg):
            raise RegistryError("skill content exceeds the size cap")
        sha = sha256_text(content)
        meta = _version_meta(key, 1, content, "create", review.get("score"),
                             _int(review.get("rounds"), 0), run_id, "candidate",
                             None, None, entry.get("cluster_id"))
        self._write_version(key, 1, content, meta)
        entry["version"] = 1
        entry["content_sha256"] = sha
        entry["history"] = [{
            "version": 1, "path": ".versions/%s/v0001.md" % key, "sha256": sha,
            "created": meta["created"], "reason": "create",
            "review_score": meta.get("review_score"), "run_id": run_id,
        }]
        self._data["skills"][key] = entry
        self.save()
        self._write_live(key, content)
        return {"action": "create", "key": key, "version": 1,
                "status": "candidate", "merged_into": None}

    def merge_locked(self, target_key, draft, review, run_id=None) -> dict:
        with self._locked():
            self.load()
            key = self.resolve_key(target_key)
            if key is None:
                return {"action": "skip", "reason": "unknown_target"}
            entry = self._data["skills"][key]
            existing = self.content(key)
            preview = self.merge_preview(key, draft)
            new_aliases = []
            for alias in (draft.get("aliases") or []):
                alias = _normalise_alias(alias)
                if alias and alias not in entry["aliases"] and alias not in new_aliases:
                    new_aliases.append(alias)
            new_keywords = []
            for word in (draft.get("keywords") or []):
                word = str(word).strip().lower()
                if word and word not in entry["keywords"] and word not in new_keywords:
                    new_keywords.append(word)
            lessons = _draft_lessons(draft)
            new_lessons = []
            for lesson in lessons:
                text = str(lesson or "").strip()
                if text and text not in new_lessons:
                    new_lessons.append(text[:240])
                if len(new_lessons) >= 8:
                    break
            changed = bool(new_aliases or new_keywords or new_lessons or
                           preview.strip() != existing.strip())
            if not changed:
                return {"action": "merge_noop", "key": key,
                        "version": entry.get("version")}
            entry["aliases"] = (entry["aliases"] + new_aliases)[:12]
            entry["keywords"] = (entry["keywords"] + new_keywords)[:30]
            for lesson in new_lessons:
                digest = _lesson_hash(lesson)
                row = entry["lessons"].get(digest)
                if row:
                    row["n"] = _int(row.get("n"), 0) + 1
                    row["last"] = _now_iso(self._clock)
                else:
                    entry["lessons"][digest] = {
                        "n": 1, "first": _now_iso(self._clock),
                        "last": _now_iso(self._clock), "sample": _lesson_sample(lesson),
                    }
            if entry.get("status") == "retired":
                entry["status"] = "candidate"
            entry["refresh"]["last_version"] = _int(entry.get("version"), 0) + 1
            new_entry = self.add_version(
                key, preview, "merge", review=review, run_id=run_id,
                cluster_id=draft.get("cluster_id") or draft.get("_cluster_id"))
            return {"action": "merge", "key": key,
                    "version": new_entry.get("version"),
                    "status": entry.get("status"), "merged_into": None}

    def merge_preview(self, target_key, draft) -> str:
        key = self.resolve_key(target_key)
        existing = self.content(key) if key else ""
        try:
            from .skill_author import merge_preview as author_preview
            return author_preview(existing, draft)
        except Exception:
            return _fallback_merge(existing, draft)

    # -- statistics / lessons ------------------------------------------------

    def note_run(self, key, run, baseline=None) -> dict:
        run = run if isinstance(run, dict) else {}
        with self._locked():
            self.load()
            resolved = self.resolve_key(key)
            if resolved is None:
                return {"skipped": "unknown"}
            entry = self._data["skills"][resolved]
            stats = entry.setdefault("stats", _default_stats())
            run_id = str(run.get("id") or run.get("run_id") or "")
            recent = list(stats.get("recent_runs") or [])
            if run_id and run_id in recent:
                return {"stats": copy.deepcopy(stats),
                        "recommendation": "hold", "reason": "duplicate_run"}
            score = _num(run.get("quality_score"))
            last_review = None
            for item in reversed(run.get("review_rounds") or []):
                if isinstance(item, dict) and item.get("kind") == "review":
                    last_review = item
                    break
            blocking = _blocking_count((last_review or {}).get("blocking_defects"))
            tolerance = _num(_get(self.cfg, "auto_skills_score_tolerance", 0.3), 0.3)
            if tolerance is None:
                tolerance = 0.3
            window = list(stats.get("window") or [])
            baseline_at_use = None
            if score is not None:
                if stats.get("baseline_ref") is None and entry.get("status") in ("seed", "active"):
                    stats["baseline_ref"] = copy.deepcopy(baseline) \
                        if isinstance(baseline, dict) else None
                ref = stats.get("baseline_ref") if isinstance(stats.get("baseline_ref"), dict) else None
                baseline_at_use = _num((ref or {}).get("median"))
                stats["score_count"] = _int(stats.get("score_count"), 0) + 1
                stats["score_sum"] = _num(stats.get("score_sum"), 0.0) + score
                stats["last_score"] = score
                window = (window + [{"run_id": run_id, "score": score,
                                     "blocking": blocking,
                                     "baseline": baseline_at_use}])[-20:]
                stats["blocking_total"] = _int(stats.get("blocking_total"), 0) + blocking
                if baseline_at_use is not None:
                    if score >= baseline_at_use - tolerance:
                        stats["wins"] = _int(stats.get("wins"), 0) + 1
                    if score < baseline_at_use - tolerance:
                        stats["losses"] = _int(stats.get("losses"), 0) + 1
                    if score < baseline_at_use - 1.0:
                        stats["consecutive_bad"] = _int(stats.get("consecutive_bad"), 0) + 1
                    else:
                        stats["consecutive_bad"] = 0
            stats["uses"] = _int(stats.get("uses"), 0) + 1
            stats["last_run"] = run_id or stats.get("last_run")
            stats["last_score"] = stats.get("last_score")
            stats["baseline_at_use"] = baseline_at_use
            stats["window"] = window
            if run_id:
                stats["recent_runs"] = (recent + [run_id])[-50:]
            stats["baseline_ref"] = stats.get("baseline_ref") if isinstance(
                stats.get("baseline_ref"), dict) else None
            entry["updated"] = _now_iso(self._clock)
            recommendation, reason = _recommendation(entry, self.cfg)
            self.save()
            return {"stats": copy.deepcopy(stats), "recommendation": recommendation,
                    "reason": reason}

    def note_lesson(self, key, text, run_id=None) -> dict:
        with self._locked():
            self.load()
            resolved = self.resolve_key(key)
            if resolved is None:
                return {"skipped": "unknown"}
            entry = self._data["skills"][resolved]
            digest = _lesson_hash(text)
            if not digest:
                return {"skipped": "empty"}
            row = entry["lessons"].get(digest)
            now = _now_iso(self._clock)
            if row:
                row["n"] = _int(row.get("n"), 0) + 1
                row["last"] = now
            else:
                entry["lessons"][digest] = {
                    "n": 1, "first": now, "last": now, "sample": _lesson_sample(text),
                }
            if len(entry["lessons"]) > 100:
                rows = sorted(entry["lessons"].items(),
                              key=lambda pair: str(pair[1].get("last") or
                                                   pair[1].get("first") or ""))
                for old_hash, _ in rows[:len(rows) - 100]:
                    entry["lessons"].pop(old_hash, None)
            entry["updated"] = now
            self.save()
            return {"key": resolved, "hash": digest,
                    "n": _int(entry["lessons"].get(digest, {}).get("n"), 0),
                    "lessons": len(entry["lessons"])}

    def set_status(self, key, status, reason, run_id=None, approved=None) -> dict:
        status = str(status or "").strip().lower()
        if status not in LIFECYCLE_STATUSES:
            raise RegistryError("invalid lifecycle status: %r" % (status,))
        with self._locked():
            self.load()
            resolved = self.resolve_key(key)
            if resolved is None:
                raise RegistryError("unknown skill key")
            entry = self._data["skills"][resolved]
            entry["status"] = status
            if approved is not None:
                entry["approved"] = bool(approved)
            if status == "retired":
                entry["retired_reason"] = str(reason or "manual")[:200]
            entry["updated"] = _now_iso(self._clock)
            self.save()
            return copy.deepcopy(entry)

    # -- validation ----------------------------------------------------------

    def validate(self) -> list:
        problems = []
        try:
            data = self.load()
        except Exception as exc:
            return ["load failed: %s" % type(exc).__name__]
        for key, entry in (data.get("skills") or {}).items():
            try:
                validate_key(key)
            except RegistryError:
                problems.append("invalid key: %r" % key)
                continue
            if not isinstance(entry, dict):
                problems.append("%s: entry is not an object" % key)
                continue
            if entry.get("task_type") not in TASK_TYPES:
                problems.append("%s: invalid task type" % key)
            if entry.get("status") not in LIFECYCLE_STATUSES:
                problems.append("%s: invalid status" % key)
            if not entry.get("title"):
                problems.append("%s: missing title" % key)
            live = self.directory / (key + ".md")
            text = _read_text(live)
            recorded = entry.get("content_sha256")
            if text is None:
                problems.append("%s: missing live file" % key)
            elif recorded and sha256_text(text) != recorded:
                problems.append("%s: live checksum mismatch" % key)
            version = _int(entry.get("version"), 0)
            if version >= 1:
                try:
                    self.read_version(key, version)
                except RegistryError:
                    problems.append("%s: current version is not readable" % key)
        return problems


# ---------------------------------------------------------------------------
# module-level helpers used by the class


def _probe_writable(path) -> bool:
    cache_key = os.path.abspath(str(path))
    if cache_key in _PROBE_CACHE:
        return _PROBE_CACHE[cache_key]
    ok = False
    try:
        folder = Path(path)
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".skill-write-probe"
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                probe.unlink()
            except OSError:
                pass
            fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            probe.unlink()
        except OSError:
            pass
        ok = True
    except OSError:
        ok = False
    _PROBE_CACHE[cache_key] = ok
    return ok


def _lock_is_stale(lock_path) -> bool:
    path = Path(lock_path)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    if age > 60:
        return True
    if os.name == "nt":
        return False
    try:
        text = path.read_text(encoding="ascii", errors="ignore")
        first = text.splitlines()[0].strip() if text.splitlines() else ""
        pid = int(first)
    except Exception:
        return False
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    return False


def _take_over_lock(lock_path) -> None:
    path = Path(lock_path)
    stale = path.with_name(path.name + ".stale-%d" % int(time.time() * 1000))
    try:
        os.replace(str(path), str(stale))
    except OSError:
        return
    try:
        stale.unlink()
    except OSError:
        pass


def _index_bytes(data) -> int:
    try:
        return len(json.dumps(data, sort_keys=True, indent=2,
                              ensure_ascii=False).encode("utf-8"))
    except Exception:
        return MAX_INDEX_BYTES + 1


def _trim_index(data) -> tuple:
    """Apply PLAN 10.1 trim ladder; return (data, dropped_version_files)."""
    dropped = []
    if _index_bytes(data) <= MAX_INDEX_BYTES:
        return data, dropped
    data["warnings"] = list(data.get("warnings") or [])[-5:]
    if _index_bytes(data) <= MAX_INDEX_BYTES:
        return data, dropped
    lesson_rows = []
    for key, entry in (data.get("skills") or {}).items():
        lessons = entry.get("lessons") or {}
        for digest, row in lessons.items():
            if isinstance(row, dict) and row.get("sample"):
                lesson_rows.append((str(row.get("last") or row.get("first") or ""),
                                    key, digest))
    for _, key, digest in sorted(lesson_rows):
        row = data["skills"][key].get("lessons", {}).get(digest)
        if isinstance(row, dict):
            row.pop("sample", None)
        if _index_bytes(data) <= MAX_INDEX_BYTES:
            return data, dropped
    for key, entry in (data.get("skills") or {}).items():
        lessons = entry.get("lessons") or {}
        if len(lessons) <= 20:
            continue
        rows = sorted(lessons.items(),
                      key=lambda pair: str((pair[1] or {}).get("last") or
                                           (pair[1] or {}).get("first") or ""))
        for digest, _ in rows[:len(rows) - 20]:
            lessons.pop(digest, None)
        if _index_bytes(data) <= MAX_INDEX_BYTES:
            return data, dropped
    for entry in (data.get("skills") or {}).values():
        stats = entry.get("stats") or {}
        stats["window"] = list(stats.get("window") or [])[-5:]
        stats["recent_runs"] = list(stats.get("recent_runs") or [])[-10:]
    if _index_bytes(data) <= MAX_INDEX_BYTES:
        return data, dropped
    for key, entry in (data.get("skills") or {}).items():
        history = list(entry.get("history") or [])
        normal = [row for row in history if row.get("reason") != "seed" and not (
            _int(row.get("version"), 0) == 1 and entry.get("source") == "builtin")]
        normal.sort(key=lambda row: _int(row.get("version"), 0))
        while normal and _index_bytes(data) > MAX_INDEX_BYTES:
            row = normal.pop(0)
            history = [item for item in history if item is not row]
            version = _int(row.get("version"), 0)
            dropped.append((".versions/%s/v%04d.md" % (key, version),
                            ".versions/%s/v%04d.json" % (key, version)))
        entry["history"] = history
        if _index_bytes(data) <= MAX_INDEX_BYTES:
            return data, dropped
    cutoff = dt.datetime.now(_UTC) - dt.timedelta(days=30)
    for cluster_id, row in list((data.get("cluster_builds") or {}).items()):
        if not isinstance(row, dict) or row.get("status") != "done":
            continue
        try:
            started = dt.datetime.fromisoformat(str(row.get("started") or ""))
            if started.tzinfo is None:
                started = started.replace(tzinfo=_UTC)
            if started < cutoff:
                data["cluster_builds"].pop(cluster_id, None)
        except Exception:
            continue
        if _index_bytes(data) <= MAX_INDEX_BYTES:
            return data, dropped
    while (data.get("baselines") or {}) and _index_bytes(data) > MAX_INDEX_BYTES:
        oldest = sorted(data["baselines"].items(),
                        key=lambda pair: str((pair[1] or {}).get("updated") or ""))[0][0]
        data["baselines"].pop(oldest, None)
    return data, dropped


def _version_meta(key, version, content, reason, review_score, review_rounds,
                  run_id, status_at_version, parent, restored_from, cluster_id) -> dict:
    return {
        "schema": SCHEMA_VERSION, "key": key, "version": version,
        "sha256": sha256_text(content), "chars": len(content),
        "created": _now_iso(), "reason": reason,
        "review_score": _num(review_score),
        "review_rounds": _int(review_rounds, 0) if review_rounds is not None else 0,
        "status_at_version": status_at_version,
        "run_id": run_id, "parent": parent, "restored_from": restored_from,
        "cluster_id": cluster_id,
    }


def _entry_meta(entry, version) -> dict:
    return {
        "schema": SCHEMA_VERSION, "key": entry.get("key"),
        "title": entry.get("title"), "aliases": list(entry.get("aliases") or []),
        "keywords": list(entry.get("keywords") or []),
        "task_type": entry.get("task_type"), "status": entry.get("status"),
        "version": version, "source": entry.get("source"),
        "created": entry.get("created"),
    }


def _review_block(review, clock=None) -> dict:
    review = review if isinstance(review, dict) else {}
    blocking = review.get("blocking_defects")
    return {
        "score": _num(review.get("score")),
        "rounds": _int(review.get("rounds"), 0),
        "blocking": len(blocking) if isinstance(blocking, list) else _int(blocking, 0),
        "reviewer": "skill-review", "at": _now_iso(clock),
    }


def _draft_body(draft) -> str:
    for name in ("content", "rendered", "body", "markdown"):
        value = draft.get(name)
        if isinstance(value, str) and value.strip():
            return value
    title = str(draft.get("title") or draft.get("key") or "Skill")
    description = str(draft.get("description") or "")
    items = []
    for item in draft.get("checklist") or draft.get("checks") or []:
        text = item.get("text") if isinstance(item, dict) else item
        if text:
            items.append("- [ ] %s" % str(text).strip())
    parts = ["# %s" % title, "", description, "", "## Brief and acceptance criteria", ""]
    parts.extend(items or ["- [ ] Restate the task and acceptance criteria before acting."])
    parts.extend(["", "## Lessons learned",
                  "<!-- Add only verified, reusable lessons here. -->", ""])
    return "\n".join(parts)


def _draft_lessons(draft) -> list:
    value = draft.get("lessons")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item.get("text") if isinstance(item, dict) else item)
                for item in value if item]
    return []


def _blocking_count(value) -> int:
    if isinstance(value, (list, tuple)):
        return len(value)
    return max(0, _int(value, 0))


def _lesson_tokens(text) -> list:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _lesson_hash(text) -> str:
    normalised = " ".join(_lesson_tokens(text))
    if not normalised:
        return ""
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def _lesson_sample(text) -> str:
    sample = str(text or "").strip()
    try:
        from .skills import sanitise_lesson
        sample = str(sanitise_lesson(sample))
    except Exception:
        pass
    return sample[:200]


def _fallback_merge(existing, draft) -> str:
    text = existing or ""
    meta, body = split_front_matter(text)
    if meta is not None:
        meta["aliases"] = list(dict.fromkeys(
            list(meta.get("aliases") or []) +
            [_normalise_alias(a) for a in (draft.get("aliases") or []) if _normalise_alias(a)]))[:12]
        meta["keywords"] = list(dict.fromkeys(
            list(meta.get("keywords") or []) +
            [str(k).strip().lower() for k in (draft.get("keywords") or []) if str(k).strip()]))[:30]
        text = render_front_matter(meta, body)
    lines = text.splitlines()
    items = []
    for item in (draft.get("checklist") or [])[:6]:
        value = item.get("text") if isinstance(item, dict) else item
        if value:
            items.append("- [ ] %s" % str(value).strip())
    if items:
        header = "## Learned checks (auto)"
        if header not in lines:
            insert_at = len(lines)
            for index, line in enumerate(lines):
                if line.strip() == "## Lessons learned":
                    insert_at = index
                    break
            lines[insert_at:insert_at] = [header, ""]
        else:
            insert_at = lines.index(header) + 2
        lines[insert_at:insert_at] = items
    lessons = _draft_lessons(draft)[:8]
    if lessons:
        if "## Lessons learned" not in [line.strip() for line in lines]:
            lines.extend(["", "## Lessons learned", ""])
        for lesson in lessons:
            lines.append("- %s" % lesson.strip()[:240])
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _recommendation(entry, cfg) -> tuple:
    stats = entry.get("stats") or {}
    ref = stats.get("baseline_ref") if isinstance(stats.get("baseline_ref"), dict) else None
    window = [row for row in (stats.get("window") or [])
              if isinstance(row, dict) and _num(row.get("score")) is not None]
    if not ref or _num(ref.get("median")) is None or not window:
        return "hold", "no_baseline"
    promote_after = max(1, _int(_get(cfg, "auto_skills_promote_after", 5), 5))
    demote_after = max(1, _int(_get(cfg, "auto_skills_demote_after", 3), 3))
    min_confidence = _num(_get(cfg, "auto_skills_min_confidence", 0.55), 0.55)
    if _int(stats.get("score_count"), 0) < promote_after:
        return "hold", "insufficient_evidence"
    baseline = _num(ref.get("median"), 0.0)
    tolerance = _num(_get(cfg, "auto_skills_score_tolerance", 0.3), 0.3)
    scores = sorted(_num(row.get("score"), 0.0) for row in window[-5:])
    middle = len(scores) // 2
    median = scores[middle] if len(scores) % 2 else (scores[middle - 1] + scores[middle]) / 2.0
    blocking_rate = _blocking_rate(window)
    bad = _int(stats.get("consecutive_bad"), 0)
    losses = _int(stats.get("losses"), 0)
    wins = _int(stats.get("wins"), 0)
    if bad >= demote_after:
        return "demote", "consecutive_bad"
    if blocking_rate >= 0.5 and len(window) >= 3:
        return "demote", "blocking_rate"
    if losses >= demote_after and median < baseline - tolerance:
        return "demote", "under_baseline"
    if (wins >= promote_after and median >= baseline - tolerance
            and blocking_rate <= 0.25 and bad == 0):
        return "promote", "above_baseline"
    if median >= baseline + max(0.5, min_confidence):
        return "promote", "strong_scores"
    return "hold", "within_baseline"


def _blocking_rate(rows) -> float:
    scored = [row for row in rows if isinstance(row, dict)
              and _num(row.get("score")) is not None]
    if not scored:
        return 0.0
    blocked = [row for row in scored if _int(row.get("blocking"), 0) >= 1]
    return float(len(blocked)) / float(len(scored))


def _unlink_quiet(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
