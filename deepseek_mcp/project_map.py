"""Persisted per-project map support (BUILD 3.10.0 item 6a).

The module keeps a small, private record of a workspace's ``PROJECT_MAP.md``
and a deterministic fingerprint of the workspace.  A later build can then be
offered the existing map for reuse, or told to refresh it when the workspace
has moved on.

Only the public summary ever leaves this module.  The map body and the
absolute workspace path are written solely to the connector state directory
(``<state_dir>/project-maps/``), never to run records, results or logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile

NAME = "PROJECT_MAP.md"
MAX_MAP_CHARS = 12000
MAX_FILES = 2000
VERSION = 1

# Hard cap on the bytes of git output that can influence a fingerprint.
_GIT_OUTPUT_LIMIT = 1024 * 1024

# Directories that never contribute to the fallback file fingerprint.
_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)

_REUSE_PROMPT = "Reuse this map; verify only what you change."
_STALE_PROMPT = "This map is stale; refresh PROJECT_MAP.md before you finish."
_NEW_PROMPT = (
    "Reuse this map; verify only what you change, and refresh it if your "
    "change makes it stale."
)


def _sha256(data):
    """Return the hex sha256 of *data* (bytes or str)."""
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()


def _root(value):
    return os.path.abspath(os.path.expanduser(str(value)))


def _read_map(workspace):
    """Read at most MAX_MAP_CHARS characters of the workspace map.

    Returns None when the file is missing or unreadable.
    """
    path = os.path.join(_root(workspace), NAME)
    try:
        # UTF-8 is at most 4 bytes per character; reading this much guarantees
        # we can produce MAX_MAP_CHARS characters without slurping a huge file.
        with open(path, "rb") as handle:
            raw = handle.read(MAX_MAP_CHARS * 4 + 4)
    except (OSError, ValueError, TypeError):
        return None
    text = raw.decode("utf-8", "replace")
    # The map is text.  Normalise CRLF/CR so digests, prompts and byte counts
    # do not depend on the platform that wrote the file (Windows text mode
    # expands every newline to CRLF).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text[:MAX_MAP_CHARS]


def _git(workspace, args, timeout):
    """Run git without a shell, bounded and timed; return stdout bytes or None."""
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 2.0
    if timeout <= 0:
        timeout = 0.05
    try:
        process = subprocess.Popen(
            ["git", "-C", _root(workspace)] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except (OSError, ValueError):
        return None
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.communicate(timeout=0.5)
        except Exception:
            pass
        return None
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        return None
    if process.returncode != 0 or output is None:
        return None
    return output[:_GIT_OUTPUT_LIMIT]


def _files_fingerprint(workspace, limit=MAX_FILES):
    """Deterministic, bounded fingerprint of a directory's files."""
    root = _root(workspace)
    entries = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                try:
                    stat = os.lstat(full)
                except OSError:
                    continue
                entries.append((rel, stat.st_mtime_ns, stat.st_size))
                if len(entries) >= limit:
                    break
            if len(entries) >= limit:
                break
    except OSError:
        pass

    entries.sort()
    digest = hashlib.sha256()
    for rel, mtime_ns, size in entries:
        line = "%s\0%d\0%d\n" % (rel, mtime_ns, size)
        digest.update(line.encode("utf-8", "surrogatepass"))
    return {"kind": "files", "files": len(entries), "sha256": digest.hexdigest()}


def fingerprint(workspace, timeout=2.0):
    """Return a deterministic fingerprint dict; never raises.

    Git workspaces use HEAD plus ``git status --porcelain``.  Anything else
    uses a bounded, sorted tracked-file list with mtimes and sizes.
    """
    try:
        root = _root(workspace)
    except Exception:
        return {"kind": "files", "files": 0, "sha256": _sha256("")}

    try:
        if os.path.exists(os.path.join(root, ".git")):
            head_raw = _git(root, ["rev-parse", "HEAD"], timeout)
            if head_raw:
                status_raw = _git(root, ["status", "--porcelain"], timeout)
                if status_raw is not None:
                    head = head_raw.decode("utf-8", "replace").strip()
                    if head:
                        status_sha = _sha256(status_raw)
                        digest = _sha256(
                            "git\0%s\0%s\0" % (head, status_sha)
                        )
                        return {
                            "kind": "git",
                            "head": head,
                            "status_sha256": status_sha,
                            "sha256": digest,
                        }
    except Exception:
        pass

    try:
        return _files_fingerprint(root)
    except Exception:
        return {"kind": "files", "files": 0, "sha256": _sha256("")}


