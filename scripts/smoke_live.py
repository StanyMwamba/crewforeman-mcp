#!/usr/bin/env python3
"""Opt-in paid end-to-end smoke test for CrewForeman (stdlib only).

This spends real money: it launches the configured Harness and its independent
reviewer against a tiny prompt. It never runs unless --yes is passed.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_mcp import __version__ as package_version  # noqa: E402
from deepseek_mcp import api, harness  # noqa: E402
from deepseek_mcp.config import SERVER_VERSION, load_config  # noqa: E402

PROMPT = "Reply with the single word OK."


def installed_version():
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python 3.8 fallback
        return package_version
    for name in ("crewforeman-mcp", "deepseek-mcp"):
        try:
            return metadata.version(name)
        except Exception:
            continue
    return package_version


def check_live_run(run, version):
    """Return (ok, error) for a completed run record and expected version."""
    if not isinstance(run, dict):
        return False, "run record is missing"
    if not version or SERVER_VERSION != version:
        return False, ("version mismatch: server %s, package %s"
                       % (SERVER_VERSION, version))
    builder = run.get("builder") or {}
    builder_status = builder.get("status") or run.get("builder_status")
    if builder_status != "ok":
        return False, "builder status is %r, expected 'ok'" % (builder_status,)
    if run.get("quality_score") is None:
        return False, "reviewer returned no quality_score"
    review_status = run.get("review_status")
    if review_status not in ("accepted", "stopped"):
        return False, "review_status is %r" % (review_status,)
    return True, None


def make_workspace():
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    try:
        return Path(tempfile.mkdtemp(prefix="crewforeman-smoke-", dir=base))
    except Exception:
        fallback = Path.cwd() / "smoke-workspace"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Paid live end-to-end smoke for CrewForeman.")
    parser.add_argument("--yes", action="store_true",
                        help="required confirmation of a real paid live run")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.yes:
        sys.stderr.write(
            "WARNING: this is a LIVE PAID run that calls the configured model "
            "endpoint and its independent reviewer. Re-run with --yes to "
            "confirm.\n")
        return 2

    version = installed_version()
    summary = {"ok": False, "version": SERVER_VERSION,
               "builder_status": None, "quality_score": None,
               "review_status": None, "workspace": None, "error": None}
    workspace = Path(args.workspace).expanduser() if args.workspace else make_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    summary["workspace"] = str(workspace)
    try:
        cfg = load_config(config_path=args.config)
        run_args = {
            "prompt": PROMPT, "workspace": str(workspace),
            "quality": True, "independent_review": True,
            "quality_rounds": 1, "subagents": 1,
            "max_seconds": args.timeout,
            "job_max_seconds": args.timeout + 60,
        }
        _text, run, _path = harness.run_harness(run_args, cfg)
        summary["builder_status"] = ((run.get("builder") or {}).get("status")
                                     or run.get("builder_status"))
        summary["quality_score"] = run.get("quality_score")
        summary["review_status"] = run.get("review_status")
        ok, error = check_live_run(run, version)
        summary["ok"] = ok
        summary["error"] = error
    except Exception as exc:
        summary["error"] = api.redact_secrets(str(exc))
        if os.environ.get("CREW_FOREMAN_SMOKE_TRACE"):
            traceback.print_exc()

    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print("CrewForeman live smoke: %s" % ("OK" if summary["ok"] else "FAILED"))
        for key in ("version", "builder_status", "quality_score",
                    "review_status", "workspace", "error"):
            print("  %s: %s" % (key, summary[key]))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
