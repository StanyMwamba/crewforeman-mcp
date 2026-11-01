#!/usr/bin/env python3
"""Run a small, fixed benchmark through the DeepSeek MCP server.

The benchmark never calls paid APIs by itself; it drives an MCP server that has
credentials configured. Use ``--dry-run`` to inspect the task set without
starting the server. Results are written as a Markdown table.

This draft fixes real defects found in ``bench/run_benchmark.py``:

* ``--config`` was used only when reading saved runs, not when starting the MCP
  server, so the benchmark and the server could use different config files.
* A background job that stopped without finishing was reported as an unknown
  success instead of an error.
* If a saved run was missing, time and cost were silently dropped even when the
  tool result already contained them.
* The MCP lifecycle was incomplete (no ``notifications/initialized``).
* Server startup errors were hidden by ``stderr=DEVNULL``.
* Error text containing ``|`` could break the Markdown table.
* The CLI exited 0 even when every task failed.

Python 3.9, standard library only.
"""

import argparse
import datetime as dt
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _find_root(start):
    """Find the checkout root from either ``bench/`` or this draft folder."""
    for parent in [start] + list(start.parents):
        if (parent / "deepseek_mcp").is_dir() and (parent / "deepseek_mcp.py").is_file():
            return parent
    return start


ROOT = _find_root(Path(__file__).resolve().parent)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_mcp.config import load_config  # noqa: E402
from deepseek_mcp.store import load_runs  # noqa: E402


TASKS = [
    {
        "key": "poster",
        "title": "Print-ready poster redesign",
        "prompt": (
            "Redesign this brief as a print-ready A3 poster for a fictional "
            "community jazz night. Required facts: 'Blue Hour Jazz', Saturday 14 March, "
            "19:30 doors, 20:00 start, Riverside Hall, 12 Market Street, tickets 15 dollars, "
            "students 8 dollars, 'live quartet + late jam', website example.com/jazz. "
            "Include a QR code that points to https://example.com/jazz. Use a clear hierarchy, "
            "print-safe margins and body text at least 10 pt. Produce poster.html plus a "
            "rendered PDF/PNG screenshot at A3, and check the rendered result for overflow, "
            "overlap and text too small to print."
        ),
    },
    {
        "key": "letter",
        "title": "One-page cover letter",
        "prompt": (
            "Write a one-page cover letter for a fictional junior data analyst role at a "
            "fictional civic research nonprofit. The applicant is a recent graduate with two "
            "internships, SQL and Python experience, and one dashboard project. Requirements: "
            "one page, formal but warm tone, no invented employers or metrics, address the "
            "role's three stated needs (clean data, clear charts, careful communication), and "
            "end with a specific call to action. Save as cover-letter.md and cover-letter.pdf."
        ),
    },
    {
        "key": "cli",
        "title": "Small Python CLI with tests",
        "prompt": (
            "Build a small Python 3.9 standard-library CLI called wordcount in the workspace. "
            "It must count lines, words and characters for files or stdin, support --json, "
            "handle missing files with a clear non-zero exit, and include unittest tests plus "
            "a short README. Run the tests and report the exact command and result. Do not use "
            "third-party packages."
        ),
    },
]


class MCPClient:
    """Minimal blocking MCP stdio client for the benchmark."""

    def __init__(self, python=None, server=None, env=None, config=None, timeout=120):
        self.python = python or sys.executable
        self.timeout = float(timeout)
        self.server = str(server or (ROOT / "deepseek_mcp.py"))
        environment = dict(os.environ)
        if env:
            environment.update(env)
        command = [self.python, self.server]
        if config:
            # Keep the child server on exactly the same config file as the
            # process that later reads the saved runs. Without this, a custom
            # --config only affected result collection.
            command += ["--config", str(config)]
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._stderr, text=True, bufsize=1, env=environment,
            )
        except Exception:
            self._stderr.close()
            raise
        self._id = 0
        try:
            self.request("initialize", {"protocolVersion": "2025-06-18",
                                        "clientInfo": {"name": "bench", "version": "3.0"}})
            self.notify("notifications/initialized")
        except Exception:
            self.close()
            raise

    def _stderr_tail(self, limit=2000):
        try:
            self._stderr.flush()
            self._stderr.seek(0)
            text = self._stderr.read()
        except Exception:
            return ""
        return _redact(text[-limit:].strip())

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._id += 1
        request_id = self._id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params or {}}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self._readline()
            if not line:
                tail = self._stderr_tail()
                message = "MCP server closed its stdout"
                if tail:
                    message += ": " + tail
                raise RuntimeError(message)
            try:
                response = json.loads(line)
            except Exception:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(response["error"].get("message") or "MCP error")
            return response.get("result") or {}

    def _readline(self):
        if os.name == "posix":
            ready, _writable, _errors = select.select([self.proc.stdout], [], [], self.timeout)
            if not ready:
                raise RuntimeError("MCP server did not answer within %.0f s" % self.timeout)
        return self.proc.stdout.readline()

    def call_tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(item.get("text", "") for item in result.get("content") or []
                       if item.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text or "Tool failed")
        return text

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        try:
            self._stderr.close()
        except Exception:
            pass


