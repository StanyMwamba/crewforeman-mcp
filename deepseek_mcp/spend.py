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


def budget_state(cfg, now=None):
    spent = spend_today(cfg, now)
    alert = cfg.daily_alert_usd
    limit = cfg.daily_limit_usd
    alert_crossed = alert is not None and spent >= float(alert)
    limit_reached = limit is not None and spent >= float(limit)
    warning = None
    if limit_reached:
        warning = ("[Daily limit reached: estimated $%.6f spent today, limit $%.2f. "
                   "Further paid DeepSeek/Harness calls are blocked until tomorrow "
                   "or until daily_limit_usd is raised or removed.]" % (spent, float(limit)))
    elif alert_crossed:
        warning = ("[Daily spend alert: estimated $%.6f spent today, above the $%.2f "
                   "daily_alert_usd threshold. No hard block is in effect.]" % (spent, float(alert)))
    return {
        "spent_usd": spent,
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
