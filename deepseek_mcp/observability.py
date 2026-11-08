"""Best-effort local trace logs and optional trace export (DeepSeek MCP v3.6.0).

Standard library only; Python 3.9 compatible.  Copy this file to
``deepseek_mcp/observability.py`` unchanged.

Local tracing
-------------
``TraceWriter(cfg, run_id)`` appends one JSON object per line to
``<state_dir>/traces/<run-id>.jsonl``.  Every record carries
``ts`` (ISO-8601 UTC), ``run_id``, ``phase`` and ``event`` plus arbitrary
keyword fields.  The traces directory is created with mode 0700 and files are
written with mode 0600.

Every public method is best-effort: disk errors, bad config, unencodable
fields and hostile ``__repr__`` implementations are swallowed, never raised.
A broken trace path can therefore never break a Harness run.

Export
------
``export_run()`` is disabled unless the config enables a target:

* ``otlp_endpoint``  -- OTLP/HTTP JSON collector base URL
  (``http://host:4318`` or a full ``.../v1/traces`` URL).
* ``langfuse_host``   -- Langfuse host, e.g. ``https://cloud.langfuse.com``.
  Credentials are read only from ``LANGFUSE_PUBLIC_KEY`` and
  ``LANGFUSE_SECRET_KEY``; they are never accepted from config or arguments.

Both targets are optional and independent.  Network access uses an injectable
``opener(request, timeout=...)`` returning a response-like object with
``status``/``status_code``/``getcode()`` and ``read()``; a ``(status, body)``
tuple is also accepted.  ``export_run`` returns exactly::

    {"attempted": bool, "exported": bool, "reason": str}

and never raises.  All outbound text passes through
``deepseek_mcp.api.redact_secrets``.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.request

try:  # normal package layout
    from .api import redact_secrets
except Exception:  # pragma: no cover - standalone file copied next to api.py
    try:
        from deepseek_mcp.api import redact_secrets
    except Exception:
        def redact_secrets(text):
            return str(text or "")


SERVICE_NAME = "deepseek-harness-mcp"
SERVICE_VERSION = "3.9.4"
SCOPE_NAME = "deepseek_mcp.observability"
DEFAULT_STATE_DIR = "~/DeepSeek-MCP/state"
DEFAULT_EXPORT_TIMEOUT = 10.0
MAX_STRING = 8000
MAX_EVENTS = 20000

_RESERVED = ("ts", "run_id", "phase", "event", "seq")
_START_EVENTS = ("start", "begin", "phase_start")
_END_EVENTS = ("end", "phase_end", "phase_finish", "finish")
_FAILED_STATUS = ("failed", "error", "errored", "timeout", "timed_out",
                  "cancelled", "canceled", "killed")
_SECRET_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# config / value helpers (never raise)
# ---------------------------------------------------------------------------

def _cfg_get(cfg, key, default=None):
    """Read a config key from a mapping or an attribute-style object."""
    if cfg is None:
        return default
    try:
        if isinstance(cfg, dict):
            return cfg.get(key, default)
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:
                return getter(key)
        return getattr(cfg, key, default)
    except Exception:
        return default


def _as_float(value, default):
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except Exception:
        return default


def _now_iso():
    return _iso(time.time())


def _iso(epoch):
    """Epoch seconds -> ISO-8601 UTC with millisecond precision, or None."""
    if epoch is None:
        return None
    try:
        moment = dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except Exception:
        return None


def _parse_iso(text):
    """ISO-8601 string (or numeric epoch) -> epoch seconds, or None."""
    try:
        if isinstance(text, (int, float)) and not isinstance(text, bool):
            return float(text)
        if not isinstance(text, str) or not text.strip():
            return None
        value = text.strip()
        try:
            return float(value)
        except ValueError:
            pass
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        moment = dt.datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.timestamp()
    except Exception:
        return None


def _safe_run_id(run_id):
    """Filesystem-safe stem for a run id; never contains a path separator."""
    try:
        text = str(run_id or "").strip()
    except Exception:
        text = ""
    text = _SECRET_NAME_RE.sub("-", text)[:100].strip(".-_")
    return text or "run"


def _safe_text(value, limit=MAX_STRING):
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    try:
        return redact_secrets(text)
    except Exception:
        return text


def _jsonable(value, depth=0):
    """Convert a value into JSON-safe data, redacting every string."""
    if depth > 6:
        return "<max-depth>"
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return _safe_text(value)
        if isinstance(value, dt.datetime):
            return _iso(value.timestamp() if value.tzinfo else
                        value.replace(tzinfo=dt.timezone.utc).timestamp())
        if isinstance(value, dict):
            out = {}
            for key in list(value.keys())[:200]:
                out[_safe_text(key, 200)] = _jsonable(value[key], depth + 1)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_jsonable(item, depth + 1) for item in list(value)[:500]]
        return _safe_text(value)
    except Exception:
        return "<unserializable>"


def _hex(value, length):
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()
    return digest[:length]


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def _state_dir(cfg):
    raw = _cfg_get(cfg, "state_dir", DEFAULT_STATE_DIR)
    try:
        raw = os.fspath(raw)
    except Exception:
        raw = DEFAULT_STATE_DIR
    return os.path.expanduser(str(raw or DEFAULT_STATE_DIR))


def trace_path(cfg, run_id):
    """Absolute path of the JSONL trace file for ``run_id``."""
    return os.path.join(_state_dir(cfg), "traces", _safe_run_id(run_id) + ".jsonl")


def read_events(cfg, run_id, limit=MAX_EVENTS):
    """Read a trace file into a list of dicts; unreadable files yield []."""
    events = []
    try:
        path = trace_path(cfg, run_id)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if isinstance(record, dict):
                    events.append(record)
                if len(events) >= int(limit):
                    break
    except Exception:
        return []
    return events


# ---------------------------------------------------------------------------
# TraceWriter
# ---------------------------------------------------------------------------

class TraceWriter(object):
    """Append-only JSONL trace for one run.

    ``clock`` is an optional callable returning epoch seconds (or a
    ``datetime``); it exists for deterministic tests.
    """

    def __init__(self, cfg, run_id, clock=None):
        self.cfg = cfg
        self.run_id = _safe_text(run_id, 200)
        self.clock = clock if callable(clock) else time.time
        self.traces_dir = os.path.join(_state_dir(cfg), "traces")
        self.path = os.path.join(self.traces_dir, _safe_run_id(run_id) + ".jsonl")
        self._events = []
        self._seq = 0
        self._finished = False
        self._summary = {}
        self._dir_ready = False

    # -- internals ---------------------------------------------------------

    def _now(self):
        try:
            value = self.clock()
        except Exception:
            return time.time()
        if isinstance(value, dt.datetime):
            try:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=dt.timezone.utc)
                return value.timestamp()
            except Exception:
                return time.time()
        try:
            return float(value)
        except Exception:
            return time.time()

    def _ensure_dir(self):
        if self._dir_ready:
            return
        os.makedirs(self.traces_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.traces_dir, 0o700)
        except OSError:
            pass
        self._dir_ready = True

    def _write(self, record):
        """Append one record.  Returns True on success, never raises."""
        try:
            self._ensure_dir()
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            data = line.encode("utf-8", "replace")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return True
        except Exception:
            return False

    # -- public API --------------------------------------------------------

    def event(self, phase, event, **fields):
        """Record one event.  Returns True when it reached the file.

        Unknown fields are redacted and JSON-normalised.  This method never
        raises; the event is kept in memory even when the disk write fails so
        ``timeline()`` still works.
        """
        try:
            epoch = self._now()
            record = {
                "ts": _iso(epoch) or _now_iso(),
                "run_id": self.run_id,
                "phase": _safe_text(phase, 200),
                "event": _safe_text(event, 200),
            }
            for key in sorted(fields, key=lambda item: str(item)):
                name = _safe_text(key, 200)
                if name in _RESERVED:
                    name = "meta_" + name
                record[name] = _jsonable(fields[key])
            self._seq += 1
            record["seq"] = self._seq
            self._events.append(record)
            if len(self._events) > MAX_EVENTS:
                self._events = self._events[-MAX_EVENTS:]
            return self._write(record)
        except Exception:
            return False

    def finish(self, summary=None):
        """Write the terminal ``run/finish`` event and return ``summary()``.

        ``summary`` may be a mapping (status, seconds, error, ...) or any
        scalar.  Never raises.
        """
        try:
            fields = _jsonable(summary) if isinstance(summary, dict) else (
                {"summary": _jsonable(summary)} if summary is not None else {})
            if not isinstance(fields, dict):
                fields = {"summary": fields}
            fields.setdefault("status", "ok")
            self._summary = dict(fields)
            self._finished = True
            self.event("run", "finish", **fields)
        except Exception:
            self._finished = True
        try:
            return self.summary()
        except Exception:
            return {}

    def _read_file_events(self):
        return read_events(self.cfg, self.run_id)

    def _timeline_events(self):
        """Prefer the on-disk trace so a fresh writer can aggregate old runs."""
        try:
            disk = self._read_file_events()
            if len(disk) >= len(self._events):
                return disk
        except Exception:
            pass
        return list(self._events)

    def timeline(self):
        """Aggregate the trace into phase start/end/seconds/status.

        Returns a dict with ``run_id``, ``started``, ``finished``,
        ``seconds``, ``status``, ``events`` and ``phases``.  Each phase entry
        has ``phase``, ``started``, ``finished``, ``seconds``, ``status`` and
        ``events``.  Never raises.
        """
        try:
            events = self._timeline_events()
            return _aggregate(events, self.run_id,
                              forced_finished=self._finished,
                              summary=self._summary)
        except Exception:
            return {"run_id": self.run_id, "started": None, "finished": None,
                    "seconds": None, "status": "unknown", "events": 0,
                    "phases": []}

    def summary(self):
        """Compact, JSON-safe fields a run record can embed directly.

        Shape: ``{"trace": {...}, "timeline": {...}}``.  Suggested embedding::

            run["observability"] = writer.summary()
        """
        timeline = self.timeline()
        trace = {
            "run_id": self.run_id,
            "path": _safe_text(self.path, 1000),
            "events": timeline.get("events", len(self._events)),
            "status": timeline.get("status", "unknown"),
            "started": timeline.get("started"),
            "finished": timeline.get("finished"),
            "seconds": timeline.get("seconds"),
            "schema": 1,
        }
        return {"trace": trace, "timeline": timeline}


def _new_phase(name):
    return {"phase": name, "started": None, "finished": None,
            "_start_ts": None, "_end_ts": None, "_reported": None,
            "seconds": None, "status": "unknown", "events": 0}


def _aggregate(events, run_id="", forced_finished=False, summary=None):
    """Build the timeline dict from JSONL records."""
    phases = {}
    order = []
    first_ts = None
    last_ts = None
    finished = bool(forced_finished)
    finish_status = None
    failed = False

    for record in events:
        if not isinstance(record, dict):
            continue
        try:
            phase = record.get("phase")
            if not isinstance(phase, str):
                phase = _safe_text(phase, 200)
            event = record.get("event")
            if not isinstance(event, str):
                event = _safe_text(event, 200)
            if phase not in phases:
                phases[phase] = _new_phase(phase)
                order.append(phase)
            entry = phases[phase]
            entry["events"] += 1
            ts = _parse_iso(record.get("ts"))
            if ts is not None:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            status = str(record.get("status") or "").strip().lower()
            if status in _FAILED_STATUS:
                entry["status"] = "failed"
                failed = True
            if event in _START_EVENTS:
                if entry["_start_ts"] is None:
                    entry["_start_ts"] = ts
                    entry["started"] = record.get("ts")
                if entry["status"] != "failed":
                    entry["status"] = "running"
            if event in _END_EVENTS:
                if entry["_end_ts"] is None:
                    entry["_end_ts"] = ts
                    entry["finished"] = record.get("ts")
                if entry["status"] != "failed":
                    entry["status"] = "ok"
                reported = record.get("seconds")
                if isinstance(reported, (int, float)) and not isinstance(reported, bool):
                    entry["_reported"] = float(reported)
                if phase in ("run", "") and event == "finish":
                    finished = True
                    finish_status = status or "ok"
            if isinstance(record.get("seconds"), (int, float)) and ts is None:
                entry["_reported"] = float(record["seconds"])
        except Exception:
            continue

    out = []
    for name in order:
        if name in ("run", ""):
            continue
        entry = phases[name]
        seconds = None
        if entry["_start_ts"] is not None and entry["_end_ts"] is not None:
            seconds = round(entry["_end_ts"] - entry["_start_ts"], 3)
        elif entry["_reported"] is not None:
            seconds = round(entry["_reported"], 3)
        if entry["status"] in ("running", "unknown") and entry["finished"] is not None:
            entry["status"] = "ok"
        out.append({
            "phase": name,
            "started": entry["started"],
            "finished": entry["finished"],
            "seconds": seconds,
            "status": entry["status"],
            "events": entry["events"],
        })

    run_status = "unknown"
    if failed:
        run_status = "failed"
    elif finished:
        run_status = "failed" if finish_status in _FAILED_STATUS else (finish_status or "ok")
    elif events:
        run_status = "running"

    total = None
    if first_ts is not None and last_ts is not None:
        total = round(max(last_ts - first_ts, 0.0), 3)

    result = {
        "run_id": run_id,
        "started": _iso(first_ts),
        "finished": _iso(last_ts) if finished else None,
        "seconds": total,
        "status": run_status,
        "events": len(events),
        "phases": out,
    }
    if isinstance(summary, dict) and summary.get("status"):
        # A caller-supplied finish summary is authoritative for the run.
        result["status"] = str(summary.get("status")).lower()
    return result


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _run_get(run, key, default=None):
    try:
        if isinstance(run, dict):
            return run.get(key, default)
        return getattr(run, key, default)
    except Exception:
        return default


def _phase_list(timeline):
    phases = timeline.get("phases") if isinstance(timeline, dict) else None
    if not isinstance(phases, list):
        return []
    return [phase for phase in phases if isinstance(phase, dict) and phase.get("phase")]


def _coerce_timeline(run, timeline):
    """Accept a timeline dict, or derive one from a saved run record."""
    if isinstance(timeline, dict) and isinstance(timeline.get("phases"), list):
        return timeline
    stored = _run_get(run, "timeline")
    if isinstance(stored, dict) and isinstance(stored.get("phases"), list):
        return stored
    stored = _run_get(run, "observability")
    if isinstance(stored, dict) and isinstance(stored.get("timeline"), dict):
        return stored["timeline"]
    run_id = _safe_text(_run_get(run, "id", _run_get(run, "run_id", "")), 200)
    raw_phases = _run_get(run, "phases")
    out = []
    if isinstance(raw_phases, list):
        for item in raw_phases:
            if not isinstance(item, dict):
                continue
            name = item.get("phase") or item.get("name") or item.get("title")
            if not name:
                continue
            seconds = _as_float(item.get("seconds", item.get("duration_seconds")), None)
            status = str(item.get("status") or "unknown").lower()
            if status in _FAILED_STATUS:
                status = "failed"
            out.append({
                "phase": _safe_text(name, 200),
                "started": item.get("started") or item.get("start"),
                "finished": item.get("finished") or item.get("end"),
                "seconds": seconds,
                "status": status,
                "events": item.get("events", 0),
            })
    started = _run_get(run, "started") or _run_get(run, "created")
    status = str(_run_get(run, "status") or ("ok" if out else "unknown")).lower()
    return {
        "run_id": run_id,
        "started": started,
        "finished": _run_get(run, "finished") or _run_get(run, "ended"),
        "seconds": _as_float(_run_get(run, "seconds"), None),
        "status": "failed" if status in _FAILED_STATUS else status,
        "events": sum(int(phase.get("events") or 0) for phase in out),
        "phases": out,
    }


def _attrs(mapping):
    attributes = []
    for key in sorted(mapping.keys(), key=lambda item: str(item)):
        value = mapping[key]
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        text = _safe_text(value, 2000)
        attributes.append({"key": str(key), "value": {"stringValue": text}})
    return attributes


def _phase_nanos(phase, default_start, default_end):
    """Return (start_ns, end_ns) for a phase entry."""
    start = _parse_iso(phase.get("started"))
    end = _parse_iso(phase.get("finished"))
    seconds = _as_float(phase.get("seconds"), None)
    if start is None:
        start = default_start
    if end is None:
        if seconds is not None:
            end = start + max(seconds, 0.0)
        else:
            end = default_end
    if end < start:
        end = start
    return int(start * 1e9), int(end * 1e9)


def _skill_otlp_attributes(run):
    block = _run_get(run, "skill_auto")
    if not isinstance(block, dict) or not block:
        return {}
    summary = block.get("summary_counts")
    if not isinstance(summary, dict):
        # Older records stored the counts directly under "summary"; keep
        # reading them, but new records carry a one-line string there.
        summary = block.get("summary")
    if not isinstance(summary, dict):
        return {}
    attrs = {}
    for name in ("captured", "drafted", "saved", "merged", "refreshed",
                 "promoted", "demoted", "retired", "rolled_back", "errors"):
        attrs["skill." + name] = summary.get(name, 0)
    return attrs


def _otlp_payload(run, timeline):
    run_id = _safe_text(timeline.get("run_id") or _run_get(run, "id", ""), 200) or "run"
    trace_id = _hex(run_id, 32)
    root_id = _hex(run_id + "|root", 16)
    now_ns = int(time.time() * 1e9)
    total_start = _parse_iso(timeline.get("started"))
    total_end = _parse_iso(timeline.get("finished"))
    if total_start is None:
        seconds = _as_float(timeline.get("seconds"), 0.0) or 0.0
        total_start = time.time() - seconds
    if total_end is None:
        total_end = total_start + (_as_float(timeline.get("seconds"), 0.0) or 0.0)
    root_start_ns, root_end_ns = _phase_nanos(timeline, total_start, total_end)
    status = str(timeline.get("status") or "unknown").lower()
    failed = status in _FAILED_STATUS

    spans = [{
        "traceId": trace_id,
        "spanId": root_id,
        "name": _safe_text(_run_get(run, "title") or _run_get(run, "name") or run_id, 200),
        "kind": 1,
        "startTimeUnixNano": str(root_start_ns),
        "endTimeUnixNano": str(max(root_end_ns, root_start_ns)),
        "attributes": _attrs({
            "run.id": run_id,
            "run.status": status,
            "run.title": _run_get(run, "title") or "",
            "run.model": _run_get(run, "model") or "",
            "run.seconds": timeline.get("seconds"),
            "timeline.phases": len(_phase_list(timeline)),
            "timeline.events": timeline.get("events"),
            **_skill_otlp_attributes(run),
        }),
        "status": {"code": 2 if failed else 1},
    }]

    for phase in _phase_list(timeline):
        name = _safe_text(phase.get("phase"), 200)
        phase_status = str(phase.get("status") or "unknown").lower()
        start_ns, end_ns = _phase_nanos(phase, total_start, total_end)
        spans.append({
            "traceId": trace_id,
            "spanId": _hex(run_id + "|" + name, 16),
            "parentSpanId": root_id,
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(max(end_ns, start_ns)),
            "attributes": _attrs({
                "run.id": run_id,
                "phase": name,
                "phase.status": phase_status,
                "phase.seconds": phase.get("seconds"),
                "phase.events": phase.get("events"),
            }),
            "status": {"code": 2 if phase_status in _FAILED_STATUS else 1},
        })

    return {
        "resourceSpans": [{
            "resource": {"attributes": _attrs({
                "service.name": SERVICE_NAME,
                "service.version": SERVICE_VERSION,
            })},
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME, "version": SERVICE_VERSION},
                "spans": spans,
            }],
        }]
    }


def _langfuse_payload(run, timeline):
    run_id = _safe_text(timeline.get("run_id") or _run_get(run, "id", ""), 200) or "run"
    trace_id = _hex(run_id, 32)
    timestamp = _iso(_parse_iso(timeline.get("started")) or time.time())
    status = str(timeline.get("status") or "unknown").lower()
    metadata = {
        "run_id": run_id,
        "status": status,
        "model": _run_get(run, "model"),
        "seconds": timeline.get("seconds"),
        "events": timeline.get("events"),
        "source": SERVICE_NAME,
        "version": SERVICE_VERSION,
    }
    extra = _run_get(run, "metadata")
    if isinstance(extra, dict):
        for key in list(extra.keys())[:50]:
            metadata.setdefault(str(key), extra[key])

    batch = [{
        "id": _hex(run_id + "|trace", 32),
        "type": "trace-create",
        "timestamp": timestamp,
        "body": {
            "id": trace_id,
            "name": _safe_text(_run_get(run, "title") or run_id, 200),
            "timestamp": timestamp,
            "metadata": _jsonable(metadata),
            "tags": [SERVICE_NAME, "run"],
        },
    }]
    for phase in _phase_list(timeline):
        name = _safe_text(phase.get("phase"), 200)
        phase_status = str(phase.get("status") or "unknown").lower()
        is_error = phase_status in _FAILED_STATUS
        body = {
            "id": _hex(run_id + "|" + name, 32),
            "traceId": trace_id,
            "name": name,
            "startTime": phase.get("started") or timestamp,
            "metadata": _jsonable({
                "run_id": run_id,
                "phase": name,
                "status": phase_status,
                "seconds": phase.get("seconds"),
                "events": phase.get("events"),
            }),
            "level": "ERROR" if is_error else "DEFAULT",
        }
        if phase.get("finished"):
            body["endTime"] = phase.get("finished")
        if is_error:
            body["statusMessage"] = "phase %s status=%s" % (name, phase_status)
        batch.append({
            "id": _hex(run_id + "|span|" + name, 32),
            "type": "span-create",
            "timestamp": body["startTime"],
            "body": body,
        })
    return {"batch": batch}


def _post(opener, url, payload, headers, timeout):
    """POST JSON via the injected opener.  Returns (status, body text)."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        response = opener(request, timeout=timeout)
    except TypeError:
        # Tolerate minimal test openers that only accept the request.
        response = opener(request)
    status = 0
    body = ""
    try:
        if isinstance(response, tuple):
            status = int(response[0])
            body = response[1] if len(response) > 1 else ""
        else:
            for name in ("status", "status_code", "code"):
                value = getattr(response, name, None)
                if value is not None:
                    status = int(value)
                    break
            if not status:
                getcode = getattr(response, "getcode", None)
                if callable(getcode):
                    status = int(getcode())
            read = getattr(response, "read", None)
            if callable(read):
                raw = read(4000)
                if isinstance(raw, bytes):
                    body = raw.decode("utf-8", "replace")
                else:
                    body = str(raw or "")
    except Exception:
        body = ""
    return status, body


