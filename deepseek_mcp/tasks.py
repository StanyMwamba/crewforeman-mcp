"""Durable MCP task store and progress emitter (v3.5.0 candidate, stdlib only).

One JSON document per task under ``root``.  Writes are atomic
(``tempfile.mkstemp`` in the target directory followed by ``os.replace``), so a
crash never leaves a half-written task behind.  Tasks expire ``ttl`` seconds
after their last update; expired files are pruned on read/list.

Public surface::

    STATUSES
    class TaskStore(root)          create/get/result/list/update/progress/
                                   complete/fail/cancel/is_cancelled/to_mcp
    class ProgressEmitter(send, token)

The store never persists full tool arguments: only the tool name and a short,
redacted summary are kept.  Results and errors are also redacted for common
token shapes (``sk-...`` and ``Bearer ...``) before being written.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

STATUSES = ("working", "input_required", "completed", "failed", "cancelled")

DEFAULT_TTL = 3600
DEFAULT_POLL_INTERVAL = 1000

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|credential|bearer)",
    re.IGNORECASE,
)
_REDACTIONS = (
    (re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{8,}"), "***REDACTED***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer ***REDACTED***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)"
            r"\b\s*[=:]\s*[\"']?[^\s\"',;}]+"
        ),
        r"\1=***REDACTED***",
    ),
)


def redact(text, limit=500):
    """Return *text* as a redacted, length-capped string."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[: max(0, limit - 3)] + "..."
    return text


