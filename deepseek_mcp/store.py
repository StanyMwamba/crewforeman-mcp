"""Run storage: one JSON file plus one readable Markdown file per run."""

import datetime as dt
import json
import os
import tempfile
import uuid
from pathlib import Path

from .api import fmt_cost, redact_secrets
from .config import ROOT_DIR

FALLBACK_LOG_DIR = ROOT_DIR / "runs"
PROBE_PREFIX = "deepseek-mcp-write-test-"
PROBE_SUFFIX = ".tmp"


def log(message):
    import sys
    print("[deepseek-mcp] %s" % message, file=sys.stderr, flush=True)


def _normalise_log_dir(raw):
    """Absolute, ``~``-expanded, whitespace-stripped path.

    ``abspath`` (not ``resolve``) keeps the path the operator configured: on
    macOS ``resolve`` rewrites ``/tmp`` to ``/private/tmp`` and would break the
    path equality the operator and tests expect.
    """
    if raw is None:
        raise ValueError("log_dir is not configured")
    text = str(raw).strip()
    if not text:
        raise ValueError("log_dir is empty")
    expanded = os.path.expanduser(text)
    if expanded == text and text.startswith("~"):
        raise ValueError("cannot expand '~' in %r (HOME is unset)" % text)
    return Path(os.path.abspath(expanded))


def _probe_dir(directory):
    """Return ``None`` if ``directory`` is writable, else ``Type: message``.

    The probe name is deliberately visible: Google Drive File Provider and
    endpoint-security tools often special-case hidden dotfiles, which made the
    old ``.write-test-*`` probe fail on a folder that was otherwise writable.
    Only OS-level failures are turned into a reason; programming errors still
    raise instead of being silently swallowed.
    """
    fd = probe = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=PROBE_PREFIX, suffix=PROBE_SUFFIX,
                                     dir=str(directory))
        os.write(fd, b"probe")
        os.close(fd)
        fd = None
        os.unlink(probe)
        probe = None
        return None
    except (OSError, ValueError) as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe is not None:
            try:
                os.unlink(probe)
            except OSError:
                pass


def resolve_log_dir(cfg):
    """Return ``(directory, fallback_reason)``; reason is ``None`` on success.

    The configured directory is always tried first. A fallback is allowed, but
    it is never silent: the reason is logged and the caller stores it in the
    run record so the result text can mention it.
    """
    primary = None
    problem = None
    try:
        primary = _normalise_log_dir(cfg.log_dir)
        problem = _probe_dir(primary)
    except (TypeError, ValueError, OSError) as exc:
        problem = "%s: %s" % (type(exc).__name__, exc)
    if problem is None:
        return primary, None

    try:
        fallback = _normalise_log_dir(FALLBACK_LOG_DIR)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Cannot write runs to %s (%s) and the fallback is invalid: %s"
                           % (primary if primary is not None else cfg.log_dir,
                              problem, exc))
    fallback_problem = _probe_dir(fallback)
    if fallback_problem is None:
        configured = primary if primary is not None else cfg.log_dir
        reason = ("configured log_dir %s is not writable (%s); using fallback %s"
                  % (configured, problem, fallback))
        log("WARNING: " + reason)
        return fallback, reason
    raise RuntimeError("Cannot write runs to %s (%s) or %s (%s)"
                       % (primary if primary is not None else cfg.log_dir,
                          problem, fallback, fallback_problem))


def writable_log_dir(cfg):
    """Backward-compatible wrapper returning only the directory."""
    return resolve_log_dir(cfg)[0]


def _run_dirs(cfg):
    directories = []
    for raw in (cfg.log_dir, FALLBACK_LOG_DIR):
        try:
            directory = _normalise_log_dir(raw)
        except (TypeError, ValueError):
            continue
        if directory not in directories:
            directories.append(directory)
    return directories


