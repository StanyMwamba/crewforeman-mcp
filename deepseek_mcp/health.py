"""Failure classification, bounded retry policy and component health (P2-2).

The retry policy is deliberately small: a phase gets a fixed attempt count,
exponential backoff with jitter capped at 60 seconds, and an immediate stop for
classes that a retry cannot fix (auth, no balance, prepare, budget, deadline,
cancel).  A writing phase is retried only when a workspace snapshot proves it
did not already change files outside allowed evidence.

``HealthBoard`` is one JSON file under ``<state_dir>/health.json`` written under
an O_EXCL lock.  A component becomes ``error``/quarantined only after three
consecutive failures; any success resets it.  Health is separate from run
records, so completed runs remain readable while a component is quarantined.

Python 3.9 compatible; standard library only.
"""

import contextlib
import datetime as dt
import email.utils
import json
import os
import random
import re
import time
from pathlib import Path

HEALTH_VERSION = 1
HEALTH_FILENAME = "health.json"
LOCK_FILENAME = "health.lock"
DEFAULT_THRESHOLD = 3
DEFAULT_CAP_SECONDS = 60.0
_LOCK_TIMEOUT = 5.0
_LOCK_STALE_AFTER = 60.0


class FailureClass:
    """Stable string vocabulary shared by logs, retries and the status view."""

    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PROVIDER_5XX = "provider_5xx"
    AUTH = "auth"
    NO_BALANCE = "no_balance"
    BUDGET = "budget"
    DEADLINE = "deadline"
    STALL = "stall"
    PREPARE = "prepare"
    REVIEWER = "reviewer"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    ALL = (RATE_LIMIT, NETWORK, PROVIDER_5XX, AUTH, NO_BALANCE, BUDGET,
           DEADLINE, STALL, PREPARE, REVIEWER, CANCELLED, UNKNOWN)


_NON_RETRYABLE = frozenset((
    FailureClass.AUTH, FailureClass.NO_BALANCE, FailureClass.PREPARE,
    FailureClass.BUDGET, FailureClass.DEADLINE, FailureClass.CANCELLED,
))

_NO_BALANCE_RE = re.compile(
    r"(?i)insufficient\s+balance|no\s+balance|out\s+of\s+credit|"
    r"payment\s+required|top\s*up|402")
_AUTH_RE = re.compile(
    r"(?i)unauthori[sz]ed|invalid\s+api\s*key|authentication|"
    r"permission\s+denied|forbidden|401|403")
_RATE_LIMIT_RE = re.compile(
    r"(?i)rate[\s_-]*limit|too\s+many\s+requests|429|retry-after")
_DEADLINE_RE = re.compile(
    r"(?i)deadline|time\s+limit|timed\s+out\s+after|whole-job\s+deadline|"
    r"deadline_exceeded")
_BUDGET_RE = re.compile(
    r"(?i)budget|cost\s+ceiling|cost_exceeded|over\s+budget")
_STALL_RE = re.compile(r"(?i)stall|no\s+progress|watchdog")
_PROVIDER_RE = re.compile(
    r"(?i)internal\s+server\s+error|bad\s+gateway|service\s+unavailable|"
    r"gateway\s+time-?out|overloaded|\b5\d\d\b")
_NETWORK_RE = re.compile(
    r"(?i)econnreset|econnrefused|socket\s+hang\s*up|connection\s+reset|"
    r"connection\s+refused|network|dns|temporarily\s+unavailable|"
    r"fetch\s+failed|broken\s+pipe|no\s+route\s+to\s+host")
_PREPARE_RE = re.compile(
    r"(?i)prepare|preflight|not\s+found\s+or\s+not\s+executable|dsh_bin|"
    r"could\s+not\s+start\s+harness")
_REVIEWER_RE = re.compile(r"(?i)reviewer|review\s+failed|independent\s+review")
_CANCEL_RE = re.compile(r"(?i)cancell?ed|canceled|cancel\s+requested")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return str(value)
    except Exception:
        return ""


