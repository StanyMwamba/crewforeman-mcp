"""Whole-job time and cost budget across every Harness phase.

The old per-process limits let a builder, reviewer, revisions and a rescue each
consume a full budget. This object is the single clock/budget the run loop
consults before starting any phase and before a retry.

Python 3.9 compatible; standard library only.
"""

import time


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