def _text_of(result_text):
    return result_text or ""


def _redact(text):
    """Keep local home paths and obvious API keys out of saved error text."""
    text = (text or "").replace(str(Path.home()), "~")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-...", text)


_JOB_STILL_RUNNING = re.compile(
    r"\s*\[(?:DeepSeek job|Job)\s+([A-Za-z0-9_.-]+)\s+"
    r"(?:is still running|still running)")


def wait_for_job(client, first_text, timeout=3600, poll=5):
    """Poll deepseek_job_status until a background Harness job finishes."""
    job = _JOB_STILL_RUNNING.match(_text_of(first_text))
    if not job:
        if "stopped without finishing" in _text_of(first_text):
            raise RuntimeError(_text_of(first_text).strip())
        return first_text
    job_id = job.group(1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = client.call_tool("deepseek_job_status", {"run_id": job_id})
        if "stopped without finishing" in (text or ""):
            raise RuntimeError("Background job %s stopped without finishing: %s"
                               % (job_id, (text or "").strip()))
        if not _JOB_STILL_RUNNING.match(_text_of(text)):
            return text
        time.sleep(poll)
    raise RuntimeError("Timed out waiting for job %s" % job_id)


def _header(text):
    """Return the Harness result header, not the model's final answer."""
    return (text or "").split("\n\n", 1)[0]


def _run_id(text):
    header = _header(text)
    match = re.search(r"DeepSeek Harness run ([A-Za-z0-9_.-]+)", header)
    if match:
        return match.group(1)
    match = re.search(r"\brun ([A-Za-z0-9][A-Za-z0-9_.-]{7,})\b", header)
    return match.group(1) if match else None


def _first_number(pattern, text):
    match = re.search(pattern, text or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _result_metrics(text):
    """Extract score, seconds and estimated cost from a Harness result string."""
    header = _header(text)
    score = _first_number(
        r"(?:final\s+quality\s+score|quality(?:\s+score)?)\s*[:=]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*/\s*10", header)
    if score is None:
        score = _first_number(
            r"final\s+quality\s+score\s*[:=]?\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*/\s*10", text)
    seconds = _first_number(
        r"Harness run\b[^|\n]*\|\s*([0-9]+(?:\.[0-9]+)?)\s*s\b", header)
    cost = _first_number(
        r"(?:est\.\s*cost|cost)\s*[:=]?\s*\$([0-9]+(?:\.[0-9]+)?)", header)
    return score, seconds, cost


def collect_row(task, text, cfg=None, elapsed=None):
    cfg = cfg or load_config()
    run_id = _run_id(text)
    run = None
    if run_id:
        try:
            candidates = load_runs(cfg)
        except Exception:
            candidates = []
        for candidate in candidates:
            if candidate.get("id") == run_id:
                run = candidate
                break
    parsed_score, parsed_seconds, parsed_cost = _result_metrics(text)
    if run:
        score = run.get("quality_score")
        seconds = run.get("seconds")
        cost = run.get("estimated_cost_usd")
        if score is None:
            score = parsed_score
        if seconds is None:
            seconds = parsed_seconds
        if cost is None:
            cost = parsed_cost
        if seconds is None:
            seconds = elapsed
        return {
            "task": task["title"],
            "key": task.get("key"),
            "score": score,
            "seconds": seconds,
            "cost": cost,
            "status": run.get("status") or "ok",
            "run_id": run.get("id") or run_id,
        }
    if parsed_seconds is None:
        parsed_seconds = elapsed
    return {
        "task": task["title"],
        "key": task.get("key"),
        "score": parsed_score,
        "seconds": parsed_seconds,
        "cost": parsed_cost,
        "status": "error: run not saved" if run_id else "error: no run id",
        "run_id": run_id,
    }


def run_benchmark(client, tasks=None, cfg=None, output=None, timeout=3600, poll=5):
    """Run tasks through ``client`` and return collected rows."""
    cfg = cfg or load_config()
    tasks = tasks if tasks is not None else TASKS
    rows = []
    for task in tasks:
        started = time.time()
        try:
            first = client.call_tool("harness_deepseek", {
                "prompt": task["prompt"], "title": "bench: " + task["title"],
                "wait_seconds": 1,
            })
            text = wait_for_job(client, first, timeout=timeout, poll=poll)
            row = collect_row(task, text, cfg,
                              elapsed=round(time.time() - started, 1))
            row["status"] = row.get("status") or "ok"
        except Exception as exc:
            row = {"task": task["title"], "key": task.get("key"), "score": None,
                   "seconds": round(time.time() - started, 1), "cost": None,
                   "status": "error: %s" % _redact(str(exc)), "run_id": None}
        rows.append(row)
    if output:
        render_results(rows, output)
    return rows


def _fmt(value, spec="%s", empty="n/a"):
    return empty if value is None else spec % value


def _cell(value):
    """Keep table cells on one row even when an error message contains pipes."""
    text = "n/a" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_results(rows, output, dry_run=False):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# DeepSeek MCP benchmark results",
        "",
        "Generated by `bench/run_benchmark.py` on %s." % dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        "",
        "| Task | Quality score | Time | Estimated cost | Status | Run |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            _cell(row.get("task")), _fmt(row.get("score"), "%.2f/10"),
            _fmt(row.get("seconds"), "%.1f s"), _fmt(row.get("cost"), "$%.6f"),
            _cell(row.get("status") or "unknown"), _cell(row.get("run_id") or "-")))
    lines += [
        "",
        "Costs are estimates from saved runs; check the provider dashboard for billing.",
        "A single task is a smoke test, not a statistically meaningful benchmark.",
        "",
    ]
    if dry_run:
        lines.insert(1, "\n_DRY RUN: no server was started and no paid calls were made._\n")
    output.write_text("\n".join(lines), encoding="utf-8")
    return str(output)


