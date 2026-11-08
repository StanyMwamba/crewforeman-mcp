"""Incremental reviewer progress checkpoint (P3.2 items 1-2).

The connector writes ``REVIEW.partial.json`` (schema v1) and a human-readable
``REVIEW.partial.md`` while an independent reviewer is still running.  Both
files are rewritten atomically from a daemon heartbeat thread, so a killed
reviewer leaves usable evidence for :func:`resume_harness`.

Python 3.9 compatible; standard library only.
"""

import datetime as dt
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

VERSION = 1
JSON_NAME = "REVIEW.partial.json"
MD_NAME = "REVIEW.partial.md"
STATUSES = ("running", "interrupted", "completed")
AREA_STATUSES = ("pending", "in_progress", "checked")

# The reviewer emits these marker lines on stdout while it works.  The
# connector owns parsing/persistence; the reviewer only has to print text, so
# no tool write permission (which a read-only review does not have) is needed.
PROGRESS_PREFIX = "@@REVIEW_PROGRESS@@"
_SEVERITY_ALIASES = {
    "blocking": "blocking",
    "non-blocking": "non-blocking",
    "non blocking": "non-blocking",
    "nonblocking": "non-blocking",
    "minor": "non-blocking",
    "info": "non-blocking",
    "low": "non-blocking",
}

# Reviewer tool names mapped to the coarse checklist they exercise.  This is a
# progress hint, not a claim that the area is finished: ``complete`` marks all
# areas checked only after the report is parsed.
_TOOL_AREA = {
    "read": "deliverables",
    "glob": "deliverables",
    "grep": "correctness",
    "bash": "tests",
    "render": "deliverables",
    "screenshot": "deliverables",
    "workflow": "correctness",
}


def _now_iso(timestamp=None):
    value = time.time() if timestamp is None else timestamp
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(
        timespec="seconds")


