"""MCP stdio server (JSON-RPC 2.0) and tool implementations.

Python 3.9 compatible, standard library only. The API key is read from the
environment only; this module never logs or stores it.
"""

import concurrent.futures as cf
import json
import math
import os
import sys
import threading
import time
from pathlib import Path

from . import (api, checkpoints, clients, events, harness, jobs, mac, providers,
               render, retention, safety, skills, spend, tasks)
from .config import (PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION,
                     SUPPORTED_PROTOCOL_VERSIONS, load_config)
from .store import (find_run, load_runs, new_run, read_run_md, save_run)

MAX_FILE_CHARS_DEFAULT = 400000


def log(message):
    print("[deepseek-mcp] %s" % message, file=sys.stderr, flush=True)


# ------------------------------------------------------------------- schemas

COMMON_PROPS = {
    "prompt": {"type": "string", "description": "The task or question."},
    "system": {"type": "string", "description": "Optional instructions (role, style, output format)."},
    "files": {"type": "array", "items": {"type": "string"},
              "description": "Optional absolute paths of text files (txt, md, csv, json, code...) to include as context."},
    "title": {"type": "string", "description": "Short name for this run, shown in the saved log."},
    "model": {"type": "string", "description": "Model ID (default deepseek-flash; or deepseek-v4-pro)."},
    "reasoning_effort": {"type": "string", "enum": ["low", "high", "max"],
                         "description": "Thinking effort (default max)."},
    "max_tokens": {"type": "integer", "description": "Optional cap on each answer's length."},
    "wait_seconds": {"type": "number", "description": (
        "How long this call waits for the result (default 45). The job runs in the background and "
        "keeps going after that; get the result later with deepseek_job_status.")},
}

TOOLS = [
    {
        "name": "ask_deepseek",
        "execution": {"taskSupport": "optional"},
        "description": (
            "Send a task to ONE DeepSeek agent (cheap, fast) and get its answer. Every run is saved "
            "so the user can check it. Use for routine work: summarizing long files, drafting, "
            "rewriting, first-pass analysis. Attach local text files with `files` instead of pasting "
            "them. Do not send confidential or personal data."
        ),
        "inputSchema": {"type": "object", "properties": dict(
            COMMON_PROPS,
            temperature={"type": "number", "description": "Optional sampling temperature."},
        ), "required": ["prompt"]},
    },
    {
        "name": "consult_deepseek",
        "description": (
            "Ask 2-3 configured OpenAI-compatible models the same question independently and "
            "return each answer separately; there is no judge and no synthesis. Models come from "
            "`models` (for example ['deepseek:deepseek-flash', 'openai:gpt-4o-mini']) or the "
            "`consult_models` config. Keys are read from the environment variables named by "
            "`providers` config; keys are never stored. Do not send confidential or personal data."
        ),
        "execution": {"taskSupport": "optional"},
        "inputSchema": {"type": "object", "properties": dict(
            COMMON_PROPS,
            models={"type": "array", "items": {"type": "string"},
                    "description": "Model ids, optionally `provider:model`. Default: consult_models."},
            temperature={"type": "number", "description": "Optional sampling temperature."},
        ), "required": ["prompt"]},
    },
    {
        "name": "swarm_deepseek",
        "execution": {"taskSupport": "optional"},
        "description": (
            "EXPERIMENTAL: solve a task with an ADAPTIVE SWARM of DeepSeek agents. Agents work in "
            "parallel from different angles; a judge agent compares their answers and writes the best "
            "final answer. It starts small and adds agents only while the judge is not confident, so easy "
            "tasks stay fast and cheap. Optional limits on agents, cost and time. Every agent's answer and "
            "the judge's decision are saved. This path is experimental: prefer harness_deepseek for real "
            "deliverables. Do not send confidential or personal data."
        ),
        "inputSchema": {"type": "object", "properties": dict(
            COMMON_PROPS,
            start_agents={"type": "integer", "minimum": 1,
                          "description": "Agents in the first round (default 3, minimum 1)."},
            max_agents={"type": "integer", "minimum": 1,
                        "description": ("Never raised. Hard cap on agents in total (default 40); the "
                                        "first round is reduced to it.")},
            confidence_target={"type": "number", "minimum": 0, "maximum": 1,
                               "description": ("Stop when the judge's confidence reaches this "
                                               "(0..1, default 0.85; 0 is honoured and stops after "
                                               "the first round).")},
            max_cost_usd={"type": "number", "minimum": 0,
                          "description": ("Whole-swarm estimated-cost limit in USD (finite, >= 0; "
                                          "0 starts no call). One in-flight call can exceed the "
                                          "remaining budget by its own cost; an unpriced model "
                                          "stops the swarm after that call.")},
            max_seconds={"type": "number", "minimum": 0,
                         "description": ("Whole-swarm time limit in seconds (finite, >= 0; 0 starts "
                                         "no call; checked before every agent, judge and round).")},
            max_tokens={"type": "integer", "minimum": 1,
                        "description": "Optional per-answer token cap (integer >= 1)."},
        ), "required": ["prompt"]},
    },
    {
        "name": "list_deepseek_runs",
        "description": "List the most recent saved DeepSeek runs (id, date, type, model, tokens, cost, status).",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many runs to list (default 10)."}}},
    },
    {
        "name": "harness_deepseek",
        "execution": {"taskSupport": "optional"},
        "description": (
            "Run a task through DeepSeek Harness in headless mode. Unlike ask/swarm (text only), Harness "
            "agents can use tools: read and write files in the workspace folder, run bash and code, search "
            "the web, and start Harness subagents in parallel (workflow). Use for tasks that need to build, "
            "run or check something. When `subagents` is omitted the connector classifies the task and "
            "starts small: 0 agents for a simple question, 4 for code, and 6/8/12 for creative or research "
            "work by size, never above `default_subagents` (16) or `subagent_soft_cap` (32), with waves "
            "capped at the detected Harness concurrency, then escalates only after a low review score. "
            "Explicit counts are kept exactly. The quality loop (default on for real work) uses pipelined, "
            "learning waves with sub-leader summaries, then the connector starts a SEPARATE fresh Harness "
            "reviewer (new session, same workspace, read-only policy) that scores the deliverables 0-10, "
            "lists blocking defects with evidence and returns a report the connector writes to REVIEW.md. "
            "Its score is the official quality_score; the builder's score is kept as self_score. Below "
            "8.5/10 or any blocking defect, the connector runs a revision with the fix list and reviews "
            "again (default max 3 "
            "rounds). A child-session aware watchdog keeps a silent workflow wave alive, TMPDIR is "
            "workspace-local for browser safety, a bounded render probe tells builders which Chrome works, "
            "one safe retry handles fast transient failures, and creative work is reviewed for visual "
            "impact as well as accuracy. Harness session token usage and estimated cost are recorded. "
            "Runs in the background; files it creates stay in the workspace. Do not send confidential or "
            "personal data."
        ),
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "The task."},
            "title": {"type": "string", "description": "Short name for this run, shown in the saved log."},
            "workspace": {"type": "string", "description": (
                "Absolute folder the agents work in (default ~/DeepSeek-MCP/harness-workspace). "
                "Put input files there; outputs are written there.")},
            "subagents": {"type": "integer", "description": (
                "Harness subagents (via its workflow tool). Omit to choose automatically: 0 for simple "
                "questions, 4 for code, 6/8/12 for creative or research work by size, never above "
                "default_subagents (16) or subagent_soft_cap (32); each wave is capped at the detected "
                "concurrency. A strong smaller history bucket can match its count; upward nudges are at "
                "most 2 and need two comparable runs. A low review score may escalate the revision. "
                "Use 0 for one agent. Explicit values are kept exactly up to the configurable hard "
                "maximum max_explicit_subagents (default 32).")},
            "quality": {"type": "boolean", "description": (
                "Run the builder quality loop (default true for real work, false for simple questions).")},
            "quality_rounds": {"type": "integer", "description": (
                "Maximum independent review/revision rounds (default 3, minimum 1).")},
            "independent_review": {"type": "boolean", "description": (
                "Run a separate fresh Harness reviewer after the builder and revise below 8.5/10 or on "
                "blocking defects. Default true for real work (subagents > 0).")},
            "skill": {"type": "string", "description": (
                "Optional skill key or alias from the registry. Omit to auto-detect by "
                "confidence; use 'none' to disable injection.")},
            "max_seconds": {"type": "number", "description": (
                "Optional time limit per Harness process. A run that stops by time limit (or by "
                "the stall watchdog) with existing output files may get one short finisher run "
                "(up to 480 s more) that completes the deliverables.")},
            "sandbox": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"],
                        "description": ("Permission preset for builder processes (default workspace-write). "
                                        "danger-full-access requires this explicit opt-in.")},
            "reviewer_sandbox": {"type": "string",
                                 "enum": ["read-only", "workspace-write", "danger-full-access"],
                                 "description": ("Permission preset for the independent reviewer "
                                                 "(default read-only; the builder cannot widen it).")},
            "approval": {"type": "string", "enum": ["ask", "never"],
                         "description": "Approval policy passed to the resolved DSH permission preset."},
            "transport": {"type": "string", "enum": ["headless", "sdk", "auto"],
                          "description": ("DSH transport: headless (stable), sdk (JSON-RPC) or auto "
                                          "(SDK first, headless fallback with a recorded reason).")},
            "add_dirs": {"type": "array", "items": {"type": "string"},
                         "description": ("Extra read roots to record for the run. The installed "
                                         "headless profile has no --add-dir flag; unsupported roots "
                                         "are recorded rather than passed.")},
            "job_max_seconds": {"type": "number", "description": (
                "Whole-job deadline in seconds across builder, live-scaling waves, reviewer, "
                "revisions, rescue and retries (default 7200; 0 disables).")},
            "job_max_cost_usd": {"type": "number", "description": (
                "Whole-job estimated-cost ceiling in USD (default none). An unknown cost cannot be "
                "proven under budget and stops further phases.")},
            "live_scaling": {"type": "boolean", "description": (
                "Score the first wave and add a bounded extra wave with new approaches when it is "
                "weak and the whole-job budget allows it (default true).")},
            "live_scaling_threshold": {"type": "number", "description": (
                "Candidates scoring below this 0-10 threshold are weak (default 7.0).")},
            "live_scaling_max_extra_waves": {"type": "integer", "minimum": 0,
                                             "description": "Maximum extra waves (default 2)."},
            "wait_seconds": {"type": "number", "description": "How long this call waits before returning the job id (default 45)."},
            "idempotency_key": {"type": "string", "description": (
                "Optional stable client key for this logical request. A retry with the same key "
                "(even after the first run finished) reuses the work item and increments its "
                "attempt; without a key, an identical request is deduplicated only while an "
                "attempt is active.")},
            "resume_run_id": {"type": "string", "description": (
                "Resume a checkpointed run from its last finished phase instead of starting over. "
                "The saved prompt, workspace, skill and policies are reused.")},
            "urgent": {"type": "boolean", "description": (
                "Queue this new build ahead of normal queued builds, below P0 resume "
                "priority. Running jobs are never suspended.")},
            "depends_on": {"type": "string", "description": (
                "Run id that must finish before this job starts. While waiting the job "
                "holds no running slot; the dependency is ordered ahead of normal "
                "queued builds. Unknown, failed, cancelled or cyclic dependencies are "
                "rejected. This is explicit only: the prompt text is never scanned.")},
        }, "required": ["prompt"]},
    },
    {
        "name": "resume_deepseek",
        "description": (
            "Resume a Harness run from its durable checkpoint. A completed builder (and an "
            "accepted independent review) is reused; an interrupted phase continues from the "
            "saved DSH session where possible. Use the run id shown in the original result."
        ),
        "execution": {"taskSupport": "optional"},
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Saved Harness run id."},
            "answer": {"type": "string", "description": (
                "Optional answer to the one validated question reported by "
                "deepseek_job_status.")},
            "option": {"type": "string", "description": (
                "Optional option chosen from that question's options list.")},
            "wait_seconds": {"type": "number", "description": "How long this call waits (default 45)."},
        }, "required": ["run_id"]},
    },
    {
        "name": "keep_awake",
        "description": ("Keep this machine from sleeping while a long run finishes. action: start | "
                        "stop | status. Uses caffeinate on macOS, systemd-inhibit on Linux and "
                        "SetThreadExecutionState on Windows; returns a clear unsupported message "
                        "where no mechanism exists. Lasts until stopped or the machine restarts."),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "status"]}}, "required": ["action"]},
    },
    {
        "name": "deepseek_job_status",
        "description": ("Check a DeepSeek job that was still running when its call returned, and get its "
                        "result once finished. Without run_id, lists recent background jobs."),
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"},
            "events_since": {"type": "integer", "minimum": 0,
                             "description": ("Optional journal cursor. When present, returns "
                                             "structured events, stage, attention, the one pending "
                                             "question and the next action; omit it for the "
                                             "unchanged text result.")},
        }},
    },
    {
        "name": "deepseek_job_cancel",
        "description": ("Stop a running DeepSeek background job and its Harness process group. "
                        "Cancellation is best-effort and idempotent."),
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Background job id."},
            "reason": {"type": "string", "description": "Optional short reason."},
        }, "required": ["run_id"]},
    },
    {
        "name": "read_deepseek_run",
        "description": "Show one saved DeepSeek run in full. Use the run id from the other tools.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "rate_deepseek_run",
        "description": ("Rate a saved run up or down. The feedback is stored in the run and distilled "
                        "into a lesson in the matching skill file, so later Harness runs improve."),
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Saved run id."},
            "rating": {"type": "string", "enum": ["up", "down"], "description": "up or down."},
            "note": {"type": "string", "description": "Optional short reason or what to fix."},
        }, "required": ["run_id", "rating"]},
    },
    {
        "name": "list_skills",
        "description": ("List skill registry entries with status, version, use/score statistics and the "
                        "unmatched baseline. Use format=json for machine-readable output."),
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string",
                       "enum": ["all", "seed", "active", "candidate", "demoted", "retired"],
                       "description": "Filter by lifecycle status (default all)."},
            "format": {"type": "string", "enum": ["text", "json"],
                       "description": "text (default) or json."},
        }},
    },
    {
        "name": "read_skill",
        "description": ("Read one skill checklist by key or alias. Optionally read a historical "
                        "version and include registry metadata."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill key or alias."},
            "version": {"type": "integer", "minimum": 1,
                        "description": "Historical version (default current)."},
            "meta": {"type": "boolean",
                     "description": "Include status/version/stats metadata (default false)."},
        }, "required": ["name"]},
    },
    {
        "name": "approve_skill",
        "description": ("Approve a candidate created in propose mode. Explicit transitions: candidate -> "
                        "candidate (approved=true); demoted -> candidate (approved=true); retired -> "
                        "candidate (approved=true). It becomes injectable; promotion to active still "
                        "needs the measured runs."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Candidate key or alias."},
            "note": {"type": "string",
                     "description": "Optional approval note recorded in the audit log."},
        }, "required": ["name"]},
    },
    {
        "name": "reject_skill",
        "description": ("Reject a candidate: retire it, keep its versions readable, and record "
                        "the reason."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Candidate key or alias."},
            "reason": {"type": "string", "description": "Optional short reason."},
        }, "required": ["name"]},
    },
    {
        "name": "retire_skill",
        "description": ("Retire any non-retired skill (seed, active, candidate or demoted). It stops "
                        "being injected but remains readable and rollback-able."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill key or alias."},
            "reason": {"type": "string", "description": "Optional short reason."},
        }, "required": ["name"]},
    },
    {
        "name": "rollback_skill",
        "description": ("Restore a previous version as a new version (history is immutable). "
                        "Defaults to the immediately previous version."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill key or alias."},
            "version": {"type": "integer", "minimum": 1,
                        "description": "Target version (default current-1)."},
        }, "required": ["name"]},
    },
    {
        "name": "take_screenshot",
        "description": ("Capture the screen to a file in the workspace. macOS uses screencapture; "
                        "Linux uses an installed screenshot tool; Windows uses PowerShell. Returns "
                        "a clear unsupported message when the platform has no supported tool."),
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Optional output path."},
            "delay_seconds": {"type": "number", "description": "Optional delay before capture."}}},
    },
    {
        "name": "restart_client_app",
        "description": ("Restart the MCP client application so it relaunches this server with new "
                        "config. Set app_name to the application name (default from client_app_name "
                        "config, else Claude). Returns a clear unsupported message when the platform "
                        "or application cannot be restarted automatically; restart it manually then."),
        "inputSchema": {"type": "object", "properties": {
            "app_name": {"type": "string", "description": "Application name, for example Cursor or Claude."},
            "wait_seconds": {"type": "number", "description": "Seconds to wait before reopening (default 5)."}}},
    },
    {
        "name": "deepseek_maintenance",
        "description": ("Inspect or clean local run/job/trace data, show state permissions and "
                        "component health. action: status | health | clear_health | cleanup | "
                        "dry_run. clear_health clears a quarantined builder/reviewer so new "
                        "automatic phases can start. Cleanup respects retention_days, keep_runs "
                        "and keep_jobs; it never deletes outside the configured roots."),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "health", "clear_health",
                                                  "cleanup", "dry_run"],
                       "description": "status (default), health, clear_health, cleanup, "
                                      "or a no-write dry run."},
            "component": {"type": "string", "description": (
                "Optional component for clear_health (builder, reviewer, revision, rescue). "
                "Omit to clear every quarantined component.")}},
            "required": ["action"]},
    },
]

