"""One-shot audit of a passing crew gate (3.14.2).

The audit is not a second reviewer loop.  It augments the job's normally
configured reviewer task with a diff-first, evidence-driven spot-check block,
then parses the reviewer's ``AUDIT_JSON`` (or headings) into a bounded record.

Risk detection never *samples the audit out*: it only records
``risky: yes/no + why`` and forces the effective rate to 1.0.  A finding that a
crew claim did not hold forces full audits for the next five crosscheck jobs,
persisted under ``cfg.state_dir``.  Standard library only, Python 3.9,
Windows-safe.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import random
import re
import secrets
import tempfile
import time

try:
    from .api import redact_secrets
except Exception:  # pragma: no cover

    def redact_secrets(text):
        return str(text or "")


DEFAULT_SAMPLE_RATE = 1.0
DEFAULT_SPOT_REQUIREMENTS = 3
DEFAULT_SPOT_FIXES = 2
DEFAULT_FORCED_JOBS = 5
STATE_VERSION = 1
MAX_STATE_BYTES = 512 * 1024
MAX_REPORT_CHARS = 200000
MAX_EVIDENCE_CHARS = 12000
MAX_SPOTS = 20

DEFAULT_RISKY_GLOBS = (
    "deepseek_mcp/sandbox_*.py",
    "**/sandbox_*.py",
    "**/redact*.py",
    "**/*redact*.py",
    "**/secret*.py",
    "**/*secret*.py",
    "**/credential*.py",
    "**/*credential*.py",
    "deepseek_mcp/jobs.py",
    "**/restart_helper.py",
    "**/loop_guard*.py",
    "**/*resume*.py",
    "**/*cancel*.py",
    "**/*restart*.py",
    "**/install*.sh",
    "**/install*.ps1",
    "**/install*.bat",
    "**/install*.cmd",
    "**/install*.py",
    "**/update*.sh",
    "**/update*.ps1",
    "**/update*.bat",
    "**/update*.cmd",
    "**/update*.py",
    ".github/workflows/*",
)

_RISK_WORDS_RE = re.compile(
    r"\b(?:security|sandbox(?:es)?|passwords?|credentials?|"
    r"delet(?:e|es|ed|ion)|permissions?)\b", re.IGNORECASE)
_DIFF_PATH_RE = re.compile(r"^(?:\+\+\+|---) [ab]/(.+?)(?:\t|$)", re.MULTILINE)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_AUDIT_JSON_RE = re.compile(r"AUDIT_JSON\s*(\{.*?\})\s*-->", re.DOTALL)
_SCORE_RE = re.compile(r"^\s*[-*]?\s*Audit score:\s*([0-9]+(?:\.[0-9]+)?)",
                       re.IGNORECASE | re.MULTILINE)
_BLOCKING_RE = re.compile(r"^\s*[-*]?\s*Audit blocking:\s*(\d+)",
                          re.IGNORECASE | re.MULTILINE)
_SPOT_RE = re.compile(r"^\s*[-*]\s*([A-Za-z0-9_.-]+):\s*(pass|fail)\s*[—:-]?\s*(.*)$",
                      re.IGNORECASE | re.MULTILINE)


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


def _bool(value, default=False):
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
    if not math.isfinite(result):
        return float(default)
    return result


def _int(value, default, low, high):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    return max(low, min(high, result))


def _truncate(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _norm_path(value):
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        return ""
    text = re.sub(r"/+", "/", text)
    if text.startswith("../") or text == "..":
        return ""
    return text


# ---------------------------------------------------------------------------
# risk detection


def detect_risk(brief, paths, cfg=None):
    """Return ``{risky, reasons, globs, words}`` for a job."""
    globs = list(DEFAULT_RISKY_GLOBS)
    extra = _cfg(cfg, "audit_risk_globs", None) or _cfg(cfg, "risk_paths", None) or []
    if isinstance(extra, str):
        extra = [extra]
    for pattern in extra:
        value = str(pattern or "").strip()
        if value and value not in globs:
            globs.append(value)
    reasons = []
    seen = set()
    for raw in paths or []:
        path = _norm_path(raw)
        if not path:
            continue
        candidates = (path, path.rsplit("/", 1)[-1])
        for pattern in globs:
            patterns = [pattern]
            if pattern.startswith("**/"):
                patterns.append(pattern[3:])
            for candidate in candidates:
                if any(fnmatch.fnmatchcase(candidate, item)
                       for item in patterns):
                    key = ("path", path, pattern)
                    if key not in seen:
                        seen.add(key)
                        reasons.append({"kind": "path", "match": path,
                                        "pattern": pattern})
                    break
    words = []
    for match in _RISK_WORDS_RE.finditer(str(brief or "")):
        word = match.group(0).lower()
        if word not in words:
            words.append(word)
    for word in words:
        key = ("brief", word, "brief")
        if key not in seen:
            seen.add(key)
            reasons.append({"kind": "brief", "match": word, "pattern": "brief"})
    return {
        "risky": bool(reasons),
        "reasons": reasons[:20],
        "reason_count": len(reasons),
        "globs": globs,
        "words": words,
    }


def extract_diff_paths(diff_text, limit=500):
    """Extract repo-relative paths from a unified diff."""
    found = []
    seen = set()
    text = str(diff_text or "")[:MAX_REPORT_CHARS]
    for match in _DIFF_GIT_RE.finditer(text):
        for value in match.groups():
            path = _norm_path(value)
            if path and path not in seen and path != "/dev/null":
                seen.add(path)
                found.append(path)
    for match in _DIFF_PATH_RE.finditer(text):
        path = _norm_path(match.group(1))
        if path and path not in seen and path != "/dev/null":
            seen.add(path)
            found.append(path)
    return found[:max(0, int(limit or 500))]


# ---------------------------------------------------------------------------
# persistent sampling state


def _state_path(cfg, workspace):
    state_dir = str(_cfg(cfg, "state_dir", "") or "").strip()
    if not state_dir:
        state_dir = os.path.join(str(workspace or "."), ".dsh-state")
    return os.path.join(state_dir, "audit_state.json")


def _fresh_state():
    return {
        "version": STATE_VERSION,
        "forced_remaining": 0,
        "forced_job_ids": [],
        "forced_started_by": None,
        "forced_set_at": None,
        "last_job_id": None,
        "last_decision": None,
        "recent": [],
    }


def load_state(cfg, workspace):
    path = _state_path(cfg, workspace)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except OSError:
        return _fresh_state(), None
    if len(raw) > MAX_STATE_BYTES:
        return _fresh_state(), "state_too_large"
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return _fresh_state(), "corrupt_state"
    if not isinstance(data, dict):
        return _fresh_state(), "corrupt_state"
    state = _fresh_state()
    state.update({key: data.get(key, state[key]) for key in state})
    try:
        state["forced_remaining"] = max(0, int(state.get("forced_remaining") or 0))
    except (TypeError, ValueError):
        state["forced_remaining"] = 0
    if not isinstance(state.get("forced_job_ids"), list):
        state["forced_job_ids"] = []
    if not isinstance(state.get("recent"), list):
        state["recent"] = []
    return state, None


def save_state(cfg, workspace, state):
    path = _state_path(cfg, workspace)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".audit-state-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True, None
    except OSError as exc:
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError):
            pass
        return False, _truncate(exc, 300)


def record_claim_failure(cfg, job_id, count=None, workspace=None):
    """Force full audits for the next ``count`` crosscheck jobs."""
    count = _int(count if count is not None else _cfg(cfg, "audit_forced_jobs",
                                                       DEFAULT_FORCED_JOBS),
                 DEFAULT_FORCED_JOBS, 1, 50)
    state, error = load_state(cfg, workspace)
    state["forced_remaining"] = max(int(state.get("forced_remaining") or 0), count)
    state["forced_started_by"] = str(job_id or "")
    state["forced_set_at"] = time.time()
    ids = list(state.get("forced_job_ids") or [])
    if job_id and job_id not in ids:
        ids.append(str(job_id))
    state["forced_job_ids"] = ids[-50:]
    state["recent"] = (list(state.get("recent") or []) + [{
        "job_id": str(job_id or ""),
        "event": "crew_claim_failed",
        "forced_remaining": state["forced_remaining"],
        "at": time.time(),
    }])[-20:]
    ok, save_error = save_state(cfg, workspace, state)
    return {"forced_remaining": state["forced_remaining"],
            "state_written": bool(ok),
            "state_error": error or save_error}


# ---------------------------------------------------------------------------
# sampling and spot checks


def _spot_seed(seed):
    return int.from_bytes(
        hashlib.sha256(("%d:spot" % int(seed)).encode("ascii")).digest()[:8], "big")


def _spot_key(item):
    ident = str(item.get("id") or "")
    parts = re.split(r"(\d+)", ident)
    return (tuple((0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts),
            re.sub(r"\s+", " ", str(item.get("text") or "")).strip().lower())


def select_spots(requirements, fixes, seed, n_req=3, n_fix=2):
    """Deterministically sample requirements and confirmed fixes."""
    reqs = []
    seen = set()
    for item in requirements or []:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        ident = str(item.get("id"))
        if ident in seen:
            continue
        seen.add(ident)
        reqs.append(item)
    fix_rows = []
    seen = set()
    for item in fixes or []:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        ident = str(item.get("id"))
        if ident in seen:
            continue
        seen.add(ident)
        fix_rows.append(item)
    reqs.sort(key=_spot_key)
    fix_rows.sort(key=_spot_key)
    spot_seed = _spot_seed(seed)
    rng = random.Random(spot_seed)
    n_req = _int(n_req, DEFAULT_SPOT_REQUIREMENTS, 0, MAX_SPOTS)
    n_fix = _int(n_fix, DEFAULT_SPOT_FIXES, 0, MAX_SPOTS)
    picked_req = rng.sample(reqs, min(n_req, len(reqs))) if n_req else []
    picked_fix = rng.sample(fix_rows, min(n_fix, len(fix_rows))) if n_fix else []
    return {
        "seed": spot_seed,
        "requirements": sorted(picked_req, key=_spot_key),
        "fixes": sorted(picked_fix, key=_spot_key),
        "pool_sizes": {"requirements": len(reqs), "fixes": len(fix_rows)},
        "coverage_gap": len(reqs) < n_req or len(fix_rows) < n_fix,
    }


def _decide(rate, risky, forced_remaining, crosscheck, seed):
    if risky:
        return {"sampled": True, "rate_source": "risk",
                "reason": "risky_path_or_brief"}
    if crosscheck and forced_remaining > 0:
        return {"sampled": True, "rate_source": "forced_claim_failure",
                "reason": "forced_after_failed_crew_claim"}
    rate = max(0.0, min(1.0, _float(rate, DEFAULT_SAMPLE_RATE)))
    if rate >= 1.0:
        sampled = True
    elif rate <= 0.0:
        sampled = False
    else:
        sampled = random.Random(seed).random() < rate
    return {"sampled": bool(sampled), "rate_source": "sample_rate",
            "reason": "seeded_draw" if 0.0 < rate < 1.0 else "rate_bound"}


def prepare(cfg, *, run, workspace, brief, changed_paths,
            requirements, fixes, crosscheck_mode, gate_passed,
            audit_review=None, sample_rate=None, seed=None,
            spot_requirements=None, spot_fixes=None):
    """Build the audit plan and persist the sampling/forced-rate state."""
    run = run or {}
    job_id = str(run.get("id") or "")
    disabled = {
        "enabled": False, "ran": False, "sampled": False,
        "crosscheck_mode": bool(crosscheck_mode),
        "risky": False, "risk_reasons": [], "risk_why": [],
        "spot": {"requirements": [], "fixes": [], "seed": None},
        "record": {"ran": False, "sampled": False},
    }
    if not crosscheck_mode:
        disabled["reason"] = "not_crosscheck"
        return disabled
    if not gate_passed:
        disabled["reason"] = "gate_not_passed"
        return disabled
    if audit_review is None:
        audit_review = _cfg(cfg, "audit_review", None)
    if audit_review is not None and not _bool(audit_review, True):
        disabled["reason"] = "audit_review_false"
        return disabled

    risk = detect_risk(brief, changed_paths, cfg)
    state, state_error = load_state(cfg, workspace)
    forced_before = int(state.get("forced_remaining") or 0)
    forced_used = False
    raw_seed = seed
    if raw_seed is None:
        raw_seed = _cfg(cfg, "audit_seed", 0)
    try:
        seed_value = int(raw_seed or 0)
    except (TypeError, ValueError):
        seed_value = 0
    if seed_value == 0:
        seed_value = secrets.randbits(63)

    rate = sample_rate
    if rate is None:
        rate = _cfg(cfg, "audit_sample_rate", DEFAULT_SAMPLE_RATE)
    decision = _decide(rate, risk["risky"], forced_before,
                       bool(crosscheck_mode), seed_value)
    if state_error:
        decision = {"sampled": True, "rate_source": "state_failure",
                    "reason": state_error}
    if forced_before > 0 and not risk["risky"]:
        forced_used = True
        state["forced_remaining"] = max(0, forced_before - 1)
    state["last_job_id"] = job_id
    state["last_decision"] = {
        "job_id": job_id,
        "sampled": bool(decision["sampled"]),
        "rate_source": decision["rate_source"],
        "forced_remaining_before": forced_before,
        "forced_remaining_after": int(state.get("forced_remaining") or 0),
        "seed": seed_value,
        "at": time.time(),
    }
    ok, save_error = save_state(cfg, workspace, state)
    spot = select_spots(
        requirements, fixes, seed_value,
        (spot_requirements if spot_requirements is not None else
         _cfg(cfg, "audit_spot_requirements", DEFAULT_SPOT_REQUIREMENTS)),
        (spot_fixes if spot_fixes is not None else
         _cfg(cfg, "audit_spot_fixes", DEFAULT_SPOT_FIXES)))
    record = {
        "ran": False,
        "job_id": job_id,
        "enabled": True,
        "sampled": bool(decision["sampled"]),
        "reason": decision["reason"],
        "rate_source": decision["rate_source"],
        "sample_rate": max(0.0, min(1.0, _float(rate, DEFAULT_SAMPLE_RATE))),
        "effective_rate": 1.0 if decision["sampled"] and (
            decision["rate_source"] in ("risk", "forced_claim_failure",
                                        "state_failure")) else max(
            0.0, min(1.0, _float(rate, DEFAULT_SAMPLE_RATE))),
        "seed": seed_value,
        "spot_seed": spot["seed"],
        "risky": bool(risk["risky"]),
        "risk_reasons": risk["reasons"],
        "risk_why": [("%s:%s" % (item.get("kind"), item.get("match")))
                     for item in risk["reasons"]],
        "forced_remaining_before": forced_before,
        "forced_remaining_after": int(state.get("forced_remaining") or 0),
        "forced_consumed": bool(forced_used),
        "state_written": bool(ok),
        "state_error": state_error or save_error,
        "spot": spot,
        "blocking": [],
        "blocking_count": 0,
        "claims_failed": [],
        "spot_checks": [],
        "score": None,
        "verdict": "pending" if decision["sampled"] else "sampled_out",
    }
    return {
        "enabled": True,
        "ran": False,
        "sampled": bool(decision["sampled"]),
        "crosscheck_mode": True,
        "risky": bool(risk["risky"]),
        "risk_reasons": risk["reasons"],
        "risk_why": record["risk_why"],
        "spot": spot,
        "seed": seed_value,
        "record": record,
    }


# ---------------------------------------------------------------------------
# prompt blocks


def _spot_lines(spot):
    lines = []
    for item in spot.get("requirements") or []:
        lines.append("- %s: %s | evidence: %s"
                     % (item.get("id"), _truncate(item.get("text"), 300),
                        _truncate(item.get("evidence_command")
                                  or item.get("evidence"), 500)))
    for item in spot.get("fixes") or []:
        lines.append("- %s: %s | confirmed by %s | evidence: %s"
                     % (item.get("id"), _truncate(item.get("text"), 300),
                        item.get("confirmed_by") or item.get("found_by_crew"),
                        _truncate(item.get("evidence_command")
                                  or item.get("evidence"), 500)))
    return lines


def prompt_block(plan, crew_evidence="", test_evidence=""):
    """Audit block appended to the normal reviewer task."""
    record = (plan or {}).get("record") or plan or {}
    spot = (plan or {}).get("spot") or record.get("spot") or {}
    lines = [
        "",
        "## Connector audit (mandatory; this review is sampled in)",
        "The connector already verified the crew gate. You are the normally "
        "configured independent reviewer and run once. Perform the normal "
        "review, then this audit. Start from the diff since the job started "
        "and the crew evidence below; do not re-review the whole repository.",
        "",
        "### Crew evidence (untrusted)",
        _truncate(crew_evidence, MAX_EVIDENCE_CHARS) or "(none recorded)",
        "",
        "### Connector test result",
        _truncate(test_evidence, 4000) or "(no test evidence)",
        "",
        "### Spot checks (report every selected item)",
        "Requirements selected (seed %s):" % spot.get("seed"),
    ]
    lines += _spot_lines(spot) or ["- (none available)"]
    lines += [
        "For each item above, re-run the exact evidence command (do not run "
        "unrelated tests) and record pass/fail plus the observed output or "
        "path:line. Then scan the connector diff for risky changes (secrets, "
        "permissions, sandbox, install/update, job control). If fewer items "
        "exist than requested, check all that exist and say so.",
        "",
        "### Required audit output (append after the normal review)",
        "## Connector audit",
        "- Audit score: <0.0-10.0>",
        "- Audit blocking: <integer>",
        "### Audit blocking items",
        "- AUD-1 [blocking] <defect> | evidence: <command + result or path:line>",
        "### Crew claims that did not hold",
        "- claim: <verbatim claim> | why: <observed> | evidence: <...>",
        "### Spot checks",
        "- R1: pass|fail — <evidence>",
        "",
        "<!-- AUDIT_JSON",
        '{"score": 8.5, "blocking": [{"id": "AUD-1", "text": "...", '
        '"evidence": "..."}], "crew_claims_that_did_not_hold": '
        '[{"claim": "...", "why": "...", "evidence": "..."}], '
        '"spot_checks": [{"id": "R1", "kind": "requirement", '
        '"result": "pass", "evidence": "..."}]}',
        "-->",
    ]
    return "\n".join(lines)


def reaudit_block(plan, prior_blocking):
    """Focused block for the round after audit blocking items were fixed."""
    if not prior_blocking:
        return ""
    lines = [
        "",
        "## Connector re-audit (only the items below)",
        "The crew fixed the audit blocking list. Re-audit ONLY these items by "
        "re-running their evidence; do not repeat the full audit.",
    ]
    for item in prior_blocking[:20]:
        if isinstance(item, dict):
            lines.append("- %s: %s | evidence: %s"
                         % (item.get("id"), _truncate(item.get("text"), 300),
                            _truncate(item.get("evidence"), 500)))
        else:
            lines.append("- %s" % _truncate(item, 500))
    lines += [
        "",
        "Append a short `## Connector re-audit` section with pass/fail per item "
        "and update the AUDIT_JSON block (same shape).",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# parsing


def _coerce_blocking(value):
    rows = []
    if isinstance(value, int):
        return [], max(0, value)
    if not isinstance(value, list):
        return [], 0
    for index, item in enumerate(value):
        if isinstance(item, str):
            rows.append({"id": "AUD-%d" % (index + 1), "text": item,
                         "evidence": ""})
        elif isinstance(item, dict):
            row = dict(item)
            row.setdefault("id", "AUD-%d" % (index + 1))
            row["text"] = str(row.get("text") or row.get("defect") or "")
            rows.append(row)
    return rows, len(rows)


def parse_audit(report_text, plan=None):
    """Parse the review report's audit section into a bounded result."""
    text = str(report_text or "")[:MAX_REPORT_CHARS]
    result = {"ran": False, "score": None, "blocking": [], "blocking_count": 0,
              "claims": [], "spot_checks": [], "source": "none"}
    match = _AUDIT_JSON_RE.search(text)
    data = None
    if match:
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            data = None
    if isinstance(data, dict):
        result["source"] = "json"
        try:
            score = float(data.get("score"))
            if math.isfinite(score):
                result["score"] = round(max(0.0, min(10.0, score)), 2)
        except (TypeError, ValueError):
            pass
        blocking, count = _coerce_blocking(data.get("blocking"))
        result["blocking"] = blocking
        result["blocking_count"] = count
        claims = data.get("crew_claims_that_did_not_hold")
        if isinstance(claims, list):
            for item in claims:
                if isinstance(item, dict):
                    result["claims"].append({
                        "claim": _truncate(item.get("claim"), 500),
                        "why": _truncate(item.get("why"), 500),
                        "evidence": _truncate(item.get("evidence"), 800),
                    })
                elif str(item).strip():
                    result["claims"].append({"claim": _truncate(item, 500),
                                             "why": "", "evidence": ""})
        checks = data.get("spot_checks")
        if isinstance(checks, list):
            result["spot_checks"] = [item for item in checks
                                     if isinstance(item, dict)][:40]
        result["ran"] = True
        return result

    score_match = _SCORE_RE.search(text)
    blocking_match = _BLOCKING_RE.search(text)
    if not score_match and not blocking_match:
        return result
    result["source"] = "headings"
    result["ran"] = True
    if score_match:
        try:
            result["score"] = round(max(0.0, min(10.0,
                                                 float(score_match.group(1)))), 2)
        except ValueError:
            pass
    if blocking_match:
        count = max(0, int(blocking_match.group(1)))
        result["blocking_count"] = count
        result["blocking"] = [
            {"id": "AUD-%d" % (index + 1), "text": "audit blocking item",
             "evidence": ""} for index in range(count)]
    section = re.search(r"###\s*Crew claims that did not hold(.*?)(?:\n###|\Z)",
                        text, re.IGNORECASE | re.DOTALL)
    if section:
        for line in section.group(1).splitlines():
            if line.strip().startswith(("-", "*")):
                result["claims"].append({
                    "claim": _truncate(line.strip().lstrip("-* "), 500),
                    "why": "", "evidence": ""})
    for spot in _SPOT_RE.finditer(text):
        result["spot_checks"].append({
            "id": spot.group(1),
            "result": spot.group(2).lower(),
            "evidence": _truncate(spot.group(3), 500),
        })
    return result


