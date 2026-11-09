"""F2 written predictions: read, validate and compare ``PLAN.json``.

The builder declares the files, tests and risks it expects before the first
edit.  This module is a pure, dependency-free read/diff layer: it never
changes a score or a run by itself, and the caller decides whether a mismatch
is a warning or a blocking defect.

Schema (version 1)::

    {"version": 1, "task_type": "code",
     "files_expected": ["src/a.py"], "tests_expected": ["tests/test_a.py"],
     "risks": ["flaky fixture"], "write_set": ["src/"]}

Paths are normalised to POSIX form, declared entries may be literals or globs
(``*``, ``?`` and ``**``) and ``write_set`` entries are file/directory
prefixes at a segment boundary.  Plan bodies are never persisted: records
carry bounded counts, 40/80-character path samples and one-line notes only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

try:  # pragma: no cover - api is import-safe in practice
    from .api import redact_secrets
except Exception:  # pragma: no cover

    def redact_secrets(value):
        return str(value or "")

from . import path_scope


PLAN_SCHEMA = 1
PLAN_DEFAULT_FILE = "PLAN.json"
PLAN_MODES = ("warn", "block", "off")
MAX_MISMATCHES = 40
MAX_PATHS = 50
MAX_RECORD_BYTES = 8192
MAX_TEXT_BYTES = 64 * 1024
MAX_TEXT_FILES = 25
_STALE_SLACK = 2.0
_LATE_SLACK = 2.0
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_KEYWORD_RE = re.compile(r"[a-z0-9_]{4,}")
_STOPWORDS = frozenset(
    (
        "about",
        "after",
        "again",
        "also",
        "because",
        "before",
        "being",
        "between",
        "could",
        "file",
        "files",
        "from",
        "have",
        "into",
        "might",
        "must",
        "need",
        "needs",
        "other",
        "should",
        "some",
        "test",
        "tests",
        "than",
        "that",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "using",
        "used",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "would",
    )
)


# ---------------------------------------------------------------------------
# config helpers (defensive: config keys may not exist yet)

def _cfg_get(cfg, name, default=None):
    """Read ``name`` from a dict or a Config-like object, never raising."""
    if cfg is None:
        return default
    value = None
    found = False
    if isinstance(cfg, dict):
        if name in cfg:
            value = cfg.get(name)
            found = True
    else:
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                value = getter(name, default)
                found = True
            except TypeError:
                try:
                    value = getter(name)
                    found = True
                except Exception:
                    found = False
            except Exception:
                found = False
        if not found:
            value = getattr(cfg, name, default)
            found = True
    if not found or value is None:
        return default
    return value


def _cfg_int(cfg, name, default, low=1, high=10000):
    value = _cfg_get(cfg, name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def plan_mode(cfg):
    """Return ``warn`` | ``block`` | ``off``; unknown values fall back to warn."""
    value = _cfg_get(cfg, "plan_contract", "warn")
    if isinstance(value, str):
        value = value.strip().lower()
    value = value if value in PLAN_MODES else "warn"
    return value


def plan_path(cfg):
    """Return the normalised workspace-relative plan path."""
    raw = _cfg_get(cfg, "plan_file", PLAN_DEFAULT_FILE)
    path = normalize_path(raw) if isinstance(raw, str) else None
    return path or PLAN_DEFAULT_FILE


# ---------------------------------------------------------------------------
# path normalisation and matching

def normalize_path(value):
    """Normalise a declared path or return ``None`` when it is unsafe.

    Backslashes become slashes, ``./`` and empty segments are dropped, and
    absolute paths, drive letters, ``..`` segments, NUL and the workspace root
    are rejected.  The comparison is case-sensitive on POSIX.
    """
    if not isinstance(value, str):
        return None
    if "\x00" in value:
        return None
    raw = value.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or _DRIVE_RE.match(raw):
        return None
    parts = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            return None
        parts.append(segment)
    if not parts:
        return None
    path = "/".join(parts)
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        path = os.path.normcase(path)
    return path


def is_plan_path(path, cfg):
    """True when ``path`` is the configured plan file (POSIX case-sensitive)."""
    normal = normalize_path(path)
    if normal is None:
        return False
    return normal == plan_path(cfg)


def _glob_regex(pattern):
    out = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def _has_glob(entry):
    return "*" in entry or "?" in entry


def _entry_matches(entry, path):
    if not isinstance(entry, str) or not isinstance(path, str):
        return False
    if _has_glob(entry):
        return bool(_glob_regex(entry).match(path))
    return entry == path


def _prefix_matches(entry, path):
    """Write-set entry: exact file or directory prefix at a segment boundary."""
    if not isinstance(entry, str) or not isinstance(path, str):
        return False
    entry = entry.rstrip("/")
    if not entry:
        return False
    if _has_glob(entry):
        regex = _glob_regex(entry)
        if regex.match(path):
            return True
        parts = path.split("/")
        return any(regex.match("/".join(parts[:index])) for index in range(1, len(parts)))
    return path == entry or path.startswith(entry + "/")


def _is_test_path(path):
    lowered = path.lower()
    base = lowered.rsplit("/", 1)[-1]
    if base.startswith("test_") or base.endswith("_test.py") or base == "test.py":
        return True
    wrapped = "/" + lowered + "/"
    return "/tests/" in wrapped or "/test/" in wrapped or lowered.startswith("tests/")


# ---------------------------------------------------------------------------
# validation

def _path_list(data, key, errors):
    raw = data.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("%s must be a list" % key)
        return []
    result = []
    seen = set()
    for item in raw:
        path = normalize_path(item)
        if path is None:
            errors.append("%s: invalid path" % key)
            continue
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def validate_plan(data, cfg):
    """Return ``(normalised, errors)`` for a decoded PLAN.json object."""
    errors = []
    if not isinstance(data, dict):
        return {}, ["plan must be a JSON object"]
    version = data.get("version")
    if isinstance(version, bool) or version != PLAN_SCHEMA:
        errors.append("version must be %d" % PLAN_SCHEMA)

    task_type = data.get("task_type", "")
    if task_type is None:
        task_type = ""
    if not isinstance(task_type, str):
        errors.append("task_type must be a string")
        task_type = ""
    task_type = task_type.strip()[:80]

    files = _path_list(data, "files_expected", errors)
    tests = _path_list(data, "tests_expected", errors)
    write_set = _path_list(data, "write_set", errors)

    max_files = _cfg_int(cfg, "plan_max_files", 200, 1, 10000)
    max_risks = _cfg_int(cfg, "plan_max_risks", 20, 1, 10000)
    truncated = False
    if len(files) > max_files:
        files = files[:max_files]
        truncated = True
    remaining = max(0, max_files - len(files))
    if len(tests) > remaining:
        tests = tests[:remaining]
        truncated = True

    raw_risks = data.get("risks", [])
    if raw_risks is None:
        raw_risks = []
    if not isinstance(raw_risks, list):
        errors.append("risks must be a list")
        raw_risks = []
    risks = []
    seen_risks = set()
    for item in raw_risks:
        mitigation = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("risk")
            mitigation = item.get("mitigation", "")
            if not isinstance(text, str):
                errors.append("risks: risk must be a string")
                continue
            if not isinstance(mitigation, str):
                errors.append("risks: mitigation must be a string")
                mitigation = ""
        else:
            errors.append("risks: entry must be a string or object")
            continue
        text = text.strip()[:300]
        mitigation = mitigation.strip()[:300]
        if not text:
            errors.append("risks: risk must not be empty")
            continue
        pair = (text, mitigation)
        if pair in seen_risks:
            continue
        seen_risks.add(pair)
        risks.append({"risk": text, "mitigation": mitigation})
    if len(risks) > max_risks:
        risks = risks[:max_risks]
        truncated = True

    normalised = {
        "version": PLAN_SCHEMA,
        "task_type": task_type,
        "files_expected": files,
        "tests_expected": tests,
        "risks": risks,
        "write_set": write_set,
        "truncated": truncated,
    }
    return normalised, errors


# ---------------------------------------------------------------------------
# reading

def read_plan(workspace, cfg):
    """Read and validate the plan; OSError-safe.

    Returns ``{status, data, mtime, reason}`` where status is ``ok``,
    ``absent`` or ``invalid`` and mtime is POSIX seconds.
    """
    rel = plan_path(cfg)
    not_found = {"status": "absent", "data": None, "mtime": None, "reason": "plan not found"}
    if workspace is not None:
        try:
            workspace = os.fspath(workspace)
        except TypeError:
            workspace = None
    if not workspace or not isinstance(workspace, str):
        return not_found
    full = os.path.join(workspace, *rel.split("/"))
    try:
        stat = os.stat(full)
    except FileNotFoundError:
        return not_found
    except OSError:
        return {
            "status": "invalid",
            "data": None,
            "mtime": None,
            "reason": "plan is unreadable",
        }
    if not os.path.isfile(full):
        return {
            "status": "invalid",
            "data": None,
            "mtime": stat.st_mtime,
            "reason": "plan is not a regular file",
        }
    if stat.st_size > 1024 * 1024:
        return {
            "status": "invalid",
            "data": None,
            "mtime": stat.st_mtime,
            "reason": "plan is too large",
        }
    try:
        with open(full, "rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        text = raw.decode("utf-8-sig", "replace")
        data = json.loads(text)
    except OSError:
        return {
            "status": "invalid",
            "data": None,
            "mtime": stat.st_mtime,
            "reason": "plan is unreadable",
        }
    except (ValueError, UnicodeError):
        return {
            "status": "invalid",
            "data": None,
            "mtime": stat.st_mtime,
            "reason": "plan is not valid JSON",
        }
    normalised, errors = validate_plan(data, cfg)
    if errors:
        reason = redact_secrets("; ".join(errors))[:300]
        return {
            "status": "invalid",
            "data": normalised,
            "mtime": stat.st_mtime,
            "reason": reason,
        }
    return {"status": "ok", "data": normalised, "mtime": stat.st_mtime, "reason": ""}


# ---------------------------------------------------------------------------
# snapshots and the actual diff

def _to_seconds(value):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 1e12:  # snapshot_workspace stores st_mtime_ns
        seconds /= 1e9
    return seconds


def _snapshot_map(snapshot):
    """Accept ``{path: (mtime, size)}``, ``{"files": {...}}`` and dict entries."""
    if not snapshot or not isinstance(snapshot, dict):
        return {}
    files = snapshot.get("files")
    if isinstance(files, dict):
        snapshot = files
    result = {}
    for path, value in snapshot.items():
        if not isinstance(path, str):
            continue
        normal = normalize_path(path)
        if normal is None:
            continue
        mtime = size = None
        if isinstance(value, dict):
            for key in ("mtime_ns", "st_mtime_ns"):
                if key in value:
                    mtime = _to_seconds(value[key])
                    break
            else:
                for key in ("mtime", "st_mtime"):
                    if key in value:
                        mtime = _to_seconds(value[key])
                        break
            size = value.get("size", value.get("st_size"))
        elif isinstance(value, (tuple, list)) and len(value) >= 2:
            mtime = _to_seconds(value[0])
            size = value[1]
        else:
            mtime = _to_seconds(value)
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        result[normal] = (mtime, size)
    return result


def _extra_wave_paths(cfg, before, after):
    values = _cfg_get(cfg, "plan_extra_wave_paths", None)
    if values is None:
        for snapshot in (after, before):
            if isinstance(snapshot, dict) and isinstance(snapshot.get("extra_wave_paths"), list):
                values = snapshot["extra_wave_paths"]
                break
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    for value in values:
        normal = normalize_path(value)
        if normal and normal not in result:
            result.append(normal)
    return result


def actual_changes(workspace, before, after, cfg):
    """Diff two snapshots without the plan and return the actual change set."""
    if workspace is not None:
        try:
            workspace = os.fspath(workspace)
        except TypeError:
            workspace = None
    before_map = _snapshot_map(before)
    after_map = _snapshot_map(after)
    plan_rel = plan_path(cfg)
    created, modified, deleted, ignored = [], [], [], []
    for path in sorted(set(before_map) | set(after_map)):
        if path == plan_rel:
            ignored.append(path)
            continue
        if path_scope.is_artifact(path, cfg):
            ignored.append(path)
            continue
        if path not in before_map:
            created.append(path)
        elif path not in after_map:
            deleted.append(path)
        else:
            before_mtime, before_size = before_map[path]
            after_mtime, after_size = after_map[path]
            changed = False
            if before_mtime is not None and after_mtime is not None:
                changed = before_mtime != after_mtime
            if not changed and before_size is not None and after_size is not None:
                changed = before_size != after_size
            if changed:
                modified.append(path)
    first_change = None
    for path in created + modified:
        mtime = after_map.get(path, (None, None))[0]
        if mtime is not None and (first_change is None or mtime < first_change):
            first_change = mtime
    for path in deleted:
        mtime = before_map.get(path, (None, None))[0]
        if mtime is not None and (first_change is None or mtime < first_change):
            first_change = mtime
    extra = _extra_wave_paths(cfg, before, after)
    result = {
        "created": created,
        "modified": modified,
        "deleted": deleted,
        "ignored": sorted(ignored),
        "extra_wave_paths": extra,
        "first_change_mtime": first_change,
    }
    if workspace and isinstance(workspace, str):
        # Internal only: lets compare_plan() sample changed text for risk_hit.
        # It is never copied into the bounded run record.
        result["_root"] = os.path.abspath(workspace)
    return result


# ---------------------------------------------------------------------------
# comparison

def _keywords(text):
    lowered = text.lower()
    result = []
    for match in _KEYWORD_RE.finditer(lowered):
        word = match.group(0)
        if word in _STOPWORDS or word in result:
            continue
        result.append(word)
    return result


def _read_sample(root, path):
    if not root:
        return ""
    full = os.path.join(root, *path.split("/"))
    try:
        with open(full, "rb") as handle:
            raw = handle.read(MAX_TEXT_BYTES)
    except (OSError, ValueError):
        return ""
    return raw.decode("utf-8", "ignore").lower()


_PRODUCT_MARKERS = (
    ".git", "pyproject.toml", "setup.py", "package.json", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json",
)


def _verified_product_prefix(changed, declared, actual):
    """Return a checkout subfolder that wraps the product, or ``""``.

    Verification is deliberately conservative.  The prefix must be the single
    top-level segment of every changed path, must not already appear in a
    declared entry, and must be evidenced by the workspace layout: the
    workspace root is not itself a checkout while ``<root>/<prefix>`` is one
    (a ``.git`` entry or a product manifest).  Without a known root there is
    no evidence, so no normalisation happens.
    """
    prefix = path_scope.common_leading_dir(changed)
    if not prefix:
        return ""
    for entry in declared:
        if not isinstance(entry, str):
            continue
        if entry == prefix or entry.startswith(prefix + "/"):
            return ""
    root = actual.get("_root") if isinstance(actual, dict) else None
    if not isinstance(root, str) or not root or not os.path.isdir(root):
        return ""
    base = os.path.join(root, prefix)
    if not os.path.isdir(base):
        return ""
    if os.path.exists(os.path.join(root, ".git")):
        # The workspace itself is the checkout: paths are already relative.
        return ""
    if os.path.exists(os.path.join(base, ".git")):
        return prefix
    root_marker = any(os.path.exists(os.path.join(root, marker))
                      for marker in _PRODUCT_MARKERS)
    base_marker = any(os.path.exists(os.path.join(base, marker))
                      for marker in _PRODUCT_MARKERS)
    if base_marker and not root_marker:
        return prefix
    return ""


def compare_plan(plan, actual):
    """Return a bounded, deterministic mismatch list for a normalised plan."""
    if not isinstance(plan, dict):
        return []
    if isinstance(plan.get("data"), dict):
        plan = plan["data"]
    actual = actual or {}
    changed = []
    for key in ("created", "modified", "deleted"):
        for path in actual.get(key) or []:
            if isinstance(path, str) and path not in changed:
                changed.append(path)
    changed_set = set(changed)
    extra = set(
        path for path in (actual.get("extra_wave_paths") or []) if isinstance(path, str)
    )
    files = [p for p in (plan.get("files_expected") or []) if isinstance(p, str)]
    tests = [p for p in (plan.get("tests_expected") or []) if isinstance(p, str)]
    write_set = [p for p in (plan.get("write_set") or []) if isinstance(p, str)]
    mismatches = []

    # When the workspace wraps the product in one checkout subfolder
    # (``repo/``), the diff paths carry that leading segment while PLAN.json
    # declares product-relative paths.  Only strip a prefix that is *verified*
    # from the workspace layout: a lone top-level directory is not enough, or
    # a plan that drops a package directory would silently "match".
    prefix = _verified_product_prefix(
        changed, files + tests + write_set, actual)
    if prefix:
        variants = {
            path: path_scope.relative_variants(path, prefix) for path in changed
        }
    else:
        variants = {path: [path] for path in changed}

    def _matches_changed(entry):
        for path in changed:
            for variant in variants[path]:
                if _entry_matches(entry, variant):
                    return True
        return False

    def _write_set_covers(path):
        for candidate in variants.get(path) or [path]:
            for entry in write_set:
                if _prefix_matches(entry, candidate):
                    return True
        return False

    for entry in files:
        if _is_test_path(entry):
            continue
        if not _matches_changed(entry):
            mismatches.append(
                {"type": "missing_file", "path": entry, "detail": "declared file was not changed"}
            )
    declared_tests = list(tests) + [p for p in files if _is_test_path(p)]
    seen_tests = set()
    for entry in declared_tests:
        if entry in seen_tests:
            continue
        seen_tests.add(entry)
        if not _matches_changed(entry):
            mismatches.append(
                {"type": "missing_test", "path": entry, "detail": "declared test was not changed"}
            )

    for path in sorted(changed_set):
        if path in extra:
            continue
        if any(_entry_matches(entry, variant)
               for variant in variants[path] for entry in files):
            continue
        if any(_entry_matches(entry, variant)
               for variant in variants[path] for entry in tests):
            continue
        if _write_set_covers(path):
            continue
        if _is_test_path(path):
            mismatches.append(
                {"type": "unplanned_test", "path": path, "detail": "test was not declared"}
            )
        else:
            mismatches.append(
                {"type": "unexpected_file", "path": path, "detail": "not declared"}
            )

    hit_keywords = set()
    text_paths = [p for p in changed if p in changed_set and p not in extra][:MAX_TEXT_FILES]
    text_cache = {}
    for item in plan.get("risks") or []:
        text = item if isinstance(item, str) else ""
        if isinstance(item, dict):
            text = item.get("risk") or ""
        for keyword in _keywords(str(text)):
            if keyword in hit_keywords:
                continue
            hit_path = next((p for p in changed if keyword in p.lower()), None)
            if hit_path is not None:
                hit_keywords.add(keyword)
                mismatches.append(
                    {"type": "risk_hit", "path": hit_path, "detail": "risk keyword in path"}
                )
                continue
            for path in text_paths:
                if path not in text_cache:
                    root = actual.get("_root")
                    text_cache[path] = _read_sample(root, path)
                if keyword in text_cache[path]:
                    hit_keywords.add(keyword)
                    mismatches.append(
                        {"type": "risk_hit", "path": path, "detail": "risk keyword in text"}
                    )
                    break

    return mismatches[:MAX_MISMATCHES]


# ---------------------------------------------------------------------------
# contract check

def _started_seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _short(value, limit=120):
    text = " ".join(str(value or "").split())
    return text[:limit]


def _mismatch_label(mismatch):
    path = _short(mismatch.get("path", ""), 40)
    if path:
        return "%s: %s" % (mismatch.get("type", "mismatch"), path)
    return str(mismatch.get("type", "mismatch"))


def _format_note(status, mismatches):
    if status == "ok":
        return "plan: ok"
    if status == "absent":
        return "plan: absent"
    if status == "invalid":
        return "plan: invalid"
    if status == "stale":
        return "plan: stale"
    if status == "optional":
        return "plan: optional (tiny job)"
    types = []
    for mismatch in mismatches:
        kind = mismatch.get("type", "mismatch")
        if kind not in types:
            types.append(kind)
    suffix = " (%s)" % ", ".join(types[:3]) if types else ""
    return "plan: %d mismatch%s" % (len(mismatches), suffix)


def format_plan_note(result):
    """Return the one-line header note (no newline); empty when skipped."""
    if not result or not isinstance(result, dict):
        return ""
    if result.get("mode") == "off" or result.get("status") == "skipped":
        return ""
    note = result.get("note") or _format_note(
        result.get("status"), result.get("mismatches") or []
    )
    return _short(redact_secrets(note), 200)


def plan_defect(result):
    """Return the single synthetic blocking defect, or ``None``."""
    if not result or not isinstance(result, dict):
        return None
    if result.get("mode") != "block":
        return None
    if not result.get("blocking"):
        return None
    defect = result.get("blocking_defect")
    return _short(redact_secrets(defect), 400) if defect else None


def _bounded_record(record):
    """Keep the JSON record under 8 KB, trimming samples and mismatches."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))

    if size(record) <= MAX_RECORD_BYTES:
        return record
    trimmed = dict(record)
    trimmed["truncated"] = True
    while trimmed.get("mismatches") and size(trimmed) > MAX_RECORD_BYTES:
        trimmed["mismatches"] = trimmed["mismatches"][:-1]
    declared = trimmed.get("declared")
    if isinstance(declared, dict):
        while declared.get("files_sample") and size(trimmed) > MAX_RECORD_BYTES:
            declared["files_sample"] = declared["files_sample"][:-1]
        while declared.get("risks") and size(trimmed) > MAX_RECORD_BYTES:
            declared["risks"] = declared["risks"][:-1]
    actual = trimmed.get("actual")
    if isinstance(actual, dict):
        while actual.get("extra_wave_paths") and size(trimmed) > MAX_RECORD_BYTES:
            actual["extra_wave_paths"] = actual["extra_wave_paths"][:-1]
    if size(trimmed) > MAX_RECORD_BYTES:
        old = trimmed.get("declared") if isinstance(trimmed.get("declared"), dict) else {}
        trimmed["declared"] = {
            "files_expected_count": old.get("files_expected_count", 0),
            "tests_expected_count": old.get("tests_expected_count", 0),
            "risks_count": old.get("risks_count", 0),
            "files_sample": [],
            "risks": [],
        }
        trimmed["mismatches"] = []
        trimmed["actual"] = {}
    if size(trimmed) > MAX_RECORD_BYTES:
        trimmed["note"] = _short(trimmed.get("note"), 80)
        trimmed["blocking_defect"] = _short(trimmed.get("blocking_defect"), 160)
    return trimmed


