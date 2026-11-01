#!/usr/bin/env python3
"""One small REAL end-to-end Harness run (costs money). Run manually only.

Checks: 2 waves (9 subagents at concurrency 8), builder self-check, independent
review, at most 2 revision rounds, cost reading. Prints a short report.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deepseek_mcp import config, harness  # noqa: E402

PROMPT = ("Create a one-page HTML flyer (A5, print) for a fictional bakery called "
          "'Le Petit Four' with exactly these 3 facts: open 7 days 7am-7pm; "
          "sourdough baked daily; 12 rue Exemple. Render it to flyer.png with headless Chrome.")


def main():
    cfg = config.load_config()
    dsh = os.path.expanduser(cfg.dsh_bin)
    if not os.path.isfile(dsh):
        sys.exit("DeepSeek Harness CLI not found at %s; not running." % dsh)
    print("Estimated cost ceiling: about $1.00 (builder + up to 2 review/revision rounds).")
    workspace = os.path.join(os.path.expanduser(cfg.harness_workspace), "live-smoke")
    args = {"prompt": PROMPT, "title": "live smoke test", "workspace": workspace,
            "subagents": 9, "quality": True, "independent_review": True,
            "quality_rounds": 2, "max_seconds": 1500}
    try:
        text, run, path = harness.run_harness(args, cfg, progress_cb=lambda m: print("..", m))
    except RuntimeError as exc:
        sys.exit("FAILED: %s" % exc)
    print("status:", run.get("status"))
    print("subagents:", run.get("subagents"), "waves:", run.get("subagent_waves"),
          "concurrency:", run.get("concurrency"), "(%s)" % run.get("concurrency_source"))
    for item in run.get("review_rounds") or []:
        print("  %s %s: score=%s blocking=%s %ss $%s" % (
            item.get("kind"), item.get("round"), item.get("score"),
            item.get("blocking_defects"), item.get("seconds"), item.get("cost")))
    print("quality_score (independent):", run.get("quality_score"),
          "self_score:", run.get("self_score"))
    print("time: %ss  cost: $%s" % (run.get("seconds"), run.get("estimated_cost_usd")))
    print("files:", ", ".join(run.get("output_files") or []))
    print("saved:", path)


if __name__ == "__main__":
    main()
