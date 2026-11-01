"""Optional retention/cleanup and strict permissions for local state.

Retention is off by default (``retention_days = 0``); ``keep_runs`` and
``keep_jobs`` always protect the newest records when cleanup is invoked. A
dry run reports exactly what would be removed.

Python 3.9 compatible; standard library only.
"""

import os
import time
from pathlib import Path

from . import platforms

RUN_SUFFIXES = (".json", ".md")
JOB_SUFFIXES = (".job.json", ".status.json", ".log")


def _get(cfg, name, default=None):
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    try:
        return cfg.get(name, default) if hasattr(cfg, "get") else getattr(cfg, name, default)
    except Exception:
        return default


def _nonneg_int(value, label, default=0):
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % label)
    value = int(value)
    if value < 0:
        raise ValueError("%s must be >= 0" % label)
    return value


def _safe_root(path, label):
    """Refuse to delete from root, the home directory, or its ancestors."""
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents:
        raise ValueError("refusing unsafe %s path: %s" % (label, resolved))
    return resolved


def _configured_root(cfg, name):
    """Return the configured directory, or ``None`` when it is absent/blank.

    A partial config must never turn a missing key into the literal ``"None"``
    or an empty value into ``"."``: cleanup would then scan and delete from
    the process working directory. Missing keys are skipped, not defaulted.
    """
    value = _get(cfg, name)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser()


def _roots(cfg):
    roots = {}
    runs = _configured_root(cfg, "log_dir")
    if runs is not None:
        roots["runs"] = (_safe_root(runs, "runs"), RUN_SUFFIXES)
    jobs = _configured_root(cfg, "jobs_dir")
    if jobs is not None:
        roots["jobs"] = (_safe_root(jobs, "jobs"), JOB_SUFFIXES)
    state = _configured_root(cfg, "state_dir")
    if state is not None:
        roots["traces"] = (_safe_root(state / "traces", "traces"),
                           (".jsonl", ".json"))
        roots["checkpoints"] = (_safe_root(state / "checkpoints", "checkpoints"),
                                (".json",))
    return roots


def _files(root, suffixes):
    found = []
    if not root.is_dir():
        return found
    for path in root.iterdir():
        if path.is_file() and not path.is_symlink() and path.name.endswith(suffixes):
            try:
                found.append((path.stat().st_mtime, path, path.stat().st_size))
            except OSError:
                continue
    return found


def _run_prefix(path):
    name = path.name
    for suffix in (".json", ".md"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _job_prefix(path):
    name = path.name
    for suffix in (".job.json", ".status.json", ".log"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def cleanup(cfg, now=None, dry_run=False):
    """Delete expired runs, jobs, traces and checkpoints.

    Returns ``{deleted, kept, bytes, errors, dry_run, roots}``. Files newer
    than ``retention_days`` (when > 0) are kept; the newest ``keep_runs`` runs
    and ``keep_jobs`` job groups are always kept.
    """
    now = time.time() if now is None else now
    try:
        now = float(now)
    except (TypeError, ValueError):
        raise ValueError("now must be a timestamp")
    days = float(_get(cfg, "retention_days", 0) or 0)
    keep_runs = _nonneg_int(_get(cfg, "keep_runs", 50), "keep_runs", 50)
    keep_jobs = _nonneg_int(_get(cfg, "keep_jobs", 50), "keep_jobs", 50)
    cutoff = now - days * 86400.0 if days > 0 else None
    dry = bool(dry_run)
    errors = []
    deleted = 0
    kept = 0
    freed = 0
    roots = {}

    for label, (root, suffixes) in _roots(cfg).items():
        roots[label] = str(root)
        try:
            entries = _files(root, suffixes)
        except OSError as exc:
            errors.append("%s: %s" % (root, exc))
            continue

        # Group by logical record so a run/job is deleted as a unit.
        groups = {}
        for mtime, path, size in entries:
            key = (_job_prefix(path) if label == "jobs" else _run_prefix(path))
            groups.setdefault(key, []).append((mtime, path, size))
        ordered = sorted(groups.items(),
                         key=lambda item: max(entry[0] for entry in item[1]),
                         reverse=True)
        protected = {key for key, _files_ in ordered[:keep_runs if label == "runs"
                                                       else keep_jobs]}

        for key, members in groups.items():
            for mtime, path, size in members:
                expired = cutoff is not None and mtime < cutoff
                if key in protected or not expired:
                    kept += 1
                    continue
                if dry:
                    deleted += 1
                    freed += size
                    continue
                try:
                    path.unlink()
                    deleted += 1
                    freed += size
                except OSError as exc:
                    errors.append("%s: %s" % (path, exc))
                    kept += 1

    # Remove empty per-run job directories left by old versions. Only do this
    # when a jobs root was configured: Path("") is ".", which would make the
    # loop inspect the current directory.
    if not dry and roots.get("jobs"):
        jobs_root = Path(roots["jobs"])
        try:
            for child in jobs_root.iterdir():
                if child.is_dir() and not child.is_symlink() and not any(child.iterdir()):
                    child.rmdir()
        except (OSError, ValueError):
            pass

    return {"deleted": deleted, "kept": kept, "bytes": freed,
            "errors": errors, "dry_run": dry, "roots": roots}


def secure_tree(path):
    """Owner-only permissions (0700 dirs / 0600 files) on POSIX."""
    return platforms.secure_tree(path)