def new_id(now=None):
    now = now or dt.datetime.now().astimezone()
    return now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def new_run(kind, args, prompt, cfg):
    now = dt.datetime.now().astimezone()
    run_id = args.get("_run_id") or new_id(now)
    if not _valid_run_id(run_id):
        raise RuntimeError("Invalid run id: %r" % run_id)
    run = {
        "id": run_id,
        "kind": kind,
        "created": now.isoformat(timespec="seconds"),
        "title": args.get("title") or prompt[:60],
        "model": args.get("model") or cfg.model,
        "reasoning_effort": args.get("reasoning_effort", cfg.reasoning),
        "system": args.get("system"),
        "prompt": prompt,
        "files": [],
    }
    # P1-1: identity is durable and separate from the attempt. Old records
    # simply lack these optional fields; every reader stays compatible.
    if args.get("_work_id"):
        run["work_id"] = str(args.get("_work_id"))
    if args.get("_attempt_no") is not None:
        try:
            run["attempt_no"] = int(args.get("_attempt_no"))
        except (TypeError, ValueError):
            pass
    if args.get("_fingerprint"):
        run["request_fingerprint"] = str(args.get("_fingerprint"))
    return run


def _md(run):
    lines = [
        "# %s" % (run.get("title") or "DeepSeek run"),
        "",
        "- **Run ID:** %s" % run["id"],
        "- **Type:** %s" % run.get("kind", "single"),
        "- **Date:** %s" % run["created"],
        "- **Model:** %s (reasoning: %s)" % (run.get("model"), run.get("reasoning_effort") or "default"),
        "- **Status:** %s" % run.get("status"),
    ]
    if run.get("work_id"):
        lines.append("- **Work:** %s (attempt %s)" % (
            run.get("work_id"), run.get("attempt_no", "?")))
    if run.get("kind") == "harness":
        lines += [
            "- **Tokens:** input %s (uncached) / cache read %s / output %s" % (
                run.get("tokens_in", "?"), run.get("cache_read_tokens", "?"),
                run.get("tokens_out", "?")),
            "- **Estimated cost (USD):** %s (%s)" % (
                fmt_cost(run.get("estimated_cost_usd")), run.get("cost_source") or "unknown"),
        ]
    else:
        lines += [
            "- **Total tokens:** input %s, output %s" % (
                run.get("tokens_in", "?"), run.get("tokens_out", "?")),
            "- **Estimated cost (USD):** %s" % fmt_cost(run.get("estimated_cost_usd")),
        ]
    if run.get("stalled"):
        lines.append("- **Stalled:** %s (no progress for %s s)" % (
            run.get("stalled_step") or "unknown step", run.get("stall_elapsed", "?")))
    if run.get("rescued"):
        lines.append("- **Rescued:** builder stopped early; a finisher completed the deliverables")
    if run.get("log_fallback_reason"):
        lines.append("- **Log fallback:** %s" % run["log_fallback_reason"])
    lines += [
        "- **Time:** %s s" % run.get("seconds", "?"),
    ]
    if run.get("subagents") is not None:
        lines.append("- **Subagents:** %s — %s" % (run.get("subagents"), run.get("subagent_reason") or ""))
    if run.get("quality_used"):
        lines.append("- **Quality loop:** %s round(s), final score %s" % (
            run.get("quality_rounds"), run.get("quality_score", "unknown")))
    if run.get("independent_review"):
        lines.append("- **Independent review:** %s, official score %s, builder self-score %s, %s review round(s)" % (
            run.get("review_status", "unknown"), run.get("quality_score", "unknown"),
            run.get("self_score", "unknown"), run.get("quality_rounds_used", 0)))
        for entry in run.get("review_rounds") or []:
            label = "Review" if entry.get("kind") == "review" else "Revision"
            detail = "  - %s %s: %s s" % (label, entry.get("round"), entry.get("seconds", "?"))
            if entry.get("score") is not None:
                detail += ", %.2f/10" % entry["score"]
            if entry.get("kind") == "review":
                detail += ", %s blocking defect(s)" % entry.get("blocking_defects", 0)
            lines.append(detail)
            cost_text = ("$%s" % fmt_cost(entry.get("cost"))
                         if entry.get("cost") is not None
                         else "cost unknown (no configured model price)")
            lines.append("    - usage: input %s (uncached) / cache read %s / "
                         "output %s / total %s / %s%s" % (
                             entry.get("tokens_in", "?"),
                             entry.get("cache_read_tokens", "?"),
                             entry.get("tokens_out", "?"),
                             entry.get("total_tokens", "?"), cost_text,
                             " (incl. pre-flight)"
                             if entry.get("preflight_usage") else ""))
    metrics = run.get("metrics") or {}
    if metrics:
        waste = metrics.get("waste_ratio")
        lines.append(
            "- **Metrics:** slices %s, %s overlap warning(s), waste ratio %s "
            "(%s launched / %s discarded), %s follow-up(s), %s blocking" % (
                "present" if metrics.get("slices_present") else "absent",
                metrics.get("slices_overlap_warnings", 0),
                "%.2f" % float(waste) if waste is not None else "n/a",
                metrics.get("waste_agents_launched", "?"),
                metrics.get("waste_agents_discarded", "?"),
                metrics.get("follow_ups", 0),
                metrics.get("blocking_follow_ups", 0)))
    if run.get("follow_ups") is not None:
        follow_ups = run.get("follow_ups") or []
        blocking_follow = sum(1 for item in follow_ups
                              if isinstance(item, dict) and item.get("blocking"))
        lines.append("- **Follow-ups:** %d (%d blocking)" % (
            len(follow_ups), blocking_follow))
    _practices = run.get("builder_practices")
    if _practices:
        if _practices.get("enabled", True):
            lines.append("- **Builder practices:** %s v%s (%s, %s words)" % (
                _practices.get("set", "builder-practices"),
                _practices.get("version", "?"),
                _practices.get("variant", "?"), _practices.get("words", 0)))
        else:
            lines.append("- **Builder practices:** disabled")
    if run.get("skill"):
        lines.append("- **Skill:** %s" % run.get("skill"))
    if run.get("agent_choice_reason"):
        lines.append("- **Agent choice:** %s" % run["agent_choice_reason"])
    if run.get("child_progress_events"):
        lines.append("- **Child progress:** %s child-session event(s), %s active at the last scan" % (
            run.get("child_progress_events"), run.get("child_sessions_active", 0)))
    if run.get("stall_cap_hit"):
        lines.append("- **Watchdog cap:** absolute child-aware cap reached after %s s" % (
            run.get("stall_elapsed", "?")))
    if run.get("builder_retry"):
        retry = run["builder_retry"]
        if retry.get("status") == "skipped":
            lines.append("- **Builder retry:** skipped (%s)" % (
                retry.get("skip_reason") or "budget"))
        else:
            lines.append("- **Builder retry:** %s (limit %s s, retry status %s)" % (
                retry.get("reason"), retry.get("limit_seconds"), retry.get("status")))
    probe = run.get("render_probe") or {}
    if probe:
        lines.append("- **Render probe:** %s%s" % (
            "available" if probe.get("available") else "unavailable",
            (" - %s" % probe.get("reason")) if probe.get("reason") else ""))
    if run.get("agent_escalations"):
        lines.append("- **Agent escalations:** %d" % len(run["agent_escalations"]))
    if run.get("lessons_added"):
        lines.append("- **Lessons added:** %d" % len(run["lessons_added"]))
    block = run.get("skill_auto") or {}
    if block:
        lines.append("- **Automatic skills:** %s" % (block.get("summary") or "observed"))
        for change in (block.get("changes") or [])[:6]:
            lines.append("  - %s %s v%s (%s)" % (change.get("action"),
                         change.get("key"), change.get("version"),
                         change.get("reason")))
    if run.get("rating"):
        lines.append("- **User rating:** %s — %s" % (run.get("rating"), run.get("rating_note") or ""))
    if run.get("kind") == "swarm":
        lines += [
            "- **Agents used:** %s in %s round(s)" % (run.get("agents_used"), run.get("rounds")),
            "- **Judge confidence:** %s | agreement: %s" % (run.get("confidence"), run.get("agreement")),
            "- **Stopped because:** %s" % run.get("stop_reason"),
        ]
    if run.get("kind") == "harness":
        lines += [
            "- **Workspace:** %s" % run.get("workspace"),
            "- **Tools used:** %s" % run.get("tools_used"),
            "- **Files written:** %s" % (", ".join(run.get("output_files") or []) or "none"),
            "- **Session logs:** %s" % (", ".join(run.get("session_logs") or []) or "not found"),
        ]
    if run.get("files"):
        lines.append("- **Files sent:** %s" % ", ".join(run["files"]))
    if run.get("daily_spend_usd") is not None:
        lines.append("- **Spend today (estimated):** $%s" % fmt_cost(run["daily_spend_usd"]))
    lines += ["", "## System", "", run.get("system") or "_(none)_", "", "## Prompt", "", run["prompt"], ""]
    lines += ["## Final answer", "", run.get("answer") or "_Error: %s_" % run.get("error"), ""]
    if run.get("judge_reason"):
        lines += ["## Judge's reasoning", "", run["judge_reason"], ""]
    for agent in run.get("agents", []):
        lines += [
            "## Agent %s (round %s) — %s" % (
                agent.get("n"), agent.get("round"), (agent.get("persona") or "")[:60]),
            "",
            "_%s s, cost %s_" % (agent.get("seconds", "?"), fmt_cost(agent.get("cost"))),
            "",
            agent.get("answer") or "_Error: %s_" % agent.get("error"),
            "",
        ]
    if run.get("kind") != "swarm" and run.get("reasoning"):
        lines += ["## DeepSeek reasoning", "", run["reasoning"], ""]
    if run.get("stderr_tail"):
        lines += ["## Harness stderr (tail)", "", "```", run["stderr_tail"], "```", ""]
    return "\n".join(lines)


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, str(path))
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_run(run, cfg, ledger=True):
    """Write ``<id>.json`` and ``<id>.md``. Returns the Markdown path.

    ``ledger=False`` is used for the intermediate run skeleton and phase
    updates: the JSON/Markdown record must be durable, but the spend ledger
    should record the final cost, not a series of partial totals.
    """
    if not _valid_run_id(run.get("id")):
        raise RuntimeError("Invalid run id: %r" % run.get("id"))
    directory, fallback_reason = resolve_log_dir(cfg)
    if fallback_reason:
        run["log_fallback_reason"] = fallback_reason
    else:
        run.pop("log_fallback_reason", None)
    base = directory / run["id"]
    json_path = base.with_suffix(".json")
    _atomic_write(json_path, redact_secrets(json.dumps(run, ensure_ascii=False, indent=2)))
    _atomic_write(base.with_suffix(".md"), redact_secrets(_md(run)))
    if ledger:
        try:
            from . import ledger
            appended = ledger.append_run(cfg, run)
            if appended:
                # The run's cost is now in the spend ledger; the reservation
                # ledger can stop counting these committed rows for the daily
                # ceiling (a later phase of the same run remains counted).
                from . import budget
                total = run.get("estimated_cost_usd")
                if not isinstance(total, (int, float)):
                    total = 0.0
                budget.CostLedger(cfg.state_dir).mark_reflected(
                    run["id"], total=total)
        except Exception:
            pass
    return str(base.with_suffix(".md"))