def finish(plan, report_text, cfg=None):
    """Parse the paid report and update the plan/record."""
    plan = plan or {}
    record = plan.get("record") or plan
    parsed = parse_audit(report_text, plan)
    if not parsed.get("ran"):
        record["ran"] = False
        record["verdict"] = "not_reported"
        return record
    record.update({
        "ran": True,
        "score": parsed.get("score"),
        "blocking": parsed.get("blocking") or [],
        "blocking_count": int(parsed.get("blocking_count") or 0),
        "claims_failed": parsed.get("claims") or [],
        "spot_checks": parsed.get("spot_checks") or [],
        "parse_source": parsed.get("source"),
        "verdict": "blocked" if int(parsed.get("blocking_count") or 0) else "passed",
    })
    if record["claims_failed"]:
        record["forced_rate_set"] = True
        if cfg is not None:
            record["forced_rate"] = record_claim_failure(
                cfg, str(record.get("job_id") or ""), workspace=None)
    return record


def confirmed_fixes(crosscheck_record):
    """Fixes eligible for audit spot checks: confirmed by the finding crew."""
    rows = []
    for item in (crosscheck_record or {}).get("findings") or []:
        if str(item.get("status") or "").lower() != "fixed":
            continue
        confirmation = item.get("confirmation")
        if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
            continue
        finder = str(item.get("found_by_crew") or item.get("found_by") or "")
        if str(confirmation.get("crew") or "") != finder:
            continue
        rows.append({
            "id": item.get("id"),
            "text": item.get("text"),
            "found_by_crew": finder,
            "confirmed_by": finder,
            "evidence_command": item.get("evidence_command")
            or (item.get("fix") or {}).get("evidence")
            or confirmation.get("evidence"),
        })
    return rows


def requirements_for_audit(crosscheck_record):
    rows = []
    for item in (crosscheck_record or {}).get("requirements") or []:
        if item.get("required", True) is False:
            continue
        rows.append({
            "id": item.get("id"),
            "text": item.get("text"),
            "evidence_command": item.get("evidence_command")
            or item.get("evidence"),
        })
    return rows
