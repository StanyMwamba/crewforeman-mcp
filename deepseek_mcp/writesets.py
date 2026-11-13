"""Automatic write-set inference and parallel-job decisions (3.14.2).

When a Harness job is submitted without an explicit ``write_set`` the
connector may still run it in a worktree in parallel with another job, but only
when the inferred sets are disjoint and confident.  Unknown is the universe:
no inference or low confidence keeps today's serial path.

All inference is cheap and pure-Python (no model call, no git subprocess); the
pipeline module owns worktrees, patches and installs.  Python 3.9 compatible.
"""

from __future__ import annotations

import math
import os
import posixpath
import re

VERSION = "3.14.3"
MAX_ITEMS = 200
MAX_PROMPT = 256 * 1024
DEFAULT_MIN_CONFIDENCE = 0.85

CONF_PLAN = 0.93
CONF_PLAN_PARTS = 0.90
CONF_SLICES = 0.88
CONF_LABELED = 0.90
CONF_BACKTICK = 0.90
CONF_BARE = 0.78
CONF_PROSE = 0.50

_EXT = (r"py|md|json|toml|cfg|ini|yml|yaml|txt|sh|ps1|bat|cmd|ts|js|jsx|tsx|"
        r"html|css|go|rs|java|c|h|cpp|hpp|sql")
_BARE_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"[A-Za-z0-9_.-]+\.(?:" + _EXT + r"))(?![\w/.-])")
_BACKTICK_RE = re.compile(r"`([^`\n]{2,240})`")
_LABEL_RE = re.compile(
    r"^\s*(?:files?|write[\s-]?set|writes|touching|modify|create|change|"
    r"paths?)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_WIDE_RE = re.compile(
    r"\b(?:entire|whole|all)\s+(?:repo|repository|codebase|files?|project)\b"
    r"|\beverywhere\b|\brefactor\s+the\s+whole\b", re.IGNORECASE)
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SAFE_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")
_SPLIT_RE = re.compile(r"[,;\s]+")


def _cfg(cfg, key, default=None):
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                pass
        except Exception:
            pass
    return getattr(cfg, key, default)


