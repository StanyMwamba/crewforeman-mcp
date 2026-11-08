"""SLICES.md write-set parser and same-wave overlap detection (item 6b).

The connector asks builders to publish a ``SLICES.md`` file before building:
one wave, one slice per builder, each slice listing the files it owns.  This
module parses the two shapes builders actually write (headings and a markdown
table) and reports files that two slices in the *same* wave claim.  Reusing a
file in a later wave is allowed and is not an overlap.

Public API is frozen for 3.10.0: ``NAME``, ``MAX_TEXT``, ``parse_slices`` and
``inspect``.  Stdlib only, Python 3.9 compatible.
"""

from __future__ import annotations

import os
import re

NAME = "SLICES.md"
MAX_TEXT = 20000

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_WAVE_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?wave\s+(\d+)\b(?:\s*[:\-—(].*)?$",
    re.IGNORECASE,
)
_SLICE_HEADING_RE = re.compile(r"^\s*(?:\*\*)?slice\b", re.IGNORECASE)
_WRITES_HEADING_RE = re.compile(
    r"^\s*(?:\*\*)?(write[\s-]?set|writes|files)\b\s*:?\s*$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*+•]\s+(.*)$")
_INLINE_WRITES_RE = re.compile(
    r"^\s*(?:\*\*)?(writes?|write[\s-]?set|files)"
    r"\s*(?:\((.*?)\))?\s*(?::\s*(.*)|\s*$)",
    re.IGNORECASE,
)
_IGNORED = {
    "",
    "-",
    "—",
    "–",
    "none",
    "(none)",
    "n/a",
    "na",
    "nil",
    "tbd",
    "todo",
    "and",
    "&",
    "plus",
}


def _strip_markup(value: str) -> str:
    """Strip code ticks, quotes and markdown emphasis from one value."""

    value = value.strip()
    value = value.strip("`'\"")
    value = value.replace("**", "").replace("__", "")
    return value.strip().strip("`'\"").strip()


def _normalise_file(value: str) -> str:
    """Normalise one path for comparison (case-sensitive)."""

    value = _strip_markup(value)
    value = value.replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    while value.startswith("./"):
        value = value[2:]
    value = value.strip()
    # Drop decoration that commonly trails a file in prose/lists.
    value = value.rstrip(",;:")
    if value.endswith(".") and not value.endswith(".."):
        value = value[:-1]
    return value


def _is_none(value: str) -> bool:
    return value.strip().lower() in _IGNORED


def _split_files(blob: str) -> list:
    """Split a write-set fragment into normalised, de-duplicated paths."""

    if not blob:
        return []
    blob = _strip_markup(blob)
    # A bullet marker can survive inline fragments such as "- a.py".
    blob = re.sub(r"^[-*+•]\s+", "", blob)
    files = []
    seen = set()
    for token in re.split(r"[,;\s+]+", blob):
        token = token.strip()
        if _is_none(token):
            continue
        token = _normalise_file(token)
        if not token or _is_none(token):
            continue
        # Ignore prose words that happen to be glued to a list.
        if token.lower() in {"none.", "n/a."}:
            continue
        if token not in seen:
            seen.add(token)
            files.append(token)
    return files


def _extract_files(line: str) -> list:
    """Parse a ``Writes: ...`` / ``Files: ...`` style line."""

    match = _INLINE_WRITES_RE.match(line)
    if not match:
        return []
    in_parens = match.group(2) or ""
    after_colon = match.group(3) or ""
    return _split_files(in_parens) + _split_files(after_colon)


