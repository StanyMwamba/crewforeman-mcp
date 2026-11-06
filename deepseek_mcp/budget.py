"""Whole-job time and cost budget across every Harness phase.

The old per-process limits let a builder, reviewer, revisions and a rescue each
consume a full budget. This object is the single clock/budget the run loop
consults before starting any phase and before a retry.

Python 3.9 compatible; standard library only.
"""

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path


def _seconds(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _money(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class JobBudget:
    """A monotonic deadline plus an optional estimated-cost ceiling."""

    def __init__(self, max_seconds=None, max_cost_usd=None, started=None,
                 clock=None):
        self.max_seconds = _seconds(max_seconds)
        self.max_cost_usd = _money(max_cost_usd)
        self._clock = clock or time.monotonic
        self._started = self._clock() if started is None else float(started)
        self.stop_reason = None

    def elapsed(self):
        return round(self._clock() - self._started, 1)

    def remaining_seconds(self):
        if self.max_seconds is None:
            return None
        return round(max(0.0, self.max_seconds - (self._clock() - self._started)), 1)

    def time_exceeded(self):
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def cost_exceeded(self, spent, cost_known=True):
        if self.max_cost_usd is None:
            return False
        if not cost_known or spent is None:
            # An unpriced run cannot be proven under budget. Stop rather than
            # silently overspend; the caller records why.
            return True
        return float(spent) >= self.max_cost_usd

    def phase_limit(self, requested=None):
        """Cap one process to the remaining whole-job time (never zero-kill)."""
        remaining = self.remaining_seconds()
        if remaining is None:
            return requested
        if remaining <= 0:
            return 0.0
        if requested is None:
            return remaining
        try:
            requested = float(requested)
        except (TypeError, ValueError):
            return remaining
        if requested <= 0:
            return remaining
        return round(min(requested, remaining), 1)

    def snapshot(self, spent=None, cost_known=True):
        remaining = self.remaining_seconds()
        return {
            "max_seconds": self.max_seconds,
            "max_cost_usd": self.max_cost_usd,
            "elapsed_seconds": self.elapsed(),
            "remaining_seconds": remaining,
            "cost_usd": None if spent is None else round(float(spent), 6),
            "cost_known": bool(cost_known),
            "time_exceeded": self.time_exceeded(),
            "cost_exceeded": self.cost_exceeded(spent, cost_known),
            "stop_reason": self.stop_reason,
        }


# ---------------------------------------------------------------------------
# P2-3: cost reservation ledger
# ---------------------------------------------------------------------------

LEDGER_FILENAME = "cost-ledger.json"
LEDGER_LOCK = "cost-ledger.lock"
_LOCK_TIMEOUT = 5.0
_LOCK_STALE_AFTER = 60.0


class BudgetExceeded(RuntimeError):
    """A reservation would take the job or the day over its ceiling."""


class CostUnknown(RuntimeError):
    """A paid phase cannot be priced, so it cannot be admitted under a ceiling."""


class LedgerCorrupt(RuntimeError):
    """The reservation ledger exists but cannot be trusted; fail closed."""


# Conservative phase assumptions. They are deliberately pessimistic: reserving
# too much only delays work, while reserving too little breaks the ceiling.
_TOKENS_IN_PER_MINUTE = 20000
_TOKENS_OUT_PER_MINUTE = 4000
_PHASE_FACTOR = {
    "builder": 1.0,
    "live-wave": 1.0,
    "reviewer": 0.55,
    "review": 0.55,
    "revision": 0.8,
    "rescue": 0.5,
    "finisher": 0.5,
}


def _today(now=None):
    if isinstance(now, dt.datetime):
        return now.astimezone().strftime("%Y-%m-%d")
    if isinstance(now, (int, float)):
        return dt.datetime.fromtimestamp(float(now)).astimezone().strftime(
            "%Y-%m-%d")
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d")


def _operation_suffix(run_id, operation):
    """Stable committed-row suffix for one operation id (no borrowing)."""
    return hashlib.sha256(
        ("%s\0%s" % (run_id, operation)).encode("utf-8")).hexdigest()[:24]


def _iso(now=None):
    if isinstance(now, dt.datetime):
        value = now
        if value.tzinfo is None:
            value = value.astimezone()
        return value.isoformat(timespec="seconds")
    if isinstance(now, (int, float)):
        return dt.datetime.fromtimestamp(float(now)).astimezone().isoformat(
            timespec="seconds")
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _complete_price(value):
    """A price row is usable only when all three rates are finite numbers."""
    if not isinstance(value, dict):
        return False
    for key in ("hit", "miss", "out"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return False
        if not math.isfinite(float(number)) or float(number) < 0:
            return False
    return True


def _prices_for(model, prices=None):
    table = prices or _default_prices()
    if not isinstance(table, dict):
        return None, str(model or "unknown")
    if model in table:
        values = table[model]
        # A partial mapping must be "unknown", never silently priced as $0 for
        # the missing rate: that would under-reserve and admit an overspend.
        return (values, model) if _complete_price(values) else (
            None, str(model or "unknown"))
    for known, values in table.items():
        if model and str(model).startswith(str(known)):
            return (values, known) if _complete_price(values) else (
                None, known)
    return None, str(model or "unknown")


def _default_prices():
    try:
        from . import api
        return api.HARNESS_PRICES
    except Exception:  # pragma: no cover - standalone import
        return {}


def estimate_phase_cost(model, phase, agent_count=1, max_seconds=None,
                        prices=None):
    """Conservative pre-phase estimate from the local price table.

    Returns ``{"cost_usd", "cost_known", "basis", "model", "phase",
    "agents"}``.  ``cost_known`` is false when the model has no price; the
    caller must then refuse a paid phase that has a ceiling.
    """
    phase_key = str(phase or "builder").strip().lower()
    factor = _PHASE_FACTOR.get(phase_key, 1.0)
    try:
        agents = max(1, int(agent_count or 1))
    except (TypeError, ValueError):
        agents = 1
    seconds = _seconds(max_seconds) or 600.0
    seconds = max(60.0, seconds)
    table, resolved = _prices_for(model, prices)
    basis = {
        "minutes": round(seconds / 60.0, 3),
        "agents": agents,
        "input_tokens_per_agent": int(_TOKENS_IN_PER_MINUTE * seconds / 60.0),
        "cache_read_tokens_per_agent": int(
            _TOKENS_IN_PER_MINUTE * seconds / 60.0 * 0.5),
        "output_tokens_per_agent": int(
            _TOKENS_OUT_PER_MINUTE * seconds / 60.0),
        "phase_factor": factor,
        "prices": table,
    }
    if not table:
        return {
            "cost_usd": None,
            "cost_known": False,
            "basis": basis,
            "model": resolved,
            "phase": phase_key,
            "agents": agents,
        }
    minutes = seconds / 60.0
    input_tokens = _TOKENS_IN_PER_MINUTE * minutes * agents
    cache_tokens = input_tokens * 0.5
    output_tokens = _TOKENS_OUT_PER_MINUTE * minutes * agents
    cost = (
        input_tokens * float(table.get("miss") or 0.0)
        + cache_tokens * float(table.get("hit") or 0.0)
        + output_tokens * float(table.get("out") or 0.0)
    ) / 1000000.0 * factor
    return {
        "cost_usd": round(cost, 6),
        "cost_known": True,
        "basis": basis,
        "model": resolved,
        "phase": phase_key,
        "agents": agents,
    }


class CostLedger:
    """Filesystem-backed reservations shared by concurrent jobs.

    Daily admission is ``settled_today + active_reservations_today + estimate``.
    Settled costs normally also land in the append-only spend ledger when the
    run is saved; callers pass their current settled total as ``spent_today`` to
    avoid double counting.
    """

    def __init__(self, state_dir, clock=None, ttl=900):
        raw = state_dir if state_dir is not None else "~/DeepSeek-MCP/state"
        self.state_dir = Path(os.path.expanduser(str(raw)))
        self.path = self.state_dir / LEDGER_FILENAME
        self.lock_path = self.state_dir / LEDGER_LOCK
        self.clock = clock or _epoch
        try:
            self.ttl = max(1, int(ttl))
        except (TypeError, ValueError):
            self.ttl = 900

    # -- locking ---------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(str(self.state_dir), 0o700)
        except OSError:
            pass
        deadline = _epoch() + _LOCK_TIMEOUT
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
                    stale = (_epoch() - self.lock_path.stat().st_mtime
                             > _LOCK_STALE_AFTER)
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if _epoch() >= deadline:
                    raise TimeoutError("Timed out locking %s" % self.path)
                _sleep(0.01)
            except OSError as exc:
                raise TimeoutError(
                    "cannot create cost-ledger lock %s: %s" % (self.lock_path, exc))
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
        return {"version": 1, "updated": _iso(self.clock()),
                "reservations": {}, "committed": {}}

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._blank()
        except Exception as exc:
            raise LedgerCorrupt(
                "cannot read cost ledger %s: %s" % (self.path, exc))
        if not isinstance(data, dict):
            raise LedgerCorrupt("cost ledger %s is not an object" % self.path)
        if not isinstance(data.get("reservations"), dict):
            data["reservations"] = {}
        if not isinstance(data.get("committed"), dict):
            data["committed"] = {}
        return data

    def _write(self, data):
        data["version"] = 1
        data["updated"] = _iso(self.clock())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=True, sort_keys=True,
                             indent=2).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".cost-", suffix=".tmp",
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
    def _active(reservations, now_ts):
        active = {}
        for key, item in (reservations or {}).items():
            if not isinstance(item, dict):
                continue
            try:
                expires = float(item.get("expires_ts") or 0)
            except (TypeError, ValueError):
                expires = 0
            if expires and expires <= now_ts:
                continue
            active[key] = item
        return active

    def reclaim(self, now=None):
        """Drop reservations whose lease expired; return the number removed."""
        now_ts = float(now) if isinstance(now, (int, float)) else self.clock()
        with self._locked():
            data = self.load()
            reservations = data.get("reservations") or {}
            active = self._active(reservations, now_ts)
            removed = len(reservations) - len(active)
            if removed:
                data["reservations"] = active
                self._write(data)
            return removed

    # -- admission -------------------------------------------------------
    def reserve(self, run_id, phase, estimate, ttl=None, cost_known=True,
                job_limit=None, daily_limit=None, spent_today=0.0, now=None,
                allow_overrun=False, settled_run_ids=None):
        """Reserve an estimate; raise when a ceiling would be exceeded.

        ``spent_today`` is settled spend already recorded elsewhere today.
        ``allow_overrun`` is used only for a run's *first* paid phase to keep
        the 3.8.0 promise that the already-started builder is not refused by a
        ceiling it may finish under; the oversized estimate is still recorded,
        so a concurrent job sees it and every later phase is admitted strictly.
        An unknown cost is never allowed over a ceiling.
        """
        run_id = str(run_id)
        phase = str(phase or "builder")
        cost_known = bool(cost_known) and estimate is not None
        try:
            value = float(estimate) if estimate is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        if value < 0:
            value = 0.0
        job_limit = _money(job_limit)
        daily_limit = _money(daily_limit)
        now_ts = self.clock()
        if isinstance(now, (int, float)):
            now_ts = float(now)
        day = _today(now if now is not None else now_ts)
        lease = self.ttl if ttl is None else max(1, int(ttl))
        base_spent = _call_spent(spent_today, now)
        with self._locked():
            data = self.load()
            reservations = self._active(data.get("reservations") or {},
                                        now_ts)
            data["reservations"] = reservations
            if (job_limit is not None or daily_limit is not None) and not cost_known:
                self._write(data)
                raise CostUnknown(
                    "cannot price phase %s for run %s; a cost ceiling is set, "
                    "so the phase is refused" % (phase, run_id))
            run_reserved = sum(
                float(item.get("estimate") or 0.0)
                for item in reservations.values()
                if str(item.get("run_id")) == run_id)
            run_committed = sum(
                float(item.get("actual") or 0.0)
                for item in (data.get("committed") or {}).values()
                if str(item.get("run_id")) == run_id)
            run_unknown = any(
                not bool(item.get("cost_known"))
                for item in list((data.get("committed") or {}).values())
                + list(reservations.values())
                if str(item.get("run_id")) == run_id)
            if (job_limit is not None or daily_limit is not None) and run_unknown:
                self._write(data)
                raise CostUnknown(
                    "run %s already has an unknown committed or reserved "
                    "cost; no paid phase can be admitted under a ceiling"
                    % run_id)
            over_limit = (
                job_limit is not None
                and run_committed + run_reserved + value > job_limit + 1e-9)
            if over_limit and not (allow_overrun and run_committed <= 0
                                   and run_reserved <= 0):
                self._write(data)
                raise BudgetExceeded(
                    "job ceiling %.6f would be exceeded: committed=%.6f "
                    "reserved=%.6f estimate=%.6f"
                    % (job_limit, run_committed, run_reserved, value))
            if daily_limit is not None:
                daily_reserved = sum(
                    float(item.get("estimate") or 0.0)
                    for item in reservations.values()
                    if str(item.get("date")) == day)
                # ``reflected`` is set by store.save_run only after the run's
                # cost reached the spend ledger. Per-row (not per-run) so a
                # later phase of the same run is still counted.
                daily_rows = [
                    item for item in (data.get("committed") or {}).values()
                    if str(item.get("date")) == day
                    and not bool(item.get("reflected"))]
                daily_committed = sum(
                    float(item.get("actual") or 0.0) for item in daily_rows)
                daily_active = [
                    item for item in reservations.values()
                    if str(item.get("date")) == day]
                if any(not bool(item.get("cost_known"))
                       for item in daily_rows + daily_active):
                    self._write(data)
                    raise CostUnknown(
                        "today's committed or reserved ledger contains an "
                        "unknown cost; no paid phase can be admitted under a "
                        "daily ceiling")
                if (base_spent + daily_reserved + daily_committed + value
                        > daily_limit + 1e-9):
                    self._write(data)
                    raise BudgetExceeded(
                        "daily ceiling %.6f would be exceeded: spent=%.6f "
                        "reserved_today=%.6f estimate=%.6f"
                        % (daily_limit, base_spent, daily_reserved, value))
            reservation = {
                "id": "res-" + uuid.uuid4().hex[:12],
                "run_id": run_id,
                "phase": phase,
                "estimate": (None if estimate is None else round(value, 6)),
                "cost_known": cost_known,
                "created": _iso(now_ts),
                "created_ts": now_ts,
                "expires_ts": now_ts + lease,
                "date": day,
            }
            reservation["overrun"] = bool(
                over_limit and allow_overrun and run_committed <= 0
                and run_reserved <= 0)
            reservations[reservation["id"]] = reservation
            data["reservations"] = reservations
            self._write(data)
            return dict(reservation)

    def record_committed(self, run_id, actual, phase="prior",
                         cost_known=True, now=None):
        """Seed a settled cost that predates this attempt (resume/restart).

        Keyed by run id + phase so a repeated call cannot double-count.
        """
        run_id = str(run_id)
        now_ts = float(now) if isinstance(now, (int, float)) else self.clock()
        try:
            value = float(actual) if actual is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        cost_known = bool(cost_known) and actual is not None
        entry_id = "com-prior-%s-%s" % (run_id, str(phase or "prior"))
        with self._locked():
            data = self.load()
            committed = data.get("committed") or {}
            entry = {
                "id": entry_id,
                "run_id": run_id,
                "phase": str(phase or "prior"),
                "actual": round(max(0.0, value), 6),
                "cost_known": cost_known,
                "settled_at": _iso(now_ts),
                "date": _today(now_ts),
            }
            committed[entry_id] = entry
            data["committed"] = committed
            self._write(data)
            return dict(entry)

    def mark_reflected(self, run_id, total=None, now=None):
        """Mark committed rows whose cost reached the spend ledger.

        ``total`` is the run's recorded cost at this save. Only rows covered by
        that total are marked, oldest first, so a row committed just before the
        save but not included in the total stays counted. ``None`` preserves
        the old all-rows behaviour for callers that cannot supply a total.
        """
        run_id = str(run_id)
        try:
            with self._locked():
                data = self.load()
                committed = data.get("committed") or {}
                rows = [item for item in committed.values()
                        if str(item.get("run_id")) == run_id]
                rows.sort(key=lambda item: (str(item.get("settled_at") or ""),
                                            str(item.get("id") or "")))
                budget = None
                if total is not None:
                    try:
                        already = sum(float(item.get("actual") or 0.0)
                                      for item in rows if item.get("reflected"))
                        budget = max(0.0, float(total) - already)
                    except (TypeError, ValueError):
                        budget = None
                changed = 0
                for item in rows:
                    if item.get("reflected"):
                        continue
                    if not bool(item.get("cost_known")):
                        # An unknown row must stay visible to daily admission
                        # and preflight even after a run save with total 0.0.
                        continue
                    actual = float(item.get("actual") or 0.0)
                    if budget is not None and actual > budget + 1e-9:
                        break
                    item["reflected"] = True
                    changed += 1
                    if budget is not None:
                        budget -= actual
                if changed:
                    self._write(data)
                return changed
        except Exception:
            return 0

    def settle(self, run_id, actual, phase=None, cost_known=True, now=None,
               operation_id=None):
        """Release matching reservations and record the actual spend once.

        ``operation_id`` is the idempotency key for the paid attempt (callers
        pass the reservation id).  A repeated settlement of the same operation
        returns the existing committed row instead of appending another one.
        With no reservation left (a TTL reclaim or a restart) the deterministic
        fallback still updates one row rather than double-counting.
        """
        run_id = str(run_id)
        now_ts = float(now) if isinstance(now, (int, float)) else self.clock()
        try:
            value = float(actual) if actual is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        value = round(max(0.0, value), 6)
        cost_known = bool(cost_known) and actual is not None
        operation = None if operation_id is None else str(operation_id)
        with self._locked():
            data = self.load()
            committed = data.get("committed") or {}
            # Fast path: this attempt was already settled. No reservation may
            # remain, so keying only on the live reservation would double-add.
            if operation is not None:
                for item in committed.values():
                    if (str(item.get("run_id")) == run_id
                            and str(item.get("operation_id")) == operation):
                        return {"actual": float(item.get("actual") or 0.0),
                                "cost_known": bool(item.get("cost_known")),
                                "released": 0, "idempotent": True,
                                "committed": dict(item)}
            reservations = data.get("reservations") or {}
            released = []
            for key, item in list(reservations.items()):
                if str(item.get("run_id")) != run_id:
                    continue
                if phase is not None and str(item.get("phase")) != str(phase):
                    continue
                released.append((key, item))
                reservations.pop(key, None)
            data["reservations"] = reservations
            entries = []
            if released:
                # One committed row per reservation, keyed by the reservation
                # id: a repeated settle can never double-count.
                share = value / float(len(released))
                for key, item in released:
                    entry = {
                        "id": "com-" + str(key),
                        "run_id": run_id,
                        "phase": str(item.get("phase") or phase or "phase"),
                        "actual": round(share, 6),
                        "cost_known": cost_known,
                        "settled_at": _iso(now_ts),
                        "date": _today(now_ts),
                        "operation_id": operation or str(key),
                    }
                    committed[entry["id"]] = entry
                    entries.append(entry)
                # Remove exactly this operation's stale fallback row, if an
                # older build left one behind.  Never sweep every same-phase
                # unmatched row: those can be distinct real operations.
                if operation is not None:
                    stale = "com-unmatched-%s-%s" % (
                        run_id, _operation_suffix(run_id, operation))
                    old = committed.get(stale)
                    if isinstance(old, dict) and not old.get("reflected"):
                        committed.pop(stale, None)
            if not released:
                if operation is not None:
                    # Settlement identity is decisive.  With a named
                    # operation and no exact prior row, write that operation's
                    # own deterministic row even if another operation already
                    # committed in the same phase (TTL reclaim/restart).  The
                    # old fallback borrowed the other row and silently lost
                    # this operation's spend.
                    suffix = _operation_suffix(run_id, operation)
                    entry = {
                        "id": "com-op-%s-%s" % (run_id, suffix),
                        "run_id": run_id,
                        "phase": str(phase or "phase"),
                        "actual": value,
                        "cost_known": cost_known,
                        "settled_at": _iso(now_ts),
                        "date": _today(now_ts),
                        "operation_id": operation,
                    }
                    committed[entry["id"]] = entry
                    entries.append(entry)
                else:
                    # Legacy path: no idempotency key.  Reconcile a repeat
                    # against an existing row for this run/phase, or write the
                    # deterministic date-scoped fallback row once.
                    candidates = [
                        item for item in committed.values()
                        if str(item.get("run_id")) == run_id
                        and str(item.get("phase")) == str(phase or "phase")
                        and str(item.get("id") or "").startswith(
                            ("com-res-", "com-unmatched-", "com-op-"))]
                    if candidates:
                        entry = sorted(
                            candidates,
                            key=lambda item: str(item.get("settled_at") or ""))[-1]
                        entry["actual"] = value
                        entry["cost_known"] = cost_known
                        entry["settled_at"] = _iso(now_ts)
                        committed[entry["id"]] = entry
                        entries.append(entry)
                    else:
                        suffix = "%s-%s" % (str(phase or "phase"),
                                            _today(now_ts))
                        entry = {
                            "id": "com-unmatched-%s-%s" % (run_id, suffix),
                            "run_id": run_id,
                            "phase": str(phase or "phase"),
                            "actual": value,
                            "cost_known": cost_known,
                            "settled_at": _iso(now_ts),
                            "date": _today(now_ts),
                            "operation_id": "unmatched",
                        }
                        committed[entry["id"]] = entry
                        entries.append(entry)
            data["committed"] = committed
            self._write(data)
            return {"actual": value,
                    "cost_known": cost_known,
                    "released": len(released),
                    "idempotent": False,
                    "committed": dict(entries[0]) if entries else None}

    def release(self, run_id, phase=None):
        run_id = str(run_id)
        with self._locked():
            data = self.load()
            reservations = data.get("reservations") or {}
            removed = 0
            for key, item in list(reservations.items()):
                if str(item.get("run_id")) != run_id:
                    continue
                if phase is not None and str(item.get("phase")) != str(phase):
                    continue
                reservations.pop(key, None)
                removed += 1
            data["reservations"] = reservations
            self._write(data)
            return removed

    # -- views -----------------------------------------------------------
    def snapshot(self, run_id=None, limit=None, estimated=0.0, cost_known=True,
                 now=None):
        """Return the ceiling/estimate/reserved/committed view."""
        now_ts = float(now) if isinstance(now, (int, float)) else self.clock()
        data = self.load()
        reservations = self._active(data.get("reservations") or {}, now_ts)
        committed = data.get("committed") or {}
        try:
            estimated = float(estimated or 0.0)
        except (TypeError, ValueError):
            estimated = 0.0
        reserved = sum(float(item.get("estimate") or 0.0)
                       for item in reservations.values()
                       if run_id is None or str(item.get("run_id")) == str(run_id))
        spent = sum(float(item.get("actual") or 0.0)
                    for item in committed.values()
                    if run_id is None or str(item.get("run_id")) == str(run_id))
        known = bool(cost_known)
        known = known and all(
            bool(item.get("cost_known"))
            for item in reservations.values()
            if run_id is None or str(item.get("run_id")) == str(run_id))
        known = known and all(
            bool(item.get("cost_known"))
            for item in committed.values()
            if run_id is None or str(item.get("run_id")) == str(run_id))
        limit = _money(limit)
        if limit is None:
            available = None
        else:
            available = round(max(0.0, limit - spent - reserved - estimated), 6)
        return {
            "limit": limit,
            "estimated": round(estimated, 6),
            "reserved": round(reserved, 6),
            "committed": round(spent, 6),
            "available": available,
            "cost_known": known,
        }

    def daily_total(self, spent_today=0.0, now=None, include_committed=False):
        """Settled base + active reservations today (+ committed when asked)."""
        now_ts = float(now) if isinstance(now, (int, float)) else self.clock()
        day = _today(now if now is not None else now_ts)
        base = _call_spent(spent_today, now)
        data = self.load()
        reservations = self._active(data.get("reservations") or {}, now_ts)
        reserved = sum(float(item.get("estimate") or 0.0)
                       for item in reservations.values()
                       if str(item.get("date")) == day)
        committed = 0.0
        if include_committed:
            committed = sum(float(item.get("actual") or 0.0)
                            for item in (data.get("committed") or {}).values()
                            if str(item.get("date")) == day)
        return round(base + reserved + committed, 6)

    def check_live(self, run_id, phase, observed_cost, limit):
        """Whether an observed live cost may continue under the job ceiling.

        This is a whole-job view: every active reservation for the run is
        counted, not only the phase being observed.  It is deliberately
        conservative; the enforced path is :class:`LiveCostGuard`.
        """
        limit = _money(limit)
        if limit is None:
            return True, None
        data = self.load()
        active = self._active(data.get("reservations") or {}, self.clock())
        reserved = sum(float(item.get("estimate") or 0.0)
                       for item in active.values()
                       if str(item.get("run_id")) == str(run_id))
        committed_rows = [item for item in
                          (data.get("committed") or {}).values()
                          if str(item.get("run_id")) == str(run_id)]
        active_rows = [item for item in active.values()
                       if str(item.get("run_id")) == str(run_id)]
        committed = sum(float(item.get("actual") or 0.0)
                        for item in committed_rows)
        known = all(bool(item.get("cost_known"))
                    for item in committed_rows + active_rows)
        try:
            observed = float(observed_cost or 0.0)
        except (TypeError, ValueError):
            observed = 0.0
        remaining = round(limit - observed - reserved - committed, 6)
        if not known:
            return False, remaining
        return (observed + reserved + committed <= limit + 1e-9), remaining


class LiveCostGuard:
    """Accumulate priced usage events and flag a boundary-safe stop.

    Enforcement is whole-job: ``prior_committed`` is spend already settled by
    earlier phases, and the phase estimate is a reserve that is consumed as
    observed usage arrives.  The projected total is therefore
    ``prior_committed + observed + max(0, reserved - observed)``, i.e.
    ``prior_committed + max(observed, reserved)``.  Adding the full reserve on
    top of observed usage (the old behaviour) both over- and under-counted
    depending on the phase.

    With no usage event the guard reports ``observable=False``: no live cost
    cap is claimed and the caller falls back to reserve-at-admission /
    settle-at-exit.
    """

    def __init__(self, limit=None, reserved=0.0, model=None, prices=None,
                 prior_committed=0.0, prior_known=True):
        self.limit = _money(limit)
        self.reserved = max(0.0, float(reserved or 0.0))
        self.model = model
        self.prices = prices
        try:
            self.prior_committed = max(0.0, float(prior_committed or 0.0))
        except (TypeError, ValueError):
            self.prior_committed = 0.0
        self.prior_known = bool(prior_known)
        self.observed_usd = 0.0
        self.events = 0
        self.observable = False
        self.cost_known = True
        self.exceeded = False
        self.reason = None

    def projected_usd(self):
        """Whole-job projection used for the ceiling decision."""
        remaining = max(0.0, self.reserved - self.observed_usd)
        return self.prior_committed + self.observed_usd + remaining

    def observe(self, usage, model=None):
        """Add one usage event; return True when the next boundary must stop."""
        if not isinstance(usage, dict):
            return False
        inp = _num(usage, ("inputTokens", "input_tokens", "prompt_tokens",
                           "uncachedInputTokens"))
        cache = _num(usage, ("cacheReadTokens", "cache_read_tokens",
                             "prompt_cache_hit_tokens"))
        out = _num(usage, ("outputTokens", "output_tokens",
                           "completion_tokens"))
        if not (inp or cache or out):
            return False
        self.events += 1
        self.observable = True
        if self.limit is not None and not self.prior_known:
            self.cost_known = False
            self.exceeded = True
            self.reason = ("prior spend is unknown; the whole-job ceiling "
                           "cannot be enforced")
            return True
        prices, _resolved = _prices_for(model or self.model, self.prices)
        if prices:
            self.observed_usd += (
                inp * float(prices.get("miss") or 0.0)
                + cache * float(prices.get("hit") or 0.0)
                + out * float(prices.get("out") or 0.0)
            ) / 1000000.0
        else:
            self.cost_known = False
            if self.limit is not None:
                # Observable usage that cannot be priced cannot be proven
                # under the ceiling; stop rather than silently count $0.
                self.exceeded = True
                self.reason = ("live usage is observable but the model has no "
                               "local price; cannot enforce the ceiling")
                return True
        if self.limit is not None and self.projected_usd() > self.limit + 1e-9:
            self.exceeded = True
            self.reason = (
                "prior committed $%.6f + live usage $%.6f + remaining reserve "
                "$%.6f exceeds job ceiling $%.6f"
                % (self.prior_committed, self.observed_usd,
                   max(0.0, self.reserved - self.observed_usd), self.limit))
        return self.exceeded


def _num(container, names):
    if not isinstance(container, dict):
        return 0
    for name in names:
        value = container.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _call_spent(spent_today, now):
    """Accept a number or a zero-/one-argument callable; never raise."""
    if not callable(spent_today):
        try:
            return float(spent_today or 0.0)
        except (TypeError, ValueError):
            return 0.0
    for args in ((now,), ()):
        try:
            return float(spent_today(*args))
        except TypeError:
            continue
        except Exception:
            return 0.0
    return 0.0


def _epoch():
    return time.time()


def _sleep(seconds):
    time.sleep(seconds)