def _coerce_now(value):
    """Accept datetime, epoch seconds or ISO string; return an aware datetime."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if isinstance(value, str):
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise TypeError("now must be a datetime, epoch number or ISO string")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _json_safe(value, what):
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be JSON-serialisable" % what) from None
    return value


class TaskStore:
    """Filesystem-backed task store.  ``root`` is created lazily."""

    def __init__(self, root, clock=None):
        self.root = os.path.abspath(os.fspath(root))
        self._clock = clock
        self._lock = threading.RLock()

    # -- paths / clock ---------------------------------------------------
    def _path(self, task_id):
        return os.path.join(self.root, "%s.json" % task_id)

    def _ensure_root(self):
        os.makedirs(self.root, exist_ok=True)

    def _now(self, now=None):
        if now is not None:
            return _coerce_now(now)
        if self._clock is not None:
            return _coerce_now(self._clock())
        return datetime.now(timezone.utc)

    @staticmethod
    def _check_id(task_id):
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
            raise ValueError(
                "invalid task id: must match [A-Za-z0-9._-]{1,128}"
            )
        if ".." in task_id:
            raise ValueError("invalid task id: '..' is not allowed")
        return task_id

    @staticmethod
    def _check_ttl(ttl):
        if ttl is None:
            return DEFAULT_TTL
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ValueError("ttl must be a positive number of seconds")
        if ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        return int(ttl)

    # -- persistence -----------------------------------------------------
    def _write(self, task):
        self._ensure_root()
        target = self._path(task["taskId"])
        fd, tmp = tempfile.mkstemp(
            dir=self.root, prefix=".tmp-task-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(task, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return task

    def _read(self, task_id):
        try:
            with open(self._path(task_id), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _expired(self, task, now):
        ttl = task.get("ttl")
        if ttl is None:
            return False
        updated = _parse_iso(task.get("updated"))
        if updated is None:
            return False
        try:
            deadline = updated + timedelta(seconds=float(ttl))
        except (TypeError, ValueError, OverflowError):
            return False
        return deadline < now

    def _prune_locked(self, now):
        if not os.path.isdir(self.root):
            return
        for name in os.listdir(self.root):
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = os.path.join(self.root, name)
            task = self._read(name[: -len(".json")])
            if task is None:
                continue
            if self._expired(task, now):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # -- public API ------------------------------------------------------
    def create(self, tool, arguments=None, ttl=None, task_id=None, now=None):
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool must be a non-empty string")
        tool = tool.strip()[:200]
        ttl = self._check_ttl(ttl)
        moment = self._now(now)
        if task_id is None:
            task_id = "task-%s" % uuid.uuid4().hex
        self._check_id(task_id)
        summary = self._summarise(arguments)
        stamp = _iso(moment)
        task = {
            "version": 1,
            "taskId": task_id,
            "tool": tool,
            "summary": summary,
            "status": "working",
            "ttl": ttl,
            "pollInterval": DEFAULT_POLL_INTERVAL,
            "created": stamp,
            "updated": stamp,
            "cancel_requested": False,
        }
        with self._lock:
            self._ensure_root()
            if os.path.exists(self._path(task_id)):
                raise ValueError("task id already exists: %s" % task_id)
            return self._write(task)

    @staticmethod
    def _summarise(arguments):
        """Short, redacted summary of arguments; values under secret keys masked."""
        if arguments is None:
            return ""
        _json_safe(arguments, "arguments")

        def scrub(value, depth=0):
            if depth > 3:
                return "***"
            if isinstance(value, dict):
                out = {}
                for key, item in list(value.items())[:20]:
                    if _SECRET_KEY_RE.search(str(key)):
                        out[str(key)[:64]] = "***"
                    else:
                        out[str(key)[:64]] = scrub(item, depth + 1)
                return out
            if isinstance(value, (list, tuple)):
                return [scrub(item, depth + 1) for item in list(value)[:8]]
            if isinstance(value, str):
                return redact(value, 120)
            return value

        try:
            text = json.dumps(scrub(arguments), ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = "<unserialisable>"
        return redact(text, 240)

    def get(self, task_id, now=None):
        self._check_id(task_id)
        moment = self._now(now)
        with self._lock:
            task = self._read(task_id)
            if task is None:
                raise KeyError(task_id)
            if self._expired(task, moment):
                try:
                    os.unlink(self._path(task_id))
                except OSError:
                    pass
                raise KeyError(task_id)
            return task

    def result(self, task_id, now=None):
        task = self.get(task_id, now=now)
        status = task.get("status")
        payload = task.get("result")
        if payload is None and status == "failed":
            payload = task.get("error")
        is_error = status == "failed" or bool(task.get("isError"))
        return {
            "task": self.to_mcp(task),
            "result": payload,
            "isError": is_error,
        }

    def list(self, limit=20, now=None):
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit <= 0:
            return []
        moment = self._now(now)
        found = []
        with self._lock:
            self._prune_locked(moment)
            if not os.path.isdir(self.root):
                return []
            for name in os.listdir(self.root):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                task = self._read(name[: -len(".json")])
                if task is None or "taskId" not in task:
                    continue
                if self._expired(task, moment):
                    continue
                created = _parse_iso(task.get("created"))
                updated = _parse_iso(task.get("updated"))
                key = (
                    created.timestamp() if created else 0.0,
                    updated.timestamp() if updated else 0.0,
                )
                found.append((key, task))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [task for _key, task in found[:limit]]

    def update(self, task_id, now=None, **fields):
        moment = self._now(now)
        with self._lock:
            task = self.get(task_id, now=moment)
            if "status" in fields and fields["status"] not in STATUSES:
                raise ValueError("unknown task status: %r" % (fields["status"],))
            if "progress" in fields:
                fields["progress"] = self._clamp_progress(
                    task.get("progress"), fields["progress"]
                )
            for protected in ("version", "taskId", "created"):
                fields.pop(protected, None)
            task.update(fields)
            task["updated"] = _iso(moment)
            return self._write(task)

    @staticmethod
    def _clamp_progress(previous, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("progress must be a number")
        if previous is not None and isinstance(previous, (int, float)):
            if value < previous:
                return previous
        return value

    def progress(self, task_id, progress, total=None, message=None, now=None):
        moment = self._now(now)
        with self._lock:
            task = self.get(task_id, now=moment)
            task["progress"] = self._clamp_progress(task.get("progress"), progress)
            if total is not None:
                if isinstance(total, bool) or not isinstance(total, (int, float)):
                    raise ValueError("total must be a number")
                task["total"] = total
            if message is not None:
                cleaned = redact(message, 200)
                task["message"] = cleaned
                task["statusMessage"] = cleaned
            task["updated"] = _iso(moment)
            return self._write(task)

    def complete(self, task_id, result, is_error=False, now=None):
        moment = self._now(now)
        _json_safe(result, "result")
        with self._lock:
            task = self.get(task_id, now=moment)
            if task.get("status") == "cancelled":
                # Cancellation is terminal: never resurrect it.
                return task
            task["status"] = "completed"
            task["result"] = result
            task["isError"] = bool(is_error)
            task["updated"] = _iso(moment)
            return self._write(task)

    def fail(self, task_id, error, now=None):
        moment = self._now(now)
        message = redact(error, 1000)
        with self._lock:
            task = self.get(task_id, now=moment)
            if task.get("status") == "cancelled":
                return task
            task["status"] = "failed"
            task["error"] = message
            task["isError"] = True
            task["statusMessage"] = message
            task["updated"] = _iso(moment)
            return self._write(task)

    def cancel(self, task_id, reason=None, now=None):
        moment = self._now(now)
        with self._lock:
            task = self.get(task_id, now=moment)
            if task.get("status") == "cancelled":
                return task
            task["cancel_requested"] = True
            if reason is not None:
                task["cancelReason"] = redact(reason, 300)
            if task.get("status") in ("completed", "failed"):
                # Keep the terminal result; only record the cancellation intent.
                task["updated"] = _iso(moment)
                return self._write(task)
            task["status"] = "cancelled"
            task["cancelledAt"] = _iso(moment)
            task["updated"] = _iso(moment)
            return self._write(task)

    def is_cancelled(self, task_id, now=None):
        try:
            task = self.get(task_id, now=now)
        except KeyError:
            return False
        return task.get("status") == "cancelled" or bool(task.get("cancel_requested"))

    @staticmethod
    def to_mcp(task):
        if not isinstance(task, dict):
            raise ValueError("task must be a mapping")
        task_id = task.get("taskId", task.get("id"))
        payload = {
            "taskId": task_id,
            "status": task.get("status", "working"),
            "ttl": task.get("ttl", DEFAULT_TTL),
            "pollInterval": task.get("pollInterval", DEFAULT_POLL_INTERVAL),
        }
        status_message = task.get("statusMessage")
        if status_message:
            payload["statusMessage"] = status_message
        return payload

    @staticmethod
    def to_wire(task):
        """Map a stored task to the MCP 2025-11-25 Task shape.

        ``to_mcp`` keeps the v3.5 candidate/back-compat shape (ttl in
        seconds); the protocol field is milliseconds and requires ISO
        creation/update timestamps.
        """
        if not isinstance(task, dict):
            raise ValueError("task must be a mapping")
        task_id = task.get("taskId", task.get("id"))
        raw_ttl = task.get("ttl", DEFAULT_TTL)
        try:
            ttl = int(float(raw_ttl) * 1000) if raw_ttl is not None else None
        except (TypeError, ValueError):
            ttl = None
        payload = {
            "taskId": task_id,
            "status": task.get("status", "working"),
            "ttl": ttl,
            "pollInterval": task.get("pollInterval", DEFAULT_POLL_INTERVAL),
        }
        created = task.get("created")
        updated = task.get("updated")
        if created:
            payload["createdAt"] = created
        if updated:
            payload["lastUpdatedAt"] = updated
        status_message = task.get("statusMessage")
        if status_message:
            payload["statusMessage"] = status_message
        return payload


class ProgressEmitter:
    """Emit ``notifications/progress`` frames, swallowing transport failures.

    ``send`` is injectable (a callable taking one dict); ``token`` is the
    progress token negotiated by the MCP caller.  Identical consecutive
    emissions are suppressed so slow consumers are not spammed.
    """

    def __init__(self, send, token):
        self._send = send
        self.token = token
        self._last = None
        self.sent = 0
        self.suppressed = 0

    def emit(self, progress, total=None, message=None):
        key = (progress, total, message)
        if key == self._last:
            self.suppressed += 1
            return None
        self._last = key
        params = {"progressToken": self.token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        frame = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": params,
        }
        try:
            self._send(frame)
        except Exception:
            self.suppressed += 1
            return None
        self.sent += 1
        return frame
