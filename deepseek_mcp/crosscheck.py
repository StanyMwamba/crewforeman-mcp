"""Crosscheck mode: crew build/check gate for Harness jobs (3.14.2).

Opt-in only.  When ``mode="crosscheck"`` (per job) or config
``default_mode="crosscheck"`` is set, the connector appends a crew contract to
the builder task and then verifies the result against ``PLAN.json`` and
``CROSSCHECK.json``.  The score is connector-owned: crew prose, self-scores and
``QUALITY_REPORT.md`` claims never enter it.

The module is deliberately pure/defensive so the harness can call it after a
paid phase without turning a malformed artifact into a run crash.  Standard
library only, Python 3.9 compatible, Windows-safe path handling.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import subprocess
import sys
import time

try:  # Keep the module importable in isolation.
    from .api import redact_secrets
except Exception:  # pragma: no cover - api always exists in the package

    def redact_secrets(text):
        return str(text or "")


VERSION = 1
CROSSCHECK_FILE = "CROSSCHECK.json"
PLAN_FILE = "PLAN.json"
DEFAULT_THRESHOLD = 10.0
DEFAULT_MAX_ROUNDS = 3
DEFAULT_CREWS = 2
DEFAULT_CREW_SIZE = 4
MIN_PARTS = 2
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TEXT = 4000

SEVERITY_BLOCKING = "blocking"
SEVERITY_MINOR = "minor"

STATUS_PASSED = "passed"
STATUS_BELOW = "crosscheck_below_threshold"

_REQUIREMENT_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.+?)\s*$")
_PATH_SPLIT_RE = re.compile(r"[,;\s]+")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


class CrosscheckError(ValueError):
    """A validated crosscheck configuration/plan problem."""

    code = "crosscheck_error"


class CrosscheckConfigError(CrosscheckError):
    code = "crosscheck_config_invalid"


class CrosscheckPlanError(CrosscheckError):
    code = "crosscheck_plan_invalid"


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


def _as_float(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return result


def _as_int(value, default, low, high):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    return max(low, min(high, result))


def _as_bool(value, default):
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _truncate(text, limit=MAX_TEXT):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _norm_path(value):
    """Normalise one repo-relative path for comparison/storage."""
    text = str(value or "").strip().strip("`'\"")
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    if text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        return ""
    text = posixpath.normpath(text)
    if text in (".", "..") or text.startswith("../") or text.startswith(".git/"):
        return ""
    return text.rstrip("/")


def _path_key(value):
    # Case-fold everywhere.  macOS volumes are commonly case-insensitive and a
    # case-only collision would let two "independent" parts write one file.
    return _norm_path(value).casefold()


def _glob_prefix(value):
    return re.split(r"[*?\[]", value, 1)[0].rstrip("/")


def _path_lists_overlap(first, second):
    """True when two write sets share a file or one directory contains the other."""
    left = [_path_key(item) for item in (first or []) if _path_key(item)]
    right = [_path_key(item) for item in (second or []) if _path_key(item)]
    if not left or not right:
        return True
    for a in left:
        for b in right:
            if a == b:
                return True
            if a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/"):
                return True
            # Glob entries are conservatively treated as overlapping when their
            # literal prefixes nest; refusing to parallelise is the safe default.
            if any(ch in a or ch in b for ch in "*?["):
                pa, pb = _glob_prefix(a), _glob_prefix(b)
                if (not pa or not pb or pa == pb
                        or pa.startswith(pb + "/") or pb.startswith(pa + "/")):
                    return True
    return False


def _split_paths(blob):
    found = []
    seen = set()
    for token in _PATH_SPLIT_RE.split(str(blob or "")):
        token = token.strip().strip("`'\"()[]{}<>")
        token = token.rstrip(".,;:")
        path = _norm_path(token)
        if not path or not _SAFE_PATH_RE.match(path):
            continue
        key = _path_key(path)
        if key not in seen:
            seen.add(key)
            found.append(path)
    return found


# ---------------------------------------------------------------------------
# configuration


def normalise_config(args, cfg):
    """Return the validated crosscheck config, or ``None`` when disabled.

    Unknown ``mode`` values raise :class:`CrosscheckConfigError`; an invalid
    threshold, crew count or round count is a caller error and must stop the
    run before a paid process starts.
    """
    args = args or {}
    mode = str(args.get("mode") or "").strip().lower()
    default_mode = str(_cfg(cfg, "default_mode", "") or "").strip().lower()
    if not mode:
        mode = default_mode or "standard"
    if mode in ("standard", "off", "none", "disabled"):
        return None
    if mode != "crosscheck":
        raise CrosscheckConfigError(
            "mode must be 'crosscheck' or empty, got %r" % (mode,))

    raw_threshold = args.get("crosscheck_threshold")
    if raw_threshold is None:
        raw_threshold = _cfg(cfg, "crosscheck_threshold", DEFAULT_THRESHOLD)
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        raise CrosscheckConfigError("crosscheck_threshold must be a number")
    if not math.isfinite(threshold):
        raise CrosscheckConfigError("crosscheck_threshold must be finite")
    if threshold < 0.0 or threshold > 10.0:
        raise CrosscheckConfigError(
            "crosscheck_threshold must be between 0 and 10")

    max_rounds = _as_int(
        args.get("crosscheck_max_rounds",
                 _cfg(cfg, "crosscheck_max_rounds", DEFAULT_MAX_ROUNDS)),
        DEFAULT_MAX_ROUNDS, 1, 10)
    crews = _as_int(
        args.get("crosscheck_crews",
                 _cfg(cfg, "crosscheck_crews", DEFAULT_CREWS)),
        DEFAULT_CREWS, 2, 8)
    crew_size = _as_int(
        args.get("crosscheck_crew_size",
                 _cfg(cfg, "crosscheck_crew_size", DEFAULT_CREW_SIZE)),
        DEFAULT_CREW_SIZE, 1, 16)
    explicit_subagents = args.get("subagents")
    if explicit_subagents is None:
        subagents = crews * crew_size
    else:
        subagents = _as_int(explicit_subagents, crews * crew_size, 2, 64)
    sizes = split_subagents(subagents, crews)
    audit_review = args.get("audit_review")
    if audit_review is None:
        audit_review = _cfg(cfg, "audit_review", None)
    audit_enabled = True if audit_review is None else _as_bool(audit_review, True)
    return {
        "enabled": True,
        "mode": "crosscheck",
        "threshold": round(threshold, 2),
        "max_rounds": max_rounds,
        "crews": len(sizes),
        "crew_size": crew_size,
        "subagents": subagents,
        "crew_sizes": sizes,
        "min_parts": MIN_PARTS,
        "audit_review": bool(audit_enabled),
        "audit_sample_rate": _as_float(
            args.get("audit_sample_rate",
                     _cfg(cfg, "audit_sample_rate", 1.0)), 1.0),
        "audit_spot_requirements": _as_int(
            args.get("audit_spot_requirements",
                     _cfg(cfg, "audit_spot_requirements", 3)), 3, 0, 20),
        "audit_spot_fixes": _as_int(
            args.get("audit_spot_fixes",
                     _cfg(cfg, "audit_spot_fixes", 2)), 2, 0, 20),
        "audit_forced_jobs": _as_int(
            _cfg(cfg, "audit_forced_jobs", 5), 5, 0, 50),
        "audit_seed": _as_int(_cfg(cfg, "audit_seed", 0), 0, 0, 2 ** 63 - 1),
    }


def split_subagents(total, crews):
    """Split ``total`` agents evenly across ``crews`` (sizes sum to total)."""
    total = max(2, int(total or 2))
    crews = max(2, int(crews or 2))
    # Never inflate the caller's agent budget: a crew with no agent is simply
    # not an active crew for this job.
    crews = min(crews, total)
    base = total // crews
    extra = total % crews
    sizes = [base + (1 if index < extra else 0) for index in range(crews)]
    return sizes


# ---------------------------------------------------------------------------
# brief requirements and PLAN parts


def brief_requirements(prompt):
    """Extract a deterministic checklist from the brief.

    Bullets and numbered lines are requirement points.  A prose-only brief
    falls back to its requirement-shaped sentences, then to the whole brief as
    one point, so the requirement ratio is never vacuous.
    """
    text = str(prompt or "")
    points = []
    seen = set()
    for line in text.splitlines():
        match = _REQUIREMENT_RE.match(line)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip()
        if len(value) < 8:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        points.append(value)
        if len(points) >= 40:
            break
    if not points:
        for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)):
            sentence = sentence.strip()
            if len(sentence) < 12:
                continue
            if not re.search(r"\b(?:must|should|need|ensure|require|acceptance|"
                             r"include|support|add|build|ship|verify)\b",
                             sentence, re.IGNORECASE):
                continue
            points.append(sentence)
            if len(points) >= 20:
                break
    if not points:
        compact = re.sub(r"\s+", " ", text).strip()
        points = [compact[:500] if compact else "complete the brief"]
    return [
        {"id": "R%d" % (index + 1), "text": _truncate(item, 600),
         "source": "brief"}
        for index, item in enumerate(points)
    ]


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def plan_parts(workspace, cfg=None):
    """Read PLAN.json and return validated, disjoint, ordered parts."""
    result = {"status": "absent", "parts": [], "errors": [], "path": PLAN_FILE,
              "sha256": None, "source": "none"}
    path = str(_cfg(cfg, "plan_file", "") or PLAN_FILE)
    full = os.path.join(str(workspace or ""), path)
    try:
        with open(full, "rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError:
        return result
    if len(raw) > MAX_ARTIFACT_BYTES:
        result.update(status="invalid", errors=["PLAN.json is too large"])
        return result
    result["sha256"] = _sha256_bytes(raw)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        result.update(status="invalid", errors=["PLAN.json is not valid JSON"])
        return result
    if not isinstance(data, dict):
        result.update(status="invalid", errors=["PLAN.json must be an object"])
        return result

    raw_parts = data.get("parts") or data.get("slices") or data.get("tasks")
    if not isinstance(raw_parts, list) or not raw_parts:
        # A top-level write_set is a valid plan for a single-part task, but
        # crosscheck requires N >= 2 independent parts.
        top = data.get("write_set") or data.get("writeSet") or []
        if top:
            raw_parts = [{"id": "p1", "title": "entire task", "write_set": top}]
        else:
            result.update(status="invalid",
                          errors=["PLAN.json has no parts with write sets"])
            return result

    parts = []
    for index, item in enumerate(raw_parts):
        if not isinstance(item, dict):
            result["errors"].append("part %d is not an object" % (index + 1))
            continue
        ident = str(item.get("id") or item.get("name") or "p%d" % (index + 1))
        ident = re.sub(r"[^A-Za-z0-9_.-]+", "-", ident).strip("-") or "p%d" % (index + 1)
        writes = (item.get("write_set") or item.get("writes")
                  or item.get("files") or item.get("files_expected") or [])
        if isinstance(writes, str):
            writes = [writes]
        write_set = []
        for entry in writes:
            path_value = _norm_path(entry)
            if path_value:
                write_set.append(path_value)
        depends = item.get("depends_on") or item.get("deps") or []
        if isinstance(depends, str):
            depends = [depends]
        parts.append({
            "id": ident,
            "title": _truncate(item.get("title") or item.get("brief") or ident, 200),
            "brief": _truncate(item.get("brief") or item.get("description") or "", 1000),
            "write_set": sorted(set(write_set)),
            "depends_on": [str(value) for value in depends if str(value).strip()],
            "status": str(item.get("status") or "planned"),
        })

    ids = [part["id"] for part in parts]
    if len(set(ids)) != len(ids):
        result["errors"].append("part ids are not unique")
    for part in parts:
        if not part["write_set"]:
            result["errors"].append("part %s has no write set" % part["id"])
        for dep in part["depends_on"]:
            if dep not in ids:
                result["errors"].append(
                    "part %s depends on unknown part %s" % (part["id"], dep))
            if dep == part["id"]:
                result["errors"].append("part %s depends on itself" % part["id"])
    for left in range(len(parts)):
        for right in range(left + 1, len(parts)):
            if _path_lists_overlap(parts[left]["write_set"],
                                   parts[right]["write_set"]):
                result["errors"].append(
                    "write sets overlap: %s and %s"
                    % (parts[left]["id"], parts[right]["id"]))
    try:
        levels = order_parts(parts)
        order = {ident: level for level, group in enumerate(levels)
                 for ident in group}
        for part in parts:
            part["order"] = order.get(part["id"], 0)
    except CrosscheckPlanError as exc:
        result["errors"].append(str(exc))
    if len(parts) < MIN_PARTS:
        result["errors"].append(
            "crosscheck needs at least %d independent parts" % MIN_PARTS)
    result["parts"] = parts
    result["status"] = "ok" if not result["errors"] else "invalid"
    result["source"] = "plan"
    return result


def order_parts(parts):
    """Topologically order parts; returns dependency levels (stable ids)."""
    by_id = {str(part.get("id")): part for part in parts}
    remaining = set(by_id)
    levels = []
    while remaining:
        ready = sorted(
            ident for ident in remaining
            if not (set(by_id[ident].get("depends_on") or []) & remaining))
        if not ready:
            raise CrosscheckPlanError("PLAN.json part dependencies contain a cycle")
        levels.append(ready)
        remaining.difference_update(ready)
    return levels


def crews_for(n_parts, cc):
    crews = _as_int((cc or {}).get("crews", DEFAULT_CREWS), DEFAULT_CREWS, 2, 8)
    return ["crew-%d" % index for index in range(1, crews + 1)]


def pairing(parts, crew_ids, round_no=1):
    """Assign each part a builder and a *different* checker crew.

    Rotation keeps the check load even and a later round rotates again, while
    the original finder remains eligible to confirm its own findings.
    """
    crew_ids = list(crew_ids or [])
    if len(crew_ids) < 2:
        crew_ids = ["crew-1", "crew-2"]
    count = len(crew_ids)
    result = {}
    for index, part in enumerate(parts or []):
        builder_index = index % count
        checker_index = (builder_index + 1 + ((index + round_no - 1) // count)
                         % (count - 1)) % count
        if checker_index == builder_index:
            checker_index = (checker_index + 1) % count
        result[str(part.get("id"))] = {
            "builder": crew_ids[builder_index],
            "checker": crew_ids[checker_index],
            "round": int(round_no),
        }
    return result


# ---------------------------------------------------------------------------
# connector test evidence


def tests_from_gate(gate_record, smoke_ok=False):
    """Map the deterministic gate's tests check to a crosscheck condition."""
    checks = gate_record.get("checks") if isinstance(gate_record, dict) else None
    if not isinstance(checks, list):
        return {"status": "error", "reason": "no_gate_tests"}
    for check in checks:
        if not isinstance(check, dict) or check.get("name") != "tests":
            continue
        status = str(check.get("status") or "error").lower()
        if status == "ok":
            return {"status": "passed", "source": "gate", "check": check}
        if status == "skipped" and check.get("skipped_reason") == "no_tests_detected":
            if smoke_ok:
                return {"status": "smoke_passed", "source": "smoke", "check": check}
            return {"status": "smoke_failed", "source": "smoke", "check": check}
        return {"status": status, "source": "gate", "check": check}
    return {"status": "error", "reason": "no_gate_tests"}


