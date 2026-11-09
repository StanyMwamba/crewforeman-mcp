"""Child-session progress for the parent-only stop watch.

Harness emits no parent event while a ``workflow`` call waits on its
``parallel()`` barrier.  A healthy wave can therefore look silent for the whole
480 s stall window even though every child is still writing its session log.
A live first-wave run was killed exactly this way: the parent's
last event was an ``agent-start`` at 74 s while children emitted events until
479 s.

This module watches only file metadata (no session contents, no writes) in the
encoded workspace session directory.  A growing ``(mtime_ns, size)`` means a
child is alive, so the caller can refresh the stall timer and report an honest
active-child count.  Missing directories and permission errors degrade to "no
child activity" instead of failing the run.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from . import platforms

DEFAULT_ACTIVE_WINDOW = 180.0
LOG_PATTERNS = ("*.jsonl.zstd", "*.jsonl.zst", "*.jsonl")
# The installed Harness keeps these characters literal and escapes every other
# UTF-16 code unit.  ``~`` is deliberately not in this set.
_SAFE_KEY_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
SEPARATOR_CHARS = frozenset("/\\:")
MAX_KEY_CHARS = 251


class ProgressPathError(ValueError):
    """A progress write would leave the workspace or follow a reparse point."""


def _call_platform(name, *args, system=None, default=False):
    helper = getattr(platforms, name, None)
    if helper is None:
        return default
    try:
        return helper(*args, system=system)
    except TypeError:
        try:
            return helper(*args)
        except Exception:
            return default
    except Exception:
        return default


def _resolved(path, system=None):
    resolved = _call_platform("canonical_path", str(path), system=system,
                              default=None)
    if resolved is None:
        return os.path.realpath(os.path.expanduser(str(path)))
    return str(resolved)


def write_json(path, data, root=None, system=None):
    """Atomically write UTF-8/LF JSON after refusing reparse/symlink escapes.

    ``root`` bounds the write (normally the workspace).  The check runs before
    the temporary file is created, so an escape writes nothing outside it.
    """
    target = Path(os.path.expanduser(str(path)))
    base = _resolved(root if root is not None else target.parent, system)
    if _call_platform("reparse_point", str(target), system=system,
                      default=False):
        raise ProgressPathError(
            "refusing to write through a reparse point (private=false)")
    resolved = _resolved(target, system)
    base_norm = os.path.normcase(base)
    target_norm = os.path.normcase(resolved)
    if (target_norm != base_norm
            and not target_norm.startswith(base_norm.rstrip(os.sep) + os.sep)):
        raise ProgressPathError("refusing to write outside the workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".progress-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(target))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return str(target)


def project_key(cwd):
    """Port of the installed Harness ``projectKey`` path encoder.

    The session directory is not a slug: Harness collapses ``/``, ``\\`` and
    ``:`` runs to one ``-``, leaves only ASCII letters/digits/``._-`` literal,
    escapes every other UTF-16 code unit as ``~XXXX`` (so ``My Drive`` becomes
    ``My~0020Drive``), strips leading dashes, truncates the readable part to
    251 code units and wraps it in ``--``.  Encoding the same path differently
    would make :class:`ChildActivity` watch an empty directory and turn a
    healthy wave into a false stall.

    This is a code-unit port, not a character port: JavaScript iterates UTF-16,
    so an emoji becomes two escapes (``~D83D~DE00``).  Python strings are
    encoded to UTF-16-LE with ``surrogatepass`` to preserve that behaviour,
    including lone surrogates.
    """
    text = str(cwd)
    if not text:
        raise ValueError("cannot encode an empty project path")
    raw = text.encode("utf-16-le", "surrogatepass")
    readable = []
    separator_run = False
    for index in range(0, len(raw), 2):
        code = raw[index] | (raw[index + 1] << 8)
        char = chr(code)
        if char in SEPARATOR_CHARS:
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif char != "~" and char in _SAFE_KEY_CHARS:
            readable.append(char)
            separator_run = False
        else:
            readable.append("~%04X" % code)
            separator_run = False
    body = "".join(readable).lstrip("-") or "root"
    return "--%s--" % body[:MAX_KEY_CHARS]


def encode_workspace(workspace):
    """Return the Harness session-directory name for a workspace cwd."""
    resolved = str(Path(os.path.expanduser(str(workspace))).resolve())
    return project_key(resolved)


class ChildActivity(object):
    """Track growth of child session logs for one workspace.

    ``scan()`` is cheap and idempotent: the first call records a baseline so
    pre-existing logs cannot count as progress; later calls return
    ``changed=True`` only when a log grows.  The object is safe to call from
    one monitor thread; the lock also makes concurrent tests harmless.
    """

    def __init__(self, sessions_root, workspace, since=None,
                 active_window=DEFAULT_ACTIVE_WINDOW):
        self.root = Path(os.path.expanduser(str(sessions_root)))
        self.workspace = str(workspace)
        self.directory = self.root / encode_workspace(workspace)
        self.cutoff = (time.time() if since is None else float(since)) - 5.0
        self.active_window = max(1.0, float(active_window))
        self._seen = {}
        self.exclude_ids = set()
        # ``None`` means "this run's child ids are not known yet"; when the
        # parent event stream advertises child ids, only those sessions count.
        # That keeps a concurrent run in the same workspace from refreshing
        # this run's watchdog.
        self.include_ids = None
        self._baseline = False
        self._events = 0
        self._active = 0
        self._sessions = 0
        self._newest = 0.0
        self._lock = threading.Lock()

    @property
    def events(self):
        with self._lock:
            return self._events

    @property
    def active(self):
        with self._lock:
            return self._active

    @property
    def sessions(self):
        with self._lock:
            return self._sessions

    def add_ids(self, session_ids):
        """Restrict future scans to this run's advertised child session ids."""
        clean = {str(item) for item in (session_ids or []) if item}
        if not clean:
            return
        with self._lock:
            if self.include_ids is None:
                self.include_ids = set()
            self.include_ids.update(clean)

    def _filters(self):
        with self._lock:
            excluded = set(self.exclude_ids)
            included = set(self.include_ids) if self.include_ids is not None else None
        return excluded, included

    def _paths(self):
        """One session-id level only, never following symlinks.

        The session layout is ``<encoded-workspace>/<session-id>/session.*``.
        A bounded non-recursive scan avoids walking a large/shared session tree
        every 250 ms and prevents a planted symlink from making the reader stat
        files outside the workspace.
        """
        try:
            entries = list(os.scandir(str(self.directory)))
        except OSError:
            return []
        found = []
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                children = os.scandir(entry.path)
            except OSError:
                continue
            try:
                for child in children:
                    try:
                        if not child.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if any(child.name.endswith(pattern.lstrip("*"))
                           for pattern in LOG_PATTERNS):
                        found.append(Path(child.path))
            finally:
                try:
                    children.close()
                except OSError:
                    pass
        return found

    def scan(self):
        """Return ``{changed, active, sessions, newest}`` and update counters."""
        current = {}
        now = time.time()
        active = 0
        newest = 0.0
        excluded, included = self._filters()
        for path in self._paths():
            if path.parent.name in excluded:
                # The parent Harness session writes its own log; that is not
                # child progress and must not mask a genuinely silent parent.
                continue
            if included is not None and path.parent.name not in included:
                # Another run in the same workspace cannot keep us alive.
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < self.cutoff:
                continue
            key = str(path)
            current[key] = (stat.st_mtime_ns, stat.st_size)
            if stat.st_mtime >= now - self.active_window:
                active += 1
            if stat.st_mtime > newest:
                newest = stat.st_mtime
        with self._lock:
            changed = False
            if not self._baseline:
                self._baseline = True
            else:
                for key, value in current.items():
                    if self._seen.get(key) != value:
                        changed = True
                        break
            self._seen = current
            if changed:
                self._events += 1
            self._active = active
            self._sessions = len(current)
            self._newest = newest
        return {"changed": changed, "active": active,
                "sessions": len(current), "newest": newest}