def classify(text, exit_code=None):
    """Return one :class:`FailureClass` value for an error string/exit code."""
    body = _text(text)
    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 124:
        return FailureClass.DEADLINE
    if _CANCEL_RE.search(body):
        return FailureClass.CANCELLED
    if _NO_BALANCE_RE.search(body) or code == 402:
        return FailureClass.NO_BALANCE
    if _AUTH_RE.search(body) or code in (401, 403):
        return FailureClass.AUTH
    if _RATE_LIMIT_RE.search(body) or code == 429:
        return FailureClass.RATE_LIMIT
    if _DEADLINE_RE.search(body):
        return FailureClass.DEADLINE
    if _BUDGET_RE.search(body):
        return FailureClass.BUDGET
    if _STALL_RE.search(body):
        return FailureClass.STALL
    if _PROVIDER_RE.search(body) or (code is not None and 500 <= code <= 599):
        return FailureClass.PROVIDER_5XX
    if _NETWORK_RE.search(body):
        return FailureClass.NETWORK
    if _PREPARE_RE.search(body):
        return FailureClass.PREPARE
    if _REVIEWER_RE.search(body):
        return FailureClass.REVIEWER
    return FailureClass.UNKNOWN


def retry_after(text):
    """Parse a Retry-After value from *text*; seconds or an HTTP date."""
    body = _text(text)
    match = re.search(
        r"(?i)retry[\s_-]*after\s*[=:]?\s*([0-9]+(?:\.[0-9]+)?)", body)
    if match:
        try:
            return max(0.0, min(DEFAULT_CAP_SECONDS, float(match.group(1))))
        except ValueError:
            return None
    match = re.search(
        r"(?i)retry[\s_-]*after\s*[=:]?\s*([A-Z][a-z]{2},?\s+[0-9]{1,2}\s+"
        r"[A-Z][a-z]{2}\s+[0-9]{4}\s+[0-9:]{8}\s+GMT)", body)
    if match:
        try:
            when = email.utils.parsedate_to_datetime(match.group(1))
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                delta = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
                return max(0.0, min(DEFAULT_CAP_SECONDS, delta))
        except Exception:
            return None
    return None


def classify_execution(execution):
    """Classify one ``_execute_once`` result dict."""
    execution = execution if isinstance(execution, dict) else {}
    if execution.get("stalled"):
        return FailureClass.STALL
    if execution.get("timed_out"):
        return FailureClass.DEADLINE
    code = execution.get("exit_code")
    body = " ".join(str(execution.get(key) or "") for key in
                    ("error", "stop_reason", "stderr_tail"))
    label = execution.get("label") or ""
    result = classify(body, code)
    reviewer_failed = (execution.get("status") != "ok"
                       or execution.get("stop_reason") not in (None, "", "ok"))
    if ("reviewer" in str(label).lower()
            and reviewer_failed
            and result == FailureClass.UNKNOWN):
        # A reviewer process that failed for an unclassified reason is still a
        # reviewer failure; it is retryable (unlike a builder unknown).
        return FailureClass.REVIEWER
    return result