def run_smoke(workspace, timeout=120):
    """Bounded compile smoke check for a tree with no detected test suite."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(workspace)],
            cwd=str(workspace), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=float(timeout),
        )
        tail = _truncate(proc.stdout or "", 1200)
        return {"ok": proc.returncode == 0, "exit": proc.returncode, "tail": tail,
                "seconds": 0.0}
    except Exception as exc:  # noqa: BLE001 - reported as evidence
        return {"ok": False, "exit": None, "tail": _truncate(exc, 600),
                "seconds": 0.0}


# ---------------------------------------------------------------------------
# scoring


def _severity(item):
    value = str(item.get("severity") or item.get("kind") or "").strip().lower()
    if value.startswith(("block", "crit", "major", "high", "error")):
        return SEVERITY_BLOCKING
    if value.startswith(("minor", "low", "nit", "info", "note", "warn")):
        return SEVERITY_MINOR
    # Unknown/missing severity is a blocking finding: fail closed.
    return SEVERITY_BLOCKING


def _finding_status(item):
    return str(item.get("status") or "open").strip().lower()


def _requirement_checks(item):
    checks = item.get("checks")
    if isinstance(checks, list) and checks:
        return [check for check in checks if isinstance(check, dict)]
    if item.get("checked_by") or item.get("evidence"):
        return [item]
    return []


def _check_ok(item):
    if item.get("passed") is False or item.get("ok") is False:
        return False
    return bool(str(item.get("evidence") or "").strip())


def _crew_set(value):
    """Normalise one crew or a set of crews (rescued parts can have several)."""
    if isinstance(value, (set, frozenset, list, tuple)):
        return {str(item or "").strip() for item in value if str(item or "").strip()}
    text = str(value or "").strip()
    return {text} if text else set()


def _check_crew_ok(check, builder, rescuer, checker=None):
    crew = str(check.get("checked_by") or check.get("crew") or "").strip()
    if not crew:
        return False
    if checker and crew != str(checker):
        return False
    blocked = {str(builder or "").strip()} | _crew_set(rescuer)
    blocked.discard("")
    return crew not in blocked


def _requirement_credit(item, builders, checkers, rescuers):
    """True when a requirement row carries evidence from its paired checker."""
    part_id = str(item.get("part_id") or "")
    if not part_id or part_id not in builders or part_id not in checkers:
        return False
    builder = builders.get(part_id)
    checker = checkers.get(part_id)
    if not builder or not checker:
        return False
    return any(_check_ok(check) and _check_crew_ok(
        check, builder, rescuers.get(part_id), checker)
        for check in _requirement_checks(item))


def evaluate(parts, findings, requirements, tests, rescues=None,
             threshold=None, brief_count=None, brief_items=None):
    """Compute the connector-owned 0-10 crosscheck score.

    Five equally weighted conditions (2.0 each) make 10.0 reachable only when
    the connector tests pass, every brief requirement has evidence from a
    non-building crew, no BLOCKING finding is open, every minor is fixed or
    justified, and every fix is confirmed by the finding crew.
    """
    parts = [part for part in (parts or []) if isinstance(part, dict)]
    findings = [item for item in (findings or []) if isinstance(item, dict)]
    requirements = [item for item in (requirements or []) if isinstance(item, dict)]
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    threshold = max(0.0, min(10.0, threshold))

    builders = {str(part.get("id")): part.get("builder_crew") for part in parts}
    checkers = {str(part.get("id")): part.get("checker_crew") for part in parts}
    rescuers = {str(part.get("id")): set() for part in parts}
    for part in parts:
        rescuers[str(part.get("id"))].update(_crew_set(part.get("rescue_crew")))
    for rescue_row in rescues or []:
        if not isinstance(rescue_row, dict):
            continue
        crew = str(rescue_row.get("rescue_crew") or "").strip()
        if not crew:
            continue
        part_id = str(rescue_row.get("part_id") or "")
        if part_id in rescuers:
            rescuers[part_id].add(crew)
        elif part_id in ("", "all"):
            for crews in rescuers.values():
                crews.add(crew)
    # Fail closed: a part counts only when the build wave (or a successful
    # rescue) actually completed it.  "planned"/"pending"/"stalled"/empty
    # statuses are not built, so an all-green evidence sheet cannot certify a
    # task that was never built.  A missing status is treated as built for
    # legacy records, matching the old contract.
    done_statuses = ("built", "done", "ok", "complete", "completed", "passed")
    part_ok = all(
        str(part.get("status") or "built").strip().lower() in done_statuses
        for part in parts)

    test_status = str((tests or {}).get("status") or "error").lower()
    t_value = 1.0 if test_status in ("passed", "smoke_passed") else 0.0

    required = [item for item in requirements if item.get("required", True) is not False]
    brief_items = [item for item in (brief_items or [])
                   if isinstance(item, dict) and item.get("id")]
    checked = 0
    if brief_items:
        # The connector owns the checklist.  Coverage is measured against its
        # own R1..Rn ids, so invented requirement rows cannot certify a brief.
        by_id = {}
        for item in required:
            by_id.setdefault(str(item.get("id") or ""), []).append(item)
        for brief in brief_items:
            for item in by_id.get(str(brief.get("id")), []):
                if _requirement_credit(item, builders, checkers, rescuers):
                    checked += 1
                    break
        total_required = len(brief_items)
    else:
        for item in required:
            if _requirement_credit(item, builders, checkers, rescuers):
                checked += 1
        # A caller may pass only the brief count; never allow a short artifact
        # to certify a longer brief.
        total_required = max(len(required), int(brief_count or 0))
    r_value = (float(checked) / total_required) if total_required else 0.0

    real = [item for item in findings
            if _finding_status(item) not in ("false_alarm", "duplicate")]
    # Only an explicitly fixed BLOCKING finding is closed.  Unknown/typo
    # statuses, "justified" and "wontfix" stay open (fail closed).
    open_blocking = [item for item in real
                     if _severity(item) == SEVERITY_BLOCKING
                     and _finding_status(item) != "fixed"]
    b_value = 1.0 if not open_blocking else 0.0

    minors = [item for item in real if _severity(item) == SEVERITY_MINOR]
    resolved_minors = 0
    for item in minors:
        status = _finding_status(item)
        if status in ("fixed", "justified"):
            if status == "justified":
                just = item.get("justification")
                text = (just.get("text") if isinstance(just, dict) else just)
                accepted = (just.get("accepted_by_crew")
                            if isinstance(just, dict) else item.get("accepted_by_crew"))
                finder = str(item.get("found_by_crew") or item.get("found_by") or "")
                checker = checkers.get(str(item.get("part_id") or ""))
                # The finding crew (or the part's paired checker) must accept
                # the justification; a builder-only self-acceptance is not
                # enough, as documented.
                if (len(str(text or "").strip()) < 20 or not accepted
                        or str(accepted) not in {finder, str(checker or "")}):
                    continue
            resolved_minors += 1
    m_value = (float(resolved_minors) / len(minors)) if minors else 1.0

    fixed = [item for item in real if _finding_status(item) == "fixed"]
    confirmed = 0
    for item in fixed:
        confirmation = item.get("confirmation")
        if not isinstance(confirmation, dict):
            continue
        part_id = str(item.get("part_id") or "")
        checker = checkers.get(part_id) if part_id else None
        finder = str(item.get("found_by_crew") or item.get("found_by") or "")
        crew = str(confirmation.get("crew") or confirmation.get("confirmed_by") or "")
        if not checker:
            # Cross-cutting finding: the finder must at least be a real checker
            # crew, never a builder certifying its own work.
            if not finder or finder not in set(checkers.values()):
                continue
        elif finder != str(checker):
            continue
        if (confirmation.get("confirmed") is True and crew
                and crew == finder
                and str(confirmation.get("evidence") or "").strip()):
            confirmed += 1
    f_value = (float(confirmed) / len(fixed)) if fixed else 1.0

    raw = 2.0 * (t_value + r_value + b_value + m_value + f_value)
    all_full = (t_value == r_value == b_value == m_value == f_value == 1.0
                and part_ok)
    if all_full:
        score = 10.0
    else:
        score = min(round(raw + 1e-9, 2), 9.99)
        if not part_ok:
            score = min(score, 6.0)
    rescue_rows = [item for item in (rescues or [])
                   if isinstance(item, dict)
                   and str(item.get("status") or "").lower() != "ok"]
    if rescue_rows:
        # An attempted rescue that did not succeed caps the gate even when the
        # remaining evidence happens to be green.
        score = min(score, 8.0)
    reasons = []
    if t_value < 1.0:
        reasons.append("tests_" + test_status)
    if r_value < 1.0:
        reasons.append("requirements_unchecked")
    if b_value < 1.0:
        reasons.append("blocking_open")
    if m_value < 1.0:
        reasons.append("minors_unresolved")
    if f_value < 1.0:
        reasons.append("fixes_unconfirmed")
    if not part_ok:
        reasons.append("part_failed")
    if rescue_rows:
        reasons.append("rescue_failed")
    score = round(max(0.0, min(10.0, score)) + 1e-9, 2)
    return {
        "score": score,
        "threshold": threshold,
        "passed": bool(score >= threshold),
        "conditions": {
            "tests": round(t_value, 4),
            "requirements": round(r_value, 4),
            "blocking": round(b_value, 4),
            "minors": round(m_value, 4),
            "fixes": round(f_value, 4),
        },
        "weights": {"tests": 2.0, "requirements": 2.0, "blocking": 2.0,
                    "minors": 2.0, "fixes": 2.0},
        "counts": {
            "requirements_total": total_required,
            "requirements_checked": checked,
            "blocking_open": len(open_blocking),
            "minors_total": len(minors),
            "minors_resolved": resolved_minors,
            "fixes_total": len(fixed),
            "fixes_confirmed": confirmed,
        },
        "reasons": reasons,
        "parts_ok": part_ok,
        "formula": "score = 2*(tests + requirements + blocking + minors + fixes); "
                   "10.0 only when every fraction is 1.0",
    }


def summarise_findings(findings):
    """Raw/unique/real/false-alarm and breaker/spec counts for the record."""
    rows = [item for item in (findings or []) if isinstance(item, dict)]
    seen = set()
    unique = []
    for item in rows:
        ident = str(item.get("id") or "").strip()
        if not ident:
            ident = hashlib.sha256(
                (str(item.get("part_id")) + "\0" + str(item.get("text")))
                .encode("utf-8", "replace")).hexdigest()[:10]
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(item)
    false_alarms = [item for item in unique
                    if _finding_status(item) == "false_alarm"]
    real = [item for item in unique if item not in false_alarms]
    lens = [str(item.get("lens") or item.get("checker") or "").lower()
            for item in unique]
    return {
        "raw": len(rows),
        "unique": len(unique),
        "real": len(real),
        "false_alarm": len(false_alarms),
        "breaker": sum(1 for value in lens if "break" in value),
        "spec": sum(1 for value in lens if "spec" in value),
        "blocking": sum(1 for item in unique
                        if _severity(item) == SEVERITY_BLOCKING),
        "minor": sum(1 for item in unique
                     if _severity(item) == SEVERITY_MINOR),
    }


# ---------------------------------------------------------------------------
# artifact parsing and records


def load_artifact(workspace):
    """Read CROSSCHECK.json defensively; never raises."""
    path = os.path.join(str(workspace or ""), CROSSCHECK_FILE)
    result = {"status": "absent", "data": {}, "path": CROSSCHECK_FILE,
              "error": None}
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError:
        return result
    if len(raw) > MAX_ARTIFACT_BYTES:
        result.update(status="invalid", error="artifact too large")
        return result
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError) as exc:
        result.update(status="invalid", error=_truncate(exc, 300))
        return result
    if not isinstance(data, dict):
        result.update(status="invalid", error="artifact must be an object")
        return result
    result.update(status="ok", data=data)
    return result


def new_record(cc, run_id=None, plan_sha=None):
    return {
        "version": VERSION,
        "mode": "crosscheck",
        "run_id": run_id,
        "plan_sha256": plan_sha,
        "status": "in_progress",
        "threshold": (cc or {}).get("threshold", DEFAULT_THRESHOLD),
        "max_rounds": (cc or {}).get("max_rounds", DEFAULT_MAX_ROUNDS),
        "started_at": time.time(),
        "score": None,
        "parts": [],
        "crews": [],
        "pairing": {},
        "findings": [],
        "requirements": [],
        "rounds": [],
        "rescues": [],
        "tests": {},
        "summary": {},
        "error": None,
    }


def merge_artifact(record, artifact):
    """Merge a validated CROSSCHECK.json into the connector record."""
    record = record or new_record({})
    if not isinstance(artifact, dict):
        return record
    for key in ("parts", "crews", "findings", "requirements", "rounds",
                "rescues"):
        value = artifact.get(key)
        if isinstance(value, list):
            record[key] = [item for item in value if isinstance(item, dict)]
        elif isinstance(value, dict) and key in ("parts", "crews"):
            record[key] = [item for item in value.values()
                           if isinstance(item, dict)]
    if isinstance(artifact.get("tests"), dict):
        record["tests"] = artifact["tests"]
    if isinstance(artifact.get("pairing"), dict):
        record["pairing"] = artifact["pairing"]
    for key in ("status", "status_detail", "error"):
        if artifact.get(key):
            record[key] = _truncate(artifact.get(key), 1000)
    return record


def record_round(record, evaluation, execution=None, round_no=1, phase="crosscheck"):
    """Append one connector-observed round with time/token totals."""
    row = {
        "round": int(round_no),
        "phase": phase,
        "score": evaluation.get("score"),
        "conditions": evaluation.get("conditions"),
        "reasons": evaluation.get("reasons"),
        "seconds": None,
        "cost": None,
        "tokens_in": None,
        "cache_read_tokens": None,
        "tokens_out": None,
        "total_tokens": None,
    }
    if isinstance(execution, dict):
        row.update({
            "seconds": execution.get("seconds"),
            "cost": execution.get("estimated_cost_usd"),
            "tokens_in": execution.get("tokens_in"),
            "cache_read_tokens": execution.get("cache_read_tokens"),
            "tokens_out": execution.get("tokens_out"),
            "total_tokens": execution.get("total_tokens"),
        })
    record.setdefault("rounds", []).append(row)
    record["score"] = evaluation.get("score")
    record["summary"] = summarise_findings(record.get("findings"))
    return record


def apply_rescue(record, rescue, rescued=False):
    """Record the finisher/rescue against the parts it actually completed.

    A rescue is ``ok`` only when the finisher completed the task *and* the
    supplied status is ``ok``; otherwise nothing is marked rescued and the
    evaluator caps the score.  The production rescue dict carries no failed
    crew, so unfinished parts are identified by their status and parts that
    are already built stay untouched.
    """
    if not rescued and not rescue:
        return record
    record = record or new_record({})
    data = rescue if isinstance(rescue, dict) else {}
    raw_reason = data.get("reason")
    if not raw_reason and not isinstance(rescue, dict):
        raw_reason = rescue
    reason = _truncate(raw_reason or data.get("status") or "", 300)
    failed_crew = str(data.get("failed_crew") or "").strip() or None
    rescue_crew = str(data.get("rescue_crew") or "").strip() or "finisher"
    explicit = str(data.get("part_id") or "").strip()
    status = str(data.get("status") or "").strip().lower()
    if not status:
        status = "ok" if rescued else "failed"
    ok = bool(rescued) and status == "ok"
    parts = [part for part in (record.get("parts") or [])
             if isinstance(part, dict)]
    if explicit:
        # An explicit target is authoritative: never widen it to other parts.
        targets = [part for part in parts
                   if str(part.get("id") or "") == explicit]
    elif failed_crew:
        targets = [part for part in parts
                   if str(part.get("builder_crew") or "") == failed_crew]
    else:
        targets = []
    if not targets and not explicit:
        targets = [
            part for part in parts
            if str(part.get("status") or "").strip().lower()
            in ("stalled", "failed", "blocked", "error", "planned",
                "pending", "queued")
            and not part.get("rescued")
        ]
    rows = record.setdefault("rescues", [])
    if not isinstance(rows, list):
        rows = []
        record["rescues"] = rows
    default_reason = reason or "finisher completed a stalled/timed-out build"
    pairing = record.get("pairing") or {}
    if targets:
        for part in targets:
            part_id = str(part.get("id") or "")
            if ok:
                part["rescued"] = True
                part["rescue_crew"] = rescue_crew
                # The finisher completed this part; a stale failed/blocked
                # status must not keep the part_ok cap at 6.0.
                if str(part.get("status") or "").strip().lower() in (
                        "failed", "blocked", "error", "stalled", "planned",
                        "pending", "queued"):
                    part["status"] = "built"
            rows.append({
                "part_id": part_id,
                "scope": "part",
                "reason": default_reason,
                "failed_crew": failed_crew,
                "rescue_crew": rescue_crew,
                "checker_crew": (pairing.get(part_id, {}).get("checker")
                                 or part.get("checker_crew")),
                "status": status,
            })
    else:
        # No part could be identified.  A successful rescue completed the
        # whole task, so it covers every part; a failed one covers none.
        rows.append({
            "part_id": "all",
            "scope": "all",
            "reason": default_reason,
            "failed_crew": failed_crew,
            "rescue_crew": rescue_crew,
            "checker_crew": None,
            "status": status,
        })
        if ok:
            for part in parts:
                part["rescued"] = True
                part["rescue_crew"] = rescue_crew
                if str(part.get("status") or "").strip().lower() in (
                        "failed", "blocked", "error", "stalled", "planned",
                        "pending", "queued"):
                    part["status"] = "built"
    # De-duplicate part/round pairs.
    seen = set()
    unique = []
    for row in rows:
        key = (row.get("part_id"), row.get("round"), row.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    record["rescues"] = unique
    return record


def evidence_text(record, limit=12000):
    """Bounded crew evidence handed to the audit reviewer."""
    record = record or {}
    bits = ["Crosscheck score: %s/10 (threshold %s)"
            % (record.get("score"), record.get("threshold"))]
    tests = record.get("tests") or {}
    bits.append("Connector tests: %s" % (tests.get("status") or "unknown"))
    for part in record.get("parts") or []:
        bits.append("part %s [%s] built by %s, checked by %s, write set: %s"
                    % (part.get("id"), part.get("status"),
                       part.get("builder_crew"), part.get("checker_crew"),
                       ", ".join(part.get("write_set") or [])))
    for req in record.get("requirements") or []:
        bits.append("requirement %s checked_by=%s evidence=%s"
                    % (req.get("id"), req.get("checked_by"),
                       _truncate(req.get("evidence"), 300)))
    for item in record.get("findings") or []:
        bits.append("finding %s [%s/%s] %s:%s status=%s found_by=%s evidence=%s"
                    % (item.get("id"), _severity(item), item.get("lens"),
                       item.get("file"), item.get("line"), _finding_status(item),
                       item.get("found_by_crew"),
                       _truncate(item.get("evidence"), 400)))
    for rescue in record.get("rescues") or []:
        bits.append("rescue part=%s status=%s reason=%s"
                    % (rescue.get("part_id"), rescue.get("status"),
                       rescue.get("reason")))
    return _truncate("\n".join(bits), limit)


def save_artifact(workspace, record):
    """Atomically write CROSSCHECK.json; returns the path or ``None``."""
    if not workspace:
        return None
    path = os.path.join(str(workspace), CROSSCHECK_FILE)
    data = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# prompt contracts


def build_contract(cc, parts_info, pairing_map, prompt):
    """Crew/part contract appended to the builder task."""
    cc = cc or {}
    parts = parts_info.get("parts") or []
    levels = order_parts(parts) if parts else []
    part_count = len(parts) if len(parts) >= MIN_PARTS else MIN_PARTS
    lines = [
        "## Crosscheck mode (mandatory, connector-scored)",
        "Build this brief as at least %d independent parts with DISJOINT write "
        "sets. The connector will recompute the score from objective evidence; "
        "your self-score and QUALITY_REPORT prose are not used." % part_count,
        "Crews: %d active (%d agents total, split %s; configured %d x %d)."
        % (len(cc.get("crew_sizes") or []) or cc.get("crews", DEFAULT_CREWS),
           cc.get("subagents", 8), cc.get("crew_sizes"),
           cc.get("crews", DEFAULT_CREWS),
           cc.get("crew_size", DEFAULT_CREW_SIZE)),
        "",
        "### PLAN.json (write this first)",
        "Extend PLAN.json with a top-level `parts` list. Each part needs a "
        "stable `id`, a `brief`, `write_set` (repo-relative files/dirs it may "
        "write) and optional `depends_on`. The top-level `write_set` remains "
        "the union. Parts must not share any path; dependent parts run after "
        "their dependency.",
        "Dependency levels: %s" % (levels or "[]"),
        "",
        "### Assigned crews",
    ]
    for part in parts:
        pair = pairing_map.get(str(part.get("id")), {})
        lines.append(
            "- %s: build with %s; cross-check with %s (never its builder)"
            % (part.get("id"), pair.get("builder"), pair.get("checker")))
    if not parts:
        crews = max(2, int(cc.get("crews") or DEFAULT_CREWS))
        lines.append(
            "- With C=%d crews, part index i (0-based) uses builder "
            "crew-(i mod C + 1) and checker crew-((b + 1 + floor(i / C) "
            "mod (C - 1)) mod C + 1), where b = i mod C. Never let a crew "
            "check its own part." % crews)
    lines += [
        "",
        "### Wave 1 - build",
        "All builder crews build their parts at once (respect the Harness "
        "child limit). Each builder writes only its part's files. If a crew "
        "stalls, stops early, loses balance or the connector's finisher takes "
        "over, mark the part `rescued` and have the finisher complete it; a "
        "different crew must still check it.",
        "",
        "### Wave 2 - cross-check",
        "Each crew reviews ONLY parts it did not build. Half of each crew are "
        "BREAKERS: attack edge cases, wrong inputs, error paths, failing "
        "tests. Half are SPEC CHECKERS: every brief requirement point by "
        "point. Every finding must be tagged BLOCKING or minor, with "
        "`file:line` and evidence (the exact command and its output). The "
        "builder crew fixes; the finding crew re-runs the evidence and "
        "confirms each fix. Repeat up to %d rounds."
        % cc.get("max_rounds", DEFAULT_MAX_ROUNDS),
        "",
        "### Connector brief checklist (use these exact requirement ids)",
    ]
    for item in brief_requirements(prompt):
        lines.append("- %s: %s" % (item.get("id"), item.get("text")))
    lines += [
        "Every point must appear in CROSSCHECK.json with this exact `id`, a "
        "real `part_id`, evidence, and the part's assigned checker as "
        "`checked_by`. The connector measures coverage against this list, not "
        "against ids your crew invents.",
        "",
        "### CROSSCHECK.json (required)",
        "Write/update `CROSSCHECK.json` at the workspace root after each "
        "round. Minimum shape:",
        '{"version":1,"parts":[{"id":"p1","title":"...","write_set":["a.py"],'
        '"depends_on":[],"status":"built","builder_crew":"crew-1",'
        '"checker_crew":"crew-2","rescued":false}],'
        '"findings":[{"id":"F1","part_id":"p1","severity":"blocking",'
        '"lens":"breaker|spec","text":"...","file":"a.py","line":12,'
        '"evidence":"command: ...\\noutput: ...","found_by_crew":"crew-2",'
        '"status":"open|fixed|justified|false_alarm",'
        '"fix":{"crew":"crew-1","round":2,"evidence":"..."},'
        '"confirmation":{"crew":"crew-2","confirmed":true,"evidence":"..."},'
        '"justification":{"text":"...","accepted_by_crew":"crew-2"}}],'
        '"requirements":[{"id":"R1","text":"...","part_id":"p1",'
        '"required":true,"checked_by":"crew-2","evidence":"command/output",'
        '"passed":true}],'
        '"rounds":[{"round":1,"score":10.0}],"rescues":[]}',
        "",
        "The connector runs the test suite itself. Do not claim tests passed "
        "without running them. Write the JSON even when a part fails; the "
        "connector will ask for one focused fix round if the gate is below "
        "%.1f/10." % cc.get("threshold", DEFAULT_THRESHOLD),
    ]
    return "\n".join(lines)


def build_fix_task(cc, record, evaluation, round_no, prompt):
    """Focused revision task for one crosscheck round below threshold."""
    parts = record.get("parts") or []
    open_items = []
    for item in record.get("findings") or []:
        if (_severity(item) == SEVERITY_BLOCKING
                and _finding_status(item) != "fixed"):
            open_items.append(
                "- %s %s %s:%s: %s (evidence: %s)"
                % (item.get("id"), _severity(item).upper(), item.get("file"),
                   item.get("line"), item.get("text"),
                   _truncate(item.get("evidence"), 500)))
    unchecked = [
        "- %s: %s (part %s)" % (item.get("id"), item.get("text"),
                                item.get("part_id"))
        for item in record.get("requirements") or []
        if not str(item.get("checked_by") or "").strip()
    ]
    unconfirmed = []
    for item in record.get("findings") or []:
        if _finding_status(item) != "fixed":
            continue
        if (item.get("confirmation") or {}).get("confirmed"):
            continue
        part_id = str(item.get("part_id") or "")
        checker = ((record.get("pairing") or {}).get(part_id) or {}).get("checker")
        unconfirmed.append(
            "- %s: fix needs confirmation from the paired checker %s"
            % (item.get("id"), checker or "that did not build the part"))
    unresolved = [
        "- %s: %s" % (item.get("id"), item.get("text"))
        for item in record.get("findings") or []
        if _severity(item) == SEVERITY_MINOR
        and _finding_status(item) not in ("fixed", "justified", "false_alarm")
    ]
    if not (open_items or unchecked or unconfirmed or unresolved):
        open_items = ["- raise the crosscheck score to %.2f: %s"
                      % (evaluation.get("threshold", DEFAULT_THRESHOLD),
                         ", ".join(evaluation.get("reasons") or ["evidence missing"]))]
    lines = [
        "Crosscheck gate round %d did not reach %.2f/10 (score %.2f)."
        % (round_no, evaluation.get("threshold", DEFAULT_THRESHOLD),
           evaluation.get("score", 0.0)),
        "This is a focused fix round. Keep the existing parts, crews and "
        "write sets; do not widen a part's write set. Fix the items below, "
        "re-run the evidence commands, and update CROSSCHECK.json with fixed "
        "status, evidence and confirmations.",
        "",
        "## Fix list",
    ]
    lines += (open_items + unchecked + unconfirmed + unresolved)[:40]
    lines += [
        "",
        "Do not stop at a plan. Change the files, run the tests, then write "
        "the updated CROSSCHECK.json and return.",
    ]
    return "\n".join(lines)


def apply_status(record, evaluation):
    record = record or new_record({})
    record["score"] = evaluation.get("score")
    record["conditions"] = evaluation.get("conditions")
    record["summary"] = summarise_findings(record.get("findings"))
    if evaluation.get("passed"):
        record["status"] = STATUS_PASSED
        record["status_detail"] = "score %.2f met threshold %.2f" % (
            evaluation.get("score", 0.0), evaluation.get("threshold", 10.0))
    else:
        record["status"] = STATUS_BELOW
        record["status_detail"] = (
            "score %.2f below threshold %.2f after %d round(s): %s"
            % (evaluation.get("score", 0.0),
               evaluation.get("threshold", 10.0),
               len(record.get("rounds") or []),
               ", ".join(evaluation.get("reasons") or ["not verified"])))
    return record


def loop_decision(round_no, evaluation, cc):
    """Return ``pass``, ``fix`` or ``stop`` for one crosscheck round."""
    if evaluation.get("passed"):
        return "pass"
    try:
        maximum = max(1, int((cc or {}).get("max_rounds") or DEFAULT_MAX_ROUNDS))
    except (TypeError, ValueError):
        maximum = DEFAULT_MAX_ROUNDS
    if int(round_no) >= maximum:
        return "stop"
    return "fix"


def status_mapping(record):
    """Return ``(status, detail)`` for the harness terminal status."""
    if (record or {}).get("status") == STATUS_PASSED:
        return "ok", (record or {}).get("status_detail") or "crosscheck passed"
    return STATUS_BELOW, (record or {}).get("status_detail") or (
        "crosscheck score did not reach the configured threshold")
