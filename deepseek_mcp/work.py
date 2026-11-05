"""Work items: stable request fingerprints, ids, and concurrency-safe leases.

A *work item* represents one logical request.  ``request_fingerprint`` derives a
stable hash from the arguments that affect the result (ignoring wait/title/
telemetry fields), ``work_id_for`` turns that hash (or an explicit idempotency
key) into a filesystem-safe id, and :class:`WorkStore` keeps the mutable lease
state for each id.

Layout under ``<state_dir>/work``::

    <work_id>/work.json       current lease snapshot (schema 1)
    <work_id>/attempts.jsonl  append-only attempt journal
    <work_id>/.lock           short-lived O_CREAT|O_EXCL mutex

All mutating operations take the per-work ``.lock``; readers tolerate a
truncated final JSONL line.  The module only depends on the standard library
and :mod:`deepseek_mcp.platforms` (for cross-platform ``pid_alive``).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import platforms

try:  # package import; keeps status/attempt payloads free of secrets
    from .api import redact_secrets as _redact
except Exception:  # pragma: no cover - standalone import
    def _redact(text):
        return text

#: Argument names that never participate in a request fingerprint.
FINGERPRINT_EXCLUDE = {
    "wait_seconds",
    "title",
    "_run_id",
    "_resume_from",
    "idempotency_key",
}

#: Private/plumbing argument prefix, excluded from fingerprints.
EXCLUDED_PREFIX = "_"

#: On-disk work.json schema version.
WORK_SCHEMA = 1

WORK_FILE = "work.json"
ATTEMPTS_FILE = "attempts.jsonl"
LOCK_FILE = ".lock"

#: A lock is considered abandoned after this many seconds.
LOCK_MAX_AGE = 60.0
#: Poll interval while waiting for a lock.
LOCK_POLL = 0.01

_TERMINAL_STATES = ("finished", "expired", "failed", "cancelled", "terminal")
_MAX_IDEMPOTENCY_KEY = 200
_WORK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _iso(epoch):
    """Return an ISO-8601 local-aware timestamp (second precision)."""
    return dt.datetime.fromtimestamp(float(epoch)).astimezone().isoformat(
        timespec="seconds")


def _epoch(value):
    """Parse a timestamp produced by :func:`_iso`; ``None`` when unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _realpath(value):
    """``~``-expand and resolve a path argument to an absolute real path."""
    return os.path.realpath(os.path.expanduser(str(value)))


def _normalise_paths(value):
    """Resolve string entries of a ``files``/``add_dirs`` argument."""
    if isinstance(value, str):
        return _realpath(value)
    if isinstance(value, (list, tuple)):
        return [_realpath(item) if isinstance(item, str) else item
                for item in value]
    return value


def _normalise_args(args):
    """Copy ``args`` without excluded/private keys and with resolved paths."""
    normalised = {}
    for key, value in (args or {}).items():
        name = key if isinstance(key, str) else str(key)
        if name == "workspace":
            # The resolved workspace lives at the payload's top level.
            continue
        if name in FINGERPRINT_EXCLUDE or name.startswith(EXCLUDED_PREFIX):
            continue
        normalised[name] = value
    for field in ("files", "add_dirs"):
        if field in normalised:
            normalised[field] = _normalise_paths(normalised[field])
    return normalised


def request_fingerprint(tool, args, workspace=None):
    """Return the SHA-256 fingerprint of a logical request.

    Canonical JSON (sorted keys, compact separators, UTF-8) hashes
    ``{"tool": tool, "workspace": <realpath>, "args": <normalised args>}``.
    Wait/title/telemetry/idempotency-key arguments and every ``_``-prefixed
    argument are ignored, so retries and re-waits share one identity.  String
    ``files`` and ``add_dirs`` entries are resolved so two spellings of the
    same path fingerprint identically.
    """
    args = args if isinstance(args, dict) else {}
    raw_workspace = args.get("workspace") if "workspace" in args else None
    if raw_workspace is not None and str(raw_workspace).strip():
        resolved_workspace = _realpath(raw_workspace)
    else:
        resolved_workspace = _realpath(workspace if workspace is not None else "")
    payload = {
        "tool": tool,
        "workspace": resolved_workspace,
        "args": _normalise_args(args),
    }
    try:
        blob = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "request arguments must be JSON-serialisable for a fingerprint: %s"
            % exc)
    return hashlib.sha256(blob).hexdigest()


_NO_KEY = object()


