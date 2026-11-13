"""F4 pipeline core: write-set admission metadata, isolated worktrees, patches
and verified installs for pipelined jobs.

Public API (frozen for 3.11.0 by S4):

    declared_write_set(args, cfg) -> dict
    prepare_args(cfg, args, run) -> dict
    note_plan(run, plan_path)
    capture_patch(run, cfg, workspace) -> dict
    finalize_run(run, cfg, workspace)
    install_pending(cfg, canonical_workspace, run_id) -> dict
    reclaim_worktrees(cfg)
    overlaps(a, b) -> bool

Helpers: ``normalise_write_set``, ``write_set_contains``,
``is_pipeline_eligible``, ``create_worktree``, ``queue_base_ref``,
``advance_queue_base``, ``touch_base_ref``, ``patch_dir``, ``patch_path`` and
``patch_sidecar_path``.

The module is deliberately defensive: every public entry point returns a
bounded dict/bool and never raises.  Standard library only, Python 3.9
compatible.  Free-form error text is passed through
``deepseek_mcp.api.redact_secrets`` and workspace paths are replaced with
``<workspace>`` before anything is persisted.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid

from . import path_scope

try:  # Keep the module importable in isolation while using the shared helper.
    from .api import redact_secrets
except Exception:  # pragma: no cover - api always exists in the package

    def redact_secrets(text):
        return str(text or "")


VERSION = 1
MAX_SLICES_TEXT = 200000
MAX_PATCH_BYTES = 64 * 1024 * 1024
LOCK_STALE_SECONDS = 600
_ARTIFACTS = ("REVIEW.md", "QUALITY_REPORT.md")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_FENCED_SLICES_RE = re.compile(
    r"```[ \t]*(?:slices?|SLICES)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE
)
_FENCED_OPEN_RE = re.compile(r"```[ \t]*(?:slices?|SLICES)[ \t]*\r?\n", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Config/argument access (works before S0 lands the new config keys)
# ---------------------------------------------------------------------------


def _cfg(cfg, key, default=None):
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                pass
        except Exception:
            pass
    return getattr(cfg, key, default)


def _cfg_bool(cfg, key, default):
    value = _cfg(cfg, key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


def _cfg_int(cfg, key, default):
    try:
        return int(_cfg(cfg, key, default))
    except (TypeError, ValueError):
        return int(default)


def _cfg_float(cfg, key, default):
    try:
        return float(_cfg(cfg, key, default))
    except (TypeError, ValueError):
        return float(default)


def _job_value(args, cfg, key, default):
    """Per-job tool argument, else the config value, else the default."""
    value = _arg(args, key, None)
    if value is None:
        value = _cfg(cfg, key, default)
    return value


def _job_bool(args, cfg, key, default):
    value = _job_value(args, cfg, key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


def _job_float(args, cfg, key, default):
    try:
        return float(_job_value(args, cfg, key, default))
    except (TypeError, ValueError):
        return float(default)


def _arg(args, key, default=None):
    if args is None:
        return default
    if isinstance(args, dict):
        return args.get(key, default)
    return getattr(args, key, default)


def _absolute(value):
    return os.path.abspath(os.path.expanduser(str(value)))


def _state_dir(cfg):
    value = _cfg(cfg, "state_dir", "") or ""
    if not value:
        value = os.path.join(os.path.expanduser("~"), ".deepseek-mcp", "state")
    return os.path.abspath(os.path.expanduser(str(value)))


def _workspace_from(args, cfg, run=None):
    for key in ("workspace", "canonical_workspace", "repo", "repo_path"):
        value = _arg(args, key, None)
        if value:
            return _absolute(value)
    pipe = run.get("pipeline") if isinstance(run, dict) else None
    if not isinstance(pipe, dict) and isinstance(args, dict):
        pipe = args.get("_pipeline")
    if isinstance(pipe, dict) and pipe.get("canonical_workspace"):
        return _absolute(pipe["canonical_workspace"])
    value = _cfg(cfg, "workspace", "") or _cfg(cfg, "repo", "") or ""
    return _absolute(value) if value else os.getcwd()


def _run_id(run):
    if isinstance(run, dict):
        for key in ("run_id", "id", "job_id"):
            if run.get(key):
                return str(run[key])
        pipe = run.get("pipeline")
        if isinstance(pipe, dict) and pipe.get("run_id"):
            return str(pipe["run_id"])
    return ""


def _safe_id(value, fallback="run"):
    text = _SAFE_ID_RE.sub("_", str(value or "")).strip("._-")
    return text[:128] or fallback


def _clean_text(value, workspace=None):
    text = redact_secrets(str(value or ""))
    text = text.replace("\x00", "")
    if workspace:
        try:
            text = text.replace(_absolute(workspace), "<workspace>")
        except Exception:
            pass
    return text[:2000]


# ---------------------------------------------------------------------------
# Write-set normalisation / matching
# ---------------------------------------------------------------------------


def _item_list(items):
    if items is None:
        return []
    if isinstance(items, str):
        parts = []
        for line in items.splitlines():
            parts.extend(line.split(","))
        return [p for p in parts if str(p).strip()]
    if isinstance(items, dict):
        return list(items.keys())
    if isinstance(items, (list, tuple, set)):
        return list(items)
    return [items]


def _clean_item(value):
    """Return ``(normalised, invalid_raw)`` for one write-set item."""

    if value is None:
        return None, None
    raw = str(value).strip()
    text = raw.strip("`'\"").strip()
    text = re.sub(r"^[-*+•]\s+", "", text)
    text = text.replace("\\", "/")
    text = re.sub(r"/+", "/", text)
    while text.startswith("./"):
        text = text[2:]
    text = text.strip().rstrip("/")
    if not text or text == ".":
        return None, None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None, raw
    if text.startswith("/") or text.startswith("~") or _DRIVE_RE.match(text):
        return None, raw
    pieces = text.split("/")
    if ".." in pieces or "" in pieces:
        return None, raw
    return text, None


def normalise_write_set(items):
    """Normalise write-set items, dropping invalid ones (never raises)."""

    valid, _invalid = _normalise_items(items)
    return valid


def _normalise_items(items):
    valid = []
    invalid = []
    seen = set()
    for raw in _item_list(items):
        cleaned, bad = _clean_item(raw)
        if bad is not None:
            if bad not in invalid:
                invalid.append(bad)
            continue
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            valid.append(cleaned)
    return valid, invalid


def _has_glob(item):
    return "*" in str(item)


def _prefix_covers(prefix, path):
    prefix = str(prefix).rstrip("/")
    path = str(path).rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _literal_prefix(pattern):
    index = str(pattern).find("*")
    if index < 0:
        return str(pattern)
    prefix = str(pattern)[:index].rstrip("/")
    return prefix


def write_set_contains(write_set, path):
    """True when *path* is covered by a declared prefix or glob.

    An empty write-set is the unknown universe (matches everything).
    """

    items = normalise_write_set(write_set)
    cleaned, _bad = _clean_item(path)
    if not cleaned:
        return False
    if not items:
        return True
    for item in items:
        if _has_glob(item):
            if fnmatch.fnmatchcase(cleaned, item):
                return True
            if item.endswith("/**") and _prefix_covers(item[:-3], cleaned):
                return True
            prefix = _literal_prefix(item)
            if prefix and _prefix_covers(prefix, cleaned):
                # A pattern below a literal prefix still overlaps that prefix.
                return True
        elif _prefix_covers(item, cleaned):
            return True
    return False


def _item_overlaps(first, second):
    if first == second:
        return True
    if _prefix_covers(first, second) or _prefix_covers(second, first):
        return True
    if _has_glob(first) and fnmatch.fnmatchcase(second, first):
        return True
    if _has_glob(second) and fnmatch.fnmatchcase(first, second):
        return True
    first_prefix = _literal_prefix(first)
    second_prefix = _literal_prefix(second)
    if first_prefix and second_prefix:
        if _prefix_covers(first_prefix, second_prefix):
            return True
        if _prefix_covers(second_prefix, first_prefix):
            return True
    return False


def overlaps(a, b):
    """Conservative overlap test; unknown/empty write-sets overlap everything."""

    try:
        first = normalise_write_set(a)
        second = normalise_write_set(b)
    except Exception:
        return True
    if not first or not second:
        return True
    for left in first:
        for right in second:
            if _item_overlaps(left, right):
                return True
    return False


# ---------------------------------------------------------------------------
# SLICES.md / prompt write-set resolution
# ---------------------------------------------------------------------------


def _read_text(path, limit=MAX_SLICES_TEXT):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except (OSError, ValueError):
        return ""


def _slice_matches(name, wanted):
    left = re.sub(r"\s+", " ", str(name or "").strip().lower())
    right = re.sub(r"\s+", " ", str(wanted or "").strip().lower())
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _select_slice_files(parsed, slice_name):
    items = parsed.get("slices") or []
    if slice_name:
        chosen = [item for item in items if _slice_matches(item.get("name"), slice_name)]
    else:
        chosen = list(items)
    files = []
    for item in chosen:
        for path in item.get("files") or []:
            files.append(path)
    return files


def _parse_slices_text(text):
    try:
        from . import slices as slices_mod

        parsed = slices_mod.parse_slices(text or "")
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {"slices": []}


def _part_b_slice_files(text, slice_name):
    """Try the optional ``slices.write_sets`` API (part B), returning files."""

    try:
        from . import slices as slices_mod
    except Exception:
        return None
    helper = getattr(slices_mod, "write_sets", None)
    if not callable(helper):
        return None
    try:
        result = helper(text, slice_name)
    except TypeError:
        return None
    except Exception:
        return None
    if isinstance(result, dict):
        result = result.get("write_set", result.get("files", result.get("slices")))
    if isinstance(result, dict):
        result = result.get("files")
    if isinstance(result, (list, tuple, set)):
        return list(result)
    if isinstance(result, str):
        return [p for p in re.split(r"[\n,]", result) if p.strip()]
    return None


def _fenced_slices_block(prompt):
    text = str(prompt or "")
    match = _FENCED_SLICES_RE.search(text)
    if match:
        return match.group(1)
    opened = _FENCED_OPEN_RE.search(text)
    if opened:
        return text[opened.end():]
    return ""


def _slices_file_text(workspace):
    if not workspace:
        return ""
    return _read_text(os.path.join(str(workspace), "SLICES.md"))


def _plan_data(args, workspace):
    path = _arg(args, "plan_path", None)
    if not path:
        candidate = _arg(args, "plan", None)
        if isinstance(candidate, str) and candidate.strip().lower().endswith(".json"):
            path = candidate
    if not path and workspace:
        path = os.path.join(str(workspace), "PLAN.json")
    if not path:
        return {}, ""
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read(4 * 1024 * 1024)
        data = json.loads(raw)
        if isinstance(data, dict):
            return data, str(path)
    except (OSError, ValueError, TypeError):
        pass
    return {}, str(path)


def _plan_write_set(args, workspace):
    data, _path = _plan_data(args, workspace)
    return data.get("write_set") or data.get("writeSet") or []


def declared_write_set(args, cfg):
    """Resolve the declared write-set using the frozen F4.1 precedence."""

    result = {"write_set": [], "source": "none", "impl": "builtin",
              "invalid": [], "confidence": 0.0, "basis": "none"}
    try:
        workspace = _workspace_from(args, cfg)
        invalid = []
        raw = _arg(args, "write_set", None)
        if raw not in (None, "", [], ()):
            valid, bad = _normalise_items(raw)
            invalid.extend(bad)
            if valid:
                result.update({"write_set": valid, "source": "arg", "impl": "builtin",
                               "confidence": 1.0, "basis": "declared",
                               "invalid": invalid})
                return result

        slice_name = _arg(args, "slice", None)
        text = _slices_file_text(workspace)
        if slice_name and text:
            part_b = _part_b_slice_files(text, slice_name)
            if part_b:
                valid, bad = _normalise_items(part_b)
                invalid.extend(bad)
                if valid:
                    result.update({"write_set": valid, "source": "slice",
                                   "impl": "part_b", "confidence": 1.0,
                                   "basis": "declared", "invalid": invalid})
                    return result
            parsed = _parse_slices_text(text)
            valid, bad = _normalise_items(_select_slice_files(parsed, slice_name))
            invalid.extend(bad)
            if valid:
                result.update({"write_set": valid, "source": "slice",
                               "impl": "builtin", "confidence": 1.0,
                               "basis": "declared", "invalid": invalid})
                return result

        prompt = _arg(args, "prompt", "") or ""
        fenced = _fenced_slices_block(prompt)
        if fenced:
            parsed = _parse_slices_text(fenced)
            valid, bad = _normalise_items(
                _select_slice_files(parsed, slice_name if slice_name else None)
            )
            invalid.extend(bad)
            if valid:
                result.update({"write_set": valid, "source": "prompt",
                               "impl": "builtin", "confidence": 1.0,
                               "basis": "declared", "invalid": invalid})
                return result

        valid, bad = _normalise_items(_plan_write_set(args, workspace))
        invalid.extend(bad)
        if valid:
            result.update({"write_set": valid, "source": "plan",
                           "impl": "builtin", "invalid": invalid,
                           "confidence": 0.93, "basis": "plan"})
            return result
        # v3.14.2: automatic inference is the last resort.  It is cheap and
        # pure-Python, and only a confident, disjoint set is allowed to run in
        # parallel; a low-confidence result keeps the serial path.
        if _job_bool(args, cfg, "auto_parallel", True):
            try:
                from . import writesets as writesets_mod
                inferred = writesets_mod.infer_write_set(prompt, workspace)
                inferred_valid, inferred_bad = _normalise_items(
                    inferred.get("write_set") or [])
                invalid.extend(inferred_bad)
                if inferred_valid:
                    result.update({
                        "write_set": inferred_valid,
                        "source": "inferred",
                        "impl": "auto",
                        "invalid": invalid,
                        "confidence": float(
                            inferred.get("confidence") or 0.0),
                        "basis": ",".join(inferred.get("sources") or [])
                        or "inferred",
                    })
                    return result
            except Exception:
                pass
        result["invalid"] = invalid
        return result
    except Exception:
        return result


# ---------------------------------------------------------------------------
# Git helpers and queue base ref
# ---------------------------------------------------------------------------


def _git(args, cwd=None, timeout=120):
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("LC_ALL", "C")
    try:
        proc = subprocess.run(
            ["git"] + [str(a) for a in args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "git_not_installed"
    except subprocess.TimeoutExpired:
        return 124, "", "git_timeout"
    except Exception as exc:  # noqa: BLE001 - never crash the caller
        return 125, "", str(exc)


def _git_head(workspace):
    rc, out, _err = _git(["rev-parse", "HEAD"], workspace)
    return out.strip() if rc == 0 else ""


def _git_ref(ref, workspace):
    if not ref:
        return ""
    rc, out, _err = _git(["rev-parse", "--verify", ref], workspace)
    return out.strip() if rc == 0 else ""


def _git_version(cfg):
    rc, out, _err = _git(["--version"])
    if rc != 0:
        return (0, 0), ""
    match = _GIT_VERSION_RE.search(out)
    if not match:
        return (0, 0), out.strip()
    return (int(match.group(1)), int(match.group(2))), out.strip()


def _queue_id(canonical_workspace):
    path = os.path.normcase(os.path.realpath(str(canonical_workspace or "")))
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16]


def queue_base_ref(canonical_workspace):
    """One private queue ref per canonical workspace."""

    return "refs/crewforeman/queue/" + _queue_id(canonical_workspace)


def _meta_path(cfg, canonical_workspace):
    return os.path.join(_state_dir(cfg), "queues",
                        _queue_id(canonical_workspace) + ".json")


def _read_json(path):
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _atomic_write(path, data, mode=0o600):
    directory = os.path.dirname(str(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if isinstance(data, bytes):
        payload = data
    else:
        payload = str(data).encode("utf-8", "replace")
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, str(path))
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _save_json(path, value):
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _queue_meta(cfg, canonical_workspace):
    return _read_json(_meta_path(cfg, canonical_workspace))


def _save_queue_meta(cfg, canonical_workspace, meta):
    _save_json(_meta_path(cfg, canonical_workspace), meta)


def patch_dir(cfg):
    configured = _cfg(cfg, "pipeline_patch_dir", "") or ""
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    return os.path.join(_state_dir(cfg), "patches")


def patch_path(cfg, run_id):
    return os.path.join(patch_dir(cfg), _safe_id(run_id) + ".patch")


def patch_sidecar_path(cfg, run_id):
    return os.path.join(patch_dir(cfg), _safe_id(run_id) + ".json")


def worktree_root(cfg):
    configured = _cfg(cfg, "pipeline_worktree_root", "") or ""
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    return os.path.join(_state_dir(cfg), "worktrees")


def _worktree_path(cfg, run_id):
    return os.path.join(worktree_root(cfg), _safe_id(run_id))


def _patch_records(cfg, canonical_workspace, statuses=None):
    directory = patch_dir(cfg)
    if not os.path.isdir(directory):
        return []
    queue_id = _queue_id(canonical_workspace)
    records = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json"):
            continue
        data = _read_json(os.path.join(directory, name))
        if not data or "sha256" not in data:
            continue
        recorded_queue = data.get("queue_id")
        if recorded_queue and recorded_queue != queue_id:
            continue
        status = str(data.get("status") or "pending")
        if statuses is not None and status not in statuses:
            continue
        data["_sidecar"] = os.path.join(directory, name)
        records.append(data)
    records.sort(key=lambda item: (str(item.get("created") or ""),
                                   str(item.get("run_id") or "")))
    return records


def _has_pending(cfg, canonical_workspace):
    return bool(_patch_records(cfg, canonical_workspace,
                               statuses={"pending", "accepted", "manual",
                                         "conflict", "serial_reintegration"}))


def _update_sidecar(record, **fields):
    path = record.get("_sidecar")
    if not path:
        return
    data = _read_json(path)
    if not data:
        data = {k: v for k, v in record.items() if not k.startswith("_")}
    data.update(fields)
    _save_json(path, data)


def _ensure_queue(cfg, canonical_workspace):
    """Create/repair the private queue ref and return its metadata."""

    canonical = _absolute(canonical_workspace)
    head = _git_head(canonical)
    if not head:
        return {"ok": False, "reason": "not_git_repo"}
    ref = queue_base_ref(canonical)
    meta = _queue_meta(cfg, canonical)
    ref_commit = _git_ref(ref, canonical)
    changed = False
    if not meta:
        meta = {
            "version": VERSION,
            "queue_id": _queue_id(canonical),
            "base_ref": ref,
            "head_at_queue_start": head,
            "base_commit": ref_commit or head,
            "installed_paths": [],
            "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        changed = True
    if ref_commit and not meta.get("base_commit"):
        meta["base_commit"] = ref_commit
        changed = True
    pending = _has_pending(cfg, canonical)
    if not ref_commit:
        base = meta.get("base_commit") or head
        rc, _out, _err = _git(["update-ref", ref, base], canonical)
        if rc != 0:
            return {"ok": False, "reason": "queue_ref_failed", "base_ref": ref}
        ref_commit = base
        changed = True
    elif not pending and meta.get("head_at_queue_start") != head:
        # Canonical moved with no uninstalled work: move the queue to HEAD.
        rc, _out, _err = _git(["update-ref", ref, head], canonical)
        if rc != 0:
            return {"ok": False, "reason": "queue_ref_failed", "base_ref": ref}
        meta["head_at_queue_start"] = head
        meta["base_commit"] = head
        ref_commit = head
        changed = True
    if changed:
        _save_queue_meta(cfg, canonical, meta)
    return {"ok": True, "base_ref": ref, "base_commit": ref_commit or head,
            "head_at_queue_start": meta.get("head_at_queue_start") or head,
            "meta": meta}


def touch_base_ref(cfg, canonical_workspace):
    """Ensure the queue base ref exists; never raises."""

    try:
        return _ensure_queue(cfg, canonical_workspace)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "queue_failed", "detail": _clean_text(exc)}


def touch_queue_base(cfg, canonical_workspace):
    return touch_base_ref(cfg, canonical_workspace)


def advance_queue_base(cfg, canonical_workspace, commit):
    """Fast-forward the queue ref to *commit* and record it."""

    try:
        canonical = _absolute(canonical_workspace)
        ref = queue_base_ref(canonical)
        if not commit:
            return {"ok": False, "reason": "missing_commit", "base_ref": ref}
        rc, _out, _err = _git(["update-ref", ref, str(commit)], canonical)
        if rc != 0:
            return {"ok": False, "reason": "queue_ref_failed", "base_ref": ref}
        meta = _queue_meta(cfg, canonical)
        meta["base_ref"] = ref
        meta["base_commit"] = str(commit)
        meta.setdefault("queue_id", _queue_id(canonical))
        meta.setdefault("head_at_queue_start", _git_head(canonical))
        meta.setdefault("installed_paths", [])
        _save_queue_meta(cfg, canonical, meta)
        return {"ok": True, "base_ref": ref, "base_commit": str(commit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "queue_failed", "detail": _clean_text(exc)}


# ---------------------------------------------------------------------------
# Status/dirt parsing and eligibility
# ---------------------------------------------------------------------------


def _status_paths(workspace):
    rc, out, _err = _git(["status", "--porcelain=v1", "-z"], workspace)
    if rc != 0:
        return None
    paths = []
    parts = out.split("\0")
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        code = entry[:2]
        path = entry[3:]
        if "R" in code or "C" in code:
            # The original path follows as its own NUL-delimited field.
            index += 1
        path, _bad = _clean_item(path)
        if path:
            paths.append(path)
    return paths


def _installed_paths(cfg, canonical_workspace):
    meta = _queue_meta(cfg, canonical_workspace)
    paths = list(meta.get("installed_paths") or [])
    for record in _patch_records(cfg, canonical_workspace, statuses={"installed"}):
        paths.extend(record.get("files") or [])
    valid, _bad = _normalise_items(paths)
    return valid


def _path_allowed(path, allowed):
    if not allowed:
        return False
    return write_set_contains(allowed, path)


def _eligibility(cfg, canonical_workspace, write_set=None):
    """Return ``(eligible, reason, info)``; never raises."""

    if not _cfg_bool(cfg, "pipeline_enabled", True):
        return False, "pipeline_disabled", {}
    isolation = str(_cfg(cfg, "pipeline_isolation", "worktree") or "worktree")
    if isolation.strip().lower() != "worktree":
        return False, "isolation_unsupported", {}
    canonical = _absolute(canonical_workspace or "")
    if not canonical or not os.path.isdir(canonical):
        return False, "workspace_missing", {}
    version, raw_version = _git_version(cfg)
    if version < (2, 5):
        return False, "git_too_old" if version != (0, 0) else "git_missing", {
            "git_version": raw_version,
        }
    rc, out, err = _git(["rev-parse", "--is-inside-work-tree"], canonical)
    if rc != 0 or out.strip().lower() != "true":
        return False, "not_git_repo", {"stderr": _clean_text(err, canonical)}
    rc, top, _err = _git(["rev-parse", "--show-toplevel"], canonical)
    if rc != 0 or not top.strip():
        return False, "not_git_repo", {}
    if os.path.normcase(os.path.realpath(top.strip())) != os.path.normcase(
            os.path.realpath(canonical)):
        return False, "not_worktree_root", {}
    if write_set is not None:
        items, _invalid = _normalise_items(write_set)
        if not items:
            return False, "no_write_set", {}
    status_paths = _status_paths(canonical)
    if status_paths is None:
        return False, "status_failed", {}
    allowed = _installed_paths(cfg, canonical)
    unknown = [path for path in status_paths if not _path_allowed(path, allowed)]
    if unknown:
        return False, "dirty_tree", {"dirty_paths": unknown[:20]}
    return True, None, {"git_version": raw_version, "dirty_paths": []}


def is_pipeline_eligible(cfg, canonical_workspace, write_set=None):
    """Eligibility predicate for F4.2 (false on every error)."""

    try:
        eligible, _reason, _info = _eligibility(cfg, canonical_workspace, write_set)
        return bool(eligible)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def _write_marker(path, **fields):
    try:
        marker = os.path.join(str(path), ".dsh-pipeline.json")
        data = _read_json(marker)
        data.update(fields)
        _save_json(marker, data)
    except Exception:
        pass


def create_worktree(cfg, canonical_workspace, run_id):
    """Create the detached queue-base worktree for one eligible run."""

    result = {"ok": False, "run_id": _safe_id(run_id), "path": None,
              "base_ref": None, "base_commit": None}
    try:
        canonical = _absolute(canonical_workspace or "")
        result["base_ref"] = queue_base_ref(canonical)
        queue = _ensure_queue(cfg, canonical)
        if not queue.get("ok"):
            result["reason"] = queue.get("reason") or "queue_unavailable"
            return result
        base = queue.get("base_commit")
        result["base_commit"] = base
        path = _worktree_path(cfg, run_id)
        result["path"] = path
        if os.path.isdir(path):
            if os.path.exists(os.path.join(path, ".git")):
                result.update({"ok": True, "reused": True})
                return result
            result["reason"] = "worktree_exists"
            return result
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        rc, _out, err = _git(["worktree", "add", "--detach", path, base], canonical)
        if rc != 0:
            shutil.rmtree(path, ignore_errors=True)
            _git(["worktree", "prune"], canonical)
            result["reason"] = "worktree_add_failed"
            result["detail"] = _clean_text(err, canonical)
            return result
        _write_marker(path, run_id=_safe_id(run_id), queue_id=_queue_id(canonical),
                      base_commit=base, created=dt.datetime.now(dt.timezone.utc).isoformat(),
                      terminal=False, retained=False)
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        result["reason"] = "worktree_failed"
        result["detail"] = _clean_text(exc, canonical_workspace)
        return result


def _remove_worktree(cfg, canonical_workspace, path):
    if not path:
        return True
    path = str(path)
    if not os.path.isdir(path):
        return True
    canonical = str(canonical_workspace or "")
    rc, _out, _err = _git(["worktree", "remove", "--force", path], canonical or path)
    if rc != 0 and os.path.isdir(path):
        _git(["worktree", "remove", "--force", path], path)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    try:
        _git(["worktree", "prune"], canonical or path)
    except Exception:
        pass
    return not os.path.exists(path)


def _write_set_for_run(run):
    pipe = run.get("pipeline") if isinstance(run, dict) else None
    if isinstance(pipe, dict):
        return pipe.get("write_set") or []
    return []


# ---------------------------------------------------------------------------
# Run preparation and PLAN narrowing
# ---------------------------------------------------------------------------


def prepare_args(cfg, args, run=None):
    """Normalise args and attach the pipeline metadata block (never raises)."""

    try:
        if args is None:
            args = {}
        # A queued job runs in its worktree (args["workspace"]) but the queue
        # base, dirty-state and repository checks belong to the canonical
        # checkout. jobs.py passes that explicitly with the frozen pipeline
        # metadata it created at admission.
        workspace = _absolute(args.get("_canonical_workspace")
                              or _workspace_from(args, cfg, run))
        existing = {}
        if isinstance(args.get("_pipeline"), dict):
            existing.update(args["_pipeline"])
        if isinstance(run, dict) and isinstance(run.get("pipeline"), dict):
            existing.update(run["pipeline"])
        decided = bool(existing.get("decided")
                       or args.get("_pipeline_decided"))
        if decided:
            # Eligibility, write set and the parallel knobs are decided once
            # at admission.  Recomputing from a resumed/queued args dict can
            # lose the raw tool argument and flip an opt-out back on.
            declared = {
                "write_set": list(existing.get("write_set") or []),
                "source": existing.get("write_set_source") or "none",
                "impl": existing.get("write_set_impl") or "builtin",
                "confidence": existing.get("write_set_confidence") or 0.0,
                "basis": existing.get("write_set_basis") or "none",
                "invalid": [],
            }
        else:
            declared = declared_write_set(args, cfg)
        source = declared.get("source") or "none"
        try:
            declared_confidence = float(declared.get("confidence") or 0.0)
        except (TypeError, ValueError):
            declared_confidence = 0.0
        auto_parallel = _job_bool(args, cfg, "auto_parallel", True)
        min_confidence = _job_float(
            args, cfg, "auto_parallel_min_confidence", 0.85)
        if existing.get("auto_parallel") is not None:
            value = existing["auto_parallel"]
            auto_parallel = (
                value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
                if isinstance(value, str) else bool(value))
        if existing.get("auto_parallel_min_confidence") is not None:
            try:
                min_confidence = float(existing["auto_parallel_min_confidence"])
            except (TypeError, ValueError):
                pass
        inferred_ok = (source == "inferred"
                       and auto_parallel
                       and declared_confidence >= min_confidence)
        pre_source = (source in {"arg", "slice", "prompt", "plan"} or inferred_ok)
        check_set = declared.get("write_set") if pre_source else []
        if decided:
            # Eligibility is decided once at admission and frozen (§F4.2).
            eligible = bool(existing.get("eligible"))
            reason = existing.get("unavailable_reason")
            info = {"git_version": existing.get("git_version")}
        elif source == "inferred" and not auto_parallel:
            eligible, reason, info = False, "auto_parallel_disabled", {}
        elif source == "inferred" and declared_confidence < min_confidence:
            eligible, reason, info = False, "inferred_low_confidence", {}
        else:
            eligible, reason, info = _eligibility(cfg, workspace, check_set)
        base_ref = existing.get("base_ref") or queue_base_ref(workspace)
        base_commit = existing.get("base_commit") or ""
        worktree = existing.get("worktree") or ""
        if eligible and not decided:
            queue = _ensure_queue(cfg, workspace)
            if queue.get("ok"):
                base_commit = existing.get("base_commit") or queue.get("base_commit") or ""
                base_ref = queue.get("base_ref") or base_ref
                worktree = worktree or _worktree_path(cfg, _run_id(run))
            else:
                eligible = False
                reason = queue.get("reason") or "queue_unavailable"
        elif eligible and decided:
            # The admission-time decision already has the queue ref, base
            # commit and worktree path. Never re-decide mid-run.
            worktree = existing.get("worktree") or ""
            base_commit = existing.get("base_commit") or ""
            base_ref = existing.get("base_ref") or base_ref
        if not base_commit:
            base_commit = _git_head(workspace)
        install = existing.get("install") if isinstance(existing.get("install"), dict) else {}
        if not install.get("status"):
            install = {"status": "pending" if eligible else "n/a", "applied": [],
                       "reason": None, "conflict": None}
        pipe = {
            "enabled": _cfg_bool(cfg, "pipeline_enabled", True),
            "eligible": bool(eligible),
            "unavailable_reason": reason,
            "slot": existing.get("slot") or _arg(args, "pipeline_slot", None) or "base",
            "worktree": worktree if eligible else None,
            "canonical_workspace": workspace,
            "base_ref": base_ref,
            "base_commit": base_commit,
            "run_id": _safe_id(_run_id(run)) if _run_id(run) else existing.get("run_id"),
            "write_set": declared.get("write_set") or [],
            "write_set_source": declared.get("source") or "none",
            "write_set_impl": declared.get("impl") or "builtin",
            "write_set_confidence": declared.get("confidence"),
            "write_set_basis": declared.get("basis"),
            "write_set_widened": bool(existing.get("write_set_widened", False)),
            "write_set_invalid": declared.get("invalid") or [],
            "auto_parallel": auto_parallel,
            "auto_parallel_min_confidence": min_confidence,
            "plan_files_outside_write_set": existing.get("plan_files_outside_write_set") or [],
            "worktree_retained": bool(existing.get("worktree_retained", False)),
            "defer_next_start": bool(existing.get("defer_next_start", False)),
            "patch": existing.get("patch") if isinstance(existing.get("patch"), dict) else {},
            "install": install,
            "git_version": info.get("git_version"),
            "decided": bool(decided),
        }
        if existing.get("worktree") and existing.get("eligible"):
            pipe["worktree"] = existing["worktree"]
        if isinstance(run, dict):
            run["pipeline"] = pipe
        if isinstance(args, dict):
            args["pipeline"] = pipe
            args.setdefault("canonical_workspace", workspace)
        return pipe
    except Exception as exc:  # noqa: BLE001
        fallback = {
            "enabled": _cfg_bool(cfg, "pipeline_enabled", True),
            "eligible": False,
            "unavailable_reason": "prepare_failed",
            "slot": "base",
            "worktree": None,
            "canonical_workspace": None,
            "base_ref": None,
            "base_commit": None,
            "write_set": [],
            "write_set_source": "none",
            "write_set_impl": "builtin",
            "write_set_widened": False,
            "write_set_invalid": [],
            "auto_parallel": _job_bool(args, cfg, "auto_parallel", True),
            "auto_parallel_min_confidence": _job_float(
                args, cfg, "auto_parallel_min_confidence", 0.85),
            "plan_files_outside_write_set": [],
            "worktree_retained": False,
            "patch": {},
            "install": {"status": "n/a", "applied": [], "reason": None,
                        "conflict": None},
            "detail": _clean_text(exc),
        }
        if isinstance(run, dict):
            run["pipeline"] = fallback
        return fallback


def note_plan(run, plan_path):
    """Read PLAN.json after the build.  PLAN may narrow, never widen silently."""

    try:
        pipe = run.get("pipeline") if isinstance(run, dict) else None
        if not isinstance(pipe, dict):
            return {"write_set": [], "write_set_source": "none", "write_set_impl": "builtin",
                    "write_set_widened": False, "write_set_invalid": [],
                    "plan_files_outside_write_set": []}
        pipe.setdefault("write_set", [])
        pipe.setdefault("write_set_source", "none")
        pipe.setdefault("write_set_impl", "builtin")
        pipe.setdefault("write_set_widened", False)
        pipe.setdefault("write_set_invalid", [])
        pipe.setdefault("plan_files_outside_write_set", [])
        pipe.setdefault("defer_next_start", False)
        data = {}
        try:
            with open(str(plan_path), "r", encoding="utf-8", errors="replace") as handle:
                loaded = json.loads(handle.read(4 * 1024 * 1024))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError, TypeError):
            return pipe
        new_set, invalid = _normalise_items(
            data.get("write_set") or data.get("writeSet") or []
        )
        old_set = normalise_write_set(pipe.get("write_set") or [])
        if invalid:
            existing = list(pipe.get("write_set_invalid") or [])
            for item in invalid:
                if item not in existing:
                    existing.append(item)
            pipe["write_set_invalid"] = existing[:100]
        if new_set:
            if old_set:
                outside = [item for item in new_set
                           if not write_set_contains(old_set, item)]
                if outside:
                    union = list(old_set)
                    for item in new_set:
                        if item not in union:
                            union.append(item)
                    pipe["write_set"] = union
                    pipe["write_set_widened"] = True
                    pipe["defer_next_start"] = True
                else:
                    pipe["write_set"] = new_set
            else:
                pipe["write_set"] = new_set
                if pipe.get("write_set_source") in (None, "", "none"):
                    pipe["write_set_source"] = "plan"
        reserved = normalise_write_set(pipe.get("write_set") or [])
        expected, _bad = _normalise_items(
            data.get("files_expected") or data.get("filesExpected") or []
        )
        # The write-set may be workspace-relative (repo/src/) while PLAN.json
        # speaks product-relative (src/).  Relate the two exactly like
        # plan_contract.compare_plan does, without ever widening the set.
        prefix = path_scope.common_leading_dir(reserved)
        if prefix and any(item == prefix or item.startswith(prefix + "/")
                          for item in expected):
            prefix = ""
        if prefix:
            comparable = [path_scope.strip_leading_dir(item, prefix)
                          for item in reserved]
        else:
            comparable = reserved
        outside_expected = [item for item in expected
                            if not write_set_contains(comparable, item)]
        pipe["plan_files_outside_write_set"] = outside_expected[:100]
        if isinstance(run, dict):
            run["pipeline"] = pipe
        return pipe
    except Exception:
        return run.get("pipeline") if isinstance(run, dict) else {}


# ---------------------------------------------------------------------------
# Patch capture
# ---------------------------------------------------------------------------


def _exclude_pathspecs(cfg, workspace):
    excludes = [":(exclude)REVIEW.md", ":(exclude)QUALITY_REPORT.md",
                ":(exclude)PLAN.json", ":(exclude)CROSSCHECK.json",
                ":(exclude).dsh-pipeline.json",
                ":(exclude).dsh-tmp-*", ":(exclude)**/.dsh-tmp-*"]
    try:
        state = os.path.realpath(_state_dir(cfg))
        root = os.path.realpath(str(workspace))
        if state.startswith(root + os.sep):
            rel = os.path.relpath(state, root).replace(os.sep, "/")
            if rel and not rel.startswith(".."):
                excludes.append(":(exclude)" + rel + "/*")
                excludes.append(":(exclude)" + rel)
    except Exception:
        pass
    return excludes


def _diff_files(workspace, base, excludes):
    rc, out, _err = _git(
        ["diff", "--name-only", "-z", base, "--", "."] + excludes, workspace
    )
    if rc != 0:
        return []
    files = []
    for item in out.split("\0"):
        cleaned, _bad = _clean_item(item)
        if cleaned and cleaned not in files:
            files.append(cleaned)
    return files


def capture_patch(run, cfg, workspace):
    """Capture a binary-safe patch plus a sha256 sidecar.  Best effort."""

    result = {"ok": False, "complete": False, "path": None, "sidecar": None,
              "sha256": None, "bytes": 0, "files": [], "base_commit": None}
    try:
        pipe = run.get("pipeline") if isinstance(run, dict) else None
        if isinstance(pipe, dict) and pipe.get("enabled") is False:
            result["reason"] = "pipeline_disabled"
            return result
        ws = _absolute(workspace or "")
        if not ws or not os.path.isdir(ws):
            result["reason"] = "workspace_missing"
            return result
        base = ""
        if isinstance(pipe, dict):
            base = str(pipe.get("base_commit") or "")
        if not base:
            base = _git_head(ws)
        result["base_commit"] = base
        if not base:
            result["reason"] = "base_commit_missing"
            return result
        _git(["add", "-N", "--", "."], ws)
        excludes = _exclude_pathspecs(cfg, ws)
        rc, diff, err = _git(
            ["diff", "--binary", "--no-color", base, "--", "."] + excludes,
            ws, timeout=180,
        )
        complete = rc == 0
        if len(diff.encode("utf-8", "replace")) > MAX_PATCH_BYTES:
            complete = False
            diff = ""
            result["reason"] = "patch_too_large"
        files = _diff_files(ws, base, excludes) if complete else []
        stray = []
        if isinstance(pipe, dict) and pipe.get("write_set"):
            try:
                from . import writesets as writesets_mod
                stray = writesets_mod.stray_files(pipe.get("write_set"), files)
            except Exception:
                stray = []
        run_id = _run_id(run) or ("run-" + uuid.uuid4().hex[:8])
        run_id = _safe_id(run_id)
        directory = patch_dir(cfg)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, run_id + ".patch")
        sidecar_path = os.path.join(directory, run_id + ".json")
        payload = diff.encode("utf-8", "replace")
        _atomic_write(path, payload, mode=0o600)
        digest = hashlib.sha256(payload).hexdigest()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        canonical = ""
        if isinstance(pipe, dict):
            canonical = str(pipe.get("canonical_workspace") or "")
        worktree = ws
        output_files = []
        raw_outputs = run.get("output_files") if isinstance(run, dict) else None
        for item in raw_outputs or []:
            rel = _safe_rel(item)
            if rel:
                output_files.append(rel)
        sidecar = {
            "version": VERSION,
            "run_id": run_id,
            "patch": os.path.basename(path),
            "sha256": digest,
            "bytes": len(payload),
            "files": files,
            "complete": bool(complete),
            "base_commit": base,
            "created": now,
            "status": ("serial_reintegration" if (complete and stray)
                       else ("pending" if complete else "partial")),
            "stray_files": list(stray),
            "integration_reason": "write_set_stray" if stray else None,
            "queue_id": _queue_id(canonical) if canonical else None,
            "worktree": worktree,
            "output_files": output_files,
            "artifacts": list(_ARTIFACTS),
        }
        if not complete and err:
            sidecar["error"] = _clean_text(err, ws)
        _save_json(sidecar_path, sidecar)
        result.update({"ok": True, "complete": bool(complete), "path": path,
                       "sidecar": sidecar_path, "sha256": digest,
                       "bytes": len(payload), "files": files, "base_commit": base,
                       "run_id": run_id})
        if isinstance(pipe, dict):
            pipe["patch"] = {"path": path, "sha256": digest, "bytes": len(payload),
                             "files": files, "complete": bool(complete),
                             "base_commit": base}
            if pipe.get("eligible"):
                if stray:
                    pipe["stray_files"] = list(stray)
                    pipe["install"] = {
                        "status": "serial_reintegration", "applied": [],
                        "reason": "write_set_stray", "conflict": None,
                    }
                else:
                    pipe["install"] = {"status": "pending", "applied": [],
                                       "reason": None, "conflict": None}
            if isinstance(run, dict):
                run["pipeline"] = pipe
        return result
    except Exception as exc:  # noqa: BLE001
        result["reason"] = "capture_failed"
        result["detail"] = _clean_text(exc, workspace)
        return result


# ---------------------------------------------------------------------------
# Terminal cleanup / reclaim
# ---------------------------------------------------------------------------


def finalize_run(run, cfg, workspace):
    """Best-effort terminal worktree cleanup/retention."""

    result = {"ok": True, "removed": False, "retained": False, "reason": None}
    try:
        pipe = run.get("pipeline") if isinstance(run, dict) else None
        if not isinstance(pipe, dict):
            return result
        install = pipe.get("install") if isinstance(pipe.get("install"), dict) else {}
        status = str(install.get("status") or "")
        path = pipe.get("worktree")
        if status in ("manual", "conflict", "serial_reintegration"):
            pipe["worktree_retained"] = True
            if path:
                _write_marker(path, terminal=True, retained=True, install_status=status)
            result.update({"retained": True, "reason": status})
            if isinstance(run, dict):
                run["pipeline"] = pipe
            return result
        if path:
            removed = _remove_worktree(cfg, workspace or pipe.get("canonical_workspace"), path)
            result["removed"] = bool(removed)
            _write_marker(path, terminal=True, retained=False, install_status=status)
            if not removed:
                result["reason"] = "worktree_remove_failed"
        pipe["worktree_retained"] = False
        if isinstance(run, dict):
            run["pipeline"] = pipe
        return result
    except Exception as exc:  # noqa: BLE001
        result.update({"ok": False, "reason": "finalize_failed",
                       "detail": _clean_text(exc, workspace)})
        return result


def reclaim_worktrees(cfg):
    """Remove terminal/timed-out isolated worktrees (never active ones)."""

    result = {"ok": True, "reclaimed": [], "retained": [], "errors": []}
    try:
        root = worktree_root(cfg)
        if not os.path.isdir(root):
            return result
        days = max(0.0, _cfg_float(cfg, "pipeline_patch_retention_days", 3.0))
        retention = days * 86400.0
        now = time.time()
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            marker_path = os.path.join(path, ".dsh-pipeline.json")
            marker = _read_json(marker_path)
            try:
                age = max(0.0, now - os.path.getmtime(path))
            except OSError:
                age = 0.0
            if not os.path.exists(os.path.join(path, ".git")) and not marker:
                # Never remove an arbitrary user directory.
                result["retained"].append(name)
                continue
            terminal = bool(marker.get("terminal")) if marker else False
            retained = bool(marker.get("retained")) if marker else False
            if terminal and (not retained or age >= retention):
                remove = True
            elif not terminal and age >= retention:
                remove = True
            else:
                remove = False
            if not remove:
                result["retained"].append(name)
                continue
            if _remove_worktree(cfg, path, path):
                result["reclaimed"].append(name)
            else:
                result["errors"].append(name)
        return result
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["errors"].append(_clean_text(exc))
        return result


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _lock_path(cfg):
    return os.path.join(patch_dir(cfg), "install.lock")


def _acquire_lock(cfg):
    path = _lock_path(cfg)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(),
                    "created": dt.datetime.now(dt.timezone.utc).isoformat(),
                }).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            if attempt == 0 and age > LOCK_STALE_SECONDS:
                try:
                    os.unlink(path)
                except OSError:
                    return False
                continue
            return False
        except OSError:
            return False
    return False


def _release_lock(cfg):
    try:
        os.unlink(_lock_path(cfg))
    except OSError:
        pass


def _safe_rel(value):
    cleaned, _bad = _clean_item(value)
    if not cleaned or cleaned.startswith(".."):
        return None
    if os.path.isabs(cleaned) or _DRIVE_RE.match(cleaned):
        return None
    return cleaned


def _safe_copy(source_root, destination_root, rel):
    """Copy one artifact, refusing traversal and symlink escapes."""

    rel = _safe_rel(rel)
    if not rel:
        return False
    source_root = os.path.realpath(str(source_root))
    destination_root = os.path.realpath(str(destination_root))
    source = os.path.realpath(os.path.join(source_root, rel))
    destination = os.path.realpath(os.path.join(destination_root, rel))
    if not source.startswith(source_root + os.sep):
        return False
    if not destination.startswith(destination_root + os.sep):
        return False
    if not os.path.isfile(source):
        return False
    try:
        if os.path.getsize(source) > MAX_PATCH_BYTES:
            return False
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        return True
    except OSError:
        return False


def _install_conflict(record, reason, detail="", canonical_dirty=False):
    files = list(record.get("files") or []) if record else []
    patch_id = record.get("run_id") if record else ""
    return {
        "ok": False,
        "status": "conflict",
        "applied": [],
        "reason": reason,
        "rolled_back": False,
        "canonical_dirty": bool(canonical_dirty),
        "conflict": {
            "patch_id": patch_id,
            "conflicting_patch": patch_id,
            "files": files,
            "stderr": _clean_text(detail),
        },
    }


def _dirty_conflict(cfg, canonical, reason, detail):
    return {
        "ok": False,
        "status": "conflict",
        "applied": [],
        "reason": reason,
        "rolled_back": False,
        "canonical_dirty": True,
        "conflict": {"patch_id": "", "conflicting_patch": "", "files": [],
                     "stderr": _clean_text(detail, canonical)},
    }


def _pending_records(cfg, canonical):
    records = _patch_records(cfg, canonical)
    pending = []
    for record in records:
        status = str(record.get("status") or "pending")
        if status == "serial_reintegration":
            # A stray/conflicting patch must reach the serial path; it is not
            # a terminal conflict and must not disappear from the queue.
            pending.append(record)
            break
        if status in ("manual", "conflict", "partial"):
            break
        if status in ("pending", "accepted"):
            pending.append(record)
    return pending


def _patch_file(record):
    name = record.get("patch") or ""
    if name:
        return os.path.join(patch_dir({"state_dir": record.get("_patch_dir")}), name)
    return ""


def _record_patch_path(record, directory):
    name = os.path.basename(str(record.get("patch") or ""))
    return os.path.join(directory, name)


def _verify_record(record, directory):
    path = _record_patch_path(record, directory)
    if not os.path.isfile(path):
        return False, "patch_missing"
    expected = str(record.get("sha256") or "")
    if expected:
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return False, "patch_unreadable"
        if digest != expected:
            return False, "patch_checksum_mismatch"
    return True, ""


def _copy_artifacts(record, canonical):
    copied = []
    worktree = record.get("worktree") or ""
    if not worktree or not os.path.isdir(str(worktree)):
        return copied
    names = list(record.get("artifacts") or _ARTIFACTS)
    names.extend(record.get("output_files") or [])
    seen = set()
    for name in names:
        rel = _safe_rel(name)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if _safe_copy(worktree, canonical, rel):
            copied.append(rel)
    return copied


def _snapshot_files(root, record):
    """Snapshot the paths a serial patch may touch (worktree + index state)."""
    names = []
    for key in ("files", "stray_files"):
        for item in record.get(key) or []:
            rel = _safe_rel(item)
            if rel and rel not in names:
                names.append(rel)
    snap = {"files": {}, "staged": []}
    for rel in names:
        full = os.path.join(root, rel)
        try:
            if os.path.isfile(full):
                with open(full, "rb") as handle:
                    data = handle.read()
                snap["files"][rel] = (True, data, os.stat(full).st_mode)
            else:
                snap["files"][rel] = (False, b"", 0)
        except OSError:
            snap["files"][rel] = (None, b"", 0)
    rc, out, _err = _git(["diff", "--cached", "--name-only", "--"] + names, root)
    if rc == 0:
        snap["staged"] = [line.strip() for line in out.splitlines()
                          if line.strip()]
    return snap


def _restore_files(root, snap):
    """Best-effort rollback of a failed serial apply; returns True on success."""
    files = (snap or {}).get("files") or {}
    names = list(files)
    ok = True
    # Clear any unmerged/staged index entries left by a failed 3-way apply.
    if names:
        _git(["reset", "-q", "--"] + names, root)
    for rel, (exists, data, mode) in files.items():
        if exists is None:
            continue
        full = os.path.join(root, rel)
        try:
            if exists:
                parent = os.path.dirname(full)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(full, "wb") as handle:
                    handle.write(data)
                try:
                    os.chmod(full, mode & 0o777)
                except OSError:
                    pass
            elif os.path.exists(full):
                os.unlink(full)
        except OSError:
            ok = False
    staged = [rel for rel in names if rel in ((snap or {}).get("staged") or [])]
    if staged:
        rc, _out, _err = _git(["add", "--"] + staged, root)
        ok = ok and rc == 0
    return ok


def serial_reintegrate(cfg, canonical_workspace, run_id):
    """Re-apply one stray/conflicting patch serially on the current tree.

    This is the safe fallback required when a parallel job's actual diff
    escaped its inferred write set, or when the patch no longer applies to the
    canonical tree.  It never force-merges: a clean ``git apply`` is tried
    first, then a three-way apply; otherwise the patch is retained as
    ``manual`` and the run says why.
    """
    result = {"ok": False, "status": "error", "applied": [], "reason": None,
              "rolled_back": False, "conflict": None, "patches": [run_id],
              "resolution": None, "stray_files": []}
    canonical = ""
    lock_held = False
    try:
        canonical = _absolute(canonical_workspace or "")
        if not canonical or not os.path.isdir(canonical):
            result.update({"status": "skipped", "reason": "workspace_missing"})
            return result
        records = _patch_records(cfg, canonical)
        record = next((item for item in records
                       if str(item.get("run_id")) == str(run_id)), None)
        if not record:
            result.update({"status": "skipped", "reason": "patch_missing"})
            return result
        result["stray_files"] = list(record.get("stray_files") or [])
        directory = patch_dir(cfg)
        ok, reason = _verify_record(record, directory)
        if not ok:
            result.update(_install_conflict(record, reason))
            return result
        if not _acquire_lock(cfg):
            result.update({"ok": True, "status": "install_locked",
                           "reason": "install_locked"})
            return result
        lock_held = True
        status_paths = _status_paths(canonical)
        if status_paths is None:
            result.update(_dirty_conflict(cfg, canonical, "canonical_dirty",
                                          "status_failed"))
            return result
        allowed = _installed_paths(cfg, canonical)
        unknown = [path for path in status_paths if not _path_allowed(path, allowed)]
        if unknown:
            result.update(_dirty_conflict(cfg, canonical, "canonical_dirty",
                                          "dirty_paths"))
            return result
        patch_file = _record_patch_path(record, directory)
        reason = str(record.get("integration_reason") or "write_set_stray")
        snap = _snapshot_files(canonical, record)
        rc, _out, err = _git(["apply", "--check", "--", patch_file], canonical)
        resolution = "apply_clean"
        if rc != 0:
            rc3, _out3, err3 = _git(
                ["apply", "--3way", "--check", "--", patch_file], canonical)
            if rc3 != 0:
                result.update(_install_conflict(
                    record, "apply_check_failed", err3 or err))
                result["status"] = "manual"
                result["reason"] = "apply_check_failed"
                _update_sidecar(record, status="manual",
                                integration_reason="apply_check_failed",
                                integration_detail=_clean_text(err3 or err,
                                                               canonical))
                return result
            resolution = "rebase_3way"
        args = ["apply"]
        if resolution == "rebase_3way":
            args.append("--3way")
        args += ["--", patch_file]
        rc, _out, err = _git(args, canonical, timeout=180)
        if rc == 0:
            # A 3-way apply can report success while leaving unmerged paths in
            # some git versions; never accept that into the canonical tree.
            urc, uout, _uerr = _git(
                ["diff", "--name-only", "--diff-filter=U"], canonical)
            if urc != 0 or uout.strip():
                rc = 1
                err = (uout.strip().splitlines() or ["unmerged paths"])[0]
        if rc != 0:
            rolled_back = _restore_files(canonical, snap)
            result.update(_install_conflict(record, "apply_failed", err))
            result["status"] = "manual"
            result["reason"] = "apply_failed"
            result["rolled_back"] = bool(rolled_back)
            _update_sidecar(record, status="manual",
                            integration_reason="apply_failed",
                            integration_detail=_clean_text(err, canonical))
            return result
        _git(["add", "-A", "--", "."], canonical)
        installed = {"ok": True, "status": "installed_serial",
                     "applied": [str(run_id)], "reason": reason,
                     "resolution": resolution,
                     "stray_files": result["stray_files"],
                     "conflict": None}
        _update_sidecar(record, status="installed",
                        integration_resolution=resolution,
                        integration_reason=reason)
        result.update(installed)
        return result
    except Exception as exc:  # noqa: BLE001
        result.update({"ok": False, "status": "error",
                       "reason": "reintegrate_failed",
                       "detail": _clean_text(exc, canonical)})
        return result
    finally:
        if lock_held:
            _release_lock(cfg)


def install_pending(cfg, canonical_workspace, run_id):
    """Verify all accepted patches together, then install (F4.6)."""

    result = {"ok": False, "status": "error", "applied": [], "reason": None,
              "rolled_back": False, "conflict": None, "patches": []}
    canonical = ""
    temp = ""
    lock_held = False
    try:
        canonical = _absolute(canonical_workspace or "")
        if not canonical or not os.path.isdir(canonical):
            result.update({"status": "skipped", "reason": "workspace_missing"})
            return result
        mode = str(_cfg(cfg, "pipeline_install", "auto") or "auto").strip().lower()
        if mode == "off":
            result.update({"ok": True, "status": "skipped", "reason": "pipeline_install_off"})
            return result
        directory = patch_dir(cfg)
        pending = _pending_records(cfg, canonical)
        serial_record = next(
            (row for row in pending
             if str(row.get("status") or "") == "serial_reintegration"), None)
        if serial_record is not None:
            if run_id and str(serial_record.get("run_id")) == str(run_id):
                rebuilt = serial_reintegrate(cfg, canonical, run_id)
                return rebuilt
            return {"ok": True, "status": "serial_reintegration",
                    "applied": [], "reason": "write_set_stray",
                    "conflict": None,
                    "stray_files": serial_record.get("stray_files") or []}
        if not pending:
            installed = _patch_records(cfg, canonical, statuses={"installed"})
            if run_id and any(str(r.get("run_id")) == str(run_id) for r in installed):
                result.update({"ok": True, "status": "installed",
                               "applied": [str(run_id)]})
            else:
                result.update({"ok": True, "status": "nothing_pending"})
            return result

        _verified = []
        for record in pending:
            ok, reason = _verify_record(record, directory)
            if not ok:
                result.update(_install_conflict(record, reason))
                return result
            _verified.append(record)

        if not _acquire_lock(cfg):
            result.update({"ok": True, "status": "install_locked",
                           "reason": "install_locked", "patches": [
                               str(r.get("run_id")) for r in pending]})
            return result
        lock_held = True

        head = _git_head(canonical)
        meta = _queue_meta(cfg, canonical)
        if not meta:
            meta = {"queue_id": _queue_id(canonical), "head_at_queue_start": head,
                    "base_commit": head, "installed_paths": []}
            _save_queue_meta(cfg, canonical, meta)
        if str(meta.get("head_at_queue_start") or "") != str(head):
            result.update(_dirty_conflict(cfg, canonical, "canonical_dirty",
                                          "head_moved"))
            return result
        status_paths = _status_paths(canonical)
        if status_paths is None:
            result.update(_dirty_conflict(cfg, canonical, "canonical_dirty",
                                          "status_failed"))
            return result
        allowed = _installed_paths(cfg, canonical)
        unknown = [path for path in status_paths if not _path_allowed(path, allowed)]
        if unknown:
            result.update(_dirty_conflict(cfg, canonical, "canonical_dirty",
                                          "dirty:" + ",".join(unknown[:5])))
            return result

        base_ref = meta.get("base_ref") or queue_base_ref(canonical)
        base_commit = (meta.get("base_commit")
                       or _git_ref(base_ref, canonical)
                       or pending[0].get("base_commit")
                       or head)
        if not _git_ref(base_ref, canonical):
            _git(["update-ref", base_ref, base_commit], canonical)

        temp_root = os.path.join(_state_dir(cfg), "tmp")
        os.makedirs(temp_root, mode=0o700, exist_ok=True)
        temp = os.path.join(temp_root, "install-" + _safe_id(run_id or "queue") +
                            "-" + uuid.uuid4().hex[:8])
        rc, _out, err = _git(["worktree", "add", "--detach", temp, base_commit],
                             canonical)
        if rc != 0:
            result.update(_install_conflict(pending[0], "worktree_add_failed", err))
            return result

        applied_temp = []
        for record in _verified:
            patch_file = _record_patch_path(record, directory)
            rc, _out, err = _git(
                ["apply", "--check", "--whitespace=nowarn", patch_file], temp
            )
            if rc == 0:
                rc, _out, err = _git(
                    ["apply", "--whitespace=nowarn", patch_file], temp
                )
            if rc != 0:
                _update_sidecar(record, status="serial_reintegration",
                                integration_reason="apply_check_failed",
                                integration_detail=_clean_text(err, temp))
                result.update({
                    "ok": True, "status": "serial_reintegration",
                    "applied": [], "reason": "apply_check_failed",
                    "conflict": None,
                    "patches": [str(r.get("run_id")) for r in pending],
                    "stray_files": record.get("stray_files") or [],
                })
                return result
            applied_temp.append(record)

        if mode == "manual":
            for record in applied_temp:
                _update_sidecar(record, status="manual", install_status="manual")
            result.update({"ok": True, "status": "manual", "applied": [],
                           "patches": [str(r.get("run_id")) for r in applied_temp],
                           "reason": "manual"})
            return result

        rc, _out, err = _git(["add", "-A"], temp)
        if rc == 0:
            from .git_identity import commit_with_identity

            rc, _out, err = commit_with_identity(
                cfg, temp,
                "pipeline install " + _safe_id(run_id or "queue"),
            )
        if rc != 0:
            result.update(_install_conflict(pending[0], "install_commit_failed", err))
            return result
        temp_commit = _git_head(temp)
        if not temp_commit:
            result.update(_install_conflict(pending[0], "install_commit_failed", ""))
            return result

        applied_canonical = []
        for record in applied_temp:
            patch_file = _record_patch_path(record, directory)
            rc, _out, err = _git(
                ["apply", "--whitespace=nowarn", patch_file], canonical
            )
            if rc != 0:
                reversed_ok = True
                for done in reversed(applied_canonical):
                    rrc, _rout, _rerr = _git(
                        ["apply", "-R", "--whitespace=nowarn",
                         _record_patch_path(done, directory)], canonical
                    )
                    if rrc != 0:
                        reversed_ok = False
                result.update(_install_conflict(record, "install_conflict", err))
                result["rolled_back"] = bool(reversed_ok)
                _update_sidecar(record, status="conflict",
                                conflict_reason="install_conflict")
                return result
            applied_canonical.append(record)

        advance = advance_queue_base(cfg, canonical, temp_commit)
        if not advance.get("ok"):
            # The canonical tree changed but the ref could not advance; roll back.
            for done in reversed(applied_canonical):
                _git(["apply", "-R", "--whitespace=nowarn",
                      _record_patch_path(done, directory)], canonical)
            result.update(_install_conflict(pending[0], "queue_ref_failed",
                                            advance.get("detail") or ""))
            result["rolled_back"] = True
            return result

        installed_ids = []
        copied_all = []
        installed_files = []
        for record in applied_canonical:
            installed_ids.append(str(record.get("run_id")))
            installed_files.extend(record.get("files") or [])
            copied_all.extend(_copy_artifacts(record, canonical))
            _update_sidecar(record, status="installed", install_status="installed",
                            installed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        meta = _queue_meta(cfg, canonical) or {}
        paths = list(meta.get("installed_paths") or [])
        for path in installed_files + copied_all:
            cleaned, _bad = _clean_item(path)
            if cleaned and cleaned not in paths:
                paths.append(cleaned)
        meta["installed_paths"] = paths
        meta["base_ref"] = queue_base_ref(canonical)
        meta["base_commit"] = temp_commit
        if not meta.get("head_at_queue_start"):
            meta["head_at_queue_start"] = head
        _save_queue_meta(cfg, canonical, meta)
        for record in applied_canonical:
            _remove_worktree(cfg, canonical, record.get("worktree"))
        result.update({
            "ok": True,
            "status": "installed",
            "applied": installed_ids,
            "patches": installed_ids,
            "base_commit": temp_commit,
            "base_ref": meta.get("base_ref"),
            "files": installed_files,
            "copied_artifacts": copied_all,
        })
        return result
    except Exception as exc:  # noqa: BLE001
        result.update({"ok": False, "status": "error", "reason": "install_failed",
                       "detail": _clean_text(exc, canonical)})
        return result
    finally:
        if temp:
            _remove_worktree(cfg, canonical, temp)
        if lock_held:
            _release_lock(cfg)