MAC_TOOL_NAMES = ("keep_awake", "take_screenshot", "restart_client_app")


# ------------------------------------------------------------------ helpers

def _cfg(cfg=None):
    return cfg or load_config()


def platforms_private(path):
    """Best-effort 0700 on a directory, no-op on Windows."""
    from . import platforms
    return platforms.private_permissions(path, is_dir=True)


def _safe_warning(text, cfg):
    try:
        return spend.with_warning(text, cfg)
    except Exception:
        return text


def _read_files(paths, cfg):
    parts, names = [], []
    limit = int(cfg.max_file_chars or MAX_FILE_CHARS_DEFAULT)
    for raw in paths or []:
        path = Path(os.path.expanduser(raw))
        if not path.is_file():
            raise RuntimeError("File not found: %s" % path)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(limit + 1)
        if len(text) > limit:
            text = text[:limit] + "\n[...truncated...]"
        parts.append('<file path="%s">\n%s\n</file>' % (path, text))
        names.append(str(path))
    return parts, names


def build_user_content(prompt, files, cfg):
    parts, names = _read_files(files, cfg)
    return (prompt if not parts else prompt + "\n\n" + "\n\n".join(parts)), names


def _first_json_object(text):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_judge(text, n_candidates):
    data = _first_json_object(text or "")
    if data:
        try:
            data["confidence"] = float(data.get("confidence", 0))
            data["agreement"] = float(data.get("agreement", 0))
            if data.get("final_answer"):
                return data
        except Exception:
            pass
    return {"best_candidate": 1, "confidence": 0.0, "agreement": 0.0,
            "reason": "Judge reply could not be read; kept candidate 1.", "final_answer": None}


# --------------------------------------------------------------- tool: ask

def _provider_result(result, requested_model):
    """Normalise a providers.complete result into the run/api shape."""
    usage = result.get("usage") or {}
    actual = result.get("model") or requested_model
    api_usage = {
        "prompt_tokens": usage.get("input_tokens", 0) or 0,
        "completion_tokens": usage.get("output_tokens", 0) or 0,
    }
    if usage.get("cache_read_tokens") is not None:
        api_usage["prompt_cache_hit_tokens"] = usage.get("cache_read_tokens")
    cost = result.get("cost")
    if cost is None:
        cost = api.estimate_cost(str(actual), api_usage)
    return {
        "answer": result.get("answer") or "",
        "reasoning": None,
        "usage": api_usage,
        "cost": cost,
        "seconds": result.get("seconds"),
        "model": actual,
        "provider": result.get("provider"),
        "attempts": result.get("attempts") or [],
    }


