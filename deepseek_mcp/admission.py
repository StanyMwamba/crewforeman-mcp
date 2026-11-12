"""Bounded, fair admission control for Harness child jobs (P3-1).

Persistent JSON queue, leases with fencing tokens, and a tiny dispatcher.
Pure stdlib; all state lives under a caller-provided root directory.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

from . import platforms

try:
    from . import concurrency
except Exception:  # pragma: no cover - defensive
    concurrency = None

PRIORITY_RESUME = 0
PRIORITY_BUILD = 1

# P3.1: urgency is a flag, not a new numeric priority.  Old records (which
# only have PRIORITY_RESUME / PRIORITY_BUILD) keep their exact ordering, while
# an urgent or depended-on build sorts ahead of ordinary builds at the same
# numeric priority.  P0 resume remains unconditionally first.
_PRIORITY_LABELS = {PRIORITY_RESUME: "P0 resume", PRIORITY_BUILD: "P1 new build"}
_TERMINAL = ("done", "failed", "cancelled", "error")
_ERROR_STATUSES = frozenset((
    "error", "failed", "failure", "cancelled", "canceled", "no_balance",
    "deadline_exceeded", "budget_exceeded", "timeout",
    "time_budget_exceeded", "cost_budget_exceeded", "gate_failed"))
_SUCCESS_STATUSES = frozenset((
    "done", "ok", "success", "succeeded", "complete", "completed",
    "ok_unreviewed", "ok_below_gate", "ok_rescued"))
_WAITING_STATUSES = frozenset((
    "queued", "running", "waiting", "pending", "starting", "in_progress",
    "needs_input"))

def _progress_blocks_completion(record):
    """True when a ``done`` status is still held for human input.

    P3.1: a run whose status is ``done`` but whose progress is
    ``needs_input`` is not finished work; dependents must keep waiting until
    progress reports a terminal value such as ``finished``.
    """
    if not isinstance(record, dict):
        return False
    progress = str(record.get("progress") or "").strip().lower()
    return progress in _WAITING_STATUSES


class AdmissionError(RuntimeError):
    """Raised when admission state cannot be safely acquired or read."""


class DependencyError(ValueError):
    """A dependency reference is unknown, failed, or cyclic."""


def priority_label(priority, urgent=False):
    try:
        value = int(priority)
    except Exception:
        value = PRIORITY_BUILD
    if value == PRIORITY_RESUME:
        return "P0 resume"
    if urgent:
        return "P1 urgent"
    return _PRIORITY_LABELS.get(value, "P1 new build")


def _detected_capacity(cfg, allow_probe=False):
    if concurrency is None:
        return 8
    try:
        result = concurrency.detect(cfg, allow_probe=allow_probe)
        limit = result.get("limit") if isinstance(result, dict) else result
        limit = int(limit)
        return limit if limit > 0 else 8
    except Exception:
        return 8


def resolved_capacity(cfg, detected_limit=None):
    """Effective admission capacity.  None means unlimited."""
    raw = getattr(cfg, "max_harness_jobs", 4)
    try:
        raw = int(raw)
    except Exception:
        raw = 4
    if raw <= 0:
        return None
    if detected_limit is None:
        detected_limit = _detected_capacity(cfg)
    try:
        detected_limit = int(detected_limit)
    except Exception:
        detected_limit = 8
    if detected_limit <= 0:
        detected_limit = 8
    return min(raw, detected_limit)


def _now_iso(now):
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def _default_pid_alive(pid):
    if pid is None:
        return False
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    return platforms.pid_alive(pid)


class AdmissionStore:
    def __init__(self, root, heartbeat_seconds=30.0, lease_seconds=90.0,
                 now=None, pid_alive=None, dependency_probe=None):
        self.root = os.path.abspath(str(root))
        self.items_dir = os.path.join(self.root, "items")
        self.leases_dir = os.path.join(self.root, "leases")
        self.locks_dir = os.path.join(self.root, "locks")
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.lease_seconds = float(lease_seconds)
        self._now = now or time.time
        self._pid_alive = pid_alive or _default_pid_alive
        # Callable ``probe(run_id) -> dict|None``.  It is only consulted for a
        # dependency that has no queue record (or to detect an error recorded
        # in the run status after the queue item finished).  File reads only:
        # cheap and no model calls.
        self.dependency_probe = dependency_probe
        self._lock = threading.RLock()
        self._thread_locks = {}

    # -- low-level helpers -------------------------------------------------
    def _now_val(self):
        try:
            return float(self._now())
        except Exception:
            return time.time()

    def _ensure_dirs(self):
        for d in (self.items_dir, self.leases_dir, self.locks_dir):
            try:
                os.makedirs(d, mode=0o700, exist_ok=True)
            except OSError as exc:
                raise AdmissionError("cannot create admission dir %s: %s" % (d, exc))

    def _safe_id(self, work_id):
        text = str(work_id or "").strip()
        if not text or "/" in text or "\\" in text or text in (".", ".."):
            raise AdmissionError("unsafe work id: %r" % (work_id,))
        return text

    def _path(self, kind, work_id):
        base = {"items": self.items_dir, "leases": self.leases_dir,
                "locks": self.locks_dir}[kind]
        return os.path.join(base, self._safe_id(work_id) + ".json")

    def _read_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise AdmissionError("cannot read %s: %s" % (path, exc))

    def _atomic_write(self, path, payload):
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = os.path.join(directory, ".%s.%s.tmp" % (os.path.basename(path),
                                                       uuid.uuid4().hex))
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise AdmissionError("cannot write %s: %s" % (path, exc))

    class _FileLock:
        def __init__(self, path):
            self.path = path
            self.fh = None
            self.exclusive = False

        def __enter__(self):
            try:
                import fcntl
            except ImportError:
                # Windows and other platforms without fcntl: an O_EXCL lock
                # file. A stale lock from a crashed process is removed after
                # 30 s; persistent failure still fails closed.
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    try:
                        fd = os.open(self.path, os.O_CREAT | os.O_EXCL |
                                     os.O_RDWR, 0o600)
                        self.fh = os.fdopen(fd, "a+")
                        self.exclusive = True
                        return self
                    except FileExistsError:
                        try:
                            if time.time() - os.path.getmtime(self.path) > 30:
                                os.unlink(self.path)
                        except OSError:
                            pass
                        time.sleep(0.02)
                raise AdmissionError(
                    "admission lock unavailable for %s" % self.path)
            try:
                self.fh = open(self.path, "a+")
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
                return self
            except Exception as exc:
                try:
                    if self.fh is not None:
                        self.fh.close()
                except Exception:
                    pass
                self.fh = None
                raise AdmissionError("admission lock unavailable for %s: %s"
                                     % (self.path, exc))

        def __exit__(self, *exc_info):
            try:
                if self.fh is not None:
                    if not self.exclusive:
                        try:
                            import fcntl
                            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
                    self.fh.close()
            except Exception:
                pass
            self.fh = None
            if self.exclusive:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            return False

    def _work_lock(self, work_id):
        # Per-work lock used for read-modify-write of item + lease state.
        self._ensure_dirs()
        return self._FileLock(self._path("locks", work_id))

    def queue_lock(self):
        """One queue-wide interprocess lock around check-capacity-and-claim.

        The per-work lock only serialises writers of the *same* work id, and
        the controller's ``_dispatch_lock`` is per instance. Two server
        processes (or two controllers in one process) handing out different
        work ids could therefore both observe a free slot and both claim it,
        exceeding ``max_harness_jobs``. This lock is shared by every process
        rooted at the same queue directory; it is always acquired before any
        per-work lock, never the other way round.
        """
        self._ensure_dirs()
        return self._FileLock(os.path.join(self.root, "queue.lock"))

    # -- record helpers ----------------------------------------------------
    def _load_item(self, work_id):
        return self._read_json(self._path("items", work_id))

    def _save_item(self, work_id, record):
        record["updated"] = _now_iso(self._now_val())
        self._atomic_write(self._path("items", work_id), record)
        return record

    def _load_lease(self, work_id):
        return self._read_json(self._path("leases", work_id))

    def _save_lease(self, work_id, lease):
        self._atomic_write(self._path("leases", work_id), lease)
        return lease

    def _remove_lease(self, work_id):
        try:
            os.unlink(self._path("leases", work_id))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AdmissionError("cannot remove lease: %s" % exc)

    def _lease_live(self, lease, now=None):
        if not lease:
            return False
        now = self._now_val() if now is None else now
        try:
            lease_until = float(lease.get("lease_until_ts") or 0)
        except Exception:
            return False
        if now > lease_until:
            return False
        return bool(self._pid_alive(lease.get("pid")))

    @staticmethod
    def _is_terminal(record):
        return bool(record) and record.get("status") in _TERMINAL

    # -- dependency helpers ------------------------------------------------
    @staticmethod
    def _identities(record):
        values = set()
        if isinstance(record, dict):
            for key in ("work_id", "run_id"):
                value = record.get(key)
                if value:
                    values.add(str(value))
        return values

    def _find_item_locked(self, ref):
        """Find a queue item by work id or run id (caller holds the lock)."""
        text = str(ref or "").strip()
        if not text or "/" in text or "\\" in text or text in (".", ".."):
            return None
        try:
            direct = self._load_item(text)
        except AdmissionError:
            direct = None
        if direct:
            return direct
        try:
            names = sorted(os.listdir(self.items_dir))
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                record = self._read_json(os.path.join(self.items_dir, name))
            except AdmissionError:
                continue
            if record and text in self._identities(record):
                return record
        return None

    def _probe_dependency(self, run_id):
        probe = self.dependency_probe
        if isinstance(probe, dict):
            data = probe.get(str(run_id))
        elif callable(probe):
            try:
                data = probe(run_id)
            except Exception:
                return None
        else:
            return None
        if isinstance(data, str):
            return {"status": data}
        return data if isinstance(data, dict) else None

    @staticmethod
    def _status_kind(status):
        value = str(status or "").strip().lower()
        if value in _ERROR_STATUSES:
            return "error"
        if value in _SUCCESS_STATUSES:
            return "done"
        if value in _WAITING_STATUSES:
            return "waiting"
        return ""

    def _dependency_state_locked(self, record):
        """Classify ``record['depends_on']`` (caller holds the lock).

        Queue records are authoritative for queued/running ids, but a status
        file is also consulted so a terminal error recorded by the worker is
        visible even if the queue item has not been released yet.  Error wins,
        then a done/needs_input hold, then success, then waiting, then unknown.
        """
        depends_on = record.get("depends_on") if isinstance(record, dict) else None
        if not depends_on:
            return {"state": "ready", "dependency": None, "detail": None}
        dependency = str(depends_on)
        if (not dependency or "/" in dependency or "\\" in dependency
                or dependency in (".", "..")):
            return {"state": "error", "dependency": dependency,
                    "detail": "invalid dependency run id"}
        item = self._find_item_locked(dependency)
        probe = self._probe_dependency(dependency)
        error = None
        detail = None
        done = False
        waiting = False
        held = False
        if item:
            kind = self._status_kind(item.get("status"))
            if kind == "error":
                error = str(item.get("status") or "error")
                detail = item.get("error") or item.get("message")
            elif kind == "done":
                if _progress_blocks_completion(item):
                    waiting = True
                    held = True
                else:
                    done = True
            elif kind == "waiting":
                waiting = True
        probe_seen = False
        if probe:
            probe_status = probe.get("status") or probe.get("state")
            kind = self._status_kind(probe_status)
            if kind:
                probe_seen = True
            if kind == "error":
                error = str(probe_status or "error")
                detail = probe.get("error") or probe.get("message") or detail
            elif kind == "done":
                if _progress_blocks_completion(probe):
                    waiting = True
                    held = True
                else:
                    done = True
            elif kind == "waiting":
                waiting = True
        if error:
            return {"state": "error", "dependency": dependency,
                    "detail": detail or error}
        if held:
            return {"state": "waiting", "dependency": dependency,
                    "detail": None}
        if done:
            return {"state": "ready", "dependency": dependency, "detail": None}
        if waiting or item or probe_seen:
            return {"state": "waiting", "dependency": dependency, "detail": None}
        return {"state": "unknown", "dependency": dependency, "detail": None}

    def dependency_state(self, record_or_work_id):
        """Return ``{state, dependency, detail}`` for a record or work id.

        ``state`` is one of ``ready``, ``waiting``, ``unknown`` or ``error``.
        A record without ``depends_on`` is always ready.
        """
        if isinstance(record_or_work_id, dict):
            record = record_or_work_id
        else:
            work_id = self._safe_id(record_or_work_id)
            with self._lock:
                record = self._load_item(work_id)
        if not record:
            return {"state": "unknown", "dependency": None,
                    "detail": "unknown work id"}
        # A dependent that failed because its dependency vanished/failed has
        # a terminal admission error.  Report that rather than re-classifying
        # the (now possibly stale) dependency as ready.
        if (record.get("status") in _ERROR_STATUSES
                and "depend" in str(record.get("error") or "").lower()):
            return {"state": "error", "dependency": record.get("depends_on"),
                    "detail": record.get("error")}
        with self._lock:
            if not record.get("depends_on"):
                return {"state": "ready", "dependency": None, "detail": None}
            return self._dependency_state_locked(record)

    def _validate_dependency_locked(self, work_id, run_id, depends_on):
        """Reject bad dependencies before mutating any queue state."""
        if depends_on is None:
            return
        dependency = str(depends_on).strip()
        if (not dependency or "/" in dependency or "\\" in dependency
                or dependency in (".", "..")):
            raise DependencyError("Invalid depends_on run id %r." % (depends_on,))
        identities = {str(work_id)}
        if run_id:
            identities.add(str(run_id))
        chain = [dependency]
        seen = set()
        current = dependency
        while current:
            if current in seen:
                raise DependencyError(
                    "Dependency cycle detected: %s." %
                    " -> ".join(chain + [current]))
            seen.add(current)
            if current in identities:
                path = [str(work_id)] + chain
                raise DependencyError(
                    "Dependency cycle detected: %s." % " -> ".join(path))
            item = self._find_item_locked(current)
            if not item or not item.get("depends_on"):
                break
            current = str(item.get("depends_on"))
            chain.append(current)
        state = self._dependency_state_locked({"depends_on": dependency})
        if state["state"] == "error":
            raise DependencyError(
                "Dependency run_id %r finished with error: %s." % (
                    dependency, state.get("detail") or "unknown error"))
        if state["state"] == "unknown":
            raise DependencyError(
                "Unknown dependency run_id %r: no queue record or run "
                "metadata was found." % (dependency,))

    # -- public API --------------------------------------------------------
    def enqueue(self, work_id, priority=PRIORITY_BUILD, run_id=None,
                spec_path=None, tool=None, meta=None, urgent=False,
                depends_on=None, pipeline=None):
        work_id = self._safe_id(work_id)
        urgent = bool(urgent)
        if depends_on is not None:
            depends_on = str(depends_on).strip()
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            existing = self._load_item(work_id)
            if existing and self._is_terminal(existing):
                return existing
            if existing and self._lease_live(self._load_lease(work_id), now):
                return existing
            # Validate before mutating: a rejected dependency must not leave a
            # half-updated item that the dispatcher could later claim.
            self._validate_dependency_locked(work_id, run_id, depends_on)
            if existing:
                record = existing
                record["priority"] = int(priority)
                record["priority_label"] = priority_label(priority, urgent)
                record["urgent"] = urgent
                record["status"] = "queued"
                record["updated"] = _now_iso(now)
                if depends_on:
                    record["depends_on"] = depends_on
                else:
                    record.pop("depends_on", None)
            else:
                record = {
                    "work_id": work_id,
                    "priority": int(priority),
                    "priority_label": priority_label(priority, urgent),
                    "urgent": urgent,
                    "created": _now_iso(now),
                    "created_ts": now,
                    "updated": _now_iso(now),
                    "status": "queued",
                    "fencing_token": 0,
                }
                if depends_on:
                    record["depends_on"] = depends_on
            if run_id is not None:
                record["run_id"] = run_id
            if spec_path is not None:
                record["spec_path"] = spec_path
            if tool is not None:
                record["tool"] = tool
            if meta:
                record["meta"] = dict(meta)
            if isinstance(pipeline, dict):
                # P3.5 pipeline reservation: write-set, eligibility, slot and
                # base-commit evidence are durable with the queue record.
                for key in ("write_set", "write_set_source", "write_set_impl",
                            "pipeline_eligible", "pipeline_extra",
                            "base_commit", "base_ref", "canonical_workspace",
                            "defer_next_start", "head_skip_count",
                            "job_max_cost_usd", "install_status"):
                    if key in pipeline:
                        record[key] = pipeline[key]
            self._save_item(work_id, record)
            self._remove_lease(work_id)
            return record

    def _pending_records(self):
        self._ensure_dirs()
        records = []
        try:
            names = sorted(os.listdir(self.items_dir))
        except OSError as exc:
            raise AdmissionError("cannot list admission items: %s" % exc)
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            rec = self._read_json(os.path.join(self.items_dir, name))
            if not rec:
                continue
            if rec.get("status") == "queued":
                try:
                    rec["_created_ts"] = float(rec.get("created_ts") or 0)
                except Exception:
                    rec["_created_ts"] = 0.0
                records.append(rec)
        # P3.1: a queued job's dependency is expedited ahead of ordinary
        # builds.  Urgent is likewise expedited, still below P0 resume.  The
        # dependency/urgent tier is a tie-breaker after the numeric priority,
        # so old records and P0 semantics are unchanged.
        depended_on = set()
        for rec in records:
            dependency = rec.get("depends_on")
            if dependency:
                depended_on.add(str(dependency))
        def sort_key(rec):
            try:
                priority = int(rec.get("priority", PRIORITY_BUILD))
            except Exception:
                priority = PRIORITY_BUILD
            identities = self._identities(rec)
            expedited = bool(rec.get("urgent")) or bool(identities & depended_on)
            return (priority, 0 if expedited else 1, rec["_created_ts"],
                    str(rec.get("work_id")))
        records.sort(key=sort_key)
        return records

    def pending(self):
        with self._lock:
            self.reclaim()
            out = []
            for rec in self._pending_records():
                clean = {k: v for k, v in rec.items() if not k.startswith("_")}
                out.append(clean)
            return out

    def active(self):
        with self._lock:
            self._ensure_dirs()
            live = []
            try:
                names = sorted(os.listdir(self.leases_dir))
            except OSError as exc:
                raise AdmissionError("cannot list admission leases: %s" % exc)
            for name in names:
                if not name.endswith(".json") or name.startswith("."):
                    continue
                lease = self._read_json(os.path.join(self.leases_dir, name))
                if lease and self._lease_live(lease):
                    item = self._load_item(lease.get("work_id", ""))
                    if item:
                        merged = dict(item)
                        merged.update({k: v for k, v in lease.items()
                                       if k in ("pid", "host", "lease_until",
                                                "lease_until_ts", "heartbeat",
                                                "fencing_token")})
                        merged["status"] = "running"
                        live.append(merged)
            return live

    def active_count(self):
        return len(self.active())

    def position(self, work_id):
        work_id = self._safe_id(work_id)
        for idx, rec in enumerate(self.pending(), 1):
            if rec.get("work_id") == work_id:
                return idx
        return 0

    def _record_for_claim(self, work_id):
        item = self._load_item(work_id)
        if item is None:
            raise AdmissionError("unknown work id: %s" % work_id)
        return item

    def claim(self, work_id, pid=None, extra=False):
        work_id = self._safe_id(work_id)
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            item = self._record_for_claim(work_id)
            if item.get("status") != "queued":
                return None
            # A dependency-waiting record is never claimable, even if a caller
            # reaches claim() outside the dispatcher.  Waiting must not hold a
            # lease/slot, and claiming it could deadlock the dependency.
            if item.get("depends_on"):
                state = self._dependency_state_locked(item)
                if state.get("state") != "ready":
                    return None
            lease = self._load_lease(work_id)
            if self._lease_live(lease, now):
                return None
            token = int(item.get("fencing_token") or 0) + 1
            pid = os.getpid() if pid is None else pid
            lease_until = now + self.lease_seconds
            item.update({
                "status": "running",
                "fencing_token": token,
                "pid": pid,
                "lease_until": _now_iso(lease_until),
                "lease_until_ts": lease_until,
                "heartbeat": _now_iso(now),
            })
            if extra:
                item["pipeline_extra"] = True
            self._save_item(work_id, item)
            lease = {
                "work_id": work_id,
                "pid": pid,
                "host": socket.gethostname(),
                "fencing_token": token,
                "lease_until": _now_iso(lease_until),
                "lease_until_ts": lease_until,
                "heartbeat": _now_iso(now),
            }
            self._save_lease(work_id, lease)
            merged = dict(item)
            merged.update(lease)
            return merged

    def heartbeat(self, work_id, token, pid=None):
        work_id = self._safe_id(work_id)
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            item = self._load_item(work_id)
            lease = self._load_lease(work_id)
            if not item or not lease:
                return False
            if item.get("status") != "running":
                return False
            if self._is_terminal(item):
                return False
            try:
                if int(lease.get("fencing_token") or -1) != int(token):
                    return False
                if int(item.get("fencing_token") or -1) != int(token):
                    return False
            except Exception:
                return False
            # The dispatcher claims with the server pid; the detached worker
            # proves ownership with the fencing token and takes the lease over.
            lease_until = now + self.lease_seconds
            lease.update({"heartbeat": _now_iso(now),
                          "lease_until": _now_iso(lease_until),
                          "lease_until_ts": lease_until})
            if pid is not None:
                lease["pid"] = pid
            self._save_lease(work_id, lease)
            item.update({"heartbeat": _now_iso(now),
                         "lease_until": _now_iso(lease_until),
                         "lease_until_ts": lease_until})
            self._save_item(work_id, item)
            return True

    def release(self, work_id, status="done", token=None):
        work_id = self._safe_id(work_id)
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            item = self._load_item(work_id)
            if item is None:
                raise AdmissionError("unknown work id: %s" % work_id)
            if token is not None:
                try:
                    if int(item.get("fencing_token") or -1) != int(token):
                        return item
                except Exception:
                    return item
            item["status"] = str(status or "done")
            item["updated"] = _now_iso(now)
            item["released"] = _now_iso(now)
            self._save_item(work_id, item)
            self._remove_lease(work_id)
            return item

    def fail(self, work_id, message, status="failed"):
        """Mark a queue record failed with a caller-visible error message."""
        work_id = self._safe_id(work_id)
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            item = self._load_item(work_id)
            if item is None:
                raise AdmissionError("unknown work id: %s" % work_id)
            item["status"] = str(status or "failed")
            item["error"] = str(message)
            item["updated"] = _now_iso(now)
            item["released"] = _now_iso(now)
            self._save_item(work_id, item)
            self._remove_lease(work_id)
            return item

    def record_event(self, event_type, payload=None):
        """Append a cheap queue-level event under ``<root>/events.jsonl``."""
        self._ensure_dirs()
        now = self._now_val()
        data = dict(payload or {})
        entry = {"at": _now_iso(now), "ts": now, "type": str(event_type),
                 "payload": data}
        # Flattening is additive: operators can read the common fields either
        # at the top level or under payload.  Reserved keys never overwrite
        # the envelope.
        for key, value in data.items():
            entry.setdefault(key, value)
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        path = os.path.join(self.root, "events.jsonl")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            raise AdmissionError("cannot record admission event: %s" % exc)
        return entry

    def _should_reclaim(self, lease, now):
        try:
            lease_until = float(lease.get("lease_until_ts") or 0)
        except Exception:
            lease_until = 0.0
        if now > lease_until:
            return True
        return not self._pid_alive(lease.get("pid"))

    def reclaim(self):
        with self._lock:
            self._ensure_dirs()
            reclaimed = []
            try:
                names = sorted(os.listdir(self.leases_dir))
            except OSError as exc:
                raise AdmissionError("cannot list admission leases: %s" % exc)
            for name in names:
                if not name.endswith(".json") or name.startswith("."):
                    continue
                work_id = name[:-5]
                lease = self._read_json(os.path.join(self.leases_dir, name))
                if not lease:
                    continue
                now = self._now_val()
                if not self._should_reclaim(lease, now):
                    continue
                with self._work_lock(work_id):
                    # Reclaim is a read-modify-write too: the lease read above
                    # is only an optimisation.  A worker heartbeat (or a
                    # competing reclaimer) may have renewed or replaced the
                    # lease after the stale _should_reclaim() check but before
                    # this lock.  Reload and revalidate identity, fencing
                    # token, expiry and PID under the lock; queueing a live
                    # holder would fence it and start a duplicate worker.
                    current = self._load_lease(work_id)
                    if not current:
                        # The holder released between the two reads; the
                        # orphan scan below handles a still-running item.
                        continue
                    try:
                        same_lease = (
                            current.get("work_id") == work_id
                            and int(current.get("fencing_token") or 0)
                            == int(lease.get("fencing_token") or 0)
                            and current.get("pid") == lease.get("pid"))
                    except Exception:
                        same_lease = False
                    if not same_lease:
                        continue
                    now = self._now_val()
                    if not self._should_reclaim(current, now):
                        continue
                    item = self._load_item(work_id)
                    if item is None:
                        self._remove_lease(work_id)
                        continue
                    if item.get("status") != "running":
                        self._remove_lease(work_id)
                        continue
                    try:
                        if (int(item.get("fencing_token") or 0)
                                != int(current.get("fencing_token") or 0)):
                            continue
                    except Exception:
                        continue
                    token = int(item.get("fencing_token") or 0) + 1
                    item.update({
                        "status": "queued",
                        "fencing_token": token,
                        "reclaimed": _now_iso(now),
                    })
                    self._save_item(work_id, item)
                    self._remove_lease(work_id)
                    reclaimed.append(item)
            # A crash between writing the running item and its lease leaves an
            # orphan. It is reclaimable: no live lease means no holder.
            try:
                names = sorted(os.listdir(self.items_dir))
            except OSError:
                names = []
            for name in names:
                if not name.endswith(".json") or name.startswith("."):
                    continue
                work_id = name[:-5]
                if os.path.exists(self._path("leases", work_id)):
                    continue
                with self._work_lock(work_id):
                    item = self._load_item(work_id)
                    if not item or item.get("status") != "running":
                        continue
                    if os.path.exists(self._path("leases", work_id)):
                        continue
                    token = int(item.get("fencing_token") or 0) + 1
                    item.update({
                        "status": "queued",
                        "fencing_token": token,
                        "reclaimed": _now_iso(self._now_val()),
                    })
                    self._save_item(work_id, item)
                    reclaimed.append(item)
            return reclaimed

    def status_fields(self, work_id):
        work_id = self._safe_id(work_id)
        item = self._load_item(work_id)
        if item is None:
            return {}
        lease = self._load_lease(work_id)
        position = self.position(work_id) if item.get("status") == "queued" else 0
        holder = None
        if item.get("status") == "running" and lease and self._lease_live(lease):
            holder = lease.get("pid")
        depends_on = item.get("depends_on")
        dependency = (self.dependency_state(item) if depends_on
                      else {"state": "ready", "dependency": None, "detail": None})
        waiting_on = (depends_on if dependency.get("state") in
                      ("waiting", "unknown") else None)
        dependency_error = (dependency.get("detail")
                            if dependency.get("state") == "error" else None)
        return {
            "queue_position": position,
            "priority": item.get("priority"),
            "priority_label": priority_label(item.get("priority"),
                                             bool(item.get("urgent"))),
            "urgent": bool(item.get("urgent")),
            "depends_on": depends_on,
            "waiting_on": waiting_on,
            "dependency_status": dependency.get("state"),
            "dependency_error": dependency_error,
            "holder": holder,
            "lease_until": lease.get("lease_until") if holder else None,
            "fencing_token": item.get("fencing_token"),
            "status": item.get("status"),
            "error": item.get("error"),
            "write_set": item.get("write_set") or [],
            "write_set_source": item.get("write_set_source"),
            "pipeline_eligible": bool(item.get("pipeline_eligible")),
            "pipeline_extra": bool(item.get("pipeline_extra")),
            "base_commit": item.get("base_commit"),
            "install_status": item.get("install_status") or "n/a",
        }


def _cfg_value(cfg, name, default=None):
    try:
        return cfg.get(name, default)
    except AttributeError:
        return getattr(cfg, name, default)


def _status_probe(cfg):
    """Cheap file-only dependency probe over jobs and saved run records."""
    jobs_dir = _cfg_value(cfg, "jobs_dir")
    log_dir = _cfg_value(cfg, "log_dir")
    if not jobs_dir and not log_dir:
        return None
    jobs_base = os.path.abspath(str(jobs_dir)) if jobs_dir else None
    runs_base = os.path.abspath(str(log_dir)) if log_dir else None

    def probe(run_id):
        text = str(run_id or "").strip()
        if (not text or "/" in text or "\\" in text
                or text in (".", "..")):
            return None
        if jobs_base:
            path = os.path.join(jobs_base, text + ".status.json")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                return {"status": data.get("status"),
                        "progress": data.get("progress"),
                        "error": data.get("error")}
        # A finished run may have outlived its job status file (retention) but
        # still have a saved run record.  Read it file-only; never guess.
        if runs_base:
            run_path = os.path.join(runs_base, text + ".json")
            try:
                with open(run_path, "r", encoding="utf-8") as handle:
                    run = json.load(handle)
            except (OSError, ValueError):
                return None
            if isinstance(run, dict):
                return {"status": run.get("status"),
                        "error": run.get("error") or run.get("detail")}
        return None
    return probe


def _idle_probe(cfg):
    """Cheap file-only 'slot idle/waiting' probe over job status files."""
    jobs_dir = _cfg_value(cfg, "jobs_dir")
    if not jobs_dir:
        return None
    base = os.path.abspath(str(jobs_dir))

    def probe(record):
        run_id = record.get("run_id") or record.get("work_id")
        text = str(run_id or "").strip()
        if (not text or "/" in text or "\\" in text
                or text in (".", "..")):
            return False
        path = os.path.join(base, text + ".status.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        status = str(data.get("status") or "").lower()
        progress = str(data.get("progress") or "").lower()
        if status in ("queued", "waiting", "idle", "needs_input", "done",
                      "error", "failed", "cancelled"):
            return True
        words = ("waiting", "idle", "needs_input")
        return any(word in progress for word in words)
    return probe


class AdmissionController:
    def __init__(self, cfg, store=None, spawn=None, detected_limit=None,
                 dependency_probe=None, idle_probe=None, event_sink=None,
                 fail_sink=None, review_probe=None):
        self.cfg = cfg
        # P3.5: injected by jobs.py as a file-only read of the durable run
        # phase plus the live admission lease. It is never imported here.
        self.review_probe = review_probe
        self.capacity = resolved_capacity(cfg, detected_limit=detected_limit)
        if store is None:
            root = getattr(cfg, "state_dir", None) or getattr(cfg, "work_dir", None)
            if not root:
                raise AdmissionError("config has no state_dir for admission store")
            root = os.path.join(str(root), "queue")
            store = AdmissionStore(
                root,
                heartbeat_seconds=getattr(cfg, "admission_heartbeat_seconds", 30.0),
                lease_seconds=getattr(cfg, "admission_lease_seconds", 90.0),
            )
        self.store = store
        if dependency_probe is not None:
            self.store.dependency_probe = dependency_probe
        elif self.store.dependency_probe is None:
            self.store.dependency_probe = _status_probe(cfg)
        if idle_probe is None:
            idle_probe = _idle_probe(cfg)
        self.idle_probe = idle_probe
        self.event_sink = event_sink
        self.fail_sink = fail_sink
        # P3.1 safety net: a fixed 2x effective-cap ceiling.  It is the only
        # path that may exceed ``capacity`` and is checked under queue.lock.
        self.hard_ceiling = (2 * self.capacity
                             if self.capacity is not None else None)
        self._last_safety_net = 0.0
        self._spawn = spawn
        self._stop = threading.Event()
        self._thread = None
        self._thread_lock = threading.Lock()
        self._dispatch_lock = threading.RLock()

    def _reserve_enabled(self):
        return bool(getattr(self.cfg, "reserve_resume_slot", True))

    def _p0_pending(self):
        return any(int(r.get("priority", PRIORITY_BUILD)) == PRIORITY_RESUME
                   for r in self.store.pending())

    @staticmethod
    def _is_extra(record):
        return bool(isinstance(record, dict) and record.get("pipeline_extra"))

    def _base_active(self):
        try:
            return [rec for rec in self.store.active()
                    if not self._is_extra(rec)]
        except Exception:
            return []

    def _slots(self):
        """Base slots only; pipeline extras never consume a base slot."""
        if self.capacity is None:
            return None
        return self.capacity - len(self._base_active())

    def _reviewing(self, record):
        probe = self.review_probe
        if probe is None:
            return False
        try:
            return bool(probe(record))
        except Exception:
            return False

    @staticmethod
    def _write_sets_overlap(first, second):
        first = [str(item) for item in (first or []) if str(item).strip()]
        second = [str(item) for item in (second or []) if str(item).strip()]
        if not first or not second:
            # Unknown is the universe: it overlaps everything.
            return True
        try:
            from . import pipeline as pipeline_mod
            return bool(pipeline_mod.overlaps(first, second))
        except Exception:
            pass
        for left in first:
            for right in second:
                left_n = left.replace("\\", "/").strip("/")
                right_n = right.replace("\\", "/").strip("/")
                if not left_n or not right_n:
                    return True
                if left_n == right_n:
                    return True
                if left_n.startswith(right_n + "/") or right_n.startswith(
                        left_n + "/"):
                    return True
        return False

    def _patch_backlog(self, record=None):
        """Count *pending* patches, not installed/retained history."""
        try:
            canonical = (record or {}).get("canonical_workspace") if isinstance(
                record, dict) else None
            if canonical:
                try:
                    from . import pipeline as pipeline_mod
                    pending = pipeline_mod._pending_records(self.cfg, canonical)
                    return len(pending)
                except Exception:
                    # Fall through to the conservative directory count.
                    pass
            state_dir = _cfg_value(self.cfg, "state_dir", "")
            patch_dir = _cfg_value(self.cfg, "pipeline_patch_dir", "") or os.path.join(
                str(state_dir), "patches")
            patch_dir = os.path.expanduser(str(patch_dir))
            if not os.path.isdir(patch_dir):
                return 0
            count = 0
            for name in os.listdir(patch_dir):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(patch_dir, name), "r",
                              encoding="utf-8") as handle:
                        data = json.load(handle)
                except Exception:
                    continue
                if str(data.get("status") or "pending") in (
                        "pending", "accepted", "manual", "conflict"):
                    count += 1
            # A directory with only raw .patch files (older/partial state) is
            # counted conservatively so extras cannot over-admit.
            if count == 0:
                count = len([name for name in os.listdir(patch_dir)
                             if name.endswith(".patch")])
            return count
        except Exception:
            # An unreadable backlog fails closed for an extra start.
            return 10 ** 6

    def _cost_headroom_ok(self, record):
        """Fail closed when an extra's cost cannot be proven affordable."""
        try:
            daily = _cfg_value(self.cfg, "daily_limit_usd", None)
            job_max = record.get("job_max_cost_usd")
            if job_max is None:
                job_max = _cfg_value(self.cfg, "job_max_cost_usd", None)
            if job_max is not None:
                job_max = float(job_max)
            if daily is None:
                return job_max is not None and job_max > 0
            try:
                from .budget import CostLedger
                ledger = CostLedger(_cfg_value(self.cfg, "state_dir"))
                spent = float(ledger.daily_total(0.0))
            except Exception:
                return False
            floor = float(_cfg_value(self.cfg, "pipeline_min_remaining_usd", 1.0)
                          or 0.0)
            required = max(floor, job_max or 0.0)
            return float(daily) - spent >= required
        except Exception:
            return False

    def _active_run_defers(self, record):
        """True when an active job's run record says defer further starts."""
        run_id = record.get("run_id") if isinstance(record, dict) else None
        if not run_id:
            return False
        try:
            from .store import find_run
            run = find_run(str(run_id), self.cfg)[0]
        except Exception:
            return False
        if not isinstance(run, dict):
            return False
        pipe = run.get("pipeline") if isinstance(run.get("pipeline"), dict) else {}
        return bool(pipe.get("defer_next_start"))

    def _pipeline_extra_allowed(self, record):
        """The exact §F4.4 extra-start test; all conditions must hold."""
        if self.capacity is None or self.hard_ceiling is None:
            return False
        try:
            if not bool(_cfg_value(self.cfg, "pipeline_enabled", True)):
                return False
        except Exception:
            return False
        if not record.get("pipeline_eligible"):
            return False
        write_set = [str(item) for item in (record.get("write_set") or [])
                     if str(item).strip()]
        if not write_set:
            return False
        if self._slots() is None or self._slots() > 0:
            return False
        try:
            active = self.store.active()
        except Exception:
            return False
        if len(active) >= self.hard_ceiling:
            return False
        try:
            max_extra = max(0, int(_cfg_value(
                self.cfg, "pipeline_max_extra_jobs", 1) or 0))
        except (TypeError, ValueError):
            max_extra = 1
        if len([rec for rec in active if self._is_extra(rec)]) >= max_extra:
            return False
        if not any((not self._is_extra(rec)) and self._reviewing(rec)
                   for rec in active):
            return False
        if self._p0_pending():
            return False
        if bool(_cfg_value(self.cfg, "pipeline_require_disjoint", True)):
            for other in active:
                if self._write_sets_overlap(write_set,
                                            other.get("write_set") or []):
                    return False
        if any(rec.get("defer_next_start") for rec in active):
            return False
        # PLAN widening happens after admission and is recorded on the run, so
        # read the durable record as well as the queue field.
        if any(self._active_run_defers(rec) for rec in active):
            return False
        try:
            backlog_cap = max(0, int(_cfg_value(
                self.cfg, "pipeline_max_pending_patches", 8) or 0))
        except (TypeError, ValueError):
            backlog_cap = 8
        if self._patch_backlog(record) >= backlog_cap:
            return False
        if not self._cost_headroom_ok(record):
            return False
        if record.get("depends_on") and not self._dependency_ready(record):
            return False
        return True

    @staticmethod
    def _effective_rank(record, pending):
        """Queue-order rank: numeric priority then urgent/dependency boost."""
        try:
            priority = int(record.get("priority", PRIORITY_BUILD))
        except Exception:
            priority = PRIORITY_BUILD
        depended_on = set()
        for other in pending:
            dependency = other.get("depends_on")
            if dependency:
                depended_on.add(str(dependency))
        identities = set()
        for key in ("work_id", "run_id"):
            value = record.get(key)
            if value:
                identities.add(str(value))
        expedited = bool(record.get("urgent")) or bool(identities & depended_on)
        return (priority, 0 if expedited else 1)

    @staticmethod
    def _queue_order_key(record, pending):
        """Full queue order: effective rank, then creation time and work id."""
        try:
            created = float(record.get("created_ts") or 0)
        except Exception:
            created = 0.0
        return (AdmissionController._effective_rank(record, pending), created,
                str(record.get("work_id") or ""))

    def _dependency_install_ready(self, record):
        """A dependency is ready only after its pipeline patch is installed."""
        dependency = str(record.get("depends_on") or "")
        canonical = record.get("canonical_workspace")
        if not dependency or not canonical:
            return True
        try:
            from . import pipeline as pipeline_mod
            records = pipeline_mod._patch_records(self.cfg, canonical)
        except Exception:
            return True
        for item in records:
            if str(item.get("run_id") or "") != dependency:
                continue
            return str(item.get("status") or "pending") == "installed"
        return True

    def _dependency_ready(self, record):
        if not record.get("depends_on"):
            return True
        try:
            if self.store.dependency_state(record).get("state") != "ready":
                return False
        except Exception:
            return False
        return self._dependency_install_ready(record)

    def _reject_bad_dependency_locked(self, record):
        """Fail a queue item whose dependency became unknown/failed."""
        if not record.get("depends_on"):
            return False
        try:
            state = self.store.dependency_state(record)
        except Exception:
            return False
        kind = state.get("state")
        if kind not in ("unknown", "error"):
            return False
        dependency = state.get("dependency") or record.get("depends_on")
        if kind == "unknown":
            message = ("Dependency run_id %r is unknown: no queue record or "
                       "run metadata was found." % (dependency,))
        else:
            message = ("Dependency run_id %r finished with error: %s" %
                       (dependency, state.get("detail") or "unknown error"))
        work_id = record.get("work_id")
        try:
            self.store.fail(work_id, message)
        except Exception:
            return False
        payload = {"work_id": work_id, "run_id": record.get("run_id"),
                   "depends_on": dependency, "error": message,
                   "dependency_status": kind}
        try:
            self.store.record_event("admission_dependency_failed", payload)
        except Exception:
            pass
        if self.event_sink is not None:
            try:
                self.event_sink("admission_dependency_failed", payload)
            except Exception:
                pass
        if self.fail_sink is not None:
            try:
                self.fail_sink(record, message)
            except Exception:
                pass
        return True

    def _parallel_decision(self, record, active):
        try:
            from . import writesets as writesets_mod
            return writesets_mod.parallel_decision(record, active, self.cfg)
        except Exception:
            return {"parallel": False, "reason": "decision_error"}

    def _note_parallel(self, record, decision):
        if not isinstance(record, dict):
            return
        record["parallel"] = bool(decision.get("parallel"))
        record["parallel_reason"] = decision.get("reason")
        record["parallel_peers"] = list(decision.get("peers") or [])
        record["parallel_write_set"] = list(decision.get("write_set") or [])
        record["parallel_confidence"] = decision.get("confidence")
        work_id = record.get("work_id")
        if not work_id:
            return
        try:
            self.store._save_item(str(work_id), record)
        except Exception:
            pass

    def _may_start(self, record):
        if not self._dependency_ready(record):
            return False
        if self.capacity is None:
            return True
        # P3.5 invariant: never overlap a canonical (non-eligible) builder with
        # a pipeline-eligible job that may need to install a patch, in either
        # direction. Install's dirty-tree check is only the second defence.
        try:
            active = self.store.active()
        except Exception:
            return False
        eligible_active = [rec for rec in active
                           if rec.get("pipeline_eligible")]
        if record.get("pipeline_eligible"):
            if any(not rec.get("pipeline_eligible") for rec in active):
                return False
            # v3.14.2: an eligible job may run beside another eligible job
            # only when both write sets are inferred/declared, confident and
            # disjoint. Unknown, wide or overlapping sets wait for a slot.
            decision = self._parallel_decision(record, eligible_active)
            self._note_parallel(record, decision)
            if not decision.get("parallel"):
                return False
        else:
            if eligible_active or self._patch_backlog(record) > 0:
                return False
        # An extra job may start only during a review, beyond capacity, and
        # with a disjoint write-set. This runs before the normal slot test.
        if self._pipeline_extra_allowed(record):
            return True
        free = self._slots()
        if free is None or free <= 0:
            return False
        priority = int(record.get("priority", PRIORITY_BUILD))
        if priority == PRIORITY_RESUME:
            return True
        if not self._reserve_enabled():
            return True
        if free > 1:
            return True
        # One free slot: keep it for a pending resume unless we are the resume.
        return not self._p0_pending()

    def submit(self, work_id, priority=PRIORITY_BUILD, run_id=None,
               spec_path=None, tool=None, spawn=None, urgent=False,
               depends_on=None, pipeline=None):
        spawn_fn = spawn or self._spawn
        # The capacity check and the claim must be one critical section across
        # every controller sharing this queue root, not just this instance.
        with self.store.queue_lock(), self._dispatch_lock:
            record = self.store.enqueue(
                work_id, priority=priority, run_id=run_id,
                spec_path=spec_path, tool=tool, urgent=urgent,
                depends_on=depends_on, pipeline=pipeline)
            fields = self.store.status_fields(work_id)
            if record.get("status") == "queued":
                self._reject_bad_dependency_locked(record)
                record = self.store._load_item(work_id) or record
                fields = self.store.status_fields(work_id)
                if record.get("status") in ("failed", "error"):
                    raise DependencyError(
                        record.get("error") or "dependency is unavailable")
            can_claim = (record.get("status") == "queued"
                         and self._may_start(record))
            if can_claim:
                # Never bypass a queued record with a better effective order
                # just because this submit observed the free slot before the
                # dispatcher did.  Without this guard a new normal job could
                # jump a queued urgent job or a queued dependency (reviewer
                # regression: submit must not self-claim past the queue head).
                pending = self.store.pending()
                ahead = []
                for pending_record in pending:
                    if str(pending_record.get("work_id")) == str(work_id):
                        break
                    ahead.append(pending_record)
                # A record already marked ``reclaimed`` lost its worker
                # (dead pid/expired lease) and is waiting for the dispatcher,
                # not a live queued holder.  It must not block a fresh submit
                # forever; a genuinely queued equal-rank record still does.
                startable = [item for item in ahead
                             if self._may_start(item)
                             and not item.get("reclaimed")]
                if startable:
                    own_key = self._queue_order_key(record, pending)
                    head_key = min(self._queue_order_key(item, pending)
                                   for item in startable)
                    # Preserve the documented reserve_resume_slot=false
                    # compatibility: only a non-urgent ordinary build may
                    # take the last slot from a waiting P0 when the operator
                    # disabled the reservation.  Urgent and dependency jobs
                    # never use this bypass, so an urgent submission can
                    # never start ahead of a queued P0.
                    legacy_no_reserve = (
                        not self._reserve_enabled()
                        and int(record.get("priority", PRIORITY_BUILD))
                        == PRIORITY_BUILD
                        and not record.get("urgent")
                        and not record.get("depends_on")
                        and all(int(item.get("priority", PRIORITY_BUILD))
                                == PRIORITY_RESUME for item in startable))
                    # Only the full queue head may self-claim.  Comparing the
                    # complete order key (rank + created_ts + work_id) keeps
                    # FIFO for equal effective rank, so a new urgent job
                    # cannot jump an older urgent/dependency record.
                    if head_key <= own_key and not legacy_no_reserve:
                        can_claim = False
            if can_claim:
                is_extra = self._pipeline_extra_allowed(record)
                if is_extra and str(_cfg_value(
                        self.cfg, "pipeline_queue_scan", "head")).lower() == "head":
                    # Head scan: a disjoint candidate may not jump the FIFO
                    # head just because submit observed the review window.
                    queue = self.store.pending()
                    if queue and str(queue[0].get("work_id")) != str(work_id):
                        can_claim = False
                if can_claim:
                    claimed = self.store.claim(work_id, extra=is_extra)
                else:
                    claimed = None
                if claimed is not None:
                    try:
                        if spawn_fn is not None:
                            spawn_fn(claimed)
                    except Exception:
                        self.store.release(work_id, status="failed")
                        raise
                    return {"state": "started", "position": 0,
                            "priority": claimed.get("priority"),
                            "urgent": bool(claimed.get("urgent")),
                            "depends_on": claimed.get("depends_on"),
                            "waiting_on": None,
                            "priority_label": priority_label(
                                claimed.get("priority"),
                                bool(claimed.get("urgent")))}
            return {"state": "queued",
                    "position": self.store.position(work_id) or fields.get("queue_position") or 0,
                    "priority": record.get("priority"),
                    "urgent": bool(record.get("urgent")),
                    "depends_on": record.get("depends_on"),
                    "waiting_on": fields.get("waiting_on"),
                    "dependency_status": fields.get("dependency_status"),
                    "priority_label": priority_label(
                        record.get("priority"), bool(record.get("urgent")))}

    def run_once(self, spawn=None):
        spawn_fn = spawn or self._spawn
        started = []
        with self.store.queue_lock(), self._dispatch_lock:
            return self._run_once_locked(spawn_fn, started)

    def _run_once_locked(self, spawn_fn, started):
        while True:
            pending = self.store.pending()
            if not pending:
                break
            # P3.5 head scan: when no base slot is free, only the FIFO head is
            # eligible for the review-window extra. first_fit may look past a
            # waited head; normal same-slot admission is unchanged.
            try:
                scan = str(_cfg_value(
                    self.cfg, "pipeline_queue_scan", "head")).lower()
                free = self._slots()
            except Exception:
                scan, free = "head", 0
            if scan == "head" and free is not None and free <= 0:
                pending = pending[:1]
            progressed = False
            for rec in pending:
                if self._reject_bad_dependency_locked(rec):
                    progressed = True
                    break
                if not self._may_start(rec):
                    continue
                work_id = rec.get("work_id")
                claimed = self.store.claim(
                    work_id, extra=self._pipeline_extra_allowed(rec))
                if claimed is None:
                    continue
                started.append(claimed)
                progressed = True
                try:
                    if spawn_fn is not None:
                        spawn_fn(claimed)
                except Exception:
                    self.store.release(work_id, status="failed",
                                       token=claimed.get("fencing_token"))
                break  # recompute priorities/slots after each start
            if not progressed:
                break
        return started

    def _emit_safety_event(self, payload):
        try:
            self.store.record_event("admission_safety_net", payload)
        except Exception:
            pass
        if self.event_sink is not None:
            try:
                self.event_sink("admission_safety_net", payload)
            except Exception:
                pass

    def safety_net(self, spawn=None):
        """Admit one extra job when every occupied slot is idle/waiting.

        Cheap file-only check (``idle_probe``), guarded by the same
        cross-process queue lock as normal admission.  It may exceed
        ``capacity`` by at most the fixed ``2 * capacity`` ceiling and never
        claims a dependency-waiting record.  Returns the claimed record, or
        None.  Queue events are recorded for every admission.
        """
        spawn_fn = spawn or self._spawn
        if spawn_fn is None:
            # Do not mark a record running when there is no worker to start.
            return None
        with self.store.queue_lock(), self._dispatch_lock:
            if self.capacity is None or self.hard_ceiling is None:
                return None
            pending = self.store.pending()
            if not pending:
                return None
            active = self.store.active()
            if len(active) < self.capacity:
                # A normal slot is free; the regular dispatcher owns this.
                return None
            if len(active) >= self.hard_ceiling:
                return None
            idle = []
            for rec in active:
                try:
                    is_idle = bool(self.idle_probe(rec)) if self.idle_probe else False
                except Exception:
                    is_idle = False
                if not is_idle:
                    return None
                idle.append(str(rec.get("work_id")))
            candidate = None
            for rec in pending:
                if self._reject_bad_dependency_locked(rec):
                    continue
                if not self._dependency_ready(rec):
                    continue
                candidate = rec
                break
            if candidate is None:
                return None
            work_id = candidate.get("work_id")
            claimed = self.store.claim(work_id)
            if claimed is None:
                return None
            payload = {
                "work_id": work_id,
                "run_id": claimed.get("run_id"),
                "active_before": len(active),
                "active_count": len(active) + 1,
                "active_after": len(active) + 1,
                "capacity": self.capacity,
                "ceiling": self.hard_ceiling,
                "idle_work_ids": idle,
                "idle": list(idle),
                "depends_on": claimed.get("depends_on"),
                "waiting_on": None,
            }
            try:
                if spawn_fn is not None:
                    spawn_fn(claimed)
            except Exception:
                self.store.release(work_id, status="failed",
                                   token=claimed.get("fencing_token"))
                payload.update({"error": "spawn failed", "admitted": False})
                self._emit_safety_event(payload)
                return None
            payload["admitted"] = True
            self._emit_safety_event(payload)
            return claimed

    def maybe_safety_net(self, spawn=None, force=False):
        """Throttled safety net used by the dispatcher loop."""
        now = time.monotonic()
        interval = max(0.05, float(self.store.heartbeat_seconds) / 10.0)
        if not force and now - self._last_safety_net < interval:
            return None
        self._last_safety_net = now
        return self.safety_net(spawn=spawn)

    def start(self, spawn=None):
        if spawn is not None:
            self._spawn = spawn
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            thread = threading.Thread(target=self._loop, name="admission-dispatch",
                                      daemon=True)
            self._thread = thread
            thread.start()
            return thread

    ensure_started = start

    def _loop(self):
        while not self._stop.is_set():
            try:
                started = self.run_once()
            except Exception:
                started = []
            # P3.1 safety net: if all occupied slots are idle/waiting and work
            # is queued, make progress without a model call.  It is throttled
            # and hard-ceiling bounded, so a stale idle signal cannot run away.
            if not started:
                try:
                    self.maybe_safety_net()
                except Exception:
                    pass
            try:
                pending = self.store.pending()
            except Exception:
                pending = []
            if not started and not pending:
                break
            self._stop.wait(max(0.05, float(self.store.heartbeat_seconds)) / 10.0)

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