def work_id_for(fingerprint, idempotency_key=_NO_KEY):
    """Map a fingerprint or explicit idempotency key to a work id.

    Without a key the first 19 hex chars of the fingerprint are used
    (``w<19>``).  With a key the id is derived only from the stripped key
    (``k<19>``), so it stays stable when the request arguments change.  An
    explicit ``None`` is not a valid key (only omitting the argument is).
    """
    if idempotency_key is _NO_KEY:
        return "w" + str(fingerprint)[:19]
    if not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key must be a string")
    key = idempotency_key.strip()
    if not key or len(key) > _MAX_IDEMPOTENCY_KEY:
        raise ValueError(
            "idempotency_key must be 1..%d characters after stripping"
            % _MAX_IDEMPOTENCY_KEY)
    digest = hashlib.sha256(b"idempotency-key\x00" + key.encode("utf-8"))
    return "k" + digest.hexdigest()[:19]


def valid_work_id(work_id):
    """True for a filesystem-safe work id (generated ids are ``w``/``k``+hex)."""
    return (isinstance(work_id, str) and work_id not in (".", "..")
            and bool(_WORK_ID_RE.match(work_id)))


def _check_work_id(work_id):
    """Reject ids that could escape the work root."""
    text = str(work_id)
    if (not text or text in (".", "..") or "/" in text or "\\" in text
            or os.sep in text or (os.altsep and os.altsep in text)):
        raise ValueError("invalid work id: %r" % (work_id,))
    return text


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class WorkStore:
    """Durable, concurrency-safe store for work admission and leases.

    ``root`` is ``<state_dir>/work``.  ``clock`` returns epoch seconds and is
    injectable for tests.  A work item is stale when its recorded pid is dead
    or when its last heartbeat is older than ``stale_after`` (default
    ``2 * heartbeat_seconds``); a work item that has not been bound to a run
    yet has no pid and is only aged out by the heartbeat timeout.
    """

    def __init__(self, root, clock=None, heartbeat_seconds=30.0,
                 stale_after=None, lock_timeout=5.0):
        self.root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self.clock = clock or time.time
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.stale_after = (float(stale_after) if stale_after is not None
                            else 2.0 * self.heartbeat_seconds)
        self.lock_timeout = float(lock_timeout)

    # -- paths and low-level IO -------------------------------------------

    def path(self, work_id):
        """Return the directory holding ``work_id`` (filesystem string path)."""
        return os.path.join(str(self.root), _check_work_id(work_id))

    def _dir(self, work_id):
        """Return the work directory as a :class:`~pathlib.Path`."""
        return Path(self.path(work_id))

    def _now(self, now=None):
        if now is not None:
            return float(now)
        return float(self.clock())

    def _ensure_dir(self, work_id):
        directory = self._dir(work_id)
        os.makedirs(str(directory), mode=0o700, exist_ok=True)
        try:
            os.chmod(str(directory), 0o700)
        except OSError:
            pass
        return directory

    @staticmethod
    def _write_json(path, data):
        """Atomically write ``data`` as indented JSON with 0600 permissions."""
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _append_jsonl(path, record):
        """Append one compact JSON line and fsync it."""
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write appending %s" % path)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def _lock(self, work_id, timeout=None):
        """Hold the per-work ``.lock``; retry ~10 ms, break abandoned locks.

        A lock is abandoned when it is older than 60 s or names a dead pid.
        The lock file is always unlinked by the holder in ``finally``.
        """
        directory = self._ensure_dir(work_id)
        lock_path = directory / LOCK_FILE
        wait = self.lock_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, wait)
        fd = None
        while True:
            try:
                fd = os.open(str(lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                if self._lock_abandoned(lock_path):
                    try:
                        os.unlink(str(lock_path))
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "timed out waiting for work lock %s" % lock_path)
                time.sleep(LOCK_POLL)
        try:
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(),
                    "at": _iso(self._now()),
                }).encode("utf-8"))
                os.fsync(fd)
            except OSError:
                pass
            os.close(fd)
            fd = None
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass

    @staticmethod
    def _lock_abandoned(lock_path):
        """Heuristic: lock is old, or its recorded owner pid is dead."""
        try:
            age = time.time() - os.stat(str(lock_path)).st_mtime
        except OSError:
            return False
        pid = None
        try:
            with open(str(lock_path), "r", encoding="utf-8") as handle:
                raw = handle.read()
            if raw.strip():
                data = json.loads(raw)
                if isinstance(data, dict):
                    pid = data.get("pid")
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            try:
                if not platforms.pid_alive(pid):
                    return True
            except Exception:
                pass
        return age > LOCK_MAX_AGE

    # -- reads ------------------------------------------------------------

    def load(self, work_id):
        """Return the current work dict, or ``None`` when absent/corrupt."""
        work_id = _check_work_id(work_id)
        path = self._dir(work_id) / WORK_FILE
        try:
            with open(str(path), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def attempts(self, work_id):
        """Return the parsed attempt journal, skipping blank/bad/partial lines."""
        work_id = _check_work_id(work_id)
        path = self._dir(work_id) / ATTEMPTS_FILE
        records = []
        try:
            with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records

    # -- staleness --------------------------------------------------------

    def _is_stale(self, work, now):
        """True when an active work's owner is gone or its heartbeat expired."""
        if not isinstance(work, dict) or work.get("state") != "active":
            return False
        pid = work.get("pid")
        if pid is not None:
            try:
                if not platforms.pid_alive(pid):
                    return True
            except Exception:
                pass
        last = _epoch(work.get("last_heartbeat"))
        if last is None:
            last = _epoch(work.get("updated"))
        if last is None:
            last = _epoch(work.get("created"))
        if last is None:
            return True
        return (now - last) > self.stale_after

    def _expire_locked(self, work_id, now, reason):
        """Mark an admitted work expired; caller must hold the work lock."""
        work = self.load(work_id) or {}
        work["state"] = "expired"
        work["terminal_status"] = "expired"
        work["updated"] = _iso(now)
        work["note"] = None
        self._write_json(self._dir(work_id) / WORK_FILE, work)
        self._append_jsonl(self._dir(work_id) / ATTEMPTS_FILE, {
            "event": "expired",
            "status": "expired",
            "reason": reason,
            "attempt_no": work.get("attempt_no"),
            "run_id": work.get("canonical_run_id"),
            "at": _iso(now),
        })
        return work

    def expire_stale(self, now=None):
        """Expire every active work whose owner died or heartbeat timed out.

        Returns the list of expired work ids.  A malformed directory is
        skipped, never raised.
        """
        now = self._now(now)
        expired = []
        try:
            names = sorted(os.listdir(str(self.root)))
        except OSError:
            return expired
        for name in names:
            try:
                directory = self.root / name
                if not directory.is_dir():
                    continue
                work = self.load(name)
                if not isinstance(work, dict) or work.get("state") != "active":
                    continue
                if not self._is_stale(work, now):
                    continue
                with self._lock(name):
                    current = self.load(name)
                    if not isinstance(current, dict) or current.get("state") != "active":
                        continue
                    if not self._is_stale(current, now):
                        continue
                    pid = current.get("pid")
                    dead_pid = False
                    if pid is not None:
                        try:
                            dead_pid = not platforms.pid_alive(pid)
                        except Exception:
                            dead_pid = False
                    reason = "pid_dead" if dead_pid else "heartbeat_timeout"
                    self._expire_locked(name, now, reason)
                    expired.append(name)
            except Exception:
                # A broken directory must never take the whole sweep down.
                continue
        return expired

    # -- admission --------------------------------------------------------

    def admit(self, fingerprint, idempotency_key=None, dedupe_active=True,
              note=None):
        """Admit a request and return its lease decision.

        Returns ``{"work_id", "attempt_no", "reused", "run_id", "pending",
        "note", "work"}``.  With ``dedupe_active`` an existing non-stale
        active work is reused without allocating an attempt; otherwise a new
        attempt is allocated under the same (stable) work id.  Every decision
        is made while holding the work lock, so concurrent admits of one
        fingerprint produce exactly one non-reused result.
        """
        work_id = (work_id_for(fingerprint) if idempotency_key is None
                   else work_id_for(fingerprint, idempotency_key))
        now = self._now()
        at = _iso(now)
        key = (idempotency_key.strip()
               if isinstance(idempotency_key, str) else None)
        key_hash = (hashlib.sha256(
            b"idempotency-key\x00" + key.encode("utf-8")).hexdigest()
            if key else None)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is not None and self._is_stale(work, now):
                work = self._expire_locked(work_id, now, "stale")
            if (work is not None and work.get("state") == "active"
                    and dedupe_active):
                if work.get("note") is not None:
                    work = dict(work)
                    work["note"] = None
                    work["updated"] = at
                    self._write_json(self._dir(work_id) / WORK_FILE, work)
                run_id = work.get("canonical_run_id")
                return {
                    "work_id": work_id,
                    "attempt_no": work.get("attempt_no"),
                    "reused": True,
                    "run_id": run_id,
                    "pending": run_id is None,
                    "note": None,
                    "work": work,
                }
            if work is None:
                attempt_no = 1
                work = {
                    "schema": WORK_SCHEMA,
                    "work_id": work_id,
                    "fingerprint": fingerprint,
                    "idempotency_key_hash": key_hash,
                    "state": "active",
                    "attempt_no": attempt_no,
                    "canonical_run_id": None,
                    "last_run_id": None,
                    "pid": None,
                    "note": note,
                    "created": at,
                    "updated": at,
                    "last_heartbeat": at,
                    "finished_at": None,
                    "terminal_status": None,
                }
            else:
                attempt_no = int(work.get("attempt_no") or 0) + 1
                work = dict(work)
                work["schema"] = WORK_SCHEMA
                work["work_id"] = work_id
                work["fingerprint"] = fingerprint
                work["idempotency_key_hash"] = key_hash
                work["state"] = "active"
                work["attempt_no"] = attempt_no
                work["canonical_run_id"] = None
                work["last_run_id"] = None
                work["pid"] = None
                work["note"] = note
                work["updated"] = at
                work["last_heartbeat"] = at
                work["finished_at"] = None
                work["terminal_status"] = None
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            line = {
                "event": "admitted",
                "attempt_no": attempt_no,
                "run_id": None,
                "at": at,
            }
            if note:
                line["note"] = note
            self._append_jsonl(self._dir(work_id) / ATTEMPTS_FILE, line)
            return {
                "work_id": work_id,
                "attempt_no": attempt_no,
                "reused": False,
                "run_id": None,
                "pending": True,
                "note": note,
                "work": work,
            }

    # -- lease transitions -------------------------------------------------

    def begin_attempt(self, work_id, reason="resume", pid=None):
        """Start a new attempt (used by resume); always increments."""
        now = self._now()
        at = _iso(now)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is None:
                raise KeyError("no work item %r" % (work_id,))
            work = dict(work)
            work["attempt_no"] = int(work.get("attempt_no") or 0) + 1
            work["state"] = "active"
            work["canonical_run_id"] = None
            work["pid"] = pid
            work["last_heartbeat"] = at
            work["updated"] = at
            work["finished_at"] = None
            work["terminal_status"] = None
            work["note"] = reason
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            self._append_jsonl(self._dir(work_id) / ATTEMPTS_FILE, {
                "event": "admitted",
                "reason": reason,
                "attempt_no": work["attempt_no"],
                "run_id": None,
                "pid": pid,
                "at": at,
            })
            return work

    def bind_run(self, work_id, attempt_no, run_id, pid=None):
        """Bind a run to an admitted attempt (no new journal line)."""
        now = self._now()
        at = _iso(now)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is None:
                raise KeyError("no work item %r" % (work_id,))
            try:
                wanted_attempt = int(attempt_no)
            except (TypeError, ValueError):
                return work
            current_attempt = int(work.get("attempt_no") or 0)
            if wanted_attempt < current_attempt:
                return work
            canonical = work.get("canonical_run_id")
            if (wanted_attempt == current_attempt and canonical not in (None, run_id)):
                # A superseded same-number bind must not steal the lease.
                return work
            work = dict(work)
            work["canonical_run_id"] = run_id
            work["last_run_id"] = run_id
            work["attempt_no"] = wanted_attempt
            work["pid"] = pid
            work["state"] = "active"
            work["last_heartbeat"] = at
            work["updated"] = at
            work["terminal_status"] = None
            work["finished_at"] = None
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            return work

    def record_attempt(self, work_id, run_id=None, attempt_no=None, pid=None,
                       status="started", note=None):
        """Append one attempt journal line and refresh the work snapshot."""
        now = self._now()
        at = _iso(now)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is None:
                raise KeyError("no work item %r" % (work_id,))
            work = dict(work)
            if run_id is not None:
                work["last_run_id"] = run_id
                if work.get("canonical_run_id") is None:
                    work["canonical_run_id"] = run_id
            if attempt_no is not None:
                try:
                    work["attempt_no"] = max(int(work.get("attempt_no") or 0),
                                             int(attempt_no))
                except (TypeError, ValueError):
                    pass
            if pid is not None:
                work["pid"] = pid
                work["last_heartbeat"] = at
            if status is not None:
                work["last_status"] = status
            if note is not None:
                work["note"] = note
            work["updated"] = at
            if work.get("state") == "active":
                work["last_heartbeat"] = at
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            self._append_jsonl(self._dir(work_id) / ATTEMPTS_FILE, {
                "event": "attempt",
                "status": status,
                "attempt_no": work.get("attempt_no"),
                "run_id": run_id,
                "pid": pid,
                "note": note,
                "at": at,
            })
            return work

    def hold(self, work_id, run_id=None, status="needs_input"):
        """Keep a work item active while a validated question awaits an answer.

        The attempt that produced the question has returned, but it is not
        terminal from the caller's point of view.  Keeping the lease active
        lets an identical retry find the existing run instead of paying for a
        duplicate; stale expiry still applies when nobody polls or answers.
        """
        now = self._now()
        at = _iso(now)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is None:
                return None
            canonical = work.get("canonical_run_id")
            if run_id is not None and canonical is not None and run_id != canonical:
                return work
            work = dict(work)
            work["state"] = "active"
            work["terminal_status"] = None
            work["last_status"] = status
            # The worker that asked the question is exiting; a dead pid must
            # not expire the held lease immediately.  Heartbeat age governs.
            work["pid"] = None
            work["finished_at"] = None
            work["updated"] = at
            work["last_heartbeat"] = at
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            self._append_jsonl(self._dir(work_id) / ATTEMPTS_FILE, {
                "event": "held",
                "status": status,
                "attempt_no": work.get("attempt_no"),
                "run_id": work.get("canonical_run_id"),
                "at": at,
            })
            return work

    def heartbeat(self, work_id, run_id=None, pid=None, force=False):
        """Refresh the lease heartbeat (throttled); never resurrect terminal work.

        Returns the work dict, or ``None`` when the work does not exist.  A
        heartbeat from a run that is no longer canonical is ignored.
        """
        now = self._now()
        at = _iso(now)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is None:
                return None
            if (work.get("state") in _TERMINAL_STATES
                    or work.get("terminal_status")):
                return work
            canonical = work.get("canonical_run_id")
            if run_id is not None and canonical is not None and run_id != canonical:
                return work
            last = _epoch(work.get("last_heartbeat"))
            threshold = self.heartbeat_seconds / 2.0
            if not force and last is not None and (now - last) < threshold:
                return work
            work = dict(work)
            if canonical is None and run_id is not None:
                work["canonical_run_id"] = run_id
            if run_id is not None:
                work["last_run_id"] = run_id
            if pid is not None:
                work["pid"] = pid
            work["last_heartbeat"] = at
            work["updated"] = at
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            return work

    def finish(self, work_id, status, run_id=None, result=None):
        """Finish the work when ``run_id`` matches the canonical attempt.

        A finish from a superseded attempt (``run_id`` differs from
        ``canonical_run_id``) is ignored and returns the unchanged work.
        Repeating the same finish is idempotent and does not add a journal
        line.
        """
        if not isinstance(status, str) or not status.strip():
            raise ValueError("finish requires a non-empty status")
        now = self._now()
        at = _iso(now)
        with self._lock(work_id):
            work = self.load(work_id)
            if work is None:
                return None
            canonical = work.get("canonical_run_id")
            if run_id is not None and canonical is not None and run_id != canonical:
                return work
            if (work.get("state") == "finished"
                    and work.get("terminal_status") == status):
                return work
            work = dict(work)
            if run_id is not None and canonical is None:
                work["canonical_run_id"] = run_id
            if run_id is not None:
                work["last_run_id"] = run_id
            work["state"] = "finished"
            work["terminal_status"] = status
            work["finished_at"] = at
            work["updated"] = at
            if result is not None:
                work["result"] = _redact(str(result))[:2000]
            self._write_json(self._dir(work_id) / WORK_FILE, work)
            line = {
                "event": "finished",
                "status": status,
                "attempt_no": work.get("attempt_no"),
                "run_id": run_id if run_id is not None else work.get("canonical_run_id"),
                "at": at,
            }
            if result is not None:
                line["result"] = _redact(str(result))[:2000]
            self._append_jsonl(self._dir(work_id) / ATTEMPTS_FILE, line)
            return work

    # -- lookup ------------------------------------------------------------

    def find_active(self, fingerprint, idempotency_key=None):
        """Return a live active work for the identity, expiring stale claims first."""
        work_id = (work_id_for(fingerprint) if idempotency_key is None
                   else work_id_for(fingerprint, idempotency_key))
        self.expire_stale()
        now = self._now()
        work = self.load(work_id)
        if not isinstance(work, dict) or work.get("state") != "active":
            return None
        if not self._is_stale(work, now):
            return work
        with self._lock(work_id):
            current = self.load(work_id)
            if not isinstance(current, dict) or current.get("state") != "active":
                return None
            if not self._is_stale(current, now):
                return current
            self._expire_locked(work_id, now, "stale")
        return None