def write_atomic(path, text):
    """Write ``text`` to ``path`` atomically (mkstemp + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".review-partial-", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return path


def normalise_text(text):
    """Flatten a defect sentence for conservative dedupe."""
    value = str(text or "").lower()
    value = re.sub(r"(?m)^\s*(?:[-*\u2022\u00b7]+|\d+[.)])\s*", " ", value)
    value = re.sub(r"[^a-z0-9./:_-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .,;:-")


_HEADING_RE = re.compile(
    r"^\s*#{0,6}\s*(blocking defects?|non-blocking defects?|"
    r"carried-over findings?|defects?|issues?)\b", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022\u00b7]+|\d+[.)])\s+(.*\S)\s*$")


_EVIDENCE_RE = re.compile(
    r"(?:\(?\s*evidence\s*:\s*)(.+?)\)?\s*$", re.IGNORECASE)
_INLINE_SEVERITY_RE = re.compile(
    r"^\s*(?:\*\*)?\[(blocking|non[- ]?blocking|minor|info)\](?:\*\*)?"
    r"\s*[:\-]?\s*", re.IGNORECASE)
_INLINE_SEVERITY_PREFIX_RE = re.compile(
    r"^\s*(?:\*\*)?(blocking|non[- ]?blocking|minor)\s*[:\-]\s+",
    re.IGNORECASE)


def severity_group(severity):
    """Map a reviewer severity onto the two report sections."""
    return _SEVERITY_ALIASES.get(str(severity or "").strip().lower(),
                                 "blocking")


def _section_severity(label, default="blocking"):
    value = str(label or "").strip().lower()
    if "non-blocking" in value or "non blocking" in value or "minor" in value:
        return "non-blocking"
    if "blocking" in value:
        return "blocking"
    return default


_CARRIED_MARKER_RE = re.compile(
    r"\s*\[carried over from interrupted review\]\s*$", re.IGNORECASE)


def _clean_bullet(text):
    """Return ``(text, evidence, severity_or_None)`` for one bullet."""
    value = str(text or "").strip()
    value = _CARRIED_MARKER_RE.sub("", value).strip()
    severity = None
    match = _INLINE_SEVERITY_RE.match(value)
    if not match:
        match = _INLINE_SEVERITY_PREFIX_RE.match(value)
    if match:
        severity = _SEVERITY_ALIASES.get(
            match.group(1).strip().lower().replace("_", "-"))
        value = value[match.end():].strip()
    evidence = ""
    match = _EVIDENCE_RE.search(value)
    if match:
        evidence = match.group(1).strip()
        value = value[:match.start()].strip()
    return value, evidence, severity


def extract_defect_records(text, default_severity="blocking"):
    """Return defect records with their section severity preserved.

    ``REVIEW.md`` has separate ``## Blocking defects`` and ``## Non-blocking
    defects`` sections.  The old extractor flattened both into strings, which
    promoted non-blocking findings when an interrupted review was merged into
    the report.  Records keep ``text``, ``evidence``, ``severity`` (the
    declared severity, inline tag first) and ``section`` (the severity of the
    heading the bullet actually sits under) so the merge can put each finding
    back under its own heading.
    """
    records = []
    active = False
    section = default_severity
    current = None
    for raw in str(text or "").splitlines():
        heading = _HEADING_RE.match(raw)
        if heading:
            section = _section_severity(heading.group(1), default_severity)
            active = True
            current = None
            continue
        if _ANY_HEADING_RE.match(raw) or re.match(r"^\s*#{1,6}\s+\S", raw):
            active = False
            current = None
            continue
        if not active:
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet:
            item = bullet.group(1).strip()
            if item.lower().strip(" .:") in ("none", "n/a", "no", "none."):
                current = None
                continue
            item, evidence, inline = _clean_bullet(item)
            if not item:
                current = None
                continue
            record = {
                "text": item,
                "evidence": evidence,
                # ``severity`` keeps the finding's declared severity (the
                # inline tag wins for checkpoints); ``section`` records where
                # the bullet actually sits so a wrong heading is still
                # detected when the inline tag agrees with the checkpoint.
                "severity": inline or section,
                "section": section,
                "carried_over": False,
            }
            records.append(record)
            current = record
            continue
        if current is None:
            continue
        line = raw.strip()
        if not line:
            current = None
            continue
        evidence_match = _EVIDENCE_RE.search(line)
        if evidence_match:
            value = evidence_match.group(1).strip()
            if value and value not in current["evidence"]:
                current["evidence"] = "; ".join(
                    item for item in (current["evidence"], value) if item)
            continue
        if line.startswith(("- ", "* ")):
            current = None
            continue
        # A wrapped continuation line belongs to the current bullet.
        current["text"] = (current["text"] + " " + line).strip()
    seen = set()
    result = []
    for record in records:
        key = normalise_text(record["text"])
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def extract_defects(text):
    """Return bullet defect strings from REVIEW.md/answer (compatibility)."""
    return [record["text"]
            for record in extract_defect_records(text)]


def _as_defect(item):
    if isinstance(item, dict):
        text = str(item.get("text") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        severity = str(item.get("severity") or "blocking").strip().lower()
        carried = bool(item.get("carried_over"))
    else:
        text = str(item or "").strip()
        evidence = ""
        severity = "blocking"
        carried = True
    severity = _SEVERITY_ALIASES.get(severity, "blocking")
    return {"text": text, "evidence": evidence, "severity": severity,
            "carried_over": carried}


def _section_heading_re(group):
    if group == "non-blocking":
        return re.compile(
            r"^\s*##\s+(?:non[- ]?blocking|minor|info)\s+defects?\s*$",
            re.IGNORECASE)
    return re.compile(r"^\s*##\s+blocking\s+defects?\s*$", re.IGNORECASE)


def _append_section(merged, group, bullets):
    """Insert a new defect section before ``## Fix list`` when possible."""
    lines = merged.splitlines()
    title = ("## Non-blocking defects" if group == "non-blocking"
             else "## Blocking defects")
    insertion = len(lines)
    for index, line in enumerate(lines):
        if re.match(r"^\s*##\s+(?:fix list|fixes|required changes)\s*$",
                    line, re.IGNORECASE):
            insertion = index
            break
    block = [title, ""] + bullets + [""]
    rebuilt = lines[:insertion] + block + lines[insertion:]
    return "\n".join(rebuilt).rstrip() + "\n"


