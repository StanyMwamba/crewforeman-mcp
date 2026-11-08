"""Review scope rules for crewforeman-mcp 3.10.0 (Part B, item 2).

The independent reviewer must separate findings about the change under review
from pre-existing issues in untouched code.  Pre-existing findings live under
``FOLLOW_UP_HEADING`` and carry exactly one explicit tag:

* a leading ``[security]`` tag -> blocking with kind ``security``;
* a leading ``[data-loss]`` tag -> blocking with kind ``data-loss``;
* a leading ``[minor]`` tag -> non-blocking regardless of wording;
* an untagged line fails safe: any security keyword makes it blocking
  ``security`` (security wins over data loss), otherwise any data-loss keyword
  makes it blocking ``data-loss``, otherwise it is non-blocking.

There is deliberately no negation handling: a keyword in an untagged line
blocks even when the sentence says the problem cannot happen.  The reviewer
must tag a benign mention ``[minor]``.

This module is stdlib-only and Python 3.9 compatible.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

FOLLOW_UP_HEADING = "## Follow-ups (pre-existing, non-blocking)"
MAX_ITEMS = 50

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s*(.*?)\s*#*\s*$")
_FOLLOW_RE = re.compile(r"^follow[\s\-_]*ups?\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(?P<text>.*\S)\s*$")

# Tags are recognised only at the start of the cleaned item.  A tag in the
# middle of a line is ordinary text, never a tag.  Inside the brackets the
# data-loss spelling tolerates ``data_loss`` and ``data loss``.
_TAG_RE = re.compile(
    r"^\s*\[\s*(security|data[\s\-_]*loss|minor)\s*\]",
    re.IGNORECASE,
)

_SKIP_WORDS = {"none", "n/a", "na", "null", "-", "--", "nil", "(none)"}

# Broad fail-safe keyword sets.  They are only consulted for untagged items;
# an explicit ``[minor]`` tag always wins over these words.
_SECURITY_RE = re.compile(
    r"\bsecurity\b"
    r"|\bauth\b"
    r"|\bauthenticat\w*"
    r"|\bauthoriz\w*"
    r"|\bcredentials?\b"
    r"|\bsecrets?\b"
    r"|\bpasswords?\b"
    r"|\btokens?\b"
    r"|\bapi[\s\-_]?keys?\b"
    r"|\binjection\b"
    r"|\bxss\b"
    r"|\bcsrf\b"
    r"|\bssrf\b"
    r"|\brce\b"
    r"|\bremote\s+code\s+execution\b"
    r"|\bpath\s+traversal\b"
    r"|\bdirectory\s+traversal\b"
    r"|\bprivilege\s+escalation\b"
    r"|\bvulnerab\w*"
    r"|\bbreach\w*"
    r"|\bleak(?:s|ed|ing)?\b[^.;\n]{0,40}"
    r"\b(?:credentials?|secrets?|passwords?|tokens?|api[\s\-_]?keys?|"
    r"data|users?|customers?)\b"
    r"|\bexpos(?:e|es|ed|ing)\b[^.;\n]{0,40}"
    r"\b(?:credentials?|secrets?|passwords?|tokens?|api[\s\-_]?keys?|"
    r"data|users?|customers?)\b"
    r"|\bprivacy\b"
    r"|\bencrypt\w*"
    r"|\bexfiltrat\w*"
    r"|\bpermissions?\b"
    r"|\baccess\s+control\b"
    r"|\bcve-\d+\b",
    re.IGNORECASE,
)
_DATA_LOSS_RE = re.compile(
    r"\bdata[\s\-_]*loss\b"
    r"|\bloss\s+of\s+data\b"
    r"|\bdata\s+(?:destruction|deletion)\b"
    r"|\bcorrupt\w*"
    r"|\bdestructive\b"
    r"|\birreversibl\w*"
    r"|\b(?:delet|drop|truncat|purg|wip|overwrit)\w*\b[^.;\n]{0,40}"
    r"\b(?:data|rows?|records?|tables?|databases?|entries|users?|"
    r"customers?)\b"
    r"|\b(?:data|rows?|records?|tables?|databases?|entries|users?|"
    r"customers?)\b[^.;\n]{0,40}"
    r"\b(?:delet|drop|truncat|purg|wip|overwrit)\w*\b"
    r"|\b(?:lose|loses|lost|losing)\b[^.;\n]{0,30}"
    r"\b(?:data|rows?|records?)\b"
    r"|\bpermanently\s+(?:delet|lose|remov)\w*\b",
    re.IGNORECASE,
)


def _heading_match(line: str):
    """Return (level, text) for a markdown heading or a bare label line."""
    if not isinstance(line, str):
        return None
    match = _HEADING_RE.match(line.rstrip())
    if match:
        return len(match.group(1)), match.group(2).strip()
    # A bare label ("Follow-ups" / "Follow-up: ...") is accepted as a heading
    # only when the whole line is the label; this keeps prose out.
    stripped = line.strip()
    if stripped and not stripped.startswith(("-", "*", "+")):
        return 0, stripped
    return None


def _heading_level(line: str) -> Optional[int]:
    if not isinstance(line, str):
        return None
    match = _HEADING_RE.match(line.rstrip())
    if not match:
        return None
    return len(match.group(1))


def is_follow_up_heading(line: str) -> bool:
    """True for ``#/##/### Follow-ups``/``Follow-up``/``Followups``.

    An optional ``(pre-existing...)`` suffix is allowed and matching is
    case-insensitive.
    """
    found = _heading_match(line)
    if found is None:
        return False
    level, text = found
    text = text.strip().rstrip("#").strip()
    match = _FOLLOW_RE.match(text)
    if not match:
        return False
    remainder = text[match.end():].strip().lstrip(":").strip()
    if not remainder:
        return True
    if remainder.startswith("(") and remainder.endswith(")"):
        return True
    if remainder in ("pre-existing", "preexisting"):
        return True
    # A real markdown heading may carry a short trailing qualifier; a bare
    # prose line may not.
    return level > 0 and len(remainder) <= 80 and not remainder.endswith(".")


def _clean_item(text: str) -> Optional[str]:
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return None
    if cleaned.lower().strip(".:;") in _SKIP_WORDS:
        return None
    return cleaned


def _split_tag(item: str):
    """Return ``(tag_kind, body)`` for a leading scope tag.

    ``tag_kind`` is ``"security"``, ``"data-loss"``, ``"minor"`` or ``None``.
    The returned body has the leading tag and surrounding whitespace removed;
    text without a leading tag is returned unchanged apart from the caller's
    whitespace normalisation.  A malformed line with several leading tags is
    resolved by the first tag, which is deterministic; the prompt requires
    exactly one.
    """
    if not isinstance(item, str):
        return None, "" if item is None else str(item)
    match = _TAG_RE.match(item)
    if not match:
        return None, item
    raw = match.group(1).lower().replace("_", " ").replace("-", " ")
    raw = " ".join(raw.split())
    if raw == "minor":
        tag = "minor"
    elif raw == "security":
        tag = "security"
    else:
        tag = "data-loss"
    body = " ".join(item[match.end():].split())
    return tag, body


def _classify_body(text: str, tag: Optional[str]):
    """Return (blocking, kind) for an item body with an optional leading tag."""
    if tag == "security":
        return True, "security"
    if tag == "data-loss":
        return True, "data-loss"
    if tag == "minor":
        return False, None
    # Untagged: fail safe.  Security wins when both keyword families appear.
    # No negation logic: "cannot avoid data loss" still blocks.
    if _SECURITY_RE.search(text):
        return True, "security"
    if _DATA_LOSS_RE.search(text):
        return True, "data-loss"
    return False, None


def _classify(item: str):
    """Return (blocking, kind) for a cleaned follow-up item."""
    tag, body = _split_tag(item)
    return _classify_body(body, tag)


def _finish(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for item in items:
        item.pop("_tag", None)
    return items[:MAX_ITEMS]


def _sections(text: str):
    """Yield (start, end) line indices of each follow-up section.

    The section starts at the heading line and ends before the next markdown
    heading (conservative: any heading ends it).
    """
    if not isinstance(text, str) or not text:
        return []
    lines = text.split("\n")
    sections = []
    index = 0
    while index < len(lines):
        if is_follow_up_heading(lines[index]):
            start = index
            index += 1
            while index < len(lines):
                if _heading_level(lines[index]) is not None:
                    break
                index += 1
            sections.append((start, index))
            continue
        index += 1
    return sections


def follow_up_items(text: str) -> List[Dict[str, Any]]:
    """Parse bullets under every follow-up heading.

    Each item is ``{"text": str, "blocking": bool, "kind": str|None}``.  The
    leading scope tag is removed from ``text``.  Empty/``None``/``N/A`` bullets
    are skipped and at most ``MAX_ITEMS`` items are returned.
    """
    if not isinstance(text, str) or not text:
        return []
    lines = text.split("\n")
    items: List[Dict[str, Any]] = []
    for start, end in _sections(text):
        current: Optional[Dict[str, Any]] = None
        for line in lines[start + 1:end]:
            bullet = _BULLET_RE.match(line)
            if bullet:
                cleaned = _clean_item(bullet.group("text"))
                if cleaned is None:
                    current = None
                    continue
                tag, body = _split_tag(cleaned)
                if not body:
                    # A bare tag with no wording is not a usable follow-up.
                    current = None
                    continue
                blocking, kind = _classify_body(body, tag)
                current = {
                    "text": body,
                    "blocking": blocking,
                    "kind": kind,
                    "_tag": tag,
                }
                items.append(current)
                if len(items) >= MAX_ITEMS:
                    return _finish(items)
                continue
            if not line.strip():
                current = None
                continue
            if current is not None and line[:1] in (" ", "\t"):
                # Indented continuation of the previous bullet.  Keep the
                # original leading tag: an explicit [minor] must not flip to
                # blocking because the continuation mentions a keyword.
                extra = _clean_item(line)
                if extra:
                    current["text"] = (current["text"] + " " + extra).strip()
                    blocking, kind = _classify_body(
                        current["text"], current.get("_tag")
                    )
                    current["blocking"] = blocking
                    current["kind"] = kind
            else:
                current = None
        if len(items) >= MAX_ITEMS:
            break
    return _finish(items)


def strip_follow_ups(text: str) -> str:
    """Remove follow-up sections (heading and bullets); other text unchanged."""
    if not isinstance(text, str) or not text:
        return text
    lines = text.split("\n")
    drop = set()
    for start, end in _sections(text):
        drop.update(range(start, end))
    if not drop:
        return text
    kept = [line for idx, line in enumerate(lines) if idx not in drop]
    return "\n".join(kept)


def prompt_block() -> str:
    """Markdown reviewer scope rule for the independent review prompt."""
    return (
        "## Review scope (change under review vs pre-existing code)\n"
        "\n"
        "Only findings caused by this change may block. Issues that already\n"
        "existed in code this change does not touch are follow-ups, not\n"
        "defects. Report them under the exact heading:\n"
        "\n"
        f"{FOLLOW_UP_HEADING}\n"
        "\n"
        "Follow-up tagging (required): every line under that heading MUST\n"
        "start with exactly one tag: `[security]`, `[data-loss]` or `[minor]`.\n"
        "- `[security]`: a security defect. Blocking.\n"
        "- `[data-loss]`: a data-loss defect. Blocking.\n"
        "- `[minor]`: non-blocking regardless of the wording that follows.\n"
        "\n"
        "Tags must be first on the line (leading whitespace is allowed and the\n"
        "tag is case-insensitive); a tag in the middle of a line is not a tag.\n"
        "An untagged line that contains any security or data-loss keyword is\n"
        "treated as blocking (fail safe), even when the sentence says the flaw\n"
        "cannot happen. Tag a benign mention `[minor]`, including a docs typo\n"
        "that merely mentions authentication.\n"
        "\n"
        "Do not repeat follow-ups under Blocking or Non-blocking defects;\n"
        "list each pre-existing issue only once, under the follow-up heading."
    )


def format_follow_up(item: Dict[str, Any]) -> str:
    """Format a parsed follow-up for the compact result list."""
    if not isinstance(item, dict):
        return str(item or "")
    text = str(item.get("text") or "").strip()
    kind = item.get("kind")
    if kind in ("security", "data-loss"):
        prefix = "[%s]" % kind
        if text.lower().startswith(prefix):
            return text
        return "%s %s" % (prefix, text)
    return text