def _configured_providers(cfg):
    try:
        return providers.load_providers(cfg)
    except Exception:
        return []


def _ask_with_providers(messages, run, args, cfg):
    """Ask an OpenAI-compatible provider when configured; return api shape."""
    model = str(args.get("model") or cfg.model or run.get("model") or "deepseek-flash")
    result = providers.complete(
        messages, model=model, providers=_configured_providers(cfg),
        timeout=cfg.timeout, max_tokens=args.get("max_tokens"),
        temperature=args.get("temperature"),
        reasoning_effort=run.get("reasoning_effort"))
    return _provider_result(result, model)


def _wants_provider_path(args, cfg):
    model = str(args.get("model") or "")
    if bool(cfg.get("provider_fallback")):
        return True
    if ":" in model:
        prefix = model.split(":", 1)[0]
        return any(item.get("name") == prefix for item in _configured_providers(cfg))
    return False


def tool_ask(args, cfg=None):
    cfg = _cfg(cfg)
    spend.preflight(cfg)
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("prompt is required")
    run = new_run("single", args, prompt, cfg)
    content, run["files"] = build_user_content(prompt, args.get("files"), cfg)
    messages = ([{"role": "system", "content": args["system"]}] if args.get("system") else []) + \
               [{"role": "user", "content": content}]
    started = time.time()
    provider_error = None
    try:
        if _wants_provider_path(args, cfg):
            try:
                result = _ask_with_providers(messages, run, args, cfg)
                run["provider"] = result.get("provider")
                run["provider_attempts"] = result.get("attempts")
                run["model"] = result.get("model") or run["model"]
            except providers.ProviderError as exc:
                provider_error = api.redact_secrets(str(exc))
                run["provider_error"] = provider_error
                if not cfg.get("provider_fallback"):
                    raise
                # Optional fallback: keep the legacy DeepSeek path working
                # even when every configured provider is unavailable.
                result = api.one_agent(messages, run["model"], run["reasoning_effort"],
                                       args.get("max_tokens"), args.get("temperature"),
                                       timeout=cfg.timeout, base_url=cfg.base_url)
                run["provider_fallback"] = "deepseek-direct"
        else:
            result = api.one_agent(messages, run["model"], run["reasoning_effort"],
                                   args.get("max_tokens"), args.get("temperature"),
                                   timeout=cfg.timeout, base_url=cfg.base_url)
    except Exception as exc:
        run.update(status="error", error=api.redact_secrets(str(exc)),
                   seconds=round(time.time() - started, 1))
        path = save_run(run, cfg)
        raise RuntimeError("%s\n(Run saved: %s)" % (api.redact_secrets(str(exc)), path))
    usage = result["usage"]
    run.update(status="ok", answer=result["answer"], reasoning=result.get("reasoning"),
               usage=usage,
               tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"),
               estimated_cost_usd=result["cost"], seconds=result["seconds"])
    if result.get("provider"):
        run["provider"] = result["provider"]
    if result.get("model"):
        run["model"] = result["model"]
    path = save_run(run, cfg)
    cost_text = ("$" + api.fmt_cost(result["cost"])
                 if result["cost"] is not None else "n/a")
    via = (" via %s" % run["provider"]) if run.get("provider") else ""
    text = ("[DeepSeek run %s | %s%s | %s s | tokens in %s / out %s | est. %s | saved: %s]\n\n%s"
            % (run["id"], run["model"], via, run["seconds"], run["tokens_in"],
               run["tokens_out"], cost_text, path, result["answer"]))
    if provider_error:
        text = ("[WARNING] configured providers failed (%s); used the direct "
                "DeepSeek path.\n" % provider_error) + text
    return _safe_warning(text, cfg)


# ----------------------------------------------------------- tool: consult

def tool_consult(args, cfg=None):
    """Ask 2-3 configured models independently; never synthesise a merge."""
    cfg = _cfg(cfg)
    spend.preflight(cfg)
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("prompt is required")
    models = args.get("models")
    if isinstance(models, str):
        models = [item.strip() for item in models.split(",") if item.strip()]
    if not isinstance(models, list) or not models:
        models = list(cfg.get("consult_models") or [])
    if not models:
        models = [cfg.model]
    models = [str(item).strip() for item in models if str(item or "").strip()][:5]
    if not models:
        raise RuntimeError("models must name at least one model")
    run = new_run("consult", args, prompt, cfg)
    content, run["files"] = build_user_content(prompt, args.get("files"), cfg)
    system = args.get("system")
    started = time.time()
    try:
        results = providers.consult(
            content, models=models, providers=_configured_providers(cfg),
            timeout=cfg.timeout, env=os.environ, system=system,
            reasoning_effort=run.get("reasoning_effort"),
            max_tokens=args.get("max_tokens"),
            temperature=args.get("temperature"))
    except Exception as exc:
        safe = api.redact_secrets(str(exc))
        run.update(status="error", error=safe,
                   seconds=round(time.time() - started, 1))
        path = save_run(run, cfg)
        raise RuntimeError("%s\n(Run saved: %s)" % (safe, path))
    answers = []
    total_cost = 0.0
    cost_known = True
    tin = tout = 0
    for item in results:
        row = {
            "model_requested": item.get("model_requested"),
            "provider": item.get("provider"),
            "model": item.get("model"),
            "answer": item.get("answer"),
            "error": item.get("error"),
            "usage": item.get("usage"),
            "cost": item.get("cost"),
            "seconds": item.get("seconds"),
        }
        answers.append(row)
        if item.get("cost") is None:
            cost_known = False
        else:
            total_cost += float(item["cost"])
        usage = item.get("usage") or {}
        tin += int(usage.get("input_tokens") or 0)
        tout += int(usage.get("output_tokens") or 0)
    run.update(
        status="ok", answers=answers, models=models,
        model="consult: " + ", ".join(models),
        tokens_in=tin, tokens_out=tout,
        estimated_cost_usd=round(total_cost, 6) if cost_known else None,
        cost_known=cost_known,
        seconds=round(time.time() - started, 1))
    path = save_run(run, cfg)
    parts = ["[Consult run %s | %d model(s) | %s s | est. %s | saved: %s]"
             % (run["id"], len(answers), run["seconds"],
                ("$" + api.fmt_cost(run["estimated_cost_usd"]))
                if cost_known else "n/a", path)]
    for index, row in enumerate(answers, 1):
        title = row.get("model_requested") or row.get("model") or "model"
        if row.get("provider"):
            title += " (%s)" % row["provider"]
        parts.append("\n---\n### Answer %d: %s\n" % (index, title))
        if row.get("error"):
            parts.append("ERROR: %s\n" % row["error"])
        else:
            parts.append((row.get("answer") or "").strip() + "\n")
    parts.append("\nThese are separate, independent answers; no synthesis was applied.")
    return _safe_warning("".join(parts), cfg)


# ------------------------------------------------------------- tool: swarm

def _swarm_int(args, name, default, minimum):
    """Return a validated integer option, or ``default`` when absent."""
    if args.get(name) is None:
        return default
    raw = args[name]
    if isinstance(raw, bool):
        raise ValueError("%s must be an integer >= %d" % (name, minimum))
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError("%s must be an integer >= %d" % (name, minimum))
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be an integer >= %d" % (name, minimum))
    if value < minimum:
        raise ValueError("%s must be an integer >= %d" % (name, minimum))
    return value


def _swarm_number(args, name, default, minimum, maximum=None):
    """Return a validated finite float option, or ``default`` when absent."""
    if args.get(name) is None:
        return default
    raw = args[name]
    if isinstance(raw, bool):
        raise ValueError("%s must be a finite number" % name)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a finite number" % name)
    if maximum is None:
        valid = math.isfinite(value) and value >= minimum
        detail = "a finite number >= %s" % minimum
    else:
        valid = math.isfinite(value) and minimum <= value <= maximum
        detail = "a finite number in [%s, %s]" % (minimum, maximum)
    if not valid:
        raise ValueError("%s must be %s" % (name, detail))
    return value