def _merge_section(merged, group, additions):
    """Add carried bullets under the section that matches their severity."""
    if not additions:
        return merged

    def _bullet(defect):
        line = "- %s" % defect["text"].strip()
        if defect.get("evidence"):
            line += " (evidence: %s)" % defect["evidence"]
        line += " [carried over from interrupted review]"
        return line

    bullets = [_bullet(item) for item in additions]
    lines = merged.splitlines()
    heading = None
    heading_re = _section_heading_re(group)
    for index, line in enumerate(lines):
        if heading_re.match(line):
            heading = index
            break
    if heading is None:
        return _append_section(merged, group, bullets)
    end = len(lines)
    for index in range(heading + 1, len(lines)):
        if re.match(r"^\s*#{1,6}\s+\S", lines[index]):
            end = index
            break
    body = [line for line in lines[heading + 1:end]
            if line.strip().lower().strip(" .:") not in ("none", "n/a", "no")]
    while body and not body[-1].strip():
        body.pop()
    body.extend(bullets)
    while body and not body[0].strip():
        body.pop(0)
    rebuilt = lines[:heading + 1] + [""] + body + [""] + lines[end:]
    return "\n".join(rebuilt).rstrip() + "\n"


def _remove_defect_bullets(text, key):
    """Remove defect-section bullets whose normalised text is ``key``.

    Only structured bullets inside a blocking/non-blocking section are
    removed; prose mentions elsewhere in the report are left untouched.
    """
    lines = str(text or "").splitlines()
    kept = []
    active = False
    removed = False
    index = 0
    while index < len(lines):
        raw = lines[index]
        heading = _HEADING_RE.match(raw)
        if heading:
            active = True
            kept.append(raw)
            index += 1
            continue
        if _ANY_HEADING_RE.match(raw) or re.match(r"^\s*#{1,6}\s+\S", raw):
            active = False
            kept.append(raw)
            index += 1
            continue
        bullet = _BULLET_RE.match(raw) if active else None
        if bullet:
            item, _evidence, _inline = _clean_bullet(bullet.group(1).strip())
            if normalise_text(item) == key:
                removed = True
                index += 1
                while index < len(lines):
                    follow = lines[index]
                    if not follow.strip():
                        index += 1
                        break
                    if (_BULLET_RE.match(follow)
                            or _ANY_HEADING_RE.match(follow)):
                        break
                    index += 1
                continue
        kept.append(raw)
        index += 1
    if not removed:
        return text
    return "\n".join(kept).rstrip() + "\n"


_LEGACY_SYNTHETIC_SCORE_RE = re.compile(
    r"independent\s+reviewer\s+did\s+not\s+report\s+a\s+0\s*[-\u2013\u2014]\s*10\s+score",
    re.IGNORECASE)


def _is_legacy_synthetic_defect(text):
    """3.9.3's connector-invented blocking defect is never carried forward."""
    return bool(_LEGACY_SYNTHETIC_SCORE_RE.search(str(text or "")))


