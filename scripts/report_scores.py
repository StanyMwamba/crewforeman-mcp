#!/usr/bin/env python3
"""Summarise CrewForeman run scores by builder-practices group.

Thin executable wrapper so the report can be run from a source checkout::

    python3 scripts/report_scores.py --runs /path/to/runs
    python3 scripts/report_scores.py --json
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from deepseek_mcp.score_report import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