def tool_swarm(args, cfg=None):
    cfg = _cfg(cfg)
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("prompt is required")
    run = new_run("swarm", args, prompt, cfg)

    # Validate and normalise every option before the first HTTP call. The
    # run record keeps both the requested start and the effective first round,
    # so a smaller max_agents can never be silently raised.
    try:
        start_agents = _swarm_int(args, "start_agents", 3, 1)
        max_agents = _swarm_int(args, "max_agents", 40, 1)
        confidence_target = _swarm_number(args, "confidence_target", 0.85, 0.0, 1.0)
        max_cost = _swarm_number(args, "max_cost_usd", None, 0.0)
        max_seconds = _swarm_number(args, "max_seconds", None, 0.0)
        max_tokens = _swarm_int(args, "max_tokens", None, 1)
    except (TypeError, ValueError) as exc:
        run.update(status="error", error="Invalid swarm option: %s" % exc, seconds=0,
                   answer=None, judge_reason=None, confidence=None, agreement=None,
                   agents=[], agents_used=0, rounds=0, stop_reason="invalid option",
                   files=[], tokens_in=0, tokens_out=0,
                   estimated_cost_usd=0.0, cost_known=True)
        path = save_run(run, cfg)
        raise RuntimeError("Invalid swarm option: %s\n(Run saved: %s)" % (exc, path))

    start_agents_effective = min(start_agents, max_agents)
    run.update(
        start_agents=start_agents,
        start_agents_effective=start_agents_effective,
        max_agents=max_agents,
        confidence_target=confidence_target,
        max_cost_usd=max_cost,
        max_seconds=max_seconds,
        max_tokens=max_tokens,
    )

    started = time.monotonic()
    lock = threading.Lock()
    state = {"cost": 0.0, "tin": 0, "tout": 0, "cost_unknown": False}
    agents, judge = [], None
    stop_reason = None

    def budget_stop():
        """Return a stop reason if no further call may start, else None."""
        with lock:
            spent_cost = state["cost"]
            cost_unknown = state["cost_unknown"]
        if max_cost is not None and cost_unknown:
            return "unpriced model: cost budget cannot be enforced"
        if max_cost is not None and spent_cost >= max_cost:
            return "budget reached"
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            return "time limit reached"
        return None

    def account(result):
        with lock:
            if result.get("cost") is None:
                state["cost_unknown"] = True
            else:
                state["cost"] += result["cost"]
            state["tin"] += result["usage"].get("prompt_tokens", 0) or 0
            state["tout"] += result["usage"].get("completion_tokens", 0) or 0

    # A zero budget means zero requests and a saved error run, not a paid
    # first round followed by a budget check.
    pre_stop = budget_stop()
    if pre_stop:
        stop_reason = pre_stop + " before launch"
        run.update(
            status="error", answer=None, error=stop_reason, judge_reason=None,
            confidence=None, agreement=None, agents=[], agents_used=0, rounds=0,
            stop_reason=stop_reason, files=[], tokens_in=0, tokens_out=0,
            estimated_cost_usd=0.0, cost_known=True, seconds=0.0,
        )
        path = save_run(run, cfg)
        raise RuntimeError("Swarm failed: %s\n(Run saved: %s)" % (stop_reason, path))

    # A zero-budget run is saved above without touching the paid ledger. A
    # valid run still honours the optional hard daily limit before any call.
    spend.preflight(cfg)
    content, run["files"] = build_user_content(prompt, args.get("files"), cfg)
    model, reasoning = run["model"], run["reasoning_effort"]

    def run_agent(number, round_number):
        reason = budget_stop()
        if reason:
            return {"n": number, "round": round_number, "stopped": reason}
        persona = api.PERSONAS[(number - 1) % len(api.PERSONAS)]
        system = persona + ("\n\n" + args["system"] if args.get("system") else "")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": content}]
        record = {"n": number, "round": round_number, "persona": persona}
        try:
            timeout = cfg.timeout
            if max_seconds is not None:
                remaining = max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    record["stopped"] = "time limit reached"
                    return record
                timeout = min(cfg.timeout, remaining)
            result = api.one_agent(messages, model, reasoning, max_tokens,
                                   temperature=min(1.3, 0.3 + 0.1 * ((number - 1) % 10)),
                                   timeout=timeout, base_url=cfg.base_url)
            account(result)
            record.update(answer=result["answer"], seconds=result["seconds"],
                          cost=result["cost"])
        except Exception as exc:
            record.update(error=str(exc))
        return record

    def launch_round(first, count, round_number):
        """Run one round; serialise whenever a whole-swarm budget is set."""
        if max_cost is None and max_seconds is None:
            futures = [pool.submit(run_agent, number, round_number)
                       for number in range(first, first + count)]
            return [future.result() for future in futures], None
        records, reason = [], None
        for number in range(first, first + count):
            reason = budget_stop()
            if reason:
                break
            record = run_agent(number, round_number)
            if record.get("stopped"):
                reason = record["stopped"]
                break
            records.append(record)
            if max_cost is not None:
                with lock:
                    unpriced = state["cost_unknown"]
                if unpriced:
                    reason = "unpriced model: cost budget cannot be enforced"
                    break
        return records, reason

    round_number, size = 0, start_agents_effective
    pool = cf.ThreadPoolExecutor(max_workers=max(1, int(cfg.max_parallel)))
    try:
        while True:
            budget = budget_stop()
            if budget:
                stop_reason = budget
                break
            capacity = max_agents - len(agents)
            if capacity <= 0:
                stop_reason = "max agents reached (%d)" % max_agents
                break
            size = min(size, capacity)
            if size < 1:
                stop_reason = "max agents reached (%d)" % max_agents
                break
            round_number += 1
            first = len(agents) + 1
            jobs.progress("round %d: %d agent(s) working" % (round_number, size))
            records, launch_stop = launch_round(first, size, round_number)
            agents.extend(records)
            agents.sort(key=lambda item: item["n"])
            if launch_stop:
                stop_reason = launch_stop
            jobs.progress("round %d: %d agent(s) done, judging"
                          % (round_number, len(agents)))

            good = [agent for agent in agents if agent.get("answer")]
            if not good:
                stop_reason = stop_reason or "no agent answered"
                break

            budget = budget_stop()
            if budget:
                stop_reason = budget
                judge = {"best_candidate": 1, "confidence": None, "agreement": None,
                         "reason": "Stopped before the judge: " + stop_reason,
                         "final_answer": good[0]["answer"], "skipped": True}
                break

            candidates = "\n\n".join(
                "<candidate %d>\n%s\n</candidate %d>" % (index, agent["answer"], index)
                for index, agent in enumerate(good, 1))
            judge_messages = [
                {"role": "system", "content": api.JUDGE_SYSTEM},
                {"role": "user", "content": "<task>\n%s\n</task>\n\n%s" % (content, candidates)},
            ]
            try:
                judge_timeout = cfg.timeout
                if max_seconds is not None:
                    remaining = max_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise RuntimeError("time limit reached")
                    judge_timeout = min(cfg.timeout, remaining)
                judge_result = api.one_agent(judge_messages, model, reasoning, None,
                                             temperature=0.0, timeout=judge_timeout,
                                             base_url=cfg.base_url)
                account(judge_result)
                judge = parse_judge(judge_result["answer"], len(good))
                judge["seconds"] = judge_result["seconds"]
            except Exception as exc:
                judge = {"best_candidate": 1, "confidence": 0.0, "agreement": 0.0,
                         "reason": "Judge failed: %s" % exc, "final_answer": None}
            if not judge.get("final_answer"):
                try:
                    index = int(judge.get("best_candidate") or 1)
                except (TypeError, ValueError):
                    index = 1
                judge["final_answer"] = good[min(max(index, 1), len(good)) - 1]["answer"]

            budget = budget_stop()
            if budget:
                stop_reason = budget
                break
            jobs.progress("round %d: judge confidence %.2f"
                          % (round_number, judge["confidence"]))
            if judge["confidence"] >= confidence_target:
                stop_reason = "judge confident (%.2f >= %s)" % (
                    judge["confidence"], confidence_target)
                break
            if len(agents) >= max_agents:
                stop_reason = "max agents reached (%d)" % max_agents
                break
            size = len(agents)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    seconds = round(time.monotonic() - started, 1)
    if stop_reason is None:
        stop_reason = "max agents reached (%d)" % max_agents
    with lock:
        tokens_in, tokens_out = state["tin"], state["tout"]
        cost_unknown = state["cost_unknown"]
        total_cost = state["cost"]
    run.update(
        status="ok" if judge and judge.get("final_answer") else "error",
        answer=(judge or {}).get("final_answer"),
        error=None if judge and judge.get("final_answer") else stop_reason,
        judge_reason=(judge or {}).get("reason"),
        confidence=(judge or {}).get("confidence"),
        agreement=(judge or {}).get("agreement"),
        judge_skipped=bool((judge or {}).get("skipped")),
        agents=agents, agents_used=len(agents), rounds=round_number,
        stop_reason=stop_reason,
        tokens_in=tokens_in, tokens_out=tokens_out,
        estimated_cost_usd=None if cost_unknown else round(total_cost, 6),
        cost_known=not cost_unknown, seconds=seconds,
    )
    path = save_run(run, cfg)
    if run["status"] != "ok":
        errors = "; ".join(agent.get("error", "") for agent in agents[:3]
                           if agent.get("error"))
        raise RuntimeError("Swarm failed: %s. Errors: %s\n(Run saved: %s)" % (
            stop_reason, errors, path))
    swarm_cost = ("$" + api.fmt_cost(run["estimated_cost_usd"])
                  if run["estimated_cost_usd"] is not None else "n/a")
    confidence_text = ("n/a" if run["confidence"] is None
                       else "%.2f" % run["confidence"])
    agreement_text = ("n/a" if run["agreement"] is None
                      else "%.2f" % run["agreement"])
    text = ("[DeepSeek swarm %s | %d agents, %d round(s) | confidence %s, agreement %s | "
            "%s s | est. %s | stopped: %s | saved: %s]\n\n%s"
            % (run["id"], len(agents), round_number, confidence_text, agreement_text,
               seconds, swarm_cost, stop_reason, path, run["answer"]))
    return _safe_warning(text, cfg)


# --------------------------------------------------------------- list/read

def tool_list(args, cfg=None):
    cfg = _cfg(cfg)
    limit = max(0, min(int(args.get("limit") or 10), 1000))
    runs = load_runs(cfg)[:limit]
    if not runs:
        return "No saved runs yet in %s." % cfg.log_dir
    lines = ["Saved runs in %s:" % cfg.log_dir]
    for run in runs:
        extra = " | %s agents" % run.get("agents_used") if run.get("kind") == "swarm" else ""
        if run.get("kind") == "harness" and run.get("subagents") is not None:
            extra = " | %s subagents" % run.get("subagents")
        quality = ""
        if run.get("quality_score") is not None:
            quality = " | Q %.2f/10" % run["quality_score"]
        lines.append(
            "- %s | %s | %s%s | %s | %s | in %s / out %s | %s | %s s%s | %s" % (
                run["id"], run.get("created"), run.get("kind", "single"), extra,
                run.get("status"), run.get("model"), run.get("tokens_in", "?"),
                run.get("tokens_out", "?"),
                ("$" + api.fmt_cost(run.get("estimated_cost_usd"))
                 if run.get("estimated_cost_usd") is not None else "n/a"),
                run.get("seconds", "?"), quality, run.get("title")))
    return "\n".join(lines)


def tool_read(args, cfg=None):
    cfg = _cfg(cfg)
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("run_id is required")
    return read_run_md(run_id, cfg)


# ------------------------------------------------------------- harness tool

