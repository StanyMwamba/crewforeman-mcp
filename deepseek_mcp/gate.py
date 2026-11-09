"""F1 deterministic gate: secret scan -> lint -> tests.

The gate runs on the exact post-builder tree, before any paid reviewer call.
It never stores file bodies, absolute paths, secrets or reviewer prose: only
bounded, redacted tails, digests and four-character finding prefixes are kept.

The module is deliberately defensive about its config source so it works both
with the ``Config`` object and with an older config that lacks the gate keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import path_scope, platforms, review_context, safety
from .api import redact_secrets

ALGORITHM = "sha256"
SECRET_BUDGET_SECONDS = 10.0
LINT_BUDGET_SECONDS = 60.0
DEFAULT_TOTAL_SECONDS = 300.0
DEFAULT_MAX_OUTPUT_CHARS = 4000
MAX_SCAN_BYTES = 1024 * 1024
MAX_FINDINGS = 100
MAX_MUTATION_PATHS = 20


def _terminate_tree(pid, system=None, timeout=0.5):
    """Kill a whole process tree through the shared platform primitive."""
    terminate = getattr(platforms, "terminate_tree", None)
    if terminate is None:
        return False
    try:
        result = terminate(pid, system=system, timeout=timeout)
    except TypeError:
        try:
            result = terminate(pid)
        except Exception:
            return False
    except Exception:
        return False
    if isinstance(result, dict):
        return bool(result.get("terminated"))
    return bool(result)
MAX_ERROR_CHARS = 600

_ALLOWLIST_MARKERS = ("# gitleaks:allow", "pragma: allowlist secret")
_CACHE_ALLOWED = ("REVIEW.md", ".ruff_cache/", ".pytest_cache/",
                  ".mypy_cache/", ".coverage", "htmlcov/")

# (rule_id, compiled pattern, captured group carrying the value)
_BUILTIN_RULES = (
    ("pem-private-key",
     re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"), 0),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("aws-secret-access-key",
     re.compile(r"(?i)aws_?secret_?access_?key\s*[:=]\s*['\"]?"
                r"([A-Za-z0-9/+=]{40})"), 1),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), 0),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), 0),
    ("provider-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), 0),
    ("generic-secret-assignment",
     re.compile(r"(?i)\b(api[_-]?key|secret|token|password|client[_-]?secret)"
                r"\b['\"]?\s*[:=]\s*(?:['\"][^'\"\r\n]{8,}['\"]"
                r"|(?:sk-|gh[pousr]_|AKIA|AIza|xox[abprs]-|-----BEGIN)"
                r"[^\s\"',;#]{4,})"), 0),
    ("generic-secret-literal",
     re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|client[_-]?secret)"
                r"\b['\"]?\s*[:=]\s*"
                r"([A-Za-z0-9][A-Za-z0-9_.+/=-]{7,})"
                r"(?![A-Za-z0-9_\[\(])"), 1),
)


# ---------------------------------------------------------------------------
# config helpers


def _cfg(cfg, key, default=None):
    """Read a config key from a dict, Config-like object or namespace."""
    if cfg is None:
        return default
    try:
        if isinstance(cfg, dict):
            value = cfg.get(key, default)
        else:
            getter = getattr(cfg, "get", None)
            if callable(getter):
                value = getter(key, default)
            else:
                value = getattr(cfg, key, default)
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default, minimum=0):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    return max(minimum, result)


def _state_dir(cfg):
    value = _cfg(cfg, "state_dir", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _max_output_chars(cfg):
    return min(100000, _as_int(
        _cfg(cfg, "gate_max_output_chars", DEFAULT_MAX_OUTPUT_CHARS),
        DEFAULT_MAX_OUTPUT_CHARS))


def _remaining_job_budget(cfg):
    """Best-effort remaining whole-job seconds, used to clamp the gate."""
    budget = _cfg(cfg, "job_budget", None)
    if budget is None:
        budget = _cfg(cfg, "budget", None)
    remaining = getattr(budget, "remaining_seconds", None)
    if not callable(remaining):
        return None
    try:
        value = remaining()
    except Exception:
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# small utilities


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(text):
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def _tail(cfg, text):
    limit = _max_output_chars(cfg)
    cleaned = redact_secrets(text or "")
    if limit <= 0:
        return ""
    return cleaned[-limit:]


def _safe_error(exc, workspace):
    try:
        message = "%s: %s" % (type(exc).__name__, exc)
    except Exception:
        message = type(exc).__name__
    message = redact_secrets(message)
    if workspace:
        message = message.replace(str(workspace), ".")
    if len(message) > MAX_ERROR_CHARS:
        message = message[:MAX_ERROR_CHARS]
    return message


def _tree_sha(workspace):
    try:
        digest = review_context.tree_digest(workspace)
        value = digest.get("sha256") if isinstance(digest, dict) else None
        if value:
            return str(value)
    except Exception:
        pass
    try:
        snapshot = _snapshot(workspace)
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode(
                "utf-8", "replace")).hexdigest()
    except Exception:
        return ""


def _snapshot(workspace):
    try:
        return safety.snapshot_workspace(workspace) or {}
    except Exception:
        return {}


def _diff(before, after):
    try:
        changes = safety.diff_workspace(before or {}, after or {})
        if isinstance(changes, list):
            return changes
    except Exception:
        pass
    changes = []
    before = before or {}
    after = after or {}
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append({"path": path, "change": "created"})
        elif path not in after:
            changes.append({"path": path, "change": "deleted"})
        elif before[path] != after[path]:
            changes.append({"path": path, "change": "modified"})
    return changes


def _tree_changes(before, after, workspace=None, state_dir=None):
    changes = _diff(before, after)
    prefix = None
    if state_dir and workspace:
        try:
            rel = os.path.relpath(os.path.abspath(str(state_dir)),
                                  os.path.abspath(str(workspace)))
            if not rel.startswith(".."):
                prefix = rel.replace(os.sep, "/").rstrip("/") + "/"
        except Exception:
            prefix = None
    allowed = []
    for change in changes:
        path = str(change.get("path") or "")
        if path.startswith(_CACHE_ALLOWED) or path in _CACHE_ALLOWED:
            continue
        if prefix and (path == prefix[:-1] or path.startswith(prefix)):
            continue
        allowed.append(change)
    return allowed[:MAX_MUTATION_PATHS]


def _safe_join(workspace, rel):
    try:
        root = os.path.abspath(str(workspace))
        full = os.path.abspath(os.path.join(root, str(rel)))
        if full != root and not full.startswith(root + os.sep):
            return None
        return full
    except Exception:
        return None


def _safe_env():
    helper = getattr(review_context, "_safe_env", None)
    if callable(helper):
        try:
            return helper(None)
        except Exception:
            pass
    names = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL",
             "SYSTEMROOT", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    env["NO_COLOR"] = "1"
    return env


def _command_parts(command):
    if command is None:
        return None
    if isinstance(command, str):
        text = command.strip()
        if not text:
            return None
        try:
            return shlex.split(text) or [text]
        except ValueError:
            return [text]
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command] or None
    return None


def _command_available(command):
    parts = _command_parts(command)
    if not parts:
        return False
    executable = parts[0]
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return os.path.isfile(executable) and os.access(executable, os.X_OK)
    return shutil.which(executable) is not None


def _start_error(command, exc):
    return {
        "command": _command_parts(command) or command,
        "exit_code": None,
        "output": "Failed to start command: %s" % exc,
        "timed_out": False,
        "seconds": 0.0,
        "start_error": True,
    }


# ---------------------------------------------------------------------------
# process runner


def _normalize_run(raw, command):
    timed_out = bool(raw.get("timed_out"))
    code = raw.get("exit_code", raw.get("returncode", raw.get("code")))
    output = raw.get("output")
    if output is None:
        stdout = raw.get("stdout", b"") or b""
        stderr = raw.get("stderr", b"") or b""
        if isinstance(stdout, bytes) or isinstance(stderr, bytes):
            output = ((stdout if isinstance(stdout, bytes) else str(stdout).encode())
                      + (stderr if isinstance(stderr, bytes) else str(stderr).encode())
                      ).decode("utf-8", "replace")
        else:
            output = str(stdout) + str(stderr)
    return {
        "command": raw.get("command") or _command_parts(command) or command,
        "exit_code": code,
        "output": str(output or ""),
        "timed_out": timed_out,
        "seconds": float(raw.get("seconds") or 0.0),
        "start_error": code is None and not timed_out,
    }


def _local_run(command, workspace, timeout):
    parts = _command_parts(command)
    if not parts:
        return _start_error(command, ValueError("empty command"))
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            parts, cwd=workspace, env=_safe_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name != "nt"))
    except OSError as exc:
        return _start_error(parts, exc)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=max(0.0, float(timeout)))
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        if not _terminate_tree(proc.pid):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = b"", b""
        code = proc.returncode
    output = ((stdout or b"") + (stderr or b"")).decode("utf-8", "replace")
    return {
        "command": parts,
        "exit_code": code,
        "output": output,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 4),
        "start_error": False,
    }


def _run_process(command, workspace, timeout):
    """Prefer ``review_context.run_command``; fall back to a bounded local run."""
    runner = getattr(review_context, "run_command", None)
    if callable(runner):
        try:
            raw = runner(command, cwd=workspace, timeout=timeout)
        except TypeError:
            raw = None
        except OSError as exc:
            return _start_error(command, exc)
        if isinstance(raw, dict):
            return _normalize_run(raw, command)
    return _local_run(command, workspace, timeout)


# ---------------------------------------------------------------------------
# signing


def _sign_check(cfg, check):
    check["signed"] = False
    check["algorithm"] = ALGORITHM
    check.setdefault("key_id", None)
    check["signature"] = None
    state_dir = _state_dir(cfg)
    if not state_dir:
        return check
    try:
        ensure = getattr(review_context, "ensure_evidence_key", None)
        sign = getattr(review_context, "sign_payload", None)
        if not callable(ensure) or not callable(sign):
            return check
        key_id, secret = ensure(state_dir)
        check["key_id"] = key_id
        check["signed"] = True
        payload = {key: value for key, value in check.items()
                   if key != "signature"}
        check["signature"] = sign(payload, secret)
    except Exception:
        # Evidence signing is best-effort: an unavailable key must not turn a
        # runnable gate into a failure.
        check["signed"] = False
        check["signature"] = None
    return check


def _redact_command(command):
    if command is None:
        return None
    if isinstance(command, (list, tuple)):
        return [redact_secrets(str(part)) for part in command]
    return redact_secrets(str(command))


# ---------------------------------------------------------------------------
# secret scan


def _allowlisted(lines, index):
    for offset in (index, index - 1):
        if 0 <= offset < len(lines):
            text = lines[offset]
            if any(marker in text for marker in _ALLOWLIST_MARKERS):
                return True
    return False


def _add_finding(findings, rule_id, path, line, prefix, allowlisted):
    if len(findings) >= MAX_FINDINGS:
        return
    key = (rule_id, path, line)
    for existing in findings:
        if (existing["rule_id"], existing["path"], existing["line"]) == key:
            return
    findings.append({
        "rule_id": rule_id,
        "path": path,
        "line": int(line),
        "prefix": str(prefix or "")[:4],
        "allowlisted": bool(allowlisted),
    })


def _secret_rules(cfg):
    rules = list(_BUILTIN_RULES)
    extra = _cfg(cfg, "gate_secret_patterns", []) or []
    if isinstance(extra, str):
        extra = [extra]
    if not isinstance(extra, (list, tuple)):
        extra = []
    for index, raw in enumerate(extra, 1):
        rules.append(("custom-%d" % index, re.compile(str(raw)), 0))
    return rules


_SECRET_SKIP_DEFAULTS = (
    "logs/**", "**/logs/**", "**/*.log", "**/*.diff", "**/*.patch",
    "**/*.rej", "**/*.orig", "repo-ref/**", "**/repo-ref/**",
    ".dsh-tmp-*", ".dsh-model-overlay-*",
    "**/cache/**", "cache/**", "**/coverage/**", "coverage/**",
)


def _secret_skip_globs(cfg):
    raw = _cfg(cfg, "gate_secret_skip_globs", None)
    if raw is None:
        return list(_SECRET_SKIP_DEFAULTS)
    if isinstance(raw, str):
        raw = [raw]
    return [str(item) for item in raw if item]


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_DOTTED_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
# A bare word that mixes upper and lower case is a class/type/function
# reference (``Optional``, ``SecretStr``, ``getToken``), not a hard-coded value.
_MIXED_CASE_WORD_RE = re.compile(r"^[A-Za-z]+$")
_PLACEHOLDER_PREFIXES = (
    "your_", "your-", "example", "sample", "changeme", "change-me",
    "placeholder", "replace", "dummy", "redacted", "xxxx", "todo",
    "none", "null", "true", "false",
)
_LITERAL_EXCLUDED_CHARS = frozenset("(){}$[]'\"`")
# Values that label themselves as fixtures/placeholders are not real secrets.
# The strong provider-prefix rule still fires independently of this filter.
_FAKE_MARKERS = (
    "-fake", "_fake", "fake-", "fake_", "-dummy", "_dummy",
    "-example", "_example", "-sample", "_sample", "-test", "_test",
    "-placeholder", "_placeholder", "-redacted", "_redacted",
)


def _self_identifying_fake(value):
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in _FAKE_MARKERS)


def _literal_like(value):
    """Return True when *value* looks like a hard-coded literal secret."""
    if value is None:
        return False
    text = str(value)
    if len(text) < 8:
        return False
    if any(ch.isspace() for ch in text):
        return False
    if any(ch in _LITERAL_EXCLUDED_CHARS for ch in text):
        return False
    if _ENV_NAME_RE.match(text):
        return False
    if _SNAKE_CASE_RE.match(text):
        return False
    if _DOTTED_NAME_RE.match(text):
        return False
    if _MIXED_CASE_WORD_RE.match(text) and not (
            text.islower() or text.isupper()):
        return False
    lowered = text.lower()
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return False
    if _self_identifying_fake(text):
        return False
    if text[0] in ".-/=" or text[-1] in ".-/":
        return False
    body = text.rstrip("=")
    if not body or "=" in body:
        # ``=`` is only valid as trailing base64 padding.
        return False
    if "+" in text:
        # ``+`` is valid base64 content, not an arithmetic expression.  Only
        # accept it in a base64-shaped value that also mixes cases/digits.
        if not re.match(r"^[A-Za-z0-9+/]+={0,2}$", text):
            return False
        if not re.search(r"[A-Z]", text) or not re.search(r"[a-z0-9]", text):
            return False
    return True


def _quoted_literal(match_text):
    """Return the quoted value of an assignment match, or None."""
    sep = re.search(r"[:=]\s*", match_text)
    if sep is None:
        return None
    rest = match_text[sep.end():]
    if len(rest) >= 2 and rest[0] in "'\"" and rest[-1] == rest[0]:
        return rest[1:-1]
    return None


_TEMPLATE_MARKERS = ("${", "{{", "os.environ", "getenv(", "env.",
                     "process.env", "%s", "$(")


def _reference_like(value):
    """True for a quoted *reference*, not a literal value.

    Quoted strings are literals by definition, so this is deliberately narrow:
    templates/interpolations, environment-variable names and obvious
    placeholders.  A real quoted password stays a finding.
    """
    text = str(value or "").strip()
    if not text:
        return True
    if any(marker in text for marker in _TEMPLATE_MARKERS):
        return True
    if text.startswith(("$", "{", "<", "%", "*")):
        return True
    if _ENV_NAME_RE.match(text):
        return True
    if _self_identifying_fake(text):
        return True
    return text.lower().startswith(_PLACEHOLDER_PREFIXES)


def _scan_rel_file(cfg, workspace, rel, rules, findings):
    """Scan one repo-relative file. Returns (scanned, binary, large) counts."""
    full = _safe_join(workspace, rel)
    if full is None or not os.path.isfile(full) or os.path.islink(full):
        return 0, 0, 0
    try:
        size = os.path.getsize(full)
    except OSError:
        return 0, 0, 0
    if size > MAX_SCAN_BYTES:
        return 0, 0, 1
    try:
        with open(full, "rb") as handle:
            raw = handle.read(MAX_SCAN_BYTES + 1)
    except OSError:
        return 0, 0, 0
    if len(raw) > MAX_SCAN_BYTES:
        return 0, 0, 1
    if b"\x00" in raw[:8192]:
        return 0, 1, 0
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    base = os.path.basename(rel)
    if base == ".env" or (base.startswith(".env.") and base != ".env.example"):
        _add_finding(findings, "dotenv-file", rel, 1, "", _allowlisted(lines, 0))
    stop = False
    for line_no, line in enumerate(lines, 1):
        allow = _allowlisted(lines, line_no - 1)
        for rule_id, pattern, group in rules:
            for match in pattern.finditer(line):
                value = match.group(group) if group else match.group(0)
                if rule_id == "generic-secret-literal":
                    if not _literal_like(value):
                        continue
                elif rule_id == "generic-secret-assignment":
                    quoted = _quoted_literal(match.group(0))
                    if quoted is not None and _reference_like(quoted):
                        continue
                _add_finding(findings, rule_id, rel, line_no,
                             str(value or "")[:4], allow)
                if len(findings) >= MAX_FINDINGS:
                    stop = True
                    break
            if stop:
                break
        if stop:
            break
    return 1, 0, 0


def _scan_paths(cfg, workspace, paths, rules, findings, deadline=None):
    stats = {"scanned": 0, "binary": 0, "large": 0, "skipped": 0}
    timed_out = False
    for rel in sorted(set(str(path) for path in paths if path)):
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        scanned, binary, large = _scan_rel_file(
            cfg, workspace, rel, rules, findings)
        if not (scanned or binary or large):
            stats["skipped"] += 1
        stats["scanned"] += scanned
        stats["binary"] += binary
        stats["large"] += large
        if len(findings) >= MAX_FINDINGS:
            break
    return stats, timed_out


def scan_files(cfg, workspace, paths):
    """Public scanner: return secret findings for explicit relative paths."""
    findings = []
    _scan_paths(cfg, os.path.abspath(str(workspace)), paths,
                _secret_rules(cfg), findings)
    return findings


def scan_tree(cfg, root, skip=None):
    """Public scanner: walk ``root`` and return findings (skip globs/dirs)."""
    root = os.path.abspath(str(root))
    skip = list(skip or (".git",))
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for name in dirnames:
            rel = (rel_dir + "/" + name) if rel_dir else name
            if name in skip or path_scope.match_any(rel, skip):
                continue
            keep.append(name)
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            rel = (rel_dir + "/" + name) if rel_dir else name
            if path_scope.match_any(rel, skip):
                continue
            paths.append(rel)
    return scan_files(cfg, root, paths)


def _secret_check(cfg, workspace, before, after, deadline, only_paths=None):
    started = time.monotonic()
    if not _as_bool(_cfg(cfg, "gate_secret_scan", True), True):
        return {
            "name": "secrets", "status": "skipped", "command": None,
            "exit_code": None, "seconds": 0.0, "tests": 0, "failures": 0,
            "errors": 0, "output_digest": _sha256_text(""),
            "output_tail": "Secret scan disabled by configuration.",
            "tree_changed": False, "timed_out": False,
            "skipped_reason": "disabled", "findings": [],
        }
    rules = _secret_rules(cfg)
    findings = []
    paths = []
    if only_paths is not None:
        paths = [str(path) for path in only_paths if path]
    else:
        for change in _diff(before, after):
            if change.get("change") in ("created", "modified"):
                paths.append(str(change.get("path") or ""))
    skip_globs = _secret_skip_globs(cfg)
    scanned_paths = []
    skipped_artifacts = 0
    for rel in sorted(set(path for path in paths if path)):
        if path_scope.is_split_fixture(rel, cfg) or path_scope.match_any(
                rel, skip_globs):
            skipped_artifacts += 1
            continue
        scanned_paths.append(rel)
    stats, timed_out = _scan_paths(
        cfg, workspace, scanned_paths, rules, findings, deadline)
    active = [item for item in findings if not item["allowlisted"]]
    if timed_out:
        status = "timed_out"
    elif active:
        status = "failed"
    else:
        status = "ok"
    summary = ("Secret scan: %d file(s) scanned, %d finding(s), "
               "%d allowlisted, %d binary skipped, %d large skipped, "
               "%d artifact/fixture skipped."
               % (stats["scanned"], len(findings),
                  len(findings) - len(active), stats["binary"],
                  stats["large"], skipped_artifacts))
    return {
        "name": "secrets", "status": status, "command": None,
        "exit_code": None, "seconds": round(time.monotonic() - started, 4),
        "tests": 0, "failures": 0, "errors": 0,
        "output_digest": _sha256_text(summary),
        "output_tail": summary,
        "tree_changed": False, "timed_out": timed_out,
        "skipped_reason": None, "findings": findings,
        "files_scanned": stats["scanned"],
        "files_skipped_binary": stats["binary"],
        "files_skipped_large": stats["large"],
        "files_skipped_artifact": skipped_artifacts,
    }


# ---------------------------------------------------------------------------
# lint


def _detect_lint(workspace):
    def read(name):
        try:
            with open(os.path.join(workspace, name), "r",
                      encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    if (os.path.isfile(os.path.join(workspace, "ruff.toml"))
            or os.path.isfile(os.path.join(workspace, ".ruff.toml"))):
        return ["ruff", "check", "."], "ruff"
    if "[tool.ruff" in read("pyproject.toml"):
        return ["ruff", "check", "."], "ruff"
    for name in ("setup.cfg", ".flake8"):
        if os.path.isfile(os.path.join(workspace, name)) \
                and "[flake8" in read(name):
            return ["flake8", "."], "flake8"
    if re.search(r"(?m)^lint\s*:", read("Makefile")):
        return ["make", "lint"], "make"
    if os.path.isdir(os.path.join(workspace, "node_modules", ".bin")):
        try:
            package = json.loads(read("package.json") or "{}")
        except ValueError:
            package = {}
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if isinstance(scripts, dict) and str(scripts.get("lint") or "").strip():
            return ["npm", "run", "lint"], "npm"
    return None, None


def _lint_check(cfg, workspace, deadline):
    started = time.monotonic()
    mode = _cfg(cfg, "gate_lint", "auto")
    explicit = _cfg(cfg, "gate_lint_command", "") or ""
    if not explicit and str(mode or "").strip().lower() != "auto":
        explicit = None if str(mode or "").strip().lower() == "off" else mode
    if not explicit and str(mode or "").strip().lower() == "off":
        return {
            "name": "lint", "status": "skipped", "command": None,
            "exit_code": None, "seconds": 0.0, "tests": 0, "failures": 0,
            "errors": 0, "output_digest": _sha256_text(""),
            "output_tail": "Lint disabled by configuration.",
            "tree_changed": False, "timed_out": False,
            "skipped_reason": "disabled", "findings": [],
        }
    tool = None
    if explicit:
        command = _command_parts(explicit)
        if not command or not _command_available(command):
            return {
                "name": "lint", "status": "error",
                "command": command or explicit, "exit_code": None,
                "seconds": round(time.monotonic() - started, 4),
                "tests": 0, "failures": 0, "errors": 1,
                "output_digest": _sha256_text("command not found"),
                "output_tail": "Configured lint command could not be started.",
                "tree_changed": False, "timed_out": False,
                "skipped_reason": None, "findings": [],
            }
    else:
        command, tool = _detect_lint(workspace)
        if not command:
            return {
                "name": "lint", "status": "skipped", "command": None,
                "exit_code": None, "seconds": round(
                    time.monotonic() - started, 4),
                "tests": 0, "failures": 0, "errors": 0,
                "output_digest": _sha256_text(""),
                "output_tail": "No supported lint configuration detected.",
                "tree_changed": False, "timed_out": False,
                "skipped_reason": "no_lint_detected", "findings": [],
            }
        if tool and shutil.which(tool) is None:
            return {
                "name": "lint", "status": "skipped", "command": command,
                "exit_code": None, "seconds": round(
                    time.monotonic() - started, 4),
                "tests": 0, "failures": 0, "errors": 0,
                "output_digest": _sha256_text(""),
                "output_tail": "Lint tool not found on PATH: %s" % tool,
                "tree_changed": False, "timed_out": False,
                "skipped_reason": "tool_not_found: %s" % tool,
                "findings": [],
            }
    timeout = min(LINT_BUDGET_SECONDS,
                  max(0.0, deadline - time.monotonic()))
    if timeout <= 0:
        return {
            "name": "lint", "status": "skipped", "command": command,
            "exit_code": None, "seconds": 0.0, "tests": 0, "failures": 0,
            "errors": 0, "output_digest": _sha256_text(""),
            "output_tail": "Gate time budget exhausted before lint.",
            "tree_changed": False, "timed_out": False,
            "skipped_reason": "budget_exhausted", "findings": [],
        }
    snap_before = _snapshot(workspace)
    run = _run_process(command, workspace, timeout)
    snap_after = _snapshot(workspace)
    changes = _tree_changes(snap_before, snap_after, workspace,
                            _state_dir(cfg))
    code = run.get("exit_code")
    if run.get("timed_out"):
        status = "timed_out"
    elif run.get("start_error"):
        status = "error"
    elif code == 0:
        status = "ok"
    else:
        status = "failed"
    if changes and status in ("ok",):
        status = "failed"
    output = str(run.get("output") or "")
    return {
        "name": "lint", "status": status,
        "command": run.get("command") or command,
        "exit_code": code, "seconds": run.get("seconds") or 0.0,
        "tests": 0, "failures": 0,
        "errors": 0 if status in ("ok", "skipped") else 1,
        "output_digest": _sha256_text(output),
        "output_tail": _tail(cfg, output),
        "tree_changed": bool(changes),
        "tree_changes": changes,
        "timed_out": bool(run.get("timed_out")),
        "skipped_reason": "tree_mutated" if (changes and status == "failed")
                          else None,
        "findings": [],
    }


def _skipped_lint_check(reason):
    return {
        "name": "lint", "status": "skipped", "command": None,
        "exit_code": None, "seconds": 0.0, "tests": 0, "failures": 0,
        "errors": 0, "output_digest": _sha256_text(""),
        "output_tail": "Lint skipped: %s" % reason,
        "tree_changed": False, "timed_out": False,
        "skipped_reason": reason, "findings": [],
    }


# ---------------------------------------------------------------------------
# tests


def _skipped_test_check(reason, command=None, seconds=0.0):
    return {
        "name": "tests", "status": "skipped", "command": command,
        "exit_code": None, "seconds": round(seconds, 4), "tests": 0,
        "failures": 0, "errors": 0, "output_digest": _sha256_text(""),
        "output_tail": "Test suite skipped: %s" % reason,
        "tree_changed": False, "timed_out": False,
        "skipped_reason": reason, "findings": [],
    }


def _tests_check(cfg, workspace, run, deadline):
    started = time.monotonic()
    mode = _cfg(cfg, "gate_tests", "auto")
    explicit = _cfg(cfg, "gate_test_command", "") or ""
    if not explicit:
        text_mode = str(mode or "").strip().lower()
        if text_mode == "off":
            return _skipped_test_check("disabled", seconds=time.monotonic() - started)
        if text_mode != "auto":
            explicit = mode
    if explicit:
        command = _command_parts(explicit)
        if not command or not _command_available(command):
            return {
                "name": "tests", "status": "error",
                "command": command or explicit, "exit_code": None,
                "seconds": round(time.monotonic() - started, 4),
                "tests": 0, "failures": 0, "errors": 1,
                "output_digest": _sha256_text("command not found"),
                "output_tail": "Configured test command could not be started.",
                "tree_changed": False, "timed_out": False,
                "skipped_reason": None, "findings": [],
            }
    else:
        detector = getattr(review_context, "_detect_test_project", None)
        try:
            detected = detector(workspace) if callable(detector) else None
        except Exception:
            detected = None
        if not detected:
            return _skipped_test_check(
                "no_tests_detected", seconds=time.monotonic() - started)
        command = None
    timeout = max(0.0, deadline - time.monotonic())
    if timeout <= 0:
        return _skipped_test_check("budget_exhausted",
                                   command=command,
                                   seconds=time.monotonic() - started)
    state_dir = _state_dir(cfg)
    snap_before = _snapshot(workspace)
    try:
        suite = review_context.run_test_suite(
            workspace, command=command, timeout=timeout,
            key_store=state_dir,
            metadata={"phase": "deterministic_gate"})
    except Exception:
        raise
    snap_after = _snapshot(workspace)
    changes = _tree_changes(snap_before, snap_after, workspace,
                            _state_dir(cfg))
    raw_status = str(suite.get("status") or "error")
    if suite.get("timed_out"):
        status = "timed_out"
    elif raw_status == "ok":
        status = "failed" if changes else "ok"
    elif raw_status == "skipped":
        status = "skipped"
    else:
        status = "error" if raw_status == "error" else "failed"
    tree_changed = bool(suite.get("tree_changed")) or bool(changes)
    if tree_changed and status == "ok":
        status = "failed"
    skipped_reason = None
    if status == "skipped":
        skipped_reason = "no_tests_detected"
    elif tree_changed:
        skipped_reason = "tree_mutated"
    evidence = dict(suite) if isinstance(suite, dict) else {}
    safe_to_reuse = (_redact_command(evidence.get("command"))
                     == evidence.get("command"))
    if status == "ok" and evidence.get("signature") and safe_to_reuse:
        try:
            if isinstance(run, dict):
                bucket = run.setdefault("review_test_evidence", [])
                if isinstance(bucket, list):
                    bucket.append(evidence)
        except Exception:
            pass
    return {
        "name": "tests", "status": status,
        "command": suite.get("command") or command,
        "exit_code": suite.get("exit_code"),
        "seconds": suite.get("seconds") or round(
            time.monotonic() - started, 4),
        "tests": _as_int(suite.get("tests"), 0),
        "failures": _as_int(suite.get("failures"), 0),
        "errors": _as_int(suite.get("errors"), 0),
        "output_digest": suite.get("output_digest") or _sha256_text(""),
        "output_tail": _tail(cfg, suite.get("output_tail") or ""),
        "tree_sha256": suite.get("tree_sha256"),
        "tree_changed": tree_changed,
        "tree_changes": changes,
        "timed_out": bool(suite.get("timed_out")),
        "skipped_reason": skipped_reason,
        "findings": [],
    }


# ---------------------------------------------------------------------------
# public API


def run_gate(cfg, workspace, run, *, phase="pre_review", before=None):
    """Run the deterministic gate and return its bounded run record.

    ``before`` is the pre-gate ``safety.snapshot_workspace`` used to find the
    lines/files the builder changed for the secret scan. When omitted, the
    current tree is used (only files changed *during* the gate are scanned).
    """
    started = time.monotonic()
    workspace = os.path.abspath(str(workspace))
    initial_sha = _tree_sha(workspace)
    result = {
        "verdict": "skipped",
        "tree_sha256": initial_sha,
        "checked_at": _iso_now(),
        "seconds": 0.0,
        "checks": [],
        "revisions": [],
        "blocked_review": False,
        "revision_rounds": 0,
        "gate_error": None,
    }
    existing = {}
    if isinstance(run, dict):
        existing = run.get("deterministic_gate") or {}
    if isinstance(existing, dict):
        result["revision_rounds"] = _as_int(
            existing.get("revision_rounds"), 0)
    if not _as_bool(_cfg(cfg, "gate_enabled", True), True):
        result["verdict"] = "skipped"
        result["skip_reason"] = "gate_disabled"
        result["seconds"] = round(time.monotonic() - started, 4)
        return result
    total = max(0.0, _as_float(
        _cfg(cfg, "gate_timeout_seconds", DEFAULT_TOTAL_SECONDS),
        DEFAULT_TOTAL_SECONDS))
    job_remaining = _remaining_job_budget(cfg)
    if job_remaining is not None:
        total = min(total, job_remaining)
    if total <= 0:
        result["verdict"] = "skipped"
        result["skip_reason"] = "budget_exhausted"
        result["seconds"] = round(time.monotonic() - started, 4)
        return result
    deadline = started + total
    baseline = before
    if before is None:
        before = _snapshot(workspace)
    snapshot = _snapshot(workspace)
    plan_rel = "PLAN.json"
    try:
        from . import plan_contract as _plan_contract
        plan_rel = _plan_contract.plan_path(cfg)
    except Exception:
        pass
    scope = path_scope.classify_changes(baseline, snapshot, cfg, plan_rel)
    output_scope = path_scope.classify_outputs(run, cfg)
    scope["product_outputs"] = bool(output_scope)
    result["scope"] = scope.summary()
    no_baseline = not bool(baseline)
    no_baseline_scan = (no_baseline
                        and str(_cfg(cfg, "gate_no_product_mode", "skip")
                                or "skip").strip().lower() == "skip")
    output_paths = []
    if no_baseline_scan and isinstance(run, dict):
        seen = set()
        raw_outputs = (list(run.get("output_files") or [])
                       + list(run.get("deliverable_files") or []))
        for path in raw_outputs:
            rel = path_scope.normalize_rel(path)
            if not rel or rel in seen:
                continue
            # Patch/log/cache artifacts and known split fixtures are the only
            # secret-scan skips; docs and config are still scanned.
            if (path_scope.is_split_fixture(rel, cfg)
                    or path_scope.match_any(rel, _secret_skip_globs(cfg))):
                continue
            seen.add(rel)
            output_paths.append(rel)
    no_product_mode = str(
        _cfg(cfg, "gate_no_product_mode", "skip") or "skip").strip().lower()
    if (not scope.product and no_product_mode == "skip"
            and (scope.baseline_known or not scope.product_outputs)):
        checks = []
        try:
            secret_check = _secret_check(
                cfg, workspace, baseline, snapshot,
                min(deadline, time.monotonic() + SECRET_BUDGET_SECONDS),
                only_paths=(output_paths if no_baseline_scan else None))
            checks.append(secret_check)
            checks.append(_skipped_lint_check("no_product_code_change"))
            checks.append(_skipped_test_check("no_product_code_change"))
            for check in checks:
                check["command"] = _redact_command(check.get("command"))
                _sign_check(cfg, check)
            if secret_check.get("status") in ("failed", "timed_out", "error"):
                result["verdict"] = "failed"
                result["blocked_review"] = True
            else:
                result["verdict"] = "skipped"
                result["skip_reason"] = "no_product_code_change"
                result["blocked_review"] = False
        except Exception as exc:
            result["verdict"] = "error"
            result["blocked_review"] = True
            result["gate_error"] = _safe_error(exc, workspace)
        result["checks"] = checks
        result["tree_sha256"] = _tree_sha(workspace) or initial_sha
        result["checked_at"] = _iso_now()
        result["seconds"] = round(time.monotonic() - started, 4)
        return result
    checks = []
    try:
        secret_check = _secret_check(
            cfg, workspace, baseline, snapshot,
            min(deadline, time.monotonic() + SECRET_BUDGET_SECONDS),
            only_paths=(output_paths if no_baseline_scan else None))
        checks.append(secret_check)
        secrets_failed = secret_check.get("status") in (
            "failed", "timed_out", "error")
        lint_check = _lint_check(cfg, workspace, deadline)
        checks.append(lint_check)
        if secrets_failed:
            tests_check = _skipped_test_check(
                "secrets_failed",
                seconds=0.0)
        else:
            tests_check = _tests_check(cfg, workspace, run, deadline)
        checks.append(tests_check)
        for check in checks:
            check["command"] = _redact_command(check.get("command"))
            _sign_check(cfg, check)
        hard_failure = any(
            check.get("status") in ("failed", "timed_out", "error")
            for check in checks)
        if hard_failure:
            result["verdict"] = "failed"
            result["blocked_review"] = True
        elif (tests_check.get("status") == "skipped"
              and lint_check.get("status") == "skipped"):
            result["verdict"] = "skipped"
            result["skip_reason"] = "no_tests_or_lint"
        else:
            result["verdict"] = "passed"
    except Exception as exc:
        result["verdict"] = "error"
        result["blocked_review"] = True
        result["gate_error"] = _safe_error(exc, workspace)
    result["checks"] = checks
    result["tree_sha256"] = _tree_sha(workspace) or initial_sha
    result["checked_at"] = _iso_now()
    result["seconds"] = round(time.monotonic() - started, 4)
    return result


def format_gate_evidence(result):
    """Bounded, redacted reviewer-facing summary of a gate record."""
    result = result if isinstance(result, dict) else {}
    lines = ["Deterministic gate: %s" % (result.get("verdict") or "unknown")]
    tree_sha = str(result.get("tree_sha256") or "")
    if tree_sha:
        lines.append("Tree: %s" % tree_sha[:16])
    for check in result.get("checks") or []:
        if not isinstance(check, dict):
            continue
        line = "- %s: %s" % (check.get("name"), check.get("status"))
        if check.get("exit_code") is not None:
            line += " (exit %s)" % check.get("exit_code")
        if check.get("timed_out"):
            line += " [timed out]"
        if check.get("skipped_reason"):
            line += " [%s]" % check.get("skipped_reason")
        lines.append(line)
        for finding in (check.get("findings") or [])[:20]:
            if not isinstance(finding, dict):
                continue
            lines.append(
                "  - %s %s:%s prefix=%r allowlisted=%s"
                % (finding.get("rule_id"), finding.get("path"),
                   finding.get("line"), finding.get("prefix"),
                   bool(finding.get("allowlisted"))))
        if check.get("tree_changed"):
            lines.append("  - checks mutated the tree")
    if result.get("gate_error"):
        lines.append("gate_error: %s" % result.get("gate_error"))
    text = redact_secrets("\n".join(lines))
    return text[:DEFAULT_MAX_OUTPUT_CHARS]


def synthetic_defects(result):
    """One synthetic defect and one fix instruction per failed gate check."""
    result = result if isinstance(result, dict) else {}
    defects = []
    fixes = []
    for check in result.get("checks") or []:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or "check")
        status = str(check.get("status") or "")
        if status in ("ok", "skipped"):
            continue
        if name == "secrets":
            active = [item for item in (check.get("findings") or [])
                      if isinstance(item, dict) and not item.get("allowlisted")]
            defects.append(
                "deterministic gate: secret scan %s with %d finding(s)%s"
                % (status, len(active),
                   " (timed out)" if check.get("timed_out") else ""))
            fixes.append(
                "Remove the committed credential(s), load them from the "
                "environment instead, and rotate anything already exposed.")
        elif name == "lint":
            defects.append(
                "deterministic gate: lint %s (exit %s)%s"
                % (status, check.get("exit_code"),
                   "; checks mutated the tree" if check.get("tree_changed")
                   else ""))
            fixes.append(
                "Fix every lint finding and ensure `%s` exits 0 without "
                "modifying the workspace."
                % (" ".join(str(part) for part in (check.get("command") or []))
                   or "the lint command"))
        elif name == "tests":
            defects.append(
                "deterministic gate: test suite %s (exit %s, tests=%s, "
                "failures=%s, errors=%s)%s"
                % (status, check.get("exit_code"), check.get("tests"),
                   check.get("failures"), check.get("errors"),
                   "; checks mutated the tree" if check.get("tree_changed")
                   else ""))
            fixes.append(
                "Fix the failing tests and rerun the suite in the workspace; "
                "the suite must exit 0 and leave the tree unchanged.")
    mutated = [check for check in (result.get("checks") or [])
               if isinstance(check, dict) and check.get("tree_changed")]
    if mutated:
        paths = []
        for check in mutated:
            for change in (check.get("tree_changes") or [])[:10]:
                if isinstance(change, dict) and change.get("path"):
                    paths.append(str(change.get("path")))
        defects.append(
            "deterministic gate: checks mutated the workspace tree%s"
            % ((" (%s)" % ", ".join(sorted(set(paths))[:10]))
               if paths else ""))
        fixes.append(
            "Do not write, delete or rewrite project files from lint or test "
            "commands; keep generated output in ignored temporary paths.")
    return defects, fixes