class RetryPolicy:
    """A phase-specific, budget-aware retry decision."""

    ATTEMPTS = {"builder": 2, "reviewer": 2, "revision": 1, "rescue": 1}
    HARD_STOP = _NON_RETRYABLE

    def __init__(self, phase, max_attempts=None, base=1.0, cap=DEFAULT_CAP_SECONDS,
                 jitter=0.25, rng=None, sleeper=time.sleep):
        self.phase = str(phase or "").strip().lower() or "builder"
        if self.phase.startswith("review"):
            self.phase = "reviewer"
        if max_attempts is None:
            max_attempts = self.ATTEMPTS.get(self.phase, 1)
        try:
            self.max_attempts = max(1, int(max_attempts))
        except (TypeError, ValueError):
            self.max_attempts = 1
        try:
            self.base = max(0.0, float(base))
        except (TypeError, ValueError):
            self.base = 1.0
        try:
            self.cap = max(0.0, float(cap))
        except (TypeError, ValueError):
            self.cap = DEFAULT_CAP_SECONDS
        try:
            self.jitter = max(0.0, float(jitter))
        except (TypeError, ValueError):
            self.jitter = 0.0
        self.rng = rng or random.Random()
        self.sleeper = sleeper

    @property
    def attempts(self):
        return self.max_attempts

    def should_retry(self, attempt, failure_class, remaining_seconds=None,
                     spent=None, ceiling=None, cost_known=True):
        """Return ``(allowed, reason)``; the reason is safe to log."""
        try:
            attempt = int(attempt)
        except (TypeError, ValueError):
            attempt = 1
        failure_class = str(failure_class or FailureClass.UNKNOWN)
        if failure_class in self.HARD_STOP:
            return False, "non-retryable: %s" % failure_class
        if failure_class == FailureClass.UNKNOWN:
            return False, "unclassified failure"
        if attempt >= self.max_attempts:
            return False, "attempts exhausted (%d/%d)" % (
                attempt, self.max_attempts)
        if remaining_seconds is not None:
            try:
                if float(remaining_seconds) <= 0:
                    return False, "whole-job deadline exhausted"
            except (TypeError, ValueError):
                pass
        if ceiling is not None:
            if not cost_known or spent is None:
                return False, "cost budget unknown under a whole-job ceiling"
            try:
                if float(spent) >= float(ceiling):
                    return False, "whole-job cost budget ceiling reached"
            except (TypeError, ValueError):
                pass
        return True, "retryable: %s" % failure_class

    def delay(self, attempt, retry_after=None):
        """Exponential backoff (capped) plus jitter; honour Retry-After.

        The 60-second cap applies to Retry-After too: a longer provider hint is
        clamped rather than sleeping for an hour, which would exceed the phase
        and whole-job deadlines anyway.
        """
        try:
            attempt = max(1, int(attempt))
        except (TypeError, ValueError):
            attempt = 1
        try:
            value = self.base * (2 ** (attempt - 1))
        except OverflowError:
            value = self.cap
        value = min(self.cap, value)
        try:
            if retry_after is not None:
                value = max(value, float(retry_after))
        except (TypeError, ValueError):
            pass
        value = min(self.cap, max(0.0, value))
        if self.jitter:
            try:
                value += self.rng.uniform(0.0, self.jitter)
            except Exception:
                pass
        return round(min(self.cap, max(0.0, value)), 3)

    def sleep(self, seconds):
        try:
            if seconds and seconds > 0:
                self.sleeper(seconds)
        except Exception:
            pass


def side_effect_guard(workspace, before, phase="", writing=True,
                      allowed=("REVIEW.md", ".browser-evidence/",
                               "EVIDENCE.json")):
    """Check a writing phase did not change files before a retry.

    Returns ``(allowed, changes)``.  A read-only phase needs no guard (nothing
    could be written); a writing phase with any non-evidence change is refused.
    """
    if not writing:
        return True, []
    try:
        from . import safety
    except Exception:  # pragma: no cover - only a broken install hits this
        return False, [{"path": "<snapshot unavailable>", "change": "unknown"}]
    try:
        after = safety.snapshot_workspace(workspace)
        changes = safety.diff_workspace(before or {}, after, allowed=allowed)
    except Exception:
        return False, [{"path": "<snapshot error>", "change": "unknown"}]
    return (not changes), changes


def _iso(now=None):
    if isinstance(now, dt.datetime):
        return (now if now.tzinfo else now.astimezone()).isoformat(
            timespec="seconds")
    if isinstance(now, (int, float)):
        return dt.datetime.fromtimestamp(float(now)).astimezone().isoformat(
            timespec="seconds")
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _state_dir(value):
    if value is None or not str(value).strip():
        value = "~/DeepSeek-MCP/state"
    return Path(os.path.expanduser(str(value)))