def _snippet(text):
    try:
        clean = " ".join(str(text or "").split())
        return _safe_text(clean[:200])
    except Exception:
        return ""


def _endpoint(value, suffix):
    """Normalise a collector/Langfuse endpoint, or return '' when unusable."""
    try:
        text = str(value or "").strip()
    except Exception:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        return ""
    text = text.rstrip("/")
    if suffix and text.endswith(suffix):
        return text
    return text + suffix


def export_run(cfg, run, timeline=None, opener=None):
    """Export one run to configured OTLP and/or Langfuse endpoints.

    Disabled by default: with neither ``otlp_endpoint`` nor ``langfuse_host``
    configured this returns immediately and performs no I/O.  ``opener`` is
    injectable (see module docstring) and is never required in production
    beyond the standard library.  Never raises.

    Returns ``{"attempted": bool, "exported": bool, "reason": str}``.
    """
    attempted = 0
    succeeded = 0
    reasons = []
    try:
        enabled = _cfg_get(cfg, "observability_export", True)
        if enabled is False or (isinstance(enabled, str) and
                                enabled.strip().lower() in ("0", "false", "off", "no")):
            return {"attempted": False, "exported": False,
                    "reason": "disabled: observability_export is off"}

        otlp_raw = _cfg_get(cfg, "otlp_endpoint")
        langfuse_raw = _cfg_get(cfg, "langfuse_host")
        otlp_url = _endpoint(otlp_raw, "/v1/traces")
        langfuse_url = ""
        host = str(langfuse_raw or "").strip()
        if host:
            if not host.lower().startswith(("http://", "https://")):
                reasons.append("langfuse: invalid LANGFUSE host")
            elif host.rstrip("/").endswith("/api/public/ingestion"):
                langfuse_url = host.rstrip("/")
            else:
                langfuse_url = _endpoint(host, "/api/public/ingestion")
        if otlp_raw and not otlp_url:
            reasons.append("otlp: invalid endpoint")

        if not otlp_url and not langfuse_url:
            detail = "; ".join(reasons)
            return {"attempted": False, "exported": False,
                    "reason": detail or "disabled: no otlp_endpoint or langfuse_host configured"}

        timeline_data = _coerce_timeline(run, timeline)
        timeout = _as_float(_cfg_get(cfg, "export_timeout", DEFAULT_EXPORT_TIMEOUT),
                            DEFAULT_EXPORT_TIMEOUT)
        if timeout <= 0:
            timeout = DEFAULT_EXPORT_TIMEOUT
        opener = opener or urllib.request.urlopen

        if otlp_url:
            attempted += 1
            try:
                payload = _otlp_payload(run, timeline_data)
                headers = {"Content-Type": "application/json",
                           "User-Agent": "%s/%s" % (SERVICE_NAME, SERVICE_VERSION)}
                status, body = _post(opener, otlp_url, payload, headers, timeout)
                if 200 <= status < 300:
                    succeeded += 1
                    reasons.append("otlp: http %s" % status)
                else:
                    reasons.append("otlp: http %s %s" % (status, _snippet(body)))
            except Exception as exc:
                reasons.append("otlp: %s" % _snippet(exc))

        if langfuse_url:
            public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
            secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
            if not public_key or not secret_key:
                reasons.append("langfuse: missing LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY")
            else:
                attempted += 1
                try:
                    payload = _langfuse_payload(run, timeline_data)
                    token = base64.b64encode(
                        ("%s:%s" % (public_key, secret_key)).encode("utf-8")
                    ).decode("ascii")
                    headers = {"Content-Type": "application/json",
                               "Authorization": "Basic " + token,
                               "User-Agent": "%s/%s" % (SERVICE_NAME, SERVICE_VERSION)}
                    status, body = _post(opener, langfuse_url, payload, headers, timeout)
                    if 200 <= status < 300:
                        succeeded += 1
                        reasons.append("langfuse: http %s" % status)
                    else:
                        reasons.append("langfuse: http %s %s" % (status, _snippet(body)))
                except Exception as exc:
                    reasons.append("langfuse: %s" % _snippet(exc))

        return {
            "attempted": attempted > 0,
            "exported": attempted > 0 and succeeded == attempted,
            "reason": _safe_text("; ".join(reasons) or "exported", 1000),
        }
    except Exception as exc:  # belt and braces: observability never breaks a run
        return {"attempted": attempted > 0, "exported": False,
                "reason": _safe_text("export error: %s" % _snippet(exc), 1000)}
