"""Append-only run event log, fenced ATTENTION parsing and stage projection.

This module is the P1-1/P1-3 event surface for CrewForeman 3.8.0.  It is
deliberately self-contained: Python 3.9, standard library only, no provider or
Harness imports.  Callers (``jobs``, ``harness``, ``store``) own run state; this
module owns the durable event journal and turns run/job records plus journal
entries into one small, machine-readable projection.

Design rules
------------
* ``events.jsonl`` is append-only and compact JSON: one object per line.
* Every append happens under ``.lock`` so ``seq`` is strictly increasing even
  with several workers writing the same work directory.
* Readers tolerate blank lines, corrupt lines and a partially written final
  line.  Reading never raises.
* ``result.json`` is written with tempfile + ``os.replace`` so a reader never
  sees a half-written result.
* Human questions are accepted ONLY from a fenced ```` ```attention ```` block.
  Prose is never interpreted and a question is never invented.
"""

import contextlib
import datetime
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from . import platforms

try:  # pragma: no cover - the normal package import path
    from .api import redact_secrets as _redact
except Exception:  # pragma: no cover - standalone import
    def _redact(text):
        return text

try:  # pragma: no cover - the normal package import path
    from .status import STATUSES as _STATUS_VOCAB
except Exception:  # pragma: no cover - keeps the module importable standalone
    _STATUS_VOCAB = (
        "ok",
        "ok_below_gate",
        "ok_unreviewed",
        "ok_rescued",
        "error",
        "no_balance",
        "cancelled",
        "deadline_exceeded",
        "budget_exceeded",
    )

EVENT_TYPES = ("admitted", "phase", "progress", "needs_input", "result",
               "failed", "cancelled", "reviewer_backup")
STAGES = ("queued", "running", "needs_human", "review_ready", "terminal")
ATTENTIONS = ("none", "answer_question", "review_result", "inspect_failure")
EVENT_SCHEMA = 1
DEFAULT_MAX_BYTES = 1_000_000

#: Status vocabulary from :mod:`deepseek_mcp.status` plus the detached-job
#: spelling ``done``.  All of these mean the run/job is finished.
STATUSES = tuple(_STATUS_VOCAB)
TERMINAL_STATUSES = frozenset(STATUSES + ("done",))

#: Event types that end a pending question.
_CLEARING_EVENTS = frozenset(("admitted", "result", "failed", "cancelled"))
_TERMINAL_EVENTS = frozenset(("result", "failed", "cancelled"))

_FAILURE_ATTENTION = frozenset((
    "ok_unreviewed", "error", "no_balance", "deadline_exceeded",
    "budget_exceeded", "gate_failed",
))
_REVIEW_EVIDENCE_STATUSES = frozenset((
    "pending", "running", "accepted", "stopped", "error", "in_progress",
))

_QUESTION_LIMIT = 500
_OPTION_LIMIT = 200
_OPTIONS_MIN = 2
_OPTIONS_MAX = 8
_EVIDENCE_LIMIT = 2000

_LOCK_TIMEOUT = 5.0
_LOCK_STALE_AFTER = 60.0

_ATTENTION_OPEN_RE = re.compile(
    r"(?m)^[ \t]{0,3}```[ \t]*attention[ \t]*\r?$", re.IGNORECASE)
_ATTENTION_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]{0,3}```[ \t]*attention[ \t]*\r?\n(.*?)^[ \t]{0,3}```[ \t]*\r?$",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Tiny shared helpers
# ---------------------------------------------------------------------------