class HealthBoard:
    """Per-component consecutive-failure health, shared across processes."""

    def __init__(self, state_dir, threshold=DEFAULT_THRESHOLD, clock=None):
        self.state_dir = _state_dir(state_dir)
        self.path = self.state_dir / HEALTH_FILENAME
        self.lock_path = self.state_dir / LOCK_FILENAME
        try:
            self.threshold = max(1, int(threshold))
        except (TypeError, ValueError):
            self.threshold = DEFAULT_THRESHOLD
        self.clock = clock or time.time

    # -- locking ---------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(str(self.state_dir), 0o700)
        except OSError:
            pass
        deadline = time.time() + _LOCK_TIMEOUT
        fd = None
        owned = False
        while True:
            try:
                fd = os.open(str(self.lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                owned = True
                break
            except FileExistsError:
                stale = False
                try:
                    stale = (time.time() - self.lock_path.stat().st_mtime
                             > _LOCK_STALE_AFTER)
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    raise TimeoutError("Timed out locking %s" % self.path)
                time.sleep(0.01)
            except OSError as exc:
                raise TimeoutError(
                    "cannot create health lock %s: %s" % (self.lock_path, exc))
        try:
            if fd is not None:
                try:
                    os.write(fd, json.dumps({"pid": os.getpid()}).encode())
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if owned:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass

    # -- state -----------------------------------------------------------
    def _blank(self):
        return {"version": HEALTH_VERSION, "updated": _iso(self.clock()),
                "components": {}}

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._blank()
        except Exception as exc:
            # A corrupt health file must not silently look healthy: report it
            # as quarantined until an operator clears it.
            broken = self._blank()
            broken["corrupt"] = True
            broken["error"] = str(exc)
            return broken
        if not isinstance(data, dict) or not isinstance(
                data.get("components"), dict):
            broken = self._blank()
            broken["corrupt"] = True
            broken["error"] = "health.json is not a component object"
            return broken
        data.setdefault("version", HEALTH_VERSION)
        return data

    def _write(self, data):
        data["version"] = HEALTH_VERSION
        data["updated"] = _iso(self.clock())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=True, sort_keys=True,
                             indent=2).encode("utf-8")
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=".health-", suffix=".tmp",
                                   dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, str(self.path))
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
        try:
            os.chmod(str(self.path), 0o600)
        except OSError:
            pass

    @staticmethod
    def _entry(data, component):
        components = data.setdefault("components", {})
        entry = components.get(component)
        if not isinstance(entry, dict):
            entry = {
                "state": "ok",
                "consecutive_failures": 0,
                "last_class": None,
                "last_error": None,
                "last_error_at": None,
                "retry_after": None,
                "last_success_at": None,
                "last_retry_at": None,
                "quarantined": False,
                "retries": [],
            }
            components[component] = entry
        entry.setdefault("retries", [])
        return entry

    def record_success(self, component):
        component = str(component)
        try:
            with self._locked():
                data = self.load()
                entry = self._entry(data, component)
                entry.update(state="ok", consecutive_failures=0,
                             quarantined=False,
                             last_success_at=_iso(self.clock()),
                             last_class=None, last_error=None,
                             retry_after=None)
                self._write(data)
                return dict(entry)
        except Exception:
            return {"state": "ok", "consecutive_failures": 0,
                    "quarantined": False, "component": component,
                    "record_error": "health board unavailable"}

    def record_failure(self, component, failure_class, error=None,
                       retry_after=None, now=None):
        component = str(component)
        failure_class = str(failure_class or FailureClass.UNKNOWN)
        try:
            with self._locked():
                data = self.load()
                entry = self._entry(data, component)
                previous = int(entry.get("consecutive_failures") or 0)
                failures = previous + 1
                entry.update(
                    consecutive_failures=failures,
                    last_class=failure_class,
                    last_error=_text(error)[:1000] or None,
                    last_error_at=_iso(self.clock() if now is None else now),
                    retry_after=retry_after,
                    last_retry_at=_iso(self.clock() if now is None else now),
                )
                entry.setdefault("retries", [])
                # ``retries`` records retry *scheduling* timestamps (see
                # record_retry); a failure itself is tracked by the failure
                # counters above.
                if failures >= self.threshold:
                    entry["state"] = "error"
                    entry["quarantined"] = True
                    entry["quarantined_at"] = entry["last_error_at"]
                else:
                    # "error" is reserved for the third consecutive failure.
                    entry["state"] = "degraded" if failures else "ok"
                self._write(data)
                result = dict(entry)
                result["component"] = component
                if entry.get("quarantined"):
                    result["attention"] = structured_attention(
                        component, entry)
                return result
        except Exception:
            return {"component": component, "state": "error",
                    "consecutive_failures": 1, "quarantined": False,
                    "last_class": failure_class,
                    "record_error": "health board unavailable"}

    def record_retry(self, component, failure_class, error=None,
                     retry_after=None, now=None):
        """Record that a retry was scheduled without adding a failure strike."""
        component = str(component)
        failure_class = str(failure_class or FailureClass.UNKNOWN)
        try:
            with self._locked():
                data = self.load()
                entry = self._entry(data, component)
                when = _iso(self.clock() if now is None else now)
                entry.update(last_class=failure_class,
                             last_retry_at=when,
                             retry_after=retry_after)
                if error:
                    entry["last_error"] = _text(error)[:1000]
                entry["retries"] = (entry.get("retries") or []) + [when]
                entry["retries"] = entry["retries"][-20:]
                self._write(data)
                return dict(entry)
        except Exception:
            return {"component": component, "state": "degraded",
                    "consecutive_failures": 0, "quarantined": False,
                    "record_error": "health board unavailable"}

    def is_quarantined(self, component):
        data = self.load()
        if data.get("corrupt"):
            return True
        entry = (data.get("components") or {}).get(str(component)) or {}
        return bool(entry.get("quarantined"))

    def quarantine_block(self, component):
        data = self.load()
        if data.get("corrupt"):
            return (
                "health.json is corrupt (%s); new automatic phases are "
                "blocked. Clear it with deepseek_maintenance "
                "action=clear_health." % (data.get("error") or "unreadable"))
        entry = (data.get("components") or {}).get(str(component)) or {}
        if not entry.get("quarantined"):
            return None
        return (
            "%s is quarantined after %s consecutive failure(s) "
            "(last: %s). Completed runs are still readable; clear it with "
            "deepseek_maintenance action=clear_health%s."
            % (component, entry.get("consecutive_failures"),
               entry.get("last_class") or "unknown",
               (" component=%s" % component) if component else ""))

    def clear(self, component=None):
        try:
            with self._locked():
                data = self.load()
                if data.get("corrupt"):
                    # A corrupt file has no trustworthy components to edit.
                    # Reset it to a clean board so clear_health really clears
                    # the quarantine it reports.
                    data = self._blank()
                    self._write(data)
                    return 1
                components = data.get("components") or {}
                names = ([str(component)] if component
                         else list(components))
                cleared = 0
                for name in names:
                    entry = components.get(name)
                    if isinstance(entry, dict) and entry.get("quarantined"):
                        cleared += 1
                    if isinstance(entry, dict):
                        entry.update(state="ok", consecutive_failures=0,
                                     quarantined=False, last_class=None,
                                     retry_after=None)
                self._write(data)
                return cleared
        except Exception:
            return 0

    def snapshot(self):
        return self.load()

    def status_lines(self):
        data = self.load()
        if data.get("corrupt"):
            return ["Health (%s): CORRUPT (%s); clear with "
                    "deepseek_maintenance action=clear_health."
                    % (self.path, data.get("error") or "unreadable")]
        components = data.get("components") or {}
        if not components:
            return ["Health: no recorded component failures."]
        lines = ["Health (%s):" % self.path]
        for name in sorted(components):
            entry = components[name] or {}
            state = "quarantined" if entry.get("quarantined") else (
                entry.get("state") or "ok")
            detail = "%s=%s failures=%s class=%s" % (
                name, state, entry.get("consecutive_failures") or 0,
                entry.get("last_class") or "none")
            if entry.get("retry_after") is not None:
                detail += " retry_after=%ss" % entry["retry_after"]
            if entry.get("last_success_at"):
                detail += " last_success=%s" % entry["last_success_at"]
            lines.append("  - " + detail)
        return lines


def structured_attention(component, entry=None, run_id=None, message=None):
    """Payload for a ``progress`` journal event carrying structured attention.

    The P1 event vocabulary is frozen (a P1 test asserts the exact tuple), so
    health attention travels as a ``progress`` event with ``kind=attention``.
    """
    entry = entry or {}
    body = {
        "kind": "attention",
        "attention": "health_quarantine",
        "component": str(component),
        "failure_class": entry.get("last_class"),
        "consecutive_failures": entry.get("consecutive_failures"),
        "message": message or (
            "%s is quarantined; new automatic phases are blocked until "
            "clear_health" % component),
    }
    if run_id:
        body["run_id"] = run_id
    return body