def tool_harness(args, cfg=None):
    cfg = _cfg(cfg)
    spend.preflight(cfg)
    ctx = getattr(_context, "value", None) or {}
    progress_cb = ctx.get("progress_cb") or jobs.progress
    # A normal (non-task) tools/call must honour notifications/cancelled too.
    # dispatch() installs this event for every request; the run loop polls it
    # in every child monitor and between phases.
    cancel_event = ctx.get("cancel_event")
    cancel_check = cancel_event.is_set if cancel_event is not None else None
    if (args.get("resume_run_id") or "").strip():
        text, _run, _path = harness.resume_harness(
            args, cfg, progress_cb=progress_cb, cancel_check=cancel_check)
    else:
        text, _run, _path = harness.run_harness(
            args, cfg, progress_cb=progress_cb, cancel_check=cancel_check)
    return _safe_warning(text, cfg)


# --------------------------------------------------------------- skills tools

def tool_rate(args, cfg=None):
    cfg = _cfg(cfg)
    return skills.rate_run((args.get("run_id") or "").strip(), args.get("rating"),
                           args.get("note") or "", cfg)


_OFF_SKILLS_ERROR = "auto_skills is off; set DSH_AUTO_SKILLS to auto or propose"
_SKILL_STATUSES = ("all", "seed", "active", "candidate", "demoted", "retired")


def _skill_stats(row):
    stats = row.get("stats")
    return stats if isinstance(stats, dict) else {}


def _skill_mean(stats):
    try:
        count = int(stats.get("score_count") or 0)
        total = float(stats.get("score_sum") or 0.0)
    except (TypeError, ValueError):
        return None
    return (total / count) if count > 0 else None


def _skill_baseline(stats):
    for name in ("baseline", "baseline_score", "baseline_value"):
        value = stats.get(name)
        if isinstance(value, (int, float)):
            return float(value)
    ref = stats.get("baseline_ref")
    if isinstance(ref, dict):
        for name in ("median", "mean"):
            value = ref.get(name)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _fmt_skill_number(value):
    return "n/a" if value is None else "%.2f" % float(value)


def _skill_line(row):
    key = row.get("key") or "?"
    status = row.get("status")
    stats = _skill_stats(row)
    if not status:
        return "- %s | %s | %s" % (key, row.get("title") or "", row.get("path") or "")
    text = str(status)
    if row.get("approved") is not None:
        text += " (approved)" if row.get("approved") else " (unapproved)"
    version = row.get("version")
    parts = ["- %s | %s | v%s" % (key, text, version if version is not None else "?")]
    try:
        uses = int(stats.get("uses") or 0)
    except (TypeError, ValueError):
        uses = 0
    detail = "%d uses" % uses
    mean = _skill_mean(stats)
    baseline = _skill_baseline(stats)
    if mean is not None:
        detail += ", mean %s" % _fmt_skill_number(mean)
        if baseline is not None:
            detail += ", baseline %s, delta %+0.2f" % (_fmt_skill_number(baseline), mean - baseline)
    parts.append(detail)
    if row.get("cluster_id"):
        parts.append("cluster %s" % row["cluster_id"])
    if row.get("retired_reason"):
        parts.append("retired: %s" % row["retired_reason"])
    return " | ".join(parts)


def _skill_rows(cfg, status="all"):
    return skills.list_skills(cfg, status=status) or []


def tool_list_skills(args, cfg=None):
    cfg = _cfg(cfg)
    args = args or {}
    status = str(args.get("status") or "all").strip().lower()
    if status not in _SKILL_STATUSES:
        raise RuntimeError("Unknown status %r; allowed: %s" % (
            status, ", ".join(_SKILL_STATUSES[1:])))
    fmt = str(args.get("format") or "text").strip().lower()
    if fmt not in ("text", "json"):
        raise RuntimeError("Unknown format %r; allowed: text, json" % fmt)
    rows = _skill_rows(cfg, status)
    if fmt == "json":
        out = []
        for row in rows:
            item = dict(row)
            item.pop("content", None)
            stats = _skill_stats(item)
            if stats:
                item["stats"] = {key: stats.get(key) for key in (
                    "uses", "score_count", "score_sum", "mean_score", "last_score",
                    "wins", "losses", "consecutive_bad")}
            history = item.get("history")
            if isinstance(history, list):
                item["history"] = history[-5:]
            out.append(item)
        return json.dumps(out, sort_keys=True, default=str)
    try:
        where = str(skills.resolved_skills_dir(cfg))
    except Exception:
        where = "skills"
    lines = ["Skills in %s (%d shown):" % (where, len(rows))]
    lines.extend(_skill_line(row) for row in rows)
    return "\n".join(lines)


def tool_read_skill(args, cfg=None):
    cfg = _cfg(cfg)
    args = args or {}
    name = args.get("name") or args.get("skill") or ""
    version = None
    raw_version = args.get("version")
    if raw_version is not None and str(raw_version).strip() != "":
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise RuntimeError("version must be an integer >= 1")
        if version < 1:
            raise RuntimeError("version must be an integer >= 1")
    meta = bool(args.get("meta"))
    result = skills.read_skill(name, cfg, version=version, meta=meta)
    if isinstance(result, dict):
        content = result.get("content") or ""
        if not meta:
            return content
        info = result.get("meta") if isinstance(result.get("meta"), dict) else result
        lines = [content, "", "Skill: %s | status %s | version %s" % (
            result.get("key") or name, info.get("status") or "?",
            info.get("version") if info.get("version") is not None else "?")]
        return "\n".join(lines)
    return result


def _require_auto_skills(cfg):
    if str(getattr(cfg, "auto_skills", "auto") or "auto").strip().lower() == "off":
        raise RuntimeError(_OFF_SKILLS_ERROR)


def _registry(cfg):
    from .skill_registry import SkillRegistry
    return SkillRegistry(cfg)


def _known_skill_keys(cfg):
    try:
        rows = skills.list_skills(cfg)
    except Exception:
        rows = []
    keys = [str(row.get("key")) for row in rows if isinstance(row, dict) and row.get("key")]
    if not keys:
        keys = [str(item.get("key")) for item in getattr(skills, "SKILLS", [])
                if isinstance(item, dict) and item.get("key")]
    return keys[:20]


def _resolve_skill(name, cfg):
    name = (name or "").strip()
    if not name:
        raise RuntimeError("name is required")
    registry = _registry(cfg)
    key = registry.resolve_key(name)
    if not key:
        known = _known_skill_keys(cfg)
        raise RuntimeError("Unknown skill %r. Known skills: %s" % (
            name, ", ".join(known) if known else "none"))
    return registry, key


def _skill_meta_info(key, cfg):
    try:
        meta = skills.skill_meta(key, cfg) or {}
    except TypeError:
        meta = skills.skill_meta(key) or {}
    if not isinstance(meta, dict):
        return {}
    nested = meta.get("meta")
    if isinstance(nested, dict):
        merged = dict(meta)
        for field in ("status", "version", "approved", "history", "stats"):
            if field not in merged and field in nested:
                merged[field] = nested[field]
        return merged
    return meta


def tool_approve_skill(args, cfg=None):
    cfg = _cfg(cfg)
    args = args or {}
    _require_auto_skills(cfg)
    registry, key = _resolve_skill(args.get("name"), cfg)
    info = _skill_meta_info(key, cfg)
    status = str(info.get("status") or "unknown")
    if status not in ("candidate", "demoted", "retired"):
        raise RuntimeError("Skill %r is %s. approve_skill allows candidate, demoted or retired; "
                           "use retire_skill to stop injection." % (key, status))
    note = str(args.get("note") or "").strip() or "manual approval"
    entry = registry.set_status(key, "candidate", note, approved=True) or {}
    version = entry.get("version") if isinstance(entry, dict) else "?"
    return ("Approved %s at v%s: status candidate, approved=true. It is injectable now; "
            "promotion to active still needs the measured runs." % (key, version))


def tool_reject_skill(args, cfg=None):
    cfg = _cfg(cfg)
    args = args or {}
    _require_auto_skills(cfg)
    registry, key = _resolve_skill(args.get("name"), cfg)
    info = _skill_meta_info(key, cfg)
    status = str(info.get("status") or "unknown")
    if status != "candidate":
        raise RuntimeError("Skill %r is %s. reject_skill only applies to candidates; "
                           "use retire_skill for seed, active or demoted skills." % (key, status))
    reason = str(args.get("reason") or "").strip() or "rejected"
    entry = registry.set_status(key, "retired", reason, approved=False) or {}
    version = entry.get("version") if isinstance(entry, dict) else "?"
    return "Rejected %s: retired at v%s; its versions remain readable." % (key, version)


def tool_retire_skill(args, cfg=None):
    cfg = _cfg(cfg)
    args = args or {}
    _require_auto_skills(cfg)
    registry, key = _resolve_skill(args.get("name"), cfg)
    info = _skill_meta_info(key, cfg)
    status = str(info.get("status") or "unknown")
    if status == "retired":
        raise RuntimeError("Skill %r is already retired." % key)
    reason = str(args.get("reason") or "").strip() or "manual retirement"
    entry = registry.set_status(key, "retired", reason, approved=False) or {}
    version = entry.get("version") if isinstance(entry, dict) else "?"
    return ("Retired %s at v%s; it is no longer injected but remains readable and "
            "rollback-able." % (key, version))