def _mismatch_dict(mismatch):
    return {
        "type": str(mismatch.get("type", "mismatch"))[:40],
        "path": _short(redact_secrets(mismatch.get("path", "")), 120),
        "detail": _short(redact_secrets(mismatch.get("detail", "")), 120),
    }


def check_plan_contract(workspace, cfg, run, before, after, builder_started, task_type):
    """Compare the written plan with the actual diff and return a bounded record.

    Returns ``None`` in ``off`` mode (no record, no note).  Warn mode records
    but never blocks; block mode returns one synthetic ``blocking_defect``.
    """
    mode = plan_mode(cfg)
    if mode == "off":
        return None
    if workspace is not None:
        try:
            workspace = os.fspath(workspace)
        except TypeError:
            workspace = None
    plan_rel = plan_path(cfg)
    read = read_plan(workspace, cfg)
    actual = actual_changes(workspace, before, after, cfg)
    mismatches = []
    status = read["status"]
    data = read.get("data") if isinstance(read.get("data"), dict) else None
    mtime = read.get("mtime")

    read_status = read["status"]
    product_paths = []
    for key in ("created", "modified", "deleted"):
        for path in actual.get(key) or []:
            if path_scope.classify_path(
                    path, cfg, plan_rel) == path_scope.CATEGORY_PRODUCT:
                if path not in product_paths:
                    product_paths.append(path)
    product_changes = len(product_paths)
    try:
        optional_max = int(_cfg_get(cfg, "plan_optional_max_files", 1))
    except (TypeError, ValueError):
        optional_max = 1
    optional_max = max(0, optional_max)
    tiny = 0 < product_changes <= optional_max
    reason = None
    snapshot_absent = False

    if status == "ok":
        after_map = _snapshot_map(after)
        detail = None
        if after_map and plan_rel not in after_map:
            detail = "plan was not present in the pre-gate snapshot"
        elif not after_map:
            before_map = _snapshot_map(before)
            if before_map and plan_rel not in before_map:
                detail = "plan was not present in the before snapshot"
        if detail:
            snapshot_absent = True
            status = "invalid"
            mismatches = [{
                "type": "invalid_plan",
                "path": plan_rel,
                "detail": detail,
            }]
            data = None

    if status == "absent":
        mismatches = [{
            "type": "absent_plan",
            "path": plan_rel,
            "detail": "plan file not found",
        }]
        if tiny:
            status = "optional"
            reason = "tiny_job_absent_plan"
            mismatches = []
    elif status == "invalid":
        if not mismatches:
            mismatches = [{
                "type": "invalid_plan",
                "path": plan_rel,
                "detail": _short(redact_secrets(read.get("reason", "")), 160),
            }]
        if tiny:
            status = "optional"
            reason = ("tiny_job_plan_not_in_snapshot" if snapshot_absent
                      else "tiny_job_invalid_plan")
            mismatches = []
    elif status == "ok":
        mismatches = compare_plan(data or {}, actual)
        stale = False
        started = _started_seconds(builder_started)
        if started is not None and mtime is not None and mtime < started - _STALE_SLACK:
            stale = True
            mismatches.insert(
                0,
                {
                    "type": "stale_plan",
                    "path": plan_rel,
                    "detail": "plan mtime predates the builder start",
                },
            )
        first_change = actual.get("first_change_mtime")
        if (
            mtime is not None
            and first_change is not None
            and mtime > first_change + _LATE_SLACK
        ):
            mismatches.append(
                {
                    "type": "late_plan",
                    "path": plan_rel,
                    "detail": "plan was written after the first change",
                }
            )
        status = "stale" if stale else ("mismatch" if mismatches else "ok")

    truncated = len(mismatches) > MAX_MISMATCHES
    mismatches = [_mismatch_dict(item) for item in mismatches[:MAX_MISMATCHES]]
    if status == "optional":
        note = "plan: optional (tiny job)"
        blocking = False
    else:
        note = _format_note(status, mismatches)
        blocking = mode == "block" and bool(mismatches)
    defect = None
    if blocking:
        labels = [_mismatch_label(item) for item in mismatches]
        defect = "plan_contract: %d deviation(s) (%s)" % (len(labels), "; ".join(labels))
        defect = _short(redact_secrets(defect), 400)

    declared = {}
    if data:
        files_sample = list(data.get("files_expected") or []) + list(
            data.get("tests_expected") or []
        )
        truncated = truncated or len(files_sample) > MAX_PATHS
        risks = []
        for item in (data.get("risks") or [])[:10]:
            risks.append(
                {
                    "risk": _short(redact_secrets(item.get("risk", "")), 160),
                    "mitigation": _short(redact_secrets(item.get("mitigation", "")), 160),
                }
            )
        declared = {
            "files_expected_count": len(data.get("files_expected") or []),
            "tests_expected_count": len(data.get("tests_expected") or []),
            "risks_count": len(data.get("risks") or []),
            "files_sample": [
                _short(redact_secrets(path), 80) for path in files_sample[:MAX_PATHS]
            ],
            "risks": risks,
        }

    record = {
        "schema": PLAN_SCHEMA,
        "status": status,
        "mode": mode,
        "path": plan_rel,
        "reason": reason,
        "plan_read_status": read_status,
        "task_type": _short(task_type, 40),
        "declared": declared,
        "actual": {
            "created": len(actual.get("created") or []),
            "modified": len(actual.get("modified") or []),
            "deleted": len(actual.get("deleted") or []),
            "ignored_count": len(actual.get("ignored") or []),
            "extra_wave_paths": list(actual.get("extra_wave_paths") or [])[:MAX_PATHS],
            "product_changes": product_changes,
            "tiny": bool(tiny),
        },
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "truncated": bool(truncated),
        "blocking": blocking,
        "blocking_defect": defect,
        "note": _short(note, 200),
        "checked_at": _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    return _bounded_record(record)