def _iso(ts=None):
    """Return an ISO-8601 local-aware timestamp (seconds precision)."""
    if isinstance(ts, datetime.datetime):
        value = ts
        if value.tzinfo is None:
            value = value.astimezone()
        return value.isoformat(timespec="seconds")
    if isinstance(ts, str):
        return ts
    try:
        value = datetime.datetime.fromtimestamp(
            time.time() if ts is None else float(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        value = datetime.datetime.now()
    return value.astimezone().isoformat(timespec="seconds")


def _as_text(value):
    """Render a result value as text without ever raising."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except Exception:
        try:
            return str(value)
        except Exception:
            return ""


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _status_of(mapping):
    if not isinstance(mapping, dict):
        return ""
    for key in ("status", "final_status", "terminal_status"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _run_id(run, job):
    for source in (run, job):
        if not isinstance(source, dict):
            continue
        for key in ("id", "run_id", "job_id"):
            value = source.get(key)
            if value:
                return str(value)
    return ""


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------

class EventLog(object):
    """Durable, rotating JSONL event journal for one work directory."""

    def __init__(self, work_dir, clock=None, max_bytes=DEFAULT_MAX_BYTES):
        self.work_dir = Path(work_dir)
        self.clock = clock if callable(clock) else time.time
        try:
            self.max_bytes = int(DEFAULT_MAX_BYTES if max_bytes is None
                                 else max_bytes)
        except (TypeError, ValueError):
            self.max_bytes = DEFAULT_MAX_BYTES
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(str(self.work_dir), 0o700)
        except OSError:
            pass

    # -- paths ---------------------------------------------------------
    @property
    def events_path(self):
        return self.work_dir / "events.jsonl"

    @property
    def rotated_path(self):
        return self.work_dir / "events.1.jsonl"

    @property
    def hwm_path(self):
        return self.work_dir / "events.hwm.json"

    @property
    def result_path(self):
        return self.work_dir / "result.json"

    @property
    def lock_path(self):
        return self.work_dir / ".lock"

    # -- locking -------------------------------------------------------
    @contextlib.contextmanager
    def _lock(self):
        """Acquire the per-directory advisory lock.

        An O_CREAT|O_EXCL existence lock cannot be shared with WorkStore's
        lock file: on Windows a reader can block the holder's unlink, and a
        lock left behind by a crashed writer blocks every later append for
        the stale timeout.  The kernel releases an advisory lock on exit.
        """
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        lock = platforms.file_lock(self.lock_path, timeout=_LOCK_TIMEOUT)
        try:
            lock.__enter__()
        except TimeoutError as exc:
            # A filesystem that cannot create the lock must not prevent
            # journal writes (the append itself is still O_APPEND).  A real
            # contention timeout must still surface.
            if "cannot open lock" not in str(exc):
                raise
            yield
            return
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    # -- reading -------------------------------------------------------
    @staticmethod
    def _iter_file(path):
        """Yield valid event dicts from *path*; skip bad/partial lines."""
        try:
            with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict):
                        yield event
        except (OSError, IOError):
            return

    def _retained(self):
        events = {}
        for path in (self.rotated_path, self.events_path):
            for event in self._iter_file(path):
                seq = _as_int(event.get("seq"))
                if seq is None:
                    continue
                events[seq] = event
        return events

    def _max_seq(self):
        try:
            retained = self._retained()
            return max(retained) if retained else 0
        except (TypeError, ValueError):
            return 0

    def read_since(self, seq=0):
        """Return retained events with ``seq > seq``, oldest first.

        Missing, empty, corrupt and partially written files are ignored rather
        than raised.  A cursor older than the oldest retained event simply
        returns every retained event (rotation is lossy by design).
        """
        try:
            since = int(seq)
        except (TypeError, ValueError):
            since = 0
        merged = {}
        for path in (self.rotated_path, self.events_path):
            for event in self._iter_file(path):
                value = _as_int(event.get("seq"))
                if value is None or value <= since:
                    continue
                merged[value] = event
        return [merged[key] for key in sorted(merged)]

    def next_seq(self):
        """Return the cursor to pass back to :meth:`read_since`.

        It is the highest retained ``seq`` (``0`` when empty), not one past
        it: ``read_since`` selects ``seq > cursor``, so handing back
        ``highest + 1`` would skip the next event that arrives.
        """
        return self._max_seq()

    # -- writing -------------------------------------------------------
    def append(self, event_type, payload=None, event_id=None, at=None):
        """Append one event and return it.

        ``event_type`` must be one of :data:`EVENT_TYPES`; ``payload`` defaults
        to an empty dict and must be a JSON-serialisable dict.  The sequence is
        allocated from the current and rotated files while holding ``.lock``.
        The line is written with ``O_APPEND`` and fsynced before returning.
        """
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise ValueError("Unknown event type: %r" % (event_type,))
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be a dict")
        try:
            rendered = json.dumps(payload, ensure_ascii=False,
                                  separators=(",", ":"))
            payload = json.loads(_redact(rendered))
        except (TypeError, ValueError) as exc:
            raise ValueError("Event payload is not JSON-serialisable: %s" % exc)

        if event_id is None:
            event_id = "ev-" + uuid.uuid4().hex
        else:
            event_id = str(event_id)
        when = _iso(self.clock() if at is None else at)

        with self._lock():
            seq = self._max_seq() + 1
            event = {
                "seq": seq,
                "event_id": event_id,
                "type": event_type,
                "at": when,
                "payload": payload,
            }
            line = (json.dumps(event, ensure_ascii=False,
                               separators=(",", ":")) + "\n").encode("utf-8")
            self._write_line(line)
            self._maybe_rotate()
            return event

    def _write_line(self, line):
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        fd = os.open(str(self.events_path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write appending %s" % self.events_path)
                view = view[written:]
            os.fsync(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _maybe_rotate(self):
        try:
            size = self.events_path.stat().st_size
        except OSError:
            return
        if self.max_bytes is not None and size <= self.max_bytes:
            return
        try:
            os.replace(str(self.events_path), str(self.rotated_path))
        except OSError:
            return
        retained = self._retained()
        if retained:
            first = min(retained)
            following = max(retained) + 1
        else:
            first = 1
            following = 1
        self._atomic_json(self.hwm_path, {
            "retained_from": first,
            "next_seq": following,
        })

    def _atomic_json(self, path, data):
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(
            dir=str(self.work_dir), prefix="." + path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def write_result(self, result, status=None, run_id=None):
        """Atomically write ``result.json`` and return its record."""
        record = {
            "schema": EVENT_SCHEMA,
            "run_id": run_id,
            "status": status,
            "result": _redact(_as_text(result)),
            "at": _iso(self.clock()),
        }
        self._atomic_json(self.result_path, record)
        return record

    def read_result(self):
        """Return the stored result record, or ``None`` when unavailable."""
        try:
            data = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, IOError, ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Fenced ATTENTION parsing
# ---------------------------------------------------------------------------

def _normalise_attention(data):
    """Validate an already-decoded attention payload; return normalised dict."""
    if not isinstance(data, dict):
        return None
    question = data.get("question")
    if not isinstance(question, str):
        return None
    question = question.strip()
    if not question or len(question) > _QUESTION_LIMIT:
        return None

    options = data.get("options")
    if not isinstance(options, list):
        return None
    if not (_OPTIONS_MIN <= len(options) <= _OPTIONS_MAX):
        return None
    clean_options = []
    for option in options:
        if not isinstance(option, str):
            return None
        option = option.strip()
        if not option or len(option) > _OPTION_LIMIT:
            return None
        clean_options.append(option)

    evidence = data.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, (str, list, dict)):
            return None
        try:
            if isinstance(evidence, str):
                rendered = evidence
            else:
                rendered = json.dumps(evidence, ensure_ascii=False,
                                      sort_keys=True,
                                      separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        if len(rendered) > _EVIDENCE_LIMIT:
            return None

    return {
        "question": question,
        "options": clean_options,
        "evidence": evidence,
    }


def validate_attention(text):
    """Return the validated question from a single fenced ``attention`` block.

    Only fenced blocks whose info string is ``attention`` (case-insensitive)
    are considered.  Prose outside the fence is never inspected.  Duplicate or
    malformed blocks, and payloads that fail the schema, return ``None``.
    """
    if not isinstance(text, str) or not text:
        return None
    if len(_ATTENTION_OPEN_RE.findall(text)) != 1:
        return None
    match = _ATTENTION_BLOCK_RE.search(text)
    if match is None:
        return None
    body = match.group(1).strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return _normalise_attention(data)


def build_reviewer_attention(payload):
    """Validate an already-decoded reviewer attention payload.

    Returns the normalised ``{"question", "options", "evidence"}`` dict, or
    ``None`` when the payload would break the 500/200/2..8 limits.  The
    harness uses this before persisting or fencing a reviewer question.
    """
    return _normalise_attention(payload)


def record_attention(log, text, source=None):
    """Validate *text* and append one ``needs_input`` event (or return None).

    The same validated question from the same source is not appended twice;
    the harness and the job wrapper can both observe the final answer.
    """
    info = validate_attention(text)
    if info is None:
        return None
    payload = dict(info)
    payload["source"] = source
    try:
        for event in reversed(log.read_since(0)):
            if event.get("type") != "needs_input":
                continue
            prior = event.get("payload") or {}
            normalised = _normalise_attention(prior)
            if normalised == info:
                return event
    except Exception:
        pass
    return log.append("needs_input", payload)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _sorted_events(events):
    if events is None:
        return []
    if isinstance(events, dict):
        events = events.get("events") or []
    try:
        items = [event for event in events if isinstance(event, dict)]
    except TypeError:
        return []
    decorated = []
    for index, event in enumerate(items):
        seq = _as_int(event.get("seq"))
        decorated.append(((0, seq) if seq is not None else (1, index), event))
    decorated.sort(key=lambda pair: pair[0])
    return [event for _key, event in decorated]


def _has_review_evidence(run, events):
    if isinstance(run, dict):
        review_status = str(run.get("review_status") or "").strip().lower()
        if review_status in _REVIEW_EVIDENCE_STATUSES:
            return True
        if run.get("review") or run.get("review_path") or run.get("review_file"):
            return True
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("phase", "name", "stage", "step", "label"):
            value = payload.get(key)
            if isinstance(value, str) and "review" in value.lower():
                return True
    return False


def _pending_question(events):
    pending = None
    for event in events:
        event_type = event.get("type")
        if event_type == "needs_input":
            info = _normalise_attention(event.get("payload") or {})
            if info is not None:
                pending = info
        elif event_type in _CLEARING_EVENTS:
            pending = None
    return pending


def _terminal_status(run, job):
    run_status = _status_of(run)
    job_status = _status_of(job)
    aliases = {"done": "done", "completed": "done", "failed": "error"}
    if run_status in TERMINAL_STATUSES:
        return aliases.get(run_status, run_status)
    if job_status in ("done", "error", "cancelled"):
        return job_status
    if job_status in aliases:
        return aliases[job_status]
    if run_status in aliases:
        return aliases[run_status]
    return None


def _terminal_from_events(events):
    for event in reversed(events):
        event_type = event.get("type")
        if event_type not in _TERMINAL_EVENTS:
            continue
        default = {"result": "ok", "failed": "error",
                   "cancelled": "cancelled"}[event_type]
        payload = event.get("payload")
        status = _status_of(payload) or default
        return status
    return None


def _project_attention(pending, terminal_status):
    if pending is not None:
        return "answer_question"
    if terminal_status is None:
        return "none"
    if terminal_status == "ok_below_gate":
        return "review_result"
    if terminal_status in _FAILURE_ATTENTION:
        return "inspect_failure"
    return "none"


def _next_action(stage, terminal_status, pending, run_id, next_seq_value):
    suffix = " run_id=%s" % run_id if run_id else ""

    if pending is not None:
        return 'Answer with resume_deepseek%s answer="...".' % suffix
    if terminal_status is None:
        return "Poll deepseek_job_status%s events_since=%d." % (
            suffix, next_seq_value)
    if terminal_status == "ok_below_gate":
        return ("Read REVIEW.md, apply the fix list, then resume_deepseek%s."
                % suffix)
    if terminal_status == "budget_exceeded":
        return "Resume with a higher job_max_cost_usd."
    if terminal_status == "deadline_exceeded":
        return "Resume with a higher job_max_seconds."
    if terminal_status == "no_balance":
        return ("Top up the provider account, then start a new "
                "harness_deepseek run.")
    if terminal_status in ("error", "ok_unreviewed"):
        return "Inspect the error, then start a new harness_deepseek run."
    if terminal_status == "cancelled":
        return "No action needed; the run was cancelled."
    if terminal_status in ("ok", "ok_rescued", "done"):
        return "Read the result; no further action is needed."
    return "Read the result."


def append(work_dir, event_type, payload=None, event_id=None, at=None):
    """Module-level convenience wrapper around :meth:`EventLog.append`."""
    return EventLog(work_dir).append(event_type, payload=payload,
                                     event_id=event_id, at=at)


def read_since(work_dir, seq=0):
    """Module-level convenience wrapper around :meth:`EventLog.read_since`."""
    return EventLog(work_dir).read_since(seq)


def write_result(work_dir, result, status=None, run_id=None):
    """Module-level convenience wrapper around :meth:`EventLog.write_result`."""
    return EventLog(work_dir).write_result(result, status=status, run_id=run_id)


def read_result(work_dir):
    """Module-level convenience wrapper around :meth:`EventLog.read_result`."""
    return EventLog(work_dir).read_result()


def pending_question(events):
    """Return the one validated pending question, or ``None``.

    Public wrapper used by the MCP task bridge; a malformed ``needs_input``
    payload is ignored, never turned into prose.
    """
    return _pending_question(_sorted_events(events))


def latest_admitted_run_id(events):
    """Return the run id named by the latest ``admitted`` event, if any."""
    for event in reversed(_sorted_events(events)):
        if event.get("type") == "admitted":
            payload = event.get("payload") or {}
            value = payload.get("run_id")
            return str(value) if value else None
    return None


def terminal_after_latest_admission(events, run_id=None):
    """Return the latest terminal event that belongs to the current attempt.

    An attempt starts at ``admitted``.  A terminal event before the latest
    ``admitted`` is from a superseded attempt and is ignored.  When the latest
    admission names a run id, terminal events with a different ``run_id`` are
    ignored too, so a late writer from an old attempt cannot close the new one.
    Journals with no ``admitted`` event (legacy/manual) keep the old behaviour
    unless an explicit ``run_id`` filter is supplied.
    """
    ordered = _sorted_events(events)
    last_admitted = -1
    admitted_run = None
    for index, event in enumerate(ordered):
        if event.get("type") == "admitted":
            last_admitted = index
            payload = event.get("payload") or {}
            admitted_run = payload.get("run_id") or admitted_run
    effective_run = run_id if run_id is not None else admitted_run
    terminal = None
    for index, event in enumerate(ordered):
        if event.get("type") not in _TERMINAL_EVENTS or index <= last_admitted:
            continue
        payload = event.get("payload") or {}
        payload_run = payload.get("run_id")
        if effective_run and payload_run not in (None, effective_run):
            continue
        terminal = event
    return terminal


def project(run=None, job=None, events=None, result=None):
    """Pure projection of run/job state and journal events.

    Returns a dict with keys ``stage``, ``attention``, ``question``,
    ``options``, ``next_action``, ``terminal`` and ``result``.  It never
    mutates its inputs and never raises on odd input shapes.
    """
    try:
        run = run if isinstance(run, dict) else {}
        job = job if isinstance(job, dict) else {}
        ordered = _sorted_events(events)

        pending = _pending_question(ordered)
        status = _terminal_status(run, job)
        if status is None and not _status_of(run) and not _status_of(job):
            status = _terminal_from_events(ordered)
        # A validated question keeps the work open even when the job handler
        # already returned: the MCP task must not complete until an answer.
        terminal = status is not None and pending is None

        run_status = _status_of(run)
        job_status = _status_of(job)

        if pending is not None:
            stage = "needs_human"
        elif not terminal and _has_review_evidence(run, ordered):
            stage = "review_ready"
        elif (not terminal and job
              and (job_status in ("queued", "pending", "starting")
                   or not job.get("pid"))):
            stage = "queued"
        elif terminal:
            stage = "terminal"
        else:
            stage = "running"

        attention = _project_attention(pending, status)
        question = pending["question"] if pending else None
        options = list(pending["options"]) if pending else []
        # Status-derived follow-up questions are deterministic, never parsed
        # from prose.  They are not the validated answer_question channel: a
        # question still counts as ``answer_question`` only from needs_input.
        if pending is None:
            if status == "ok_below_gate":
                question = "Apply the review fix list and resume the run?"
                options = ["resume", "accept"]
            elif status in ("budget_exceeded", "deadline_exceeded"):
                question = "Resume with a higher limit?"
                options = ["resume_higher_limit", "stop"]
            elif status in _FAILURE_ATTENTION:
                question = "The run did not finish successfully. What next?"
                options = ["resume", "start_new"]

        next_seq_value = 0
        for event in ordered:
            value = _as_int(event.get("seq"))
            if value is not None and value > next_seq_value:
                next_seq_value = value

        run_id = _run_id(run, job)
        next_action = _next_action(stage, status, pending, run_id,
                                   next_seq_value)

        value = result
        if isinstance(value, dict) and "result" in value:
            value = value.get("result")
        if value is None:
            value = run.get("answer")
        if value is None:
            value = job.get("result")

        return {
            "stage": stage,
            "attention": attention,
            "question": question,
            "options": options,
            "next_action": next_action,
            "terminal": bool(terminal),
            "result": value,
        }
    except Exception:
        return {
            "stage": "running",
            "attention": "none",
            "question": None,
            "options": [],
            "next_action": "Poll deepseek_job_status.",
            "terminal": False,
            "result": None,
        }


# ---------------------------------------------------------------------------
# Terminal recording and optional emitters
# ---------------------------------------------------------------------------

def _terminal_event_type(status):
    if status == "cancelled":
        return "cancelled"
    if status in _FAILURE_ATTENTION or status in ("error", "failed"):
        return "failed"
    return "result"


def record_terminal(log, run=None, job=None, text=None):
    """Write ``result.json`` and append one idempotent terminal event.

    A terminal event is appended once per ``(run_id, status, sha256(text))``;
    repeating the call returns the existing event instead of growing the
    journal.
    """
    run = run if isinstance(run, dict) else {}
    job = job if isinstance(job, dict) else {}
    status = _terminal_status(run, job) or "ok"
    run_id = _run_id(run, job)

    body = text
    if body is None:
        body = run.get("answer")
    if body is None:
        body = job.get("result")
    if body is None:
        body = ""
    body_text = _as_text(body)

    # Result file first, then the journal event (crash-safe ordering).
    log.write_result(body_text, status=status, run_id=run_id or None)

    event_type = _terminal_event_type(status)
    fingerprint = _digest(body_text)
    for event in log.read_since(0):
        if event.get("type") != event_type:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if (payload.get("run_id") == (run_id or None)
                and payload.get("status") == status
                and payload.get("text_sha256") == fingerprint):
            return event

    return log.append(event_type, {
        "status": status,
        "run_id": run_id or None,
        "text_sha256": fingerprint,
    })


def emit_phase(log, run, phase):
    """Optional helper: append a ``phase`` event for *run*."""
    return log.append("phase", {
        "phase": str(phase),
        "run_id": _run_id(run, None) or None,
    })


def emit_job_started(log, spec):
    """Optional helper: append an ``admitted`` event for a job spec."""
    spec = spec if isinstance(spec, dict) else {}
    return log.append("admitted", {
        "run_id": spec.get("run_id") or spec.get("id"),
        "tool": spec.get("tool"),
    })
