"""Append-only daily spend ledger.

Every ``save_run`` appends one JSON line: ``{"date", "run_id", "cost"}``.  The
ledger lives in ``state_dir/spend-ledger.jsonl``.  Reads are cheap and only
touch today's entries; if the ledger is missing, ``spend_today`` rebuilds the
day's total from saved run records (the v3.0 behaviour) and repopulates the
ledger best-effort.

Python 3.9 compatible; standard library only.
"""

import datetime as dt
import json
import os
from pathlib import Path

LEDGER_FILENAME = "spend-ledger.jsonl"


def today_key(now=None):
    return (now or dt.datetime.now().astimezone()).strftime("%Y-%m-%d")


def ledger_path(cfg):
    raw = getattr(cfg, "state_dir", "") or "~/DeepSeek-MCP/state"
    return Path(os.path.expanduser(str(raw))) / LEDGER_FILENAME


def _entry(run):
    created = str(run.get("created") or "")[:10]
    cost = run.get("estimated_cost_usd")
    if not isinstance(cost, (int, float)):
        cost = None
    return {
        "date": created or today_key(),
        "run_id": run.get("id"),
        "cost": None if cost is None else round(float(cost), 6),
    }


def append_run(cfg, run):
    """Append one save event.  Best effort: never break a run save."""
    if not run or not run.get("id"):
        return False
    path = ledger_path(cfg)
    line = json.dumps(_entry(run), ensure_ascii=True, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def read_entries(cfg, day=None):
    """Read ledger entries (optionally for one day), deduped by run_id.

    Returns ``None`` when the ledger file does not exist, which tells the
    caller to rebuild.  Last write wins, so a rating/update save does not
    double-count a run.
    """
    path = ledger_path(cfg)
    if not path.is_file():
        return None
    ordered = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict) or not entry.get("run_id"):
                    continue
                if day and str(entry.get("date") or "")[:10] != day:
                    continue
                ordered[str(entry["run_id"])] = entry
    except Exception:
        return None
    return list(ordered.values())


def rebuild_entries(cfg, day):
    """Rebuild one day's entries from saved run records."""
    try:
        from .store import load_runs
        runs = load_runs(cfg)
    except Exception:
        return []
    entries = {}
    for run in runs:
        run_id = run.get("id")
        if not run_id or str(run.get("created") or "")[:10] != day:
            continue
        cost = run.get("estimated_cost_usd")
        entries[run_id] = {
            "date": day,
            "run_id": run_id,
            "cost": round(float(cost), 6) if isinstance(cost, (int, float)) else None,
        }
    return list(entries.values())


def spend_today(cfg, now=None):
    """Sum estimated USD for today from the ledger, rebuilding if absent."""
    day = today_key(now)
    entries = read_entries(cfg, day)
    if entries is None:
        entries = rebuild_entries(cfg, day)
        # Repopulate the ledger so the next call is O(today). Duplicate lines
        # are harmless: reads dedupe by run_id.
        if entries:
            for entry in entries:
                run = {"id": entry["run_id"], "created": day + "T00:00:00",
                       "estimated_cost_usd": entry.get("cost")}
                append_run(cfg, run)
    total = 0.0
    for entry in entries or []:
        cost = entry.get("cost")
        if isinstance(cost, (int, float)):
            total += float(cost)
    return round(total, 6)