def tool_rollback_skill(args, cfg=None):
    cfg = _cfg(cfg)
    args = args or {}
    _require_auto_skills(cfg)
    registry, key = _resolve_skill(args.get("name"), cfg)
    info = _skill_meta_info(key, cfg)
    try:
        current = int(info.get("version") or 0)
    except (TypeError, ValueError):
        current = 0
    available = set()
    history = info.get("history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict) and item.get("version") is not None:
                try:
                    available.add(int(item["version"]))
                except (TypeError, ValueError):
                    pass
    if current:
        available.add(current)
    raw_version = args.get("version")
    if raw_version is None or str(raw_version).strip() == "":
        if current <= 1:
            raise RuntimeError("Skill %r has no previous version; current version is %s" % (
                key, current or 1))
        target = current - 1
    else:
        try:
            target = int(raw_version)
        except (TypeError, ValueError):
            raise RuntimeError("version must be an integer >= 1")
        if target < 1:
            raise RuntimeError("version must be an integer >= 1")
    if available and target not in available:
        raise RuntimeError("Skill %r has no version %s. Available versions: %s" % (
            key, target, ", ".join(str(item) for item in sorted(available))))
    if current and target >= current:
        raise RuntimeError("Skill %r version %s is not a previous version; current version is %s" % (
            key, target, current))
    result = registry.rollback(key, version=target) or {}
    return ("Rolled back %s from v%s to v%s as new v%s (history is immutable)." % (
        key, result.get("from_version", current), result.get("restored_from", target),
        result.get("to_version", "?")))


# ---------------------------------------------------------------- mac tools

def tool_keep_awake(args, cfg=None):
    return mac.tool_keep_awake(args, _cfg(cfg))


def tool_screenshot(args, cfg=None):
    return mac.take_screenshot(args, _cfg(cfg))


def tool_restart_client(args, cfg=None):
    return clients.restart_client_app(args, _cfg(cfg))


def tool_maintenance(args, cfg=None):
    cfg = _cfg(cfg)
    action = (args.get("action") or "status").strip().lower()
    from . import health as health_mod
    board = health_mod.HealthBoard(cfg.state_dir)
    if action == "health":
        lines = list(board.status_lines())
        snapshot = board.snapshot().get("components") or {}
        for name in sorted(snapshot):
            blocked = board.quarantine_block(name)
            if blocked:
                lines.append("  ! " + blocked)
        return "\n".join(lines)
    if action == "clear_health":
        component = (args.get("component") or "").strip() or None
        cleared = board.clear(component)
        scope = component or "all components"
        return ("Health cleared: %d quarantined component(s) reset (%s).\n%s"
                % (cleared, scope, "\n".join(board.status_lines())))
    if action == "status":
        roots = {}
        for name in ("log_dir", "jobs_dir", "state_dir"):
            raw = cfg.get(name)
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            state = "missing"
            try:
                if path.exists():
                    state = "dir" if path.is_dir() else "file"
                    mode = oct(path.stat().st_mode & 0o777)
                    state += " mode=%s" % mode
            except OSError:
                state = "unreadable"
            roots[name] = {"path": str(path), "state": state}
        counts = {}
        pairs = [("runs", cfg.get("log_dir")), ("jobs", cfg.get("jobs_dir"))]
        state_raw = cfg.get("state_dir")
        if state_raw:
            state_path = Path(str(state_raw)).expanduser()
            pairs.append(("traces", state_path / "traces"))
            pairs.append(("checkpoints", state_path / "checkpoints"))
        for name, raw in pairs:
            if not raw:
                counts[name] = 0
                continue
            try:
                counts[name] = sum(1 for item in Path(str(raw)).expanduser().iterdir()
                                   if item.is_file())
            except OSError:
                counts[name] = 0
        return ("Local data: %s. File counts: %s. Retention: days=%s, keep_runs=%s, "
                "keep_jobs=%s. Use action=cleanup or action=dry_run. %s"
                % (roots, counts, cfg.get("retention_days"), cfg.get("keep_runs"),
                   cfg.get("keep_jobs"), " ".join(board.status_lines())))
    try:
        result = retention.cleanup(cfg, dry_run=(action == "dry_run"))
    except Exception as exc:
        raise RuntimeError("Cleanup refused: %s" % api.redact_secrets(str(exc)))
    return ("Maintenance %s: deleted=%s kept=%s bytes=%s windows=%s errors=%s"
            % ("dry run" if action == "dry_run" else "cleanup",
               result.get("deleted"), result.get("kept"), result.get("bytes"),
               result.get("dry_run"), len(result.get("errors") or [])))


def tool_status(args, cfg=None):
    return jobs.tool_status(args, _cfg(cfg))


def tool_resume(args, cfg=None):
    cfg = _cfg(cfg)
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("run_id is required")
    forwarded = {"resume_run_id": run_id,
                 "wait_seconds": args.get("wait_seconds")}
    for name in ("answer", "option"):
        if args.get(name) is not None:
            forwarded[name] = args.get(name)
    return tool_harness(forwarded, cfg)


def tool_cancel(args, cfg=None):
    return jobs.tool_cancel(args, _cfg(cfg))


# --------------------------------------------------------------- background

def _job_options():
    return _job_handler_options()


def bg_ask(args, cfg=None):
    return jobs.start_job("ask_deepseek", args, cfg=cfg, **_job_options())


def bg_consult(args, cfg=None):
    return jobs.start_job("consult_deepseek", args, cfg=cfg, **_job_options())


def bg_swarm(args, cfg=None):
    return jobs.start_job("swarm_deepseek", args, cfg=cfg, **_job_options())


def bg_harness(args, cfg=None):
    return jobs.start_job("harness_deepseek", args, cfg=cfg, **_job_options())


def bg_resume(args, cfg=None):
    return jobs.start_job("resume_deepseek", args, cfg=cfg, **_job_options())


TOOL_IMPL = {
    "ask_deepseek": tool_ask,
    "consult_deepseek": tool_consult,
    "swarm_deepseek": tool_swarm,
    "list_deepseek_runs": tool_list,
    "read_deepseek_run": tool_read,
    "deepseek_job_status": tool_status,
    "deepseek_job_cancel": tool_cancel,
    "harness_deepseek": tool_harness,
    "resume_deepseek": tool_resume,
    "keep_awake": tool_keep_awake,
    "take_screenshot": tool_screenshot,
    "restart_client_app": tool_restart_client,
    "rate_deepseek_run": tool_rate,
    "list_skills": tool_list_skills,
    "read_skill": tool_read_skill,
    "approve_skill": tool_approve_skill,
    "reject_skill": tool_reject_skill,
    "retire_skill": tool_retire_skill,
    "rollback_skill": tool_rollback_skill,
    "deepseek_maintenance": tool_maintenance,
}

HANDLERS = {
    "ask_deepseek": bg_ask,
    "consult_deepseek": bg_consult,
    "swarm_deepseek": bg_swarm,
    "harness_deepseek": bg_harness,
    "resume_deepseek": bg_resume,
    "keep_awake": tool_keep_awake,
    "take_screenshot": tool_screenshot,
    "restart_client_app": tool_restart_client,
    "list_deepseek_runs": tool_list,
    "read_deepseek_run": tool_read,
    "deepseek_job_status": tool_status,
    "deepseek_job_cancel": tool_cancel,
    "rate_deepseek_run": tool_rate,
    "list_skills": tool_list_skills,
    "read_skill": tool_read_skill,
    "approve_skill": tool_approve_skill,
    "reject_skill": tool_reject_skill,
    "retire_skill": tool_retire_skill,
    "rollback_skill": tool_rollback_skill,
    "deepseek_maintenance": tool_maintenance,
}

# Backward-compatible but hidden names (v3.5.0 clients keep working).
LEGACY_TOOL_ALIASES = {
    "keep_mac_awake": "keep_awake",
    "mac_screenshot": "take_screenshot",
    "restart_claude_app": "restart_client_app",
}

# Worker processes call the direct implementations, never the background wrappers.
JOB_IMPL = TOOL_IMPL


def tools_for_mode(mode="all"):
    if mode == "mac":
        return [tool for tool in TOOLS if tool["name"] in MAC_TOOL_NAMES]
    return list(TOOLS)


# ------------------------------------------------------------- JSON-RPC/MCP

class RpcError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def _ok(result):
    return {"result": result}


def _err(code, message):
    return {"error": {"code": code, "message": message}}


_out_lock = threading.Lock()
_workers = []
_notifier = None
_task_stores = {}
_task_store_lock = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()
_task_events = {}
_context = threading.local()


def set_notifier(fn):
    """Install a notification sink (tests use this; production writes stdout)."""
    global _notifier
    _notifier = fn


def notify(method, params):
    """Send one JSON-RPC notification without an id."""
    frame = {"jsonrpc": "2.0", "method": method, "params": params}
    sink = _notifier
    if sink is not None:
        try:
            sink(frame)
            return
        except Exception:
            pass
    with _out_lock:
        sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _progress_token(params):
    meta = params.get("_meta")
    if isinstance(meta, dict) and meta.get("progressToken") is not None:
        return meta.get("progressToken")
    return params.get("progressToken")


def _task_store(cfg):
    root = str(Path(cfg.state_dir) / "mcp-tasks")
    with _task_store_lock:
        store = _task_stores.get(root)
        if store is None:
            store = tasks.TaskStore(root)
            _task_stores[root] = store
        return store


def _make_progress(store=None, task_id=None, token=None):
    # ``notify`` takes (method, params); ProgressEmitter sends one frame dict.
    emitter = (tasks.ProgressEmitter(
        lambda frame: notify(frame["method"], frame["params"]), token)
        if token is not None else None)
    counter = [0]

    def callback(state):
        counter[0] += 1
        # Harness progress callbacks pass a text line; job callbacks may pass a
        # state mapping.  Both shapes must reach the client.
        if isinstance(state, dict):
            message = str(state.get("progress") or state.get("status") or "working")
        else:
            message = str(state or "working")
        if store is not None and task_id:
            try:
                store.progress(task_id, counter[0], message=message)
            except Exception:
                pass
        if emitter is not None:
            emitter.emit(counter[0], message=message)

    return callback


def _terminal(task):
    return (task or {}).get("status") in ("completed", "failed", "cancelled")


def _task_job_started(store, task_id, run_id):
    try:
        store.update(task_id, job_id=run_id)
        store.progress(task_id, 1, message="job %s started" % run_id)
    except Exception:
        pass


def _task_payload(task_id, text=None, error=None):
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    if error is not None:
        payload = {"content": [{"type": "text", "text": "Error: %s" % error}],
                   "isError": True}
    else:
        payload = {"content": [{"type": "text", "text": text or ""}],
                   "isError": False}
    payload["_meta"] = {"io.modelcontextprotocol/related-task": {"taskId": task_id}}
    return payload


_JOB_TERMINAL = ("done", "error", "cancelled")


def _cfg_get(cfg, name, default=None):
    try:
        return cfg.get(name, default)
    except AttributeError:
        return getattr(cfg, name, default)


def _job_state(run_id, cfg):
    """Best-effort job status mapping; ``None`` when absent/unreadable."""
    if not run_id:
        return None
    path = Path(cfg.jobs_dir) / (str(run_id) + ".status.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _task_journal(work_id, cfg):
    """Return ``(events, result_record)`` for one work item; never raises."""
    if not work_id or not jobs.work.valid_work_id(str(work_id)):
        return [], None
    try:
        log_obj = events.EventLog(Path(cfg.state_dir) / "work" / str(work_id))
        return log_obj.read_since(0), log_obj.read_result()
    except Exception:
        return [], None


def _refresh_task_from_job(store, task_id, cfg):
    """Sync one MCP task from its detached job and journal.

    A detached job keeps the task ``working`` (or ``input_required`` for a
    validated question) until the job/journal reaches a terminal state.  This
    is the P1-3 truth fix: the handler returning is not completion.
    """
    task = store.get(task_id)
    if task.get("status") in ("completed", "failed", "cancelled"):
        return task
    completion = str(_cfg_get(cfg, "task_completion", "job")
                     or "job").strip().lower()
    if completion == "request":
        return task
    run_id = task.get("job_id")
    if run_id and not jobs._valid_run_id(str(run_id)):
        run_id = None
    state = _job_state(run_id, cfg) if run_id else None
    work_id = (state or {}).get("work_id") or task.get("work_id")
    journal, result_record = _task_journal(work_id, cfg)
    question = events.pending_question(journal)
    if state is None and not journal:
        return task
    current_run_id = events.latest_admitted_run_id(journal) or run_id
    if (result_record and current_run_id
            and result_record.get("run_id") not in (None, current_run_id)):
        result_record = None
    run = None
    if current_run_id:
        try:
            run = find_run(current_run_id, cfg)[0]
        except Exception:
            run = None
    projection = events.project(run, state, journal, result_record)
    status = str((state or {}).get("status") or "").lower()
    current_terminal = events.terminal_after_latest_admission(journal)
    terminal_event = current_terminal

    current_kind = (current_terminal or {}).get("type")
    current_payload = (current_terminal or {}).get("payload") or {}
    result_status = str((result_record or {}).get("status") or "").lower()
    result_failed = result_status in (
        "error", "failed", "no_balance", "deadline_exceeded",
        "budget_exceeded")

    if status == "cancelled" or (
            state is None and current_kind == "cancelled"):
        return store.cancel(task_id, reason=(state or {}).get("error")
                            or "cancelled by client")
    if status == "error" or (state is None and current_kind == "failed"):
        message = (state or {}).get("error")
        if not message:
            message = current_payload.get("error")
        return store.fail(task_id, message or "job failed")

    if question is not None:
        if task.get("status") != "input_required":
            return store.update(
                task_id, status="input_required",
                question=question["question"], options=list(question["options"]),
                work_id=work_id,
                statusMessage=question["question"])
        return task
    if task.get("status") == "input_required":
        store.update(task_id, status="working", question=None, options=[],
                     statusMessage=None)

    if status in _JOB_TERMINAL:
        if status == "done":
            if work_id:
                # Only the latest attempt's terminal event may complete the
                # task; an old result before a newer admitted is ignored.
                if current_terminal is None:
                    if terminal_event is None and result_record is not None:
                        if result_failed:
                            return store.fail(
                                task_id,
                                (result_record or {}).get("result")
                                or "job failed")
                        text = projection.get("result")
                        return store.complete(
                            task_id,
                            _task_payload(task_id, text=str(text or "")))
                    return store.get(task_id)
                if current_kind == "failed":
                    return store.fail(
                        task_id, current_payload.get("error")
                        or (result_record or {}).get("result") or "job failed")
                if current_kind == "cancelled":
                    return store.cancel(task_id, reason="cancelled by client")
                if result_failed:
                    return store.fail(
                        task_id, (result_record or {}).get("result")
                        or "job failed")
            if projection["terminal"] or not work_id:
                text = projection.get("result")
                if text is None:
                    text = (state or {}).get("result") or ""
                return store.complete(
                    task_id, _task_payload(task_id, text=str(text)))
        return store.get(task_id)
    if state is None:
        if current_kind is not None:
            if current_kind == "failed":
                return store.fail(task_id, current_payload.get("error")
                                  or "job failed")
            if current_kind == "cancelled":
                return store.cancel(task_id, reason="cancelled by client")
            text = (current_payload.get("result")
                    or (result_record or {}).get("result") or "")
            return store.complete(task_id, _task_payload(task_id, text=str(text)))
        return task
    return store.get(task_id)


def _execute_task(name, args, cfg, ctx):
    _context.value = ctx
    store = ctx["store"]
    task_id = ctx["task_id"]
    try:
        fn = HANDLERS[name]
        text = fn(args, cfg)
        if ctx["cancel_event"].is_set() or store.is_cancelled(task_id):
            task = store.cancel(task_id, reason="cancelled by client")
        else:
            current = store.get(task_id)
            completion = str(_cfg_get(cfg, "task_completion", "job")
                             or "job").lower()
            if completion != "request" and current.get("job_id"):
                # The handler detached a job; its return text is only a
                # "still running" marker.  The journal decides terminality.
                task = _refresh_task_from_job(store, task_id, cfg)
            else:
                task = store.complete(task_id, _task_payload(task_id, text=text))
    except Exception as exc:
        if ctx["cancel_event"].is_set() or store.is_cancelled(task_id):
            task = store.cancel(task_id, reason="cancelled by client")
        else:
            task = store.fail(task_id, api.redact_secrets(str(exc)))
    finally:
        _context.value = None
        _task_events.pop(task_id, None)
    try:
        notify("notifications/tasks/status", tasks.TaskStore.to_wire(task))
    except Exception:
        pass


def _task_augmented_call(name, args, params, cfg, context):
    store = _task_store(cfg)
    requested = params.get("task")
    ttl = None
    if isinstance(requested, dict) and requested.get("ttl") is not None:
        try:
            ttl = max(1, int(float(requested["ttl"]) / 1000.0))
        except (TypeError, ValueError):
            ttl = None
    task = store.create(name, args,
                        ttl=ttl or int(float(cfg.get("task_ttl") or 3600)))
    token = _progress_token(params)
    cancel_event = threading.Event()
    ctx = dict(context or {})
    ctx.update({
        "task_id": task["taskId"],
        "store": store,
        "cancel_event": cancel_event,
        "progress_token": token,
        "progress_cb": _make_progress(store, task["taskId"], token),
        "on_start": lambda run_id, _path: _task_job_started(
            store, task["taskId"], run_id),
    })
    _task_events[task["taskId"]] = cancel_event
    thread = threading.Thread(
        target=_execute_task, args=(name, dict(args), cfg, ctx),
        name="mcp-task-%s" % task["taskId"], daemon=True)
    thread.start()
    return _ok({"task": tasks.TaskStore.to_wire(task)})


def _cancel_task(task_id, cfg, reason="cancelled by request"):
    store = _task_store(cfg)
    task = store.get(task_id)
    if _terminal(task):
        raise ValueError("already terminal status %r" % task.get("status"))
    event = _task_events.get(task_id)
    if event is not None:
        event.set()
    job_id = task.get("job_id")
    if job_id:
        try:
            jobs.cancel_job(job_id, cfg, reason=reason)
        except Exception:
            pass
    task = store.cancel(task_id, reason=reason)
    notify("notifications/tasks/status", tasks.TaskStore.to_wire(task))
    return task


def _wait_task(store, task_id, cfg):
    try:
        timeout = float(cfg.get("timeout") or 600)
    except (TypeError, ValueError):
        timeout = 600.0
    deadline = time.monotonic() + max(1.0, min(timeout, 600.0))
    while True:
        task = _refresh_task_from_job(store, task_id, cfg)
        if _terminal(task) or task.get("status") == "input_required":
            return task
        if time.monotonic() >= deadline:
            raise TimeoutError("task result is still pending")
        time.sleep(0.2)


def _handle_task_method(method, params, cfg):
    store = _task_store(cfg)
    if method == "tasks/list":
        # Listing is not scoped to one task, so it must not require taskId.
        try:
            items = store.list(limit=50)
            return _ok({"tasks": [tasks.TaskStore.to_wire(item)
                                  for item in items]})
        except Exception as exc:
            return _err(-32603, api.redact_secrets(str(exc)))
    task_id = params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        return _err(-32602, "taskId is required")
    try:
        if method == "tasks/get":
            task = _refresh_task_from_job(store, task_id, cfg)
            return _ok(tasks.TaskStore.to_wire(task))
        if method == "tasks/cancel":
            return _ok(tasks.TaskStore.to_wire(
                _cancel_task(task_id, cfg, reason="cancelled by tasks/cancel")))
        if method == "tasks/result":
            task = _wait_task(store, task_id, cfg)
            if task.get("status") == "cancelled":
                return _err(-32602, "Task %s was cancelled" % task_id)
            if task.get("status") == "input_required":
                question = task.get("question") or task.get("statusMessage")
                options = list(task.get("options") or [])
                payload = _task_payload(
                    task_id,
                    text="Input required: %s%s" % (
                        question or "answer the question",
                        (" (options: %s)" % ", ".join(options)) if options else ""))
                payload["structuredContent"] = {
                    "status": "input_required",
                    "question": question,
                    "options": options,
                }
                return _ok(payload)
            payload = task.get("result")
            if not isinstance(payload, dict):
                payload = _task_payload(
                    task_id, text=task.get("statusMessage") or task.get("status"),
                    error=task.get("error") if task.get("status") == "failed" else None)
            payload = dict(payload)
            meta = dict(payload.get("_meta") or {})
            meta["io.modelcontextprotocol/related-task"] = {"taskId": task_id}
            payload["_meta"] = meta
            return _ok(payload)
    except KeyError:
        return _err(-32602, "Failed to retrieve task: Task not found")
    except ValueError as exc:
        return _err(-32602, "Cannot cancel task: %s" % exc)
    except TimeoutError as exc:
        return _err(-32603, str(exc))
    return _err(-32601, "Method not found: %s" % method)


def _job_handler_options():
    ctx = getattr(_context, "value", None) or {}
    options = {}
    if ctx.get("progress_cb") is not None:
        options["progress_cb"] = ctx["progress_cb"]
    if ctx.get("cancel_event") is not None:
        options["cancel_event"] = ctx["cancel_event"]
    if ctx.get("on_start") is not None:
        options["on_start"] = ctx["on_start"]
    return options


def dispatch(method, params=None, tool_mode="all", cfg=None, context=None):
    """Handle one MCP method and return a JSON-RPC result/error dict."""
    params = params if isinstance(params, dict) else {}
    cfg = _cfg(cfg)
    context = dict(context or {})
    if method == "initialize":
        requested = params.get("protocolVersion")
        supported = (PROTOCOL_VERSION,) + tuple(
            item for item in SUPPORTED_PROTOCOL_VERSIONS if item != PROTOCOL_VERSION)
        version = requested if requested in supported else PROTOCOL_VERSION
        if tool_mode == "mac":
            instructions = (
                "Host helper tools only: keep_awake, take_screenshot and "
                "restart_client_app. Start the server without --tools mac to "
                "expose the DeepSeek and Harness tools.")
        else:
            instructions = (
                f"crewforeman-mcp {SERVER_VERSION}: a generic MCP stdio server "
                "for DeepSeek "
                "text models and the separately installed DeepSeek Harness. Use "
                "ask_deepseek for one cheap text call; consult_deepseek for "
                "independent answers from configured OpenAI-compatible models; "
                "swarm_deepseek is experimental. Use harness_deepseek for work that "
                "must build, run or check files: it applies permission presets, "
                "sizes the team, runs bounded waves, checkpoints every phase, resumes from "
                "them, "
                "scores the first wave and can add a bounded extra wave when the "
                "candidates are weak, then runs a separate fresh read-only reviewer "
                "that returns a report with a 0-10 score and a fix list; the "
                "connector writes it to REVIEW.md, followed by revision rounds. "
                "status distinguishes ok from ok_below_gate, "
                "ok_unreviewed, ok_rescued and error; quality_score and review_status "
                "remain available. Transport can be headless, sdk (--profile sdk) or "
                "auto with a recorded fallback. Long calls support MCP task augmentation, progress "
                "notifications and cancellation. Optional structured traces and "
                "OTLP/Langfuse export are off unless configured. Runs run in the "
                "background and every paid run is saved locally.")
        return _ok({
            "protocolVersion": version,
            "capabilities": {
                "tools": {},
                "tasks": {
                    "list": {},
                    "cancel": {},
                    "requests": {"tools": {"call": {}}},
                },
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": instructions,
        })
    if method == "ping":
        return _ok({})
    if method == "tools/list":
        return _ok({"tools": tools_for_mode(tool_mode)})
    if method.startswith("tasks/"):
        return _handle_task_method(method, params, cfg)
    if method == "tools/call":
        name = params.get("name")
        name = LEGACY_TOOL_ALIASES.get(name, name)
        allowed = {tool["name"] for tool in tools_for_mode(tool_mode)}
        if name not in allowed:
            return _err(-32602, "Unknown tool: %s" % name)
        fn = HANDLERS.get(name)
        if not fn:
            return _err(-32602, "Unknown tool: %s" % name)
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(-32602, "arguments must be an object")
        if isinstance(params.get("task"), dict):
            return _task_augmented_call(name, args, params, cfg, context)
        token = _progress_token(params)
        ctx = dict(context)
        ctx["progress_cb"] = _make_progress(None, None, token) if token is not None else None
        ctx.setdefault("cancel_event", threading.Event())
        _context.value = ctx
        try:
            text = fn(args, cfg)
            if isinstance(text, (dict, list)):
                # Structured additive responses (deepseek_job_status with
                # events_since) keep a JSON text block for old clients and add
                # the MCP 2025 structuredContent field.
                return _ok({
                    "content": [{"type": "text", "text": json.dumps(
                        text, ensure_ascii=False, sort_keys=True)}],
                    "structuredContent": text,
                    "isError": False,
                })
            return _ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            if ctx["cancel_event"].is_set():
                return _ok({"content": [{"type": "text", "text": "Error: cancelled by client"}],
                            "isError": True})
            return _ok({"content": [{"type": "text", "text": "Error: %s" % exc}],
                        "isError": True})
        finally:
            _context.value = None
    if method in ("resources/list", "prompts/list"):
        return _ok({method.split("/")[0]: []})
    return _err(-32601, "Method not found: %s" % method)


def handle_notification(msg):
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "notifications/cancelled":
        request_id = params.get("requestId")
        task_id = params.get("taskId")
        meta = params.get("_meta") or {}
        related = (meta.get("io.modelcontextprotocol/related-task") or {}) \
            if isinstance(meta, dict) else {}
        task_id = task_id or related.get("taskId")
        if task_id:
            event = _task_events.get(task_id)
            if event is not None:
                event.set()
            return
        with _inflight_lock:
            event = _inflight.get(str(request_id))
        if event is not None:
            event.set()


def reply(msg_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    with _out_lock:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def handle(msg, tool_mode="all", cfg=None):
    if not isinstance(msg, dict):
        reply(None, error={"code": -32600, "message": "Invalid Request"})
        return
    if "id" not in msg:
        handle_notification(msg)
        return
    request_id = msg.get("id")
    cancel_event = threading.Event()
    with _inflight_lock:
        _inflight[str(request_id)] = cancel_event
    try:
        result = dispatch(msg.get("method"), msg.get("params") or {}, tool_mode,
                          cfg, context={"request_id": request_id,
                                        "cancel_event": cancel_event})
    finally:
        with _inflight_lock:
            _inflight.pop(str(request_id), None)
    if "error" in result:
        reply(msg.get("id"), error=result["error"])
    else:
        reply(msg.get("id"), result=result["result"])


def serve(tool_mode="all", cfg=None):
    cfg = _cfg(cfg)
    try:
        from . import jobs as _jobs
        _jobs.resume_admission(cfg)
    except Exception as exc:
        log("admission dispatcher skipped: %s" % exc)
    log("crewforeman-mcp v%s started; model=%s; reasoning=%s; parallel=%s; "
        "runs=%s; tools=%s"
        % (SERVER_VERSION, cfg.model, cfg.reasoning, cfg.max_parallel, cfg.log_dir, tool_mode))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "Parse error"})
            continue

        def work(message=msg):
            try:
                handle(message, tool_mode, cfg)
            except Exception as exc:  # never crash the server loop
                log("internal error: %s" % exc)
                if message.get("id") is not None:
                    reply(message.get("id"), error={"code": -32603, "message": str(exc)})

        thread = threading.Thread(target=work, daemon=True)
        _workers.append(thread)
        thread.start()
    for thread in _workers:
        thread.join()


def _parse_argv(argv):
    mode, config_path, job_spec = "all", None, None
    maintenance = False
    show_version = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--version", "-V"):
            show_version = True
            index += 1
        elif arg == "--maintenance":
            maintenance = True
            index += 1
        elif arg == "--job":
            if index + 1 >= len(argv):
                raise RuntimeError("--job requires a spec path")
            job_spec = argv[index + 1]
            index += 2
        elif arg == "--tools":
            if index + 1 >= len(argv):
                raise RuntimeError("--tools requires 'all' or 'mac'")
            mode = argv[index + 1]
            index += 2
        elif arg.startswith("--tools="):
            mode = arg.split("=", 1)[1]
            index += 1
        elif arg == "--config":
            if index + 1 >= len(argv):
                raise RuntimeError("--config requires a path")
            config_path = argv[index + 1]
            index += 2
        elif arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            index += 1
        elif arg in ("-h", "--help"):
            print("usage: crewforeman-mcp [--tools all|mac] [--config PATH] "
                  "[--job SPEC] [--maintenance] [--version]")
            raise SystemExit(0)
        else:
            raise RuntimeError("Unknown argument: %s" % arg)
    if mode not in ("all", "mac"):
        raise RuntimeError("--tools must be 'all' or 'mac'")
    return mode, config_path, job_spec, maintenance, show_version


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        mode, config_path, job_spec, maintenance, show_version = _parse_argv(argv)
    except RuntimeError as exc:
        print("deepseek-mcp: %s" % exc, file=sys.stderr)
        return 2
    if show_version:
        print("crewforeman-mcp %s" % SERVER_VERSION)
        return 0
    if maintenance:
        try:
            cfg = load_config(config_path=config_path)
            result = retention.cleanup(cfg)
        except Exception as exc:
            print("deepseek-mcp: maintenance failed: %s" % api.redact_secrets(str(exc)),
                  file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if job_spec:
        # The spec contains the absolute config path chosen by the parent
        # server. Let run_job load it instead of overriding it with the
        # environment/default config of this worker process.
        jobs.run_job(job_spec, JOB_IMPL)
        return 0
    try:
        cfg = load_config(config_path=config_path)
    except Exception as exc:
        print("deepseek-mcp: configuration error: %s" % exc, file=sys.stderr)
        return 2
    # Strict permissions and optional startup retention. Both are best-effort:
    # a read-only or Windows filesystem must not stop the server.
    try:
        for name in ("log_dir", "jobs_dir", "state_dir"):
            raw = cfg.get(name)
            if raw:
                path = Path(str(raw)).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                platforms_private(path)
        raw_state = cfg.get("state_dir")
        if raw_state:
            retention.secure_tree(str(Path(str(raw_state)).expanduser()))
    except Exception:
        pass
    try:
        if float(cfg.get("retention_days") or 0) > 0:
            retention.cleanup(cfg)
    except Exception as exc:
        log("retention skipped: %s" % api.redact_secrets(str(exc)))
    serve(mode, cfg)
    return 0