def merge_review(report_text, partial):
    """Merge carried findings into the matching blocking/non-blocking section.

    Returns ``(merged_text, carried_defects)``.  Dedupe is based on structured
    defect records, not the report prose.  A finding already under the same
    severity heading is not duplicated; one under the other heading is moved
    (with its evidence) so ``quality.parse_review`` counts it once, under the
    severity recorded in the checkpoint.
    """
    carried = []
    for item in (partial or {}).get("defects") or []:
        defect = _as_defect(item)
        if defect["text"] and not _is_legacy_synthetic_defect(defect["text"]):
            defect["carried_over"] = True
            carried.append(defect)
    merged = str(report_text or "")
    for defect in carried:
        key = normalise_text(defect["text"])
        if not key:
            continue
        target = severity_group(defect["severity"])
        records = extract_defect_records(merged)
        matches = [record for record in records
                   if normalise_text(record["text"]) == key]
        # Relocation is decided by the actual Markdown heading.  A matching
        # inline `[blocking]`/`[non-blocking]` tag must not make a finding
        # under the wrong heading look correctly placed.
        if any(severity_group(record.get("section", record["severity"]))
               == target for record in matches):
            continue
        if matches:
            evidence = [defect.get("evidence") or ""]
            evidence.extend(record.get("evidence") or ""
                            for record in matches)
            merged = _remove_defect_bullets(merged, key)
            defect = dict(defect)
            defect["evidence"] = "; ".join(dict.fromkeys(
                value for value in evidence if value))
        merged = _merge_section(merged, target, [defect])
    return merged, carried


# Compatibility aliases for callers/tests that use a more explicit name.
merge_review_text = merge_review


def merge_defects(defects, partial):
    """Return ``defects`` plus carried partial defects, deduped."""
    result = [str(item) for item in (defects or [])
              if not _is_legacy_synthetic_defect(item)]
    known = {normalise_text(item) for item in result}
    for item in (partial or {}).get("defects") or []:
        defect = _as_defect(item)
        if _is_legacy_synthetic_defect(defect["text"]):
            continue
        key = normalise_text(defect["text"])
        if defect["text"] and key not in known:
            result.append(defect["text"])
            known.add(key)
    return result


def strip_progress_lines(text):
    """Remove connector protocol lines from reviewer output/report text."""
    lines = [line for line in str(text or "").splitlines()
             if PROGRESS_PREFIX not in line]
    return "\n".join(lines).strip()


class ProgressProtocol:
    """Incremental parser for the reviewer's structured stdout checkpoint lines.

    The reviewer prints lines such as::

        @@REVIEW_PROGRESS@@ area=deliverables | status=checked
        @@REVIEW_PROGRESS@@ defect=blocking | text=Missing QR | evidence=page 2
        @@REVIEW_PROGRESS@@ test=python3 -m unittest | status=ok | seconds=1.5

    ``feed`` accepts arbitrary byte/line chunks (assistant messages can be
    split anywhere) and returns whole events as soon as a line completes.
    """

    def __init__(self):
        self._buffer = ""

    @staticmethod
    def parse_line(line):
        text = str(line or "").strip()
        index = text.find(PROGRESS_PREFIX)
        if index < 0:
            return None
        payload = text[index + len(PROGRESS_PREFIX):].strip()
        if not payload:
            return None
        parts = re.split(
            r"\s*\|\s*(?=[A-Za-z_][A-Za-z0-9_]*\s*=)", payload)
        fields = {}
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip().lower()] = value.strip()
        if "defect" in fields:
            text_value = fields.get("text") or ""
            if not text_value:
                return None
            return {
                "type": "defect",
                "severity": _SEVERITY_ALIASES.get(
                    (fields.get("defect") or "blocking").lower(),
                    "blocking"),
                "text": text_value,
                "evidence": fields.get("evidence") or "",
                "area": fields.get("area") or "",
            }
        if "test" in fields:
            command = fields.get("test") or ""
            if not command:
                return None
            seconds = None
            try:
                seconds = float(fields.get("seconds"))
            except (TypeError, ValueError):
                pass
            return {"type": "test", "command": command,
                    "status": (fields.get("status") or "ok").lower(),
                    "seconds": seconds}
        if "area" in fields:
            key = fields.get("area") or ""
            status = (fields.get("status") or "in_progress").lower()
            if status not in AREA_STATUSES:
                status = "in_progress"
            if not key:
                return None
            return {"type": "area", "key": key, "status": status}
        return None

    def feed(self, text, final=False):
        data = self._buffer + str(text or "")
        if final and data and not data.endswith(("\n", "\r")):
            data += "\n"
        lines = data.splitlines()
        if not final and data and not data.endswith(("\n", "\r")):
            self._buffer = lines.pop() if lines else ""
        else:
            self._buffer = ""
        events = []
        for line in lines:
            event = self.parse_line(line)
            if event:
                events.append(event)
        return events