def _task_selection(raw_keys, parser):
    selected_keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
    known = {task["key"] for task in TASKS}
    unknown = [key for key in selected_keys if key not in known]
    if unknown:
        parser.error("unknown task key(s): %s (choose from %s)"
                     % (", ".join(unknown), ", ".join(sorted(known))))
    return [task for task in TASKS if task["key"] in selected_keys]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the DeepSeek MCP benchmark.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--server", default=str(ROOT / "deepseek_mcp.py"))
    parser.add_argument("--config", default=None)
    parser.add_argument("--output", default=str(ROOT / "bench" / "results.md"))
    parser.add_argument("--wait", type=float, default=3600)
    parser.add_argument("--poll", type=float, default=5)
    parser.add_argument("--tasks", default="poster,letter,cli")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    tasks = _task_selection(args.tasks, parser)
    if not tasks:
        parser.error("no matching tasks in --tasks")

    if args.dry_run:
        output = str(Path(args.output).with_name("results.dry-run.md"))
        rows = [{"task": task["title"], "key": task["key"], "score": None,
                 "seconds": None, "cost": None, "status": "not run (dry run)",
                 "run_id": None} for task in tasks]
        print("Dry run: %d task(s). No server started." % len(tasks))
        print(render_results(rows, output, dry_run=True))
        return 0

    cfg = load_config(config_path=args.config)
    client = MCPClient(python=args.python, server=args.server,
                       config=cfg.get("_config_path"))
    try:
        rows = run_benchmark(client, tasks=tasks, cfg=cfg, output=args.output,
                             timeout=args.wait, poll=args.poll)
    finally:
        client.close()
    for row in rows:
        print("%-35s score=%-8s time=%-9s cost=%-10s %s" % (
            row["task"], _fmt(row.get("score"), "%.2f"), _fmt(row.get("seconds"), "%.1fs"),
            _fmt(row.get("cost"), "$%.6f"), row["status"]))
    print("Wrote %s" % args.output)
    failed = any(str(row.get("status") or "").startswith("error") for row in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
