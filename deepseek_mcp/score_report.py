"""Score report for CrewForeman run records (stdlib only).

Summarises completed run records in a directory by builder-practices group
(``builder_practices.version`` / ``"disabled"`` / ``"pre-3.9.5"``) and exposes
mean first score, mean final score, mean delta and the accepted count.

Run records are the JSON files written by the harness under ``log_dir``.  A
record is accepted when its final score is at least 8.5 and its final review
round carries no blocking defects.  Malformed or non-object JSON files are
ignored so that a partially written run never breaks the report.

CLI::

    python3 scripts/report_scores.py --runs ~/.deepseek-mcp/runs
    python3 scripts/report_scores.py --json
"""

import argparse
import json
import os
import sys

from .config import load_config

ACCEPT_THRESHOLD = 8.5

HEADERS = ("group", "runs", "mean_first", "mean_final", "mean_delta", "accepted")


def _number(value):
    """Return *value* as a float when it is a numeric score, else ``None``.

    Booleans are not scores.  Numeric strings are tolerated because run
    records are sometimes edited by hand; anything else is ignored.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _review_rounds(run):
    rounds = run.get("review_rounds")
    return rounds if isinstance(rounds, list) else []


def _first_review(run):
    """First entry in ``review_rounds`` whose kind is ``review``/score numeric."""
    for entry in _review_rounds(run):
        if not isinstance(entry, dict) or entry.get("kind") != "review":
            continue
        score = _number(entry.get("score"))
        if score is not None:
            return score
    return None


def _last_review_round(run):
    rounds = _review_rounds(run)
    for entry in reversed(rounds):
        if isinstance(entry, dict) and entry.get("kind") == "review":
            return entry
    return None


def first_score(run):
    """First review score for a run, or ``None``.

    Prefers the explicit ``first_review_score`` recorded by the harness and
    falls back to the first ``kind == "review"`` entry in ``review_rounds``.
    """
    if not isinstance(run, dict):
        return None
    explicit = _number(run.get("first_review_score"))
    if explicit is not None:
        return explicit
    return _first_review(run)


def final_score(run):
    """Final review score for a run, or ``None``.

    ``quality_score`` is the harness' last-review score.  If it is missing or
    not numeric, the last review round with a numeric score is used instead.
    """
    if not isinstance(run, dict):
        return None
    explicit = _number(run.get("quality_score"))
    if explicit is not None:
        return explicit
    for entry in reversed(_review_rounds(run)):
        if not isinstance(entry, dict):
            continue
        score = _number(entry.get("score"))
        if score is not None:
            return score
    return None


def _defect_count(value):
    """Coerce a ``blocking_defects`` value to a non-negative integer count."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("none", "null"):
            return 0
        try:
            return max(0, int(float(text)))
        except ValueError:
            return 1
    if isinstance(value, dict):
        for key in ("blocking_defects", "blocking", "count"):
            if key in value:
                return _defect_count(value[key])
        return len(value)
    try:
        return len(value)
    except TypeError:
        return 1


def blocking_defects(run):
    """Number of blocking defects on the final review (0 when unknown)."""
    if not isinstance(run, dict):
        return 0
    review = _last_review_round(run)
    if review is not None and "blocking_defects" in review:
        return _defect_count(review.get("blocking_defects"))
    if "blocking_defects" in run:
        return _defect_count(run.get("blocking_defects"))
    return 0


def run_group(run):
    """Return the builder-practices group label for a run record."""
    if not isinstance(run, dict):
        return "pre-3.9.5"
    practices = run.get("builder_practices")
    if not isinstance(practices, dict):
        return "pre-3.9.5"
    if practices.get("enabled"):
        version = practices.get("version")
        if version is None or not str(version).strip():
            return "unknown"
        return str(version)
    return "disabled"


def _mean(values):
    if not values:
        return None
    return sum(values) / float(len(values))


def summarise(run_dir):
    """Aggregate run records in *run_dir* into sorted per-group summary rows.

    Each row is a dict with ``group``, ``runs``, ``mean_first``, ``mean_final``,
    ``mean_delta`` and ``accepted``.  Means ignore runs where the relevant
    score is missing; ``mean_delta`` averages the per-run ``final - first``
    differences.  Malformed JSON and non-object JSON are skipped.
    """
    if not run_dir or not os.path.isdir(run_dir):
        return []

    buckets = {}
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        return []

    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                run = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(run, dict):
            continue

        group = run_group(run)
        bucket = buckets.setdefault(
            group, {"runs": 0, "first": [], "final": [], "delta": [], "accepted": 0}
        )
        bucket["runs"] += 1

        first = first_score(run)
        final = final_score(run)
        if first is not None:
            bucket["first"].append(first)
        if final is not None:
            bucket["final"].append(final)
        if first is not None and final is not None:
            bucket["delta"].append(final - first)
        if final is not None and final >= ACCEPT_THRESHOLD and blocking_defects(run) == 0:
            bucket["accepted"] += 1

    rows = []
    for group in sorted(buckets):
        bucket = buckets[group]
        rows.append(
            {
                "group": group,
                "runs": bucket["runs"],
                "mean_first": _mean(bucket["first"]),
                "mean_final": _mean(bucket["final"]),
                "mean_delta": _mean(bucket["delta"]),
                "accepted": bucket["accepted"],
            }
        )
    return rows


def _cell(row, key):
    value = row.get(key)
    if value is None:
        return "-"
    if isinstance(value, float):
        return "%.2f" % value
    return str(value)


def format_table(rows):
    """Render summary rows as a fixed-width text table."""
    rows = list(rows or [])
    body = [[_cell(row, header) for header in HEADERS] for row in rows]
    table = [list(HEADERS)] + body
    widths = [max(len(line[index]) for line in table) for index in range(len(HEADERS))]

    lines = []
    for line in table:
        cells = []
        for index, cell in enumerate(line):
            if index == 0:
                cells.append(cell.ljust(widths[index]))
            else:
                cells.append(cell.rjust(widths[index]))
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def main(argv=None):
    """CLI entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="report_scores",
        description="Summarise CrewForeman run scores by builder-practices group.",
    )
    parser.add_argument(
        "--runs",
        metavar="DIR",
        default=None,
        help="directory of run JSON records (default: configured log_dir)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the summary as JSON instead of a table",
    )
    args = parser.parse_args(argv)

    run_dir = args.runs
    if not run_dir:
        run_dir = load_config().log_dir

    if not os.path.isdir(run_dir):
        print("Runs directory not found: %s" % run_dir)
        return 1

    rows = summarise(run_dir)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No completed runs found")
        return 0
    print(format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
