"""WASTE_REPORT.md parsing for crewforeman-mcp 3.10.0 (item 6c).

A multi-agent builder may leave a ``WASTE_REPORT.md`` in its workspace with the
agents launched / used / discarded and an optional waste ratio.  This module
parses that file tolerantly (label lines, compact summary lines, explicit or
derived ratios) and never raises.
"""

import os
import re

NAME = "WASTE_REPORT.md"
MAX_TEXT = 20000

_INT = r"(\d+)"

# Label forms: "Agents launched: 6", "Launched: 6", "Agents used: 5",
# "Used: 5", "Discarded: 1", "Discarded agents: 1", "Agents discarded: 1".
_LAUNCHED = re.compile(
    r"(?<![A-Za-z])(?:agents?\s+launched|launched)\s*[:=]\s*" + _INT,
    re.IGNORECASE,
)
_USED = re.compile(
    r"(?<![A-Za-z])(?:agents?\s+used|used)\s*[:=]\s*" + _INT,
    re.IGNORECASE,
)
_DISCARDED = re.compile(
    r"(?<![A-Za-z])(?:agents?\s+discarded|discarded(?:\s+agents?)?)"
    r"\s*[:=]\s*" + _INT,
    re.IGNORECASE,
)
_RATIO = re.compile(
    r"waste\s*ratio\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*(%)?",
    re.IGNORECASE,
)

# Compact form: "6 launched / 5 used / 1 discarded".
_COMPACT_LAUNCHED = re.compile(_INT + r"\s+launched\b", re.IGNORECASE)
_COMPACT_USED = re.compile(_INT + r"\s+used\b", re.IGNORECASE)
_COMPACT_DISCARDED = re.compile(_INT + r"\s+discarded\b", re.IGNORECASE)


def _first_int(pattern, text):
    match = pattern.search(text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _count(label_pattern, compact_pattern, text):
    value = _first_int(label_pattern, text)
    if value is None:
        value = _first_int(compact_pattern, text)
    return value


def _empty(present=False):
    return {
        "present": present,
        "agents_launched": None,
        "agents_used": None,
        "agents_discarded": None,
        "waste_ratio": None,
        "source": None,
    }


def parse_waste(text):
    """Parse WASTE_REPORT text into counts and a bounded waste ratio."""
    result = _empty(present=False)
    if not isinstance(text, str):
        return result
    text = text[:MAX_TEXT]

    launched = _count(_LAUNCHED, _COMPACT_LAUNCHED, text)
    used = _count(_USED, _COMPACT_USED, text)
    discarded = _count(_DISCARDED, _COMPACT_DISCARDED, text)

    if discarded is None and launched is not None and used is not None:
        discarded = max(0, launched - used)

    explicit = None
    ratio_match = _RATIO.search(text)
    if ratio_match:
        try:
            explicit = float(ratio_match.group(1))
        except (TypeError, ValueError):
            explicit = None
        else:
            if ratio_match.group(2):
                explicit = explicit / 100.0
            explicit = min(1.0, max(0.0, explicit))

    ratio = None
    source = None
    if launched is None or launched == 0:
        # Documented rule: no launched count (or zero launched) means no
        # ratio, even when the file states an explicit "Waste ratio:".
        ratio = None
    elif explicit is not None:
        ratio = explicit
        source = "explicit"
    elif launched is not None and discarded is not None:
        ratio = min(1.0, max(0.0, float(discarded) / float(launched)))
        source = "derived"

    result.update(
        {
            "agents_launched": launched,
            "agents_used": used,
            "agents_discarded": discarded,
            "waste_ratio": ratio,
            "source": source,
        }
    )
    return result


def inspect(workspace):
    """Read ``<workspace>/WASTE_REPORT.md`` (bounded) and parse it."""
    path = os.path.join(workspace, NAME)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_TEXT)
    except (OSError, IOError):
        return _empty(present=False)
    parsed = parse_waste(text)
    parsed["present"] = True
    return parsed
