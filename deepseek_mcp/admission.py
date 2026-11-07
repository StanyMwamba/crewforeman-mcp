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

try:
    from . import concurrency
except Exception:  # pragma: no cover - defensive
    concurrency = None

PRIORITY_RESUME = 0
PRIORITY_BUILD = 1

_PRIORITY_LABELS = {PRIORITY_RESUME: "P0 resume", PRIORITY_BUILD: "P1 new build"}
_TERMINAL = ("done", "failed", "cancelled", "error")


class AdmissionError(RuntimeError):
    """Raised when admission state cannot be safely acquired or read."""


def priority_label(priority):
    try:
        return _PRIORITY_LABELS.get(int(priority), "P1 new build")
    except Exception:
        return "P1 new build"


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


class AdmissionStore:
    def __init__(self, root, heartbeat_seconds=30.0, lease_seconds=90.0,
                 now=None, pid_alive=None):
        self.root = os.path.abspath(str(root))
        self.items_dir = os.path.join(self.root, "items")
        self.leases_dir = os.path.join(self.root, "leases")
        self.locks_dir = os.path.join(self.root, "locks")
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.lease_seconds = float(lease_seconds)
        self._now = now or time.time
        self._pid_alive = pid_alive or _default_pid_alive
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

    # -- public API --------------------------------------------------------
    def enqueue(self, work_id, priority=PRIORITY_BUILD, run_id=None,
                spec_path=None, tool=None, meta=None):
        work_id = self._safe_id(work_id)
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            existing = self._load_item(work_id)
            if existing and self._is_terminal(existing):
                return existing
            if existing and self._lease_live(self._load_lease(work_id), now):
                return existing
            if existing:
                record = existing
                record["priority"] = int(priority)
                record["priority_label"] = priority_label(priority)
                record["status"] = "queued"
                record["updated"] = _now_iso(now)
            else:
                record = {
                    "work_id": work_id,
                    "priority": int(priority),
                    "priority_label": priority_label(priority),
                    "created": _now_iso(now),
                    "created_ts": now,
                    "updated": _now_iso(now),
                    "status": "queued",
                    "fencing_token": 0,
                }
            if run_id is not None:
                record["run_id"] = run_id
            if spec_path is not None:
                record["spec_path"] = spec_path
            if tool is not None:
                record["tool"] = tool
            if meta:
                record["meta"] = dict(meta)
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
        records.sort(key=lambda r: (int(r.get("priority", PRIORITY_BUILD)),
                                    r["_created_ts"],
                                    str(r.get("work_id"))))
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

    def claim(self, work_id, pid=None):
        work_id = self._safe_id(work_id)
        with self._lock, self._work_lock(work_id):
            now = self._now_val()
            item = self._record_for_claim(work_id)
            if item.get("status") != "queued":
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
        return {
            "queue_position": position,
            "priority": item.get("priority"),
            "priority_label": priority_label(item.get("priority")),
            "holder": holder,
            "lease_until": lease.get("lease_until") if holder else None,
            "fencing_token": item.get("fencing_token"),
            "status": item.get("status"),
        }


class AdmissionController:
    def __init__(self, cfg, store=None, spawn=None, detected_limit=None):
        self.cfg = cfg
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

    def _slots(self):
        if self.capacity is None:
            return None
        return self.capacity - self.store.active_count()

    def _may_start(self, record):
        if self.capacity is None:
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
               spec_path=None, tool=None, spawn=None):
        spawn_fn = spawn or self._spawn
        # The capacity check and the claim must be one critical section across
        # every controller sharing this queue root, not just this instance.
        with self.store.queue_lock(), self._dispatch_lock:
            record = self.store.enqueue(work_id, priority=priority, run_id=run_id,
                                        spec_path=spec_path, tool=tool)
            fields = self.store.status_fields(work_id)
            if record.get("status") == "queued" and self._may_start(record):
                claimed = self.store.claim(work_id)
                if claimed is not None:
                    try:
                        if spawn_fn is not None:
                            spawn_fn(claimed)
                    except Exception:
                        self.store.release(work_id, status="failed")
                        raise
                    return {"state": "started", "position": 0,
                            "priority": claimed.get("priority"),
                            "priority_label": priority_label(claimed.get("priority"))}
            return {"state": "queued",
                    "position": self.store.position(work_id) or fields.get("queue_position") or 0,
                    "priority": record.get("priority"),
                    "priority_label": priority_label(record.get("priority"))}

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
            progressed = False
            for rec in pending:
                if not self._may_start(rec):
                    continue
                work_id = rec.get("work_id")
                claimed = self.store.claim(work_id)
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
