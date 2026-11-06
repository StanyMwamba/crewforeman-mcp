"""Daily estimated-spend tracking and optional alert/limit enforcement.

Spend is read from the append-only daily ledger in the state directory.  If
that ledger is missing, it is rebuilt once from saved run records (the v3.0
behaviour).  Only estimated costs are available; this is a guardrail, not
billing.
"""

import datetime as dt

from . import ledger


def today_key(now=None):
    return (now or dt.datetime.now().astimezone()).strftime("%Y-%m-%d")


def spend_today(cfg, now=None):
    """Sum estimated USD for runs created today (unknown-cost runs ignored)."""
    return ledger.spend_today(cfg, now=now)


def _reserved_today(cfg, now=None):
    """Active cost reservations for today (P2-3), best effort.

    Reservations are money that a concurrent job has already committed to
    spend; the daily preflight must count them or two jobs can jointly pass the
    same check.
    """
    try:
        from . import budget
        ledger = budget.CostLedger(
            getattr(cfg, "state_dir", None),
            ttl=getattr(cfg, "cost_reservation_ttl_seconds", 900))
        day = today_key(now)
        data = ledger.load()
        active = ledger._active(data.get("reservations") or {}, ledger.clock())
        reserved = sum(float(item.get("estimate") or 0.0)
                       for item in active.values()
                       if str(item.get("date")) == day)
        committed_rows = [
            item for item in (data.get("committed") or {}).values()
            if str(item.get("date")) == day
            and not bool(item.get("reflected"))]
        committed = sum(float(item.get("actual") or 0.0)
                        for item in committed_rows)
        active_today = [
            item for item in active.values()
            if str(item.get("date")) == day]
        unknown = any(not bool(item.get("cost_known"))
                      for item in committed_rows + active_today)
        return round(reserved + committed, 6), unknown
    except Exception:
        # A corrupt/unreadable reservation ledger cannot prove headroom.
        return 0.0, True


def budget_state(cfg, now=None):
    spent = spend_today(cfg, now)
    reserved, reserved_unknown = _reserved_today(cfg, now)
    effective = round(spent + reserved, 6)
    alert = cfg.daily_alert_usd
    limit = cfg.daily_limit_usd
    alert_crossed = alert is not None and effective >= float(alert)
    limit_reached = (limit is not None
                     and (reserved_unknown or effective >= float(limit)))
    warning = None
    if limit_reached and reserved_unknown:
        warning = ("[Daily limit cannot be verified: an unknown committed or "
                   "reserved cost exists today (estimated $%.6f spent + $%.6f "
                   "reserved, limit $%.2f). Further paid calls are blocked "
                   "until the unknown cost is cleared.]"
                   % (spent, reserved, float(limit)))
    elif limit_reached:
        warning = ("[Daily limit reached: estimated $%.6f spent today + $%.6f "
                   "reserved, limit $%.2f. Further paid DeepSeek/Harness calls "
                   "are blocked until tomorrow or until daily_limit_usd is "
                   "raised or removed.]" % (spent, reserved, float(limit)))
    elif alert_crossed:
        warning = ("[Daily spend alert: estimated $%.6f spent today + $%.6f "
                   "reserved, above the $%.2f daily_alert_usd threshold. No "
                   "hard block is in effect.]" % (spent, reserved, float(alert)))
    return {
        "spent_usd": spent,
        "reserved_usd": reserved,
        "reserved_cost_known": not reserved_unknown,
        "effective_usd": effective,
        "alert_usd": alert,
        "limit_usd": limit,
        "alert_crossed": alert_crossed,
        "limit_reached": limit_reached,
        "warning": warning,
    }


def preflight(cfg):
    """Raise when the optional hard daily limit has already been reached."""
    state = budget_state(cfg)
    if state["limit_reached"]:
        raise RuntimeError(state["warning"])
    return state


def with_warning(text, cfg):
    """Append the current daily warning to a tool result, if any."""
    state = budget_state(cfg)
    if state["warning"]:
        return text + "\n\n" + state["warning"]
    return text