def _state_path(workspace, state_dir):
    """Private state file for a workspace under the connector state dir."""
    try:
        identity = _sha256(_root(workspace))[:16]
    except Exception:
        identity = _sha256("")[:16]
    return os.path.join(_root(state_dir), "project-maps", identity + ".json")


def load(workspace, state_dir):
    """Load the stored record for *workspace*, or None (test/debug helper)."""
    try:
        with open(_state_path(workspace, state_dir), "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def _summary(state, present, reused, stale, nbytes, current, map_text=""):
    """Public, path-free, body-free summary."""
    return {
        "state": state,
        "present": bool(present),
        "reused": bool(reused),
        "stale": bool(stale),
        "bytes": int(nbytes),
        "fingerprint": str((current or {}).get("sha256", ""))[:12],
        # A content hash lets the connector decide whether the map was
        # refreshed during the job without storing or re-reading the body.
        "map_sha256": _sha256(map_text) if map_text else "",
        "version": VERSION,
    }


def map_digest(workspace):
    """Return the current map's sha256 (or None when the file is absent)."""
    text = _read_map(workspace)
    return _sha256(text) if text is not None else None


def _record_state(record, current):
    """Classify a present map against an optional saved record."""
    saved = record.get("fingerprint") if isinstance(record, dict) else None
    if not isinstance(saved, dict) or not saved.get("sha256"):
        return "new", False, False
    if saved.get("sha256") == current.get("sha256"):
        return "reuse", True, False
    return "stale", False, True


def offer(workspace, state_dir):
    """Offer the workspace map to the next builder.

    States: ``absent`` (no map), ``new`` (map, no saved record), ``reuse``
    (saved fingerprint matches) and ``stale`` (saved fingerprint differs).
    """
    map_text = _read_map(workspace)
    if map_text is None:
        summary = _summary("absent", False, False, False, 0, {})
        return {
            "state": "absent",
            "present": False,
            "reused": False,
            "stale": False,
            "bytes": 0,
            "prompt": "",
            "summary": summary,
        }

    current = fingerprint(workspace)
    state, reused, stale = _record_state(load(workspace, state_dir), current)
    if state == "reuse":
        instruction = _REUSE_PROMPT
    elif state == "stale":
        instruction = _STALE_PROMPT
    else:
        instruction = _NEW_PROMPT

    nbytes = len(map_text.encode("utf-8", "replace"))
    prompt = "Current PROJECT_MAP.md:\n\n%s\n\n%s" % (map_text, instruction)
    summary = _summary(state, True, reused, stale, nbytes, current, map_text)
    return {
        "state": state,
        "present": True,
        "reused": reused,
        "stale": stale,
        "bytes": nbytes,
        "prompt": prompt,
        "summary": summary,
    }


def _store(workspace, state_dir, current, map_text):
    """Atomically write the private record with 0600/0700 permissions."""
    path = _state_path(workspace, state_dir)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        payload = {
            "version": VERSION,
            "workspace": _root(workspace),
            "fingerprint": current,
            "bytes": len(map_text.encode("utf-8", "replace")),
            "map": map_text,
        }
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (OSError, TypeError, ValueError):
        return False

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".project-map-", suffix=".tmp", dir=directory)
        try:
            # os.fchmod is Unix-only; on Windows (notably 3.9) it does not
            # exist, and the existing os.chmod(path) below is the fallback.
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                try:
                    fchmod(fd, 0o600)
                except (AttributeError, OSError):
                    pass
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
            os.chmod(path, 0o600)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except (OSError, ValueError):
        return False
    return True


def capture(workspace, state_dir):
    """Store the current map/fingerprint and return the public summary + stored."""
    map_text = _read_map(workspace)
    if map_text is None:
        summary = _summary("absent", False, False, False, 0, {})
        return {
            "state": "absent",
            "present": False,
            "reused": False,
            "stale": False,
            "bytes": 0,
            "prompt": "",
            "summary": summary,
            "stored": False,
        }

    current = fingerprint(workspace)
    state, reused, stale = _record_state(load(workspace, state_dir), current)
    stored = _store(workspace, state_dir, current, map_text)
    nbytes = len(map_text.encode("utf-8", "replace"))
    summary = _summary(state, True, reused, stale, nbytes, current, map_text)
    return {
        "state": state,
        "present": True,
        "reused": reused,
        "stale": stale,
        "bytes": nbytes,
        "prompt": "",
        "summary": summary,
        "stored": stored,
    }