def _bool(value, default):
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _float(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _record_value(record, key):
    """Effective setting frozen on a queue item or its pipeline block."""
    if not isinstance(record, dict):
        return None
    if record.get(key) is not None:
        return record[key]
    pipeline = record.get("pipeline")
    if isinstance(pipeline, dict) and pipeline.get(key) is not None:
        return pipeline[key]
    return None


def record_auto_parallel(record, cfg=None):
    """Admission view: the job's frozen opt-out wins over live config."""
    value = _record_value(record, "auto_parallel")
    if value is None:
        value = _cfg(cfg, "auto_parallel", True)
    return _bool(value, True)


def record_min_confidence(record, cfg=None):
    """Admission view: the job's frozen threshold wins over live config."""
    value = _record_value(record, "auto_parallel_min_confidence")
    if value is None:
        value = _cfg(cfg, "auto_parallel_min_confidence",
                     DEFAULT_MIN_CONFIDENCE)
    return _float(value, DEFAULT_MIN_CONFIDENCE)


def normalise_rel(path, workspace=None):
    """Normalise one path to a repo-relative forward-slash string."""
    text = str(path or "").strip().strip("`'\"")
    text = text.split("#", 1)[0].split("?", 1)[0].replace("\\", "/")
    if _URL_RE.match(text):
        return ""
    if workspace:
        root = os.path.normcase(os.path.realpath(str(workspace))).replace("\\", "/")
        candidate = (os.path.normcase(os.path.realpath(text)).replace("\\", "/")
                     if os.path.isabs(text) else "")
        if candidate and candidate.startswith(root.rstrip("/") + "/"):
            # Strip the separator too: "/root/src/a.py" -> "src/a.py".
            text = candidate[len(root):].lstrip("/")
    if (text.startswith("/") or os.path.isabs(text)
            or re.match(r"^[A-Za-z]:/", text)):
        return ""
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    text = posixpath.normpath(text).replace("\\", "/")
    if text in (".", "..") or text.startswith("../") or text.startswith(".git/"):
        return ""
    if text in ("PLAN.json", "SLICES.md", "CROSSCHECK.json"):
        return ""
    if not _SAFE_RE.match(text):
        return ""
    return text.rstrip("/")


def normalise_set(items, workspace=None):
    valid = []
    seen = set()
    invalid = []
    for item in items or []:
        path = normalise_rel(item, workspace)
        if not path:
            if str(item or "").strip():
                invalid.append(str(item).strip()[:120])
            continue
        key = path.casefold() if os.name == "nt" else path
        if key in seen:
            continue
        seen.add(key)
        valid.append(path)
        if len(valid) >= MAX_ITEMS:
            break
    return {"valid": valid, "invalid": invalid[:40], "truncated": len(valid) >= MAX_ITEMS}


def _safe_glob(raw):
    """Normalise a write-set glob, rejecting traversal/absolute patterns."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        return ""
    while text.startswith("./"):
        text = text[2:]
    if not text or ".." in text.split("/") or text.startswith(".git/"):
        return ""
    # Keep glob metacharacters, but never shell/format punctuation.
    if not re.match(r"^[A-Za-z0-9._\-/*?\[\]]+$", text):
        return ""
    return text


def _fs_key(path):
    """Case-normalise for the current filesystem and keep '/' separators.

    ``os.path.normcase`` turns '/' into '\\\\' on Windows; converting back
    keeps the directory-prefix comparison and glob patterns consistent.
    """
    return os.path.normcase(str(path)).replace("\\", "/")


def path_covered(path, write_set):
    """True when ``path`` is the same as or under a write-set entry."""
    target = normalise_rel(path)
    if not target:
        return False
    for item in write_set or []:
        raw = str(item or "").strip().replace("\\", "/")
        if any(char in raw for char in "*?["):
            import fnmatch
            pattern = _safe_glob(raw)
            # Containment follows the actual filesystem's case rules: a
            # case-only difference must not let a stray file install on a
            # case-sensitive Linux tree.
            if pattern and fnmatch.fnmatchcase(
                    _fs_key(target), _fs_key(pattern)):
                return True
            continue
        entry = normalise_rel(raw)
        if not entry:
            continue
        target_key = _fs_key(target)
        entry_key = _fs_key(entry)
        if (target_key == entry_key
                or target_key.startswith(entry_key.rstrip("/") + "/")):
            return True
    return False


def sets_overlap(first, second):
    left = [normalise_rel(item) for item in (first or [])]
    right = [normalise_rel(item) for item in (second or [])]
    left = [item.casefold() for item in left if item]
    right = [item.casefold() for item in right if item]
    if not left or not right:
        return True
    for a in left:
        for b in right:
            if a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(
                    a.rstrip("/") + "/"):
                return True
    return False


def _tokens_from_blob(blob, workspace=None):
    found = []
    for token in _SPLIT_RE.split(str(blob or "")):
        token = token.strip().strip("`'\"()[]{}<>").rstrip(".,;:")
        path = normalise_rel(token, workspace)
        if path:
            found.append(path)
    return found


def _read_plan(workspace):
    import json
    path = os.path.join(str(workspace or ""), "PLAN.json")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4 * 1024 * 1024)
    except OSError:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def infer_write_set(prompt, workspace, plan_data=None):
    """Infer the files/dirs a job is likely to write, with confidence.

    Returns ``{write_set, confidence, source, sources, wide, reason}``.
    Unknown/low-confidence results are still returned for the record; callers
    decide whether they are safe enough to parallelise.
    """
    result = {"write_set": [], "confidence": 0.0, "source": "none",
              "sources": [], "wide": False, "reason": "no_signal",
              "truncated": False}
    try:
        plan = plan_data if isinstance(plan_data, dict) else _read_plan(workspace)
        items = []
        sources = []
        if plan:
            for key in ("write_set", "writeSet"):
                value = plan.get(key)
                if isinstance(value, str):
                    value = [value]
                if isinstance(value, list) and value:
                    items.extend(value)
                    sources.append("plan")
                    break
            if not items:
                parts = plan.get("parts") or plan.get("slices")
                if isinstance(parts, list):
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        for key in ("write_set", "writes", "files",
                                    "files_expected"):
                            value = part.get(key)
                            if isinstance(value, str):
                                value = [value]
                            if isinstance(value, list):
                                items.extend(value)
                    if items:
                        sources.append("plan_parts")
            if not items:
                for key in ("files_expected", "files", "directories", "dirs"):
                    value = plan.get(key)
                    if isinstance(value, str):
                        value = [value]
                    if isinstance(value, list) and value:
                        items.extend(value)
                        sources.append("plan_files")
                        break

        slices_path = os.path.join(str(workspace or ""), "SLICES.md")
        try:
            with open(slices_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_PROMPT)
            for line in text.splitlines():
                if re.match(r"^\s*(?:\*\*)?writes?\b", line, re.IGNORECASE):
                    items.extend(_tokens_from_blob(
                        re.sub(r"^\s*[*\s]*writes?\s*:?", "", line,
                               flags=re.IGNORECASE), workspace))
                    sources.append("slices")
        except OSError:
            pass

        text = str(prompt or "")[:MAX_PROMPT]
        labeled = []
        for match in _LABEL_RE.finditer(text):
            labeled.extend(_tokens_from_blob(match.group(1), workspace))
        if labeled:
            items.extend(labeled)
            sources.append("labeled")
        backticked = []
        for match in _BACKTICK_RE.finditer(text):
            token = match.group(1).strip()
            token = re.sub(r":\d+(?::\d+)?$", "", token)
            path = normalise_rel(token, workspace)
            if path:
                backticked.append(path)
        if backticked:
            items.extend(backticked)
            sources.append("backtick")
        bare = []
        for match in _BARE_PATH_RE.finditer(text):
            path = normalise_rel(match.group(1), workspace)
            if path:
                bare.append(path)
        if bare:
            items.extend(bare)
            sources.append("bare")

        normal = normalise_set(items, workspace)
        valid = normal["valid"]
        wide = bool(_WIDE_RE.search(text))
        result["write_set"] = valid
        result["wide"] = wide
        result["sources"] = sorted(set(sources))
        result["truncated"] = normal["truncated"]
        if not valid:
            return result
        base = 0.0
        if "plan" in sources:
            base = max(base, CONF_PLAN)
        if "plan_parts" in sources:
            base = max(base, CONF_PLAN_PARTS)
        if "plan_files" in sources:
            base = max(base, CONF_PLAN_PARTS)
        if "slices" in sources:
            base = max(base, CONF_SLICES)
        if "labeled" in sources:
            base = max(base, CONF_LABELED)
        if "backtick" in sources:
            base = max(base, CONF_BACKTICK)
        if "bare" in sources:
            base = max(base, CONF_BARE)
        if len(set(sources)) >= 2:
            base += 0.03
        if wide:
            base -= 0.20
        if len(valid) > 40 or normal["truncated"]:
            base -= 0.10
        if 0.0 < base < 0.5:
            base = min(base, CONF_PROSE)
        result["confidence"] = round(max(0.0, min(0.99, base)), 2)
        result["source"] = sources[0] if sources else "none"
        result["reason"] = "inferred"
        return result
    except Exception:
        result["reason"] = "inference_failed"
        return result


def _record_set(record):
    if not isinstance(record, dict):
        return []
    for key in ("inferred_write_set", "write_set", "pipeline_write_set"):
        value = record.get(key)
        if isinstance(value, list) and value:
            return value
    pipeline = record.get("pipeline")
    if isinstance(pipeline, dict):
        return pipeline.get("inferred_write_set") or pipeline.get("write_set") or []
    return []


def _record_confidence(record):
    if not isinstance(record, dict):
        return 0.0
    for key in ("write_set_confidence", "inferred_confidence", "confidence"):
        if record.get(key) is not None:
            return _float(record.get(key), 0.0)
    pipeline = record.get("pipeline")
    if isinstance(pipeline, dict):
        for key in ("write_set_confidence", "inferred_confidence"):
            if pipeline.get(key) is not None:
                return _float(pipeline.get(key), 0.0)
    source = record.get("write_set_source")
    if source is None and isinstance(pipeline, dict):
        source = pipeline.get("write_set_source")
    # Legacy/explicit declarations predate confidence and are trusted; only a
    # missing confidence on an *inferred* set is unknown.
    if source == "inferred":
        return 0.0
    return 1.0


def _workspace(record):
    if not isinstance(record, dict):
        return ""
    value = (record.get("canonical_workspace")
             or (record.get("pipeline") or {}).get("canonical_workspace")
             or record.get("workspace") or "")
    if not value:
        return ""
    try:
        return os.path.normcase(os.path.realpath(str(value)))
    except OSError:
        return os.path.normcase(str(value))


def parallel_decision(record, active, cfg=None):
    """Decide whether ``record`` may run beside the already-active records."""
    auto = record_auto_parallel(record, cfg)
    minimum = record_min_confidence(record, cfg)
    write_set = _record_set(record)
    confidence = _record_confidence(record)
    workspace = _workspace(record)
    if not auto:
        same_workspace = 0
        for peer in active or []:
            if (isinstance(peer, dict) and peer is not record
                    and _workspace(peer) == workspace):
                same_workspace += 1
        return {"parallel": False, "reason": "auto_parallel_disabled",
                "write_set": write_set, "confidence": confidence,
                "same_workspace_active": same_workspace}
    if not write_set:
        return {"parallel": False, "reason": "no_write_set",
                "write_set": [], "confidence": confidence}
    if confidence < minimum:
        return {"parallel": False, "reason": "low_confidence",
                "write_set": write_set, "confidence": confidence}
    pipeline = (record.get("pipeline") if isinstance(record, dict) else None) or {}
    if pipeline.get("write_set_wide") or record.get("write_set_wide"):
        return {"parallel": False, "reason": "wide_task",
                "write_set": write_set, "confidence": confidence}
    peers = []
    for peer in active or []:
        if not isinstance(peer, dict) or peer is record:
            continue
        # Different workspaces are independent: a serial (non-pipeline) job in
        # another repo must not turn this workspace's job serial.
        peer_workspace = _workspace(peer)
        if workspace and peer_workspace and peer_workspace != workspace:
            continue
        if not bool(peer.get("pipeline_eligible")):
            return {"parallel": False, "reason": "peer_serial",
                    "write_set": write_set, "confidence": confidence}
        if not record_auto_parallel(peer, cfg):
            return {"parallel": False, "reason": "peer_serial",
                    "write_set": write_set, "confidence": confidence}
        peer_set = _record_set(peer)
        if not peer_set:
            return {"parallel": False, "reason": "peer_unknown_write_set",
                    "write_set": write_set, "confidence": confidence}
        peer_conf = _record_confidence(peer)
        if peer_conf < record_min_confidence(peer, cfg):
            return {"parallel": False, "reason": "peer_low_confidence",
                    "write_set": write_set, "confidence": confidence}
        if sets_overlap(write_set, peer_set):
            return {"parallel": False, "reason": "write_set_overlap",
                    "write_set": write_set, "confidence": confidence,
                    "peers": peers}
        peers.append(str(peer.get("run_id") or peer.get("work_id") or ""))
    return {"parallel": True, "reason": "disjoint_confident",
            "write_set": write_set, "confidence": confidence, "peers": peers}


def stray_files(write_set, changed_files, extra_allowed=()):
    """Files in the actual diff that escaped the inferred/declared write set."""
    allowed = list(write_set or []) + list(extra_allowed or [])
    stray = []
    for path in changed_files or []:
        normal = normalise_rel(path)
        if not normal:
            # Unknown/absolute paths cannot be proven inside the write set, so
            # treat them as stray rather than silently dropping them.
            text = str(path or "").strip().replace("\\", "/")
            if text:
                stray.append(text)
            continue
        if not path_covered(normal, allowed):
            stray.append(normal)
    return sorted(set(stray))


def integration_decision(pipe, changed_files, apply_check_ok):
    """Return how a finished patch may be integrated."""
    write_set = (pipe or {}).get("write_set") or []
    if not write_set:
        # An empty set cannot prove that any changed file is allowed.
        return {"action": "serial_reintegrate", "reason": "no_write_set",
                "stray": []}
    stray = stray_files(write_set, changed_files,
                        extra_allowed=(pipe or {}).get("extra_allowed") or [])
    if stray:
        return {"action": "serial_reintegrate", "reason": "write_set_stray",
                "stray": stray}
    if not apply_check_ok:
        return {"action": "serial_reintegrate", "reason": "apply_check_failed",
                "stray": []}
    return {"action": "install", "reason": "clean", "stray": []}


def annotate(pipe, record, decision):
    """Copy the inference/parallel record into the pipeline block."""
    if not isinstance(pipe, dict):
        pipe = {}
    pipe["inferred_write_set"] = list(decision.get("write_set") or [])
    pipe["write_set_confidence"] = decision.get("confidence")
    pipe["parallel"] = bool(decision.get("parallel"))
    pipe["parallel_reason"] = decision.get("reason")
    pipe["parallel_peers"] = list(decision.get("peers") or [])
    if isinstance(record, dict):
        record.update({
            "inferred_write_set": pipe["inferred_write_set"],
            "write_set_confidence": pipe["write_set_confidence"],
            "parallel": pipe["parallel"],
            "parallel_reason": pipe["parallel_reason"],
        })
    return pipe
