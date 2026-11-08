"""Run status vocabulary and the review-aware final status.

Backward compatibility: ``status`` remains a string, and ``quality_score``,
``review_status``, ``review_rounds`` and every existing field are unchanged.
What changed in 3.6.0 is that a completed build whose independent review is
below the gate (or still has blocking defects) is no longer reported as plain
``ok``.

Python 3.9 compatible; standard library only.
"""

STATUSES = (
    "ok",
    "ok_below_gate",
    "ok_unreviewed",
    "ok_rescued",
    "error",
    "no_balance",
    "cancelled",
    "deadline_exceeded",
    "budget_exceeded",
    "gate_failed",
    "needs_input",
)

# Convenience groupings for clients and dashboards.  A successful status means
# the run produced usable output, even when the review gate was not reached;
# failure statuses mean the run stopped without a usable result.
SUCCESS_STATUSES = frozenset(("ok", "ok_rescued", "ok_unreviewed",
                              "ok_below_gate"))
FAILURE_STATUSES = frozenset(("error", "no_balance", "cancelled",
                              "deadline_exceeded", "budget_exceeded", "gate_failed"))

STATUS_MEANING = {
    "ok": "finished and, when a review ran, accepted at or above the gate with zero blocking defects.",
    "ok_below_gate": "finished, but the independent review scored below the gate or left blocking defects; read REVIEW.md and the fix list.",
    "ok_unreviewed": "the builder finished but the independent review failed; deliverables were not verified.",
    "ok_rescued": "a stalled/time-limited builder was completed by the short finisher and the review passed (or was disabled).",
    "error": "the run failed; the builder or a required phase did not complete.",
    "no_balance": "the provider account has no credit; top up and retry.",
    "cancelled": "the client cancelled the request; partial files may remain.",
    "deadline_exceeded": "the whole-job deadline stopped the run before it finished.",
    "budget_exceeded": "the whole-job estimated-cost budget stopped the run before it finished.",
    "gate_failed": "the deterministic gate exhausted its revision budget; the paid reviewer was not called.",
    "needs_input": "the job paused on one validated question and is waiting for a client answer; resume it.",
}


def final_status(builder_status="ok", review_status="disabled", score=None,
                 blocking_defects=0, target=8.5, rescued=False, cancelled=False,
                 stop_reason=None, review_recorded=True, gate_status=None):
    """Return ``(status, detail, review_passed)``.

    ``review_passed`` is ``True`` for an accepted review, ``False`` for a
    stopped/failed one, and ``None`` when no independent review ran.

    ``review_recorded=False`` means a requested review never produced a
    durable round: that must not become a plain ``ok`` even when the builder
    itself succeeded.  It defaults to ``True`` so existing callers are
    unchanged.

    ``gate_status="gate_failed"`` returns that status before any review
    mapping, but only after the builder has succeeded.
    """
    if cancelled or stop_reason == "cancelled":
        return "cancelled", STATUS_MEANING["cancelled"], None
    if stop_reason in ("deadline", "time_budget_exceeded"):
        return "deadline_exceeded", STATUS_MEANING["deadline_exceeded"], None
    if stop_reason in ("budget", "cost_budget_exceeded"):
        return "budget_exceeded", STATUS_MEANING["budget_exceeded"], None
    if builder_status == "no_balance":
        return "no_balance", STATUS_MEANING["no_balance"], None
    if builder_status not in ("ok", "ok_rescued"):
        return "error", STATUS_MEANING["error"], None
    if gate_status == "gate_failed":
        return "gate_failed", STATUS_MEANING["gate_failed"], None
    if review_status == "declined":
        return ("ok_unreviewed", "review declined by user", False)
    if review_recorded is False and review_status != "disabled":
        return ("error",
                "review was requested but no review round was recorded",
                None)

    try:
        blocking = int(blocking_defects or 0)
    except (TypeError, ValueError):
        blocking = 0
    reviewed = review_status in ("accepted", "stopped", "error")
    passed = None
    if review_status == "accepted" and score is not None and blocking == 0:
        try:
            passed = float(score) >= float(target)
        except (TypeError, ValueError):
            passed = False
    elif reviewed:
        passed = False

    if review_status == "error":
        return "ok_unreviewed", STATUS_MEANING["ok_unreviewed"], False
    if reviewed and not passed:
        return "ok_below_gate", STATUS_MEANING["ok_below_gate"], False
    if rescued:
        return "ok_rescued", STATUS_MEANING["ok_rescued"], passed
    return "ok", STATUS_MEANING["ok"], passed