def load_runs(cfg):
    runs, seen = [], set()
    for directory in _run_dirs(cfg):
        try:
            names = os.listdir(str(directory))
        except FileNotFoundError:
            continue
        except Exception as exc:
            log("cannot list %s: %s" % (directory, exc))
            continue
        for name in names:
            if not name.endswith(".json") or name in seen:
                continue
            seen.add(name)
            try:
                data = json.loads((directory / name).read_text(encoding="utf-8"))
            except Exception as exc:
                log("cannot read run %s: %s" % (directory / name, exc))
                continue
            if isinstance(data, dict) and data.get("id"):
                runs.append(data)
    runs.sort(key=lambda item: item.get("created", ""), reverse=True)
    return runs


def _valid_run_id(run_id):
    return bool(run_id) and Path(run_id).name == run_id and run_id not in (".", "..")


def find_run(run_id, cfg):
    """Return ``(run, json_path, md_path)`` or raise RuntimeError."""
    if not _valid_run_id(run_id):
        raise RuntimeError("Invalid run id: %r" % run_id)
    for directory in _run_dirs(cfg):
        json_path = directory / (run_id + ".json")
        md_path = directory / (run_id + ".md")
        if json_path.is_file():
            try:
                run = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError("Cannot read run %s: %s" % (run_id, exc))
            return run, json_path, md_path
    raise RuntimeError("No saved run with id %r in %s" % (run_id, cfg.log_dir))


def update_run(run, cfg):
    return save_run(run, cfg)


def read_run_md(run_id, cfg):
    _run, _json_path, md_path = find_run(run_id, cfg)
    if not md_path.is_file():
        raise RuntimeError("No Markdown file for run %s" % run_id)
    return md_path.read_text(encoding="utf-8")


def append_run_md(run, text, cfg):
    """Append a short section to the run's Markdown record (best effort)."""
    try:
        _run, _json_path, md_path = find_run(run["id"], cfg)
    except Exception:
        return None
    with md_path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + text.rstrip() + "\n")
    return str(md_path)