class ReviewProgress:
    """Durable, heartbeat-backed progress for one reviewer round."""

    def __init__(self, workspace, run_id=None, round_no=1, areas=None,
                 partial=None, heartbeat_interval=None):
        self.workspace = Path(workspace)
        self.json_path = self.workspace / JSON_NAME
        self.md_path = self.workspace / MD_NAME
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        base = dict(partial) if isinstance(partial, dict) else {}
        self.run_id = str(run_id or base.get("run_id") or "")
        try:
            self.round = int(round_no if round_no is not None
                             else base.get("round") or 1)
        except (TypeError, ValueError):
            self.round = 1
        self.status = "running"
        self.started = str(base.get("started") or _now_iso())
        self.updated = _now_iso()
        self.heartbeat_at = base.get("heartbeat_at")
        self.last_event = "created"
        self._elapsed_base = 0.0
        try:
            self._elapsed_base = float(base.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            self._elapsed_base = 0.0
        self._clock_start = time.monotonic()
        interval = heartbeat_interval
        if interval is None:
            try:
                interval = float(os.environ.get(
                    "DSH_REVIEW_HEARTBEAT_SECONDS", "5"))
            except (TypeError, ValueError):
                interval = 5.0
        self.heartbeat_interval = max(0.1, float(interval))
        source_areas = base.get("areas") or areas
        self.areas = [self._area(item) for item in (source_areas or [])]
        self.defects = []
        for item in base.get("defects") or []:
            defect = _as_defect(item)
            if defect["text"]:
                self.defects.append(defect)
        self.tests_rerun = []
        for item in base.get("tests_rerun") or []:
            if isinstance(item, dict):
                self.tests_rerun.append({
                    "command": str(item.get("command") or ""),
                    "status": str(item.get("status") or "ok"),
                    "seconds": item.get("seconds"),
                })
        self._protocol = ProgressProtocol()
        self._report_text = None

    @staticmethod
    def _area(item):
        if isinstance(item, dict):
            key = str(item.get("key") or item.get("label") or "").strip()
            label = str(item.get("label") or key).strip()
            status = str(item.get("status") or "pending").strip()
        else:
            key = str(item or "").strip()
            label = key
            status = "pending"
        if status not in AREA_STATUSES:
            status = "pending"
        return {"key": key, "label": label or key, "status": status}

    def _elapsed(self):
        return round(self._elapsed_base + (time.monotonic() - self._clock_start), 3)

    def _snapshot_locked(self):
        return {
            "version": VERSION,
            "run_id": self.run_id,
            "round": int(self.round),
            "status": self.status,
            "started": self.started,
            "updated": self.updated,
            "heartbeat_at": self.heartbeat_at,
            "elapsed_seconds": self._elapsed(),
            "areas": [dict(item) for item in self.areas],
            "defects": [dict(item) for item in self.defects],
            "tests_rerun": [dict(item) for item in self.tests_rerun],
            "last_event": self.last_event,
        }

    @staticmethod
    def _markdown(snapshot):
        lines = [
            "# Review progress - round %s" % snapshot.get("round"),
            "",
            "- Run: `%s`" % snapshot.get("run_id"),
            "- Status: **%s**" % snapshot.get("status"),
            "- Started: %s" % snapshot.get("started"),
            "- Updated: %s" % snapshot.get("updated"),
            "- Heartbeat: %s" % (snapshot.get("heartbeat_at") or "not yet"),
            "- Elapsed: %ss" % snapshot.get("elapsed_seconds"),
            "- Last event: %s" % (snapshot.get("last_event") or ""),
            "",
            "## Areas checked",
            "",
        ]
        areas = snapshot.get("areas") or []
        glyphs = {"checked": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        for area in areas:
            lines.append("- %s `%s` - %s (%s)" % (
                glyphs.get(area.get("status"), "[ ]"),
                area.get("key"), area.get("label"), area.get("status")))
        if not areas:
            lines.append("- (no areas recorded)")
        lines += ["", "## Defects", ""]
        defects = snapshot.get("defects") or []
        for defect in defects:
            lines.append("- **[%s]** %s" % (
                defect.get("severity"), defect.get("text")))
            if defect.get("evidence"):
                lines.append("  - Evidence: %s" % defect["evidence"])
            if defect.get("carried_over"):
                lines.append("  - Carried over: yes")
        if not defects:
            lines.append("- (none recorded yet)")
        lines += ["", "## Tests rerun", ""]
        tests = snapshot.get("tests_rerun") or []
        for test in tests:
            lines.append("- `%s` - %s (%ss)" % (
                test.get("command"), test.get("status"), test.get("seconds")))
        if not tests:
            lines.append("- (none recorded yet)")
        lines.append("")
        return "\n".join(lines)

    def _write_locked(self):
        self.updated = _now_iso()
        snapshot = self._snapshot_locked()
        write_atomic(self.json_path, json.dumps(
            snapshot, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
        write_atomic(self.md_path, self._markdown(snapshot))
        return snapshot

    def _start_thread_locked(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._beat, name="review-progress-heartbeat", daemon=True)
        self._thread.start()

    def _beat(self):
        while not self._stop_event.wait(self.heartbeat_interval):
            try:
                self.heartbeat(note="heartbeat")
            except Exception:
                pass

    def stop(self):
        """Stop the heartbeat thread; partial files are left in place."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def start(self):
        with self._lock:
            self.status = "running"
            self.last_event = "review started"
            if self.areas and not any(
                    area["status"] == "in_progress" for area in self.areas):
                for area in self.areas:
                    if area["status"] == "pending":
                        area["status"] = "in_progress"
                        break
            self._write_locked()
            self._start_thread_locked()
        return self.snapshot()

    def heartbeat(self, note=None, tool=None):
        with self._lock:
            self.heartbeat_at = _now_iso()
            if tool:
                self.last_event = "tool: %s" % str(tool)
            elif note:
                self.last_event = str(note)[:1000]
            else:
                self.last_event = "heartbeat"
            return self._write_locked()

    def note_tool(self, name):
        tool = str(name or "").strip().lower()
        area = _TOOL_AREA.get(tool)
        with self._lock:
            self.last_event = "tool: %s" % str(name)
            if area:
                for item in self.areas:
                    if item.get("key") == area and item.get("status") == "pending":
                        item["status"] = "in_progress"
            return self._write_locked()

    def note_area(self, key, status="checked"):
        """Record the connector's own progress for one coarse review area."""
        area_key = str(key or "").strip()
        state = str(status or "checked").strip().lower()
        if state not in AREA_STATUSES:
            state = "pending"
        with self._lock:
            for item in self.areas:
                if (item.get("key") == area_key
                        or str(item.get("label") or "").strip().lower()
                        == area_key.lower()):
                    item["status"] = state
                    self.last_event = "area %s: %s" % (
                        item.get("key"), state)
                    return self._write_locked()
        return self.snapshot()

    def _apply_progress_locked(self, event):
        """Persist one parsed protocol event through the public API."""
        kind = (event or {}).get("type")
        if kind == "area":
            self.note_area(event.get("key"), event.get("status"))
            return
        if kind == "defect":
            self.add_defect(
                event.get("text"), evidence=event.get("evidence") or "",
                severity=event.get("severity") or "blocking")
            return
        if kind == "test":
            self.add_test(
                event.get("command"), status=event.get("status") or "ok",
                seconds=event.get("seconds"))

    def feed_progress(self, text, final=False):
        """Parse live reviewer protocol lines and persist the events.

        Returns the list of events that were applied.  A line split across
        stream chunks is buffered until the newline (or ``final=True``)
        completes it, so a hard kill leaves every fully emitted event durable.
        """
        with self._lock:
            events = self._protocol.feed(text, final=final)
            if not events:
                return []
            for event in events:
                self._apply_progress_locked(event)
            self._write_locked()
            return events

    def add_defect(self, text, evidence="", severity="blocking",
                   carried_over=False):
        text = str(text or "").strip()
        if not text:
            return False
        severity = _SEVERITY_ALIASES.get(
            str(severity or "blocking").strip().lower(), "blocking")
        with self._lock:
            key = normalise_text(text)
            for existing in self.defects:
                if normalise_text(existing["text"]) != key:
                    continue
                evidence = str(evidence or "").strip()
                if evidence and evidence not in existing["evidence"]:
                    existing["evidence"] = "; ".join(
                        item for item in (existing["evidence"], evidence) if item)
                if severity == "blocking":
                    # A finding that any source calls blocking stays blocking;
                    # never silently promote the other direction.
                    existing["severity"] = "blocking"
                existing["carried_over"] = bool(
                    existing["carried_over"] or carried_over)
                self.last_event = "defect updated: %s" % text[:160]
                self._write_locked()
                return False
            self.defects.append({
                "text": text,
                "evidence": str(evidence or "").strip(),
                "severity": severity,
                "carried_over": bool(carried_over),
            })
            self.last_event = "defect added: %s" % text[:160]
            self._write_locked()
        return True

    def add_test(self, command, status="ok", seconds=None):
        with self._lock:
            command = str(command or "")
            status = str(status or "ok")
            for existing in self.tests_rerun:
                if (existing.get("command") == command
                        and existing.get("status") == status):
                    existing["seconds"] = seconds
                    self.last_event = "test: %s (%s)" % (command, status)
                    self._write_locked()
                    return self.snapshot()
            self.tests_rerun.append({
                "command": command,
                "status": status,
                "seconds": seconds,
            })
            self.last_event = "test: %s (%s)" % (command, status)
            self._write_locked()
        return self.snapshot()

    def interrupt(self, reason):
        self._stop_event.set()
        with self._lock:
            try:
                # A final line without a trailing newline is still a complete
                # event when the reviewer has already stopped.
                self.feed_progress("", final=True)
            except Exception:
                pass
            self.status = "interrupted"
            self.last_event = "interrupted: %s" % str(reason or "unknown")
            self._write_locked()
        self.stop()
        return self.snapshot()

    def complete(self, report_text=None, defects=None):
        self._stop_event.set()
        with self._lock:
            self.status = "completed"
            for area in self.areas:
                area["status"] = "checked"
            for item in defects or []:
                defect = _as_defect(item)
                if not defect["text"] or _is_legacy_synthetic_defect(
                        defect["text"]):
                    continue
                key = normalise_text(defect["text"])
                match = next((existing for existing in self.defects
                              if normalise_text(existing["text"]) == key), None)
                if match is None:
                    self.defects.append(defect)
                else:
                    if defect["evidence"] and defect["evidence"] not in match["evidence"]:
                        match["evidence"] = "; ".join(
                            value for value in (match["evidence"],
                                                defect["evidence"]) if value)
                    if severity_group(defect["severity"]) == "blocking":
                        match["severity"] = "blocking"
                    match["carried_over"] = bool(
                        match["carried_over"] or defect["carried_over"])
            self._report_text = str(report_text or "")
            self.last_event = "review completed"
            self._write_locked()
        self.stop()
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    @classmethod
    def load(cls, workspace):
        """Load the partial snapshot from a workspace (or a JSON path)."""
        path = Path(workspace)
        if path.is_file() or path.suffix == ".json":
            return cls.load_file(path)
        return cls.load_file(path / JSON_NAME)

    @staticmethod
    def load_file(path):
        """Load and validate one partial JSON file; ``None`` when unusable."""
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("version", VERSION) != VERSION:
            return None
        if not isinstance(data.get("areas", []), list):
            return None
        if not isinstance(data.get("defects", []), list):
            return None
        return data