def _wave_from_heading(text: str):
    match = re.search(r"\bwave\s+(\d+)\b", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _wave_from_cell(value: str):
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else None


def _new_slice(name: str, wave: int) -> dict:
    clean = _strip_markup(name).rstrip(":").strip()
    return {"name": clean or "slice", "wave": int(wave), "files": []}


def _is_write_set_heading(text: str) -> bool:
    clean = _strip_markup(text).rstrip(":").strip().lower()
    return clean in {
        "writes",
        "write-set",
        "write set",
        "write sets",
        "write-sets",
        "files",
        "write set files",
        "write-set files",
    }


def _table_columns(header: str):
    cells = [c.strip() for c in header.strip().strip("|").split("|")]
    lower = [c.lower() for c in cells]
    slice_idx = None
    wave_idx = None
    writes_idx = None
    for idx, cell in enumerate(lower):
        cell = cell.replace("**", "").strip()
        if slice_idx is None and (
            "slice" in cell or "builder" in cell or cell in {"agent", "name", "id"}
        ):
            slice_idx = idx
            continue
        if wave_idx is None and "wave" in cell:
            wave_idx = idx
            continue
        if writes_idx is None and (
            "writes" in cell
            or "write-set" in cell
            or "write set" in cell
            or cell in {"files", "file", "write-set files"}
        ):
            writes_idx = idx
    return slice_idx, wave_idx, writes_idx


def _table_row_cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_slices(text: str) -> dict:
    """Parse SLICES text into waves, slices and same-wave file overlaps."""

    text = (text or "")[:MAX_TEXT]
    lines = text.splitlines()

    slices = []
    declared_waves = set()
    current_wave = None
    current_slice = None
    in_write_bullets = False

    def ensure_wave_default() -> int:
        return current_wave if current_wave is not None else 1

    def add_slice(name: str, wave) -> dict:
        nonlocal current_slice
        item = _new_slice(name, wave if wave is not None else ensure_wave_default())
        slices.append(item)
        current_slice = item
        return item

    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        line = raw.strip()

        # Markdown table: header row followed by a separator row.
        if "|" in raw and idx + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(lines[idx + 1]):
            slice_idx, wave_idx, writes_idx = _table_columns(raw)
            if slice_idx is not None and writes_idx is not None:
                idx += 2
                while idx < len(lines):
                    row = lines[idx]
                    if "|" not in row:
                        break
                    cells = _table_row_cells(row)
                    if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                        idx += 1
                        continue
                    if len(cells) <= slice_idx:
                        idx += 1
                        continue
                    name = _strip_markup(cells[slice_idx])
                    if not name or _is_none(name):
                        idx += 1
                        continue
                    wave = None
                    if wave_idx is not None and wave_idx < len(cells):
                        wave = _wave_from_cell(cells[wave_idx])
                    if wave is not None:
                        declared_waves.add(wave)
                    item = add_slice(name, wave)
                    if writes_idx < len(cells):
                        item["files"].extend(_split_files(cells[writes_idx]))
                    idx += 1
                current_slice = None
                in_write_bullets = False
                continue

        heading = _HEADING_RE.match(raw)
        if heading:
            level = len(heading.group(1))
            heading_text = _strip_markup(heading.group(2))
            wave = _wave_from_heading(heading_text)
            if wave is not None:
                declared_waves.add(wave)
                current_wave = wave
                current_slice = None
                in_write_bullets = False
                idx += 1
                continue
            if _is_write_set_heading(heading_text):
                in_write_bullets = current_slice is not None
                idx += 1
                continue
            if _SLICE_HEADING_RE.match(heading_text) or level >= 3:
                add_slice(heading_text, ensure_wave_default())
                in_write_bullets = False
                idx += 1
                continue
            # A top-level heading that is not a slice closes the current slice.
            current_slice = None
            in_write_bullets = False
            idx += 1
            continue

        if not line:
            in_write_bullets = False
            idx += 1
            continue

        # "Wave 2" as a plain/bold line rather than a heading.
        if _WAVE_LINE_RE.match(line):
            wave = _wave_from_heading(line)
            if wave is not None:
                declared_waves.add(wave)
                current_wave = wave
                current_slice = None
                in_write_bullets = False
                idx += 1
                continue

        if current_slice is not None and _INLINE_WRITES_RE.match(line):
            current_slice["files"].extend(_extract_files(line))
            in_write_bullets = True
            idx += 1
            continue

        bullet = _BULLET_RE.match(line)
        if in_write_bullets and current_slice is not None and bullet:
            current_slice["files"].extend(_split_files(bullet.group(1)))
            idx += 1
            continue

        if in_write_bullets and bullet:
            # Bullet without an open write-set section: normal prose.
            idx += 1
            continue

        if in_write_bullets:
            in_write_bullets = False
        idx += 1

    # De-duplicate files within a slice while keeping declaration order.
    for item in slices:
        seen = set()
        unique = []
        for path in item["files"]:
            if path not in seen:
                seen.add(path)
                unique.append(path)
        item["files"] = unique

    # Same-wave only: (wave, file) -> ordered claimants.  Identity is the slice
    # index, not the display name: two slices may share a name (for example two
    # "Builder 1" headings) and must still count as two claimants of a file.
    claims = {}
    for index, item in enumerate(slices):
        for path in item["files"]:
            key = (item["wave"], path)
            claimants = claims.setdefault(key, [])
            claimants.append((index, item["name"]))

    overlaps = []
    for (wave, path), claimants in claims.items():
        if len(claimants) > 1:
            overlaps.append({"wave": wave, "file": path,
                             "slices": [name for _, name in claimants]})

    return {
        "waves": len(declared_waves),
        "slices": slices,
        "overlaps": overlaps,
        "warnings": len(overlaps),
    }


def inspect(workspace: str) -> dict:
    """Inspect ``<workspace>/SLICES.md`` without raising on a missing file."""

    empty = {
        "present": False,
        "waves": 0,
        "slices": 0,
        "overlaps": [],
        "warnings": 0,
    }
    path = os.path.join(workspace or ".", NAME)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_TEXT)
    except (OSError, ValueError):
        return empty
    result = parse_slices(text)
    result["present"] = True
    return result
