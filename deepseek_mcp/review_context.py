"""Review-context helpers: deterministic tree digests, signed evidence payloads,
bounded test-suite evidence, baseline capture and focused diffs.

Stdlib only. Additive: nothing in this module mutates the workspace except
``capture_baseline`` which writes copies under the caller-supplied ``root``.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .api import redact_secrets

ALGORITHM = "sha256"
IGNORED_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
IGNORED_NAMES = {"REVIEW.md"}
IGNORED_PREFIXES = (".dsh-tmp-",)
STATE_DIRS = {"state", ".state", "state_dir"}
_TEXT_LIMIT_DEFAULT = 60000
_TEST_OUTPUT_TAIL = 4000


def _ignored(rel_path, ignore=None):
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in IGNORED_DIRS or part in STATE_DIRS or part.startswith(IGNORED_PREFIXES):
            return True
    name = parts[-1]
    if name in IGNORED_NAMES or name.startswith(IGNORED_PREFIXES):
        return True
    if ignore:
        for pattern in ignore:
            pattern = str(pattern).strip("/")
            if rel_path == pattern or rel_path.startswith(pattern + "/") or name == pattern:
                return True
    return False


def _iter_files(root, ignore=None):
    root = os.path.abspath(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and d not in STATE_DIRS
            and not d.startswith(IGNORED_PREFIXES)
        )
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if _ignored(rel, ignore):
                continue
            out.append((rel, full))
    out.sort(key=lambda item: item[0])
    return out


def tree_digest(root, ignore=None):
    """Deterministic digest over sorted relative paths, sizes and content hashes."""
    digest = hashlib.sha256()
    files = 0
    total = 0
    for rel, full in _iter_files(root, ignore):
        try:
            size = os.path.getsize(full)
            file_hash = hashlib.sha256()
            with open(full, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    file_hash.update(chunk)
            content = file_hash.hexdigest()
        except OSError:
            continue
        digest.update(("%s\0%d\0%s\n" % (rel, size, content)).encode("utf-8"))
        files += 1
        total += size
    return {"sha256": digest.hexdigest(), "files": files, "bytes": total}


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _resolve_secret(secret):
    if secret is None:
        # hmac.new requires bytes; a hex *str* generated here used to make
        # sign_payload(payload) raise TypeError.
        return secrets.token_bytes(32)
    if isinstance(secret, bytes):
        return secret
    return str(secret).encode("utf-8")


def _state_dir(state_dir):
    if state_dir is None:
        return None
    if hasattr(state_dir, "state_dir"):
        return str(state_dir.state_dir)
    try:
        if isinstance(state_dir, dict):
            return str(state_dir["state_dir"])
    except Exception:
        return None
    return str(state_dir)


def _evidence_keys_dir(state_dir):
    return os.path.join(os.path.abspath(_state_dir(state_dir)),
                        "review-evidence", "keys")


def _key_path(state_dir, key_id):
    text = str(key_id or "")
    if (not re.match(r"^[A-Za-z0-9._-]{1,64}$", text)
            or text in (".", "..")):
        raise ValueError("unsafe evidence key reference")
    return os.path.join(_evidence_keys_dir(state_dir), text + ".key")


def ensure_evidence_key(state_dir):
    """Create/read the connector-controlled HMAC key, returning its reference.

    The key never leaves connector state; the reviewer receives only
    ``key_id`` plus the signature. Later connector runs can reload the key by
    reference and verify the exact evidence they produced.
    """
    state_dir = _state_dir(state_dir)
    if not state_dir:
        raise ValueError("a state_dir is required for evidence keys")
    keys_dir = _evidence_keys_dir(state_dir)
    os.makedirs(keys_dir, mode=0o700, exist_ok=True)
    path = os.path.join(keys_dir, "connector.key")
    if not os.path.isfile(path):
        key = secrets.token_bytes(32)
        tmp = os.path.join(keys_dir, ".connector.%s.tmp"
                           % secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Link, not replace: two connector processes racing to create
                # the first key must converge on one key, never overwrite the
                # key another process already used to sign evidence.
                os.link(tmp, path)
            except FileExistsError:
                pass
            except OSError:
                # Filesystems without hard links: replace is still atomic and
                # the result is a complete key file, but never overwrite a key
                # another process created in the meantime.
                if not os.path.isfile(path):
                    os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    with open(path, "rb") as handle:
        return "connector", handle.read()


def verify_evidence(evidence, state_dir=None, secret=None):
    """Verify a signed evidence dict by key reference (or an explicit key)."""
    if not isinstance(evidence, dict):
        return False
    if secret is None:
        key_id = evidence.get("key_id")
        if not key_id or state_dir is None:
            return False
        try:
            with open(_key_path(state_dir, key_id), "rb") as handle:
                key = handle.read()
        except Exception:
            return False
    else:
        key = _resolve_secret(secret)
    return verify_signature(evidence, evidence.get("signature"), key)


def sign_payload(payload, secret=None):
    """Hex HMAC-SHA256 over canonical JSON."""
    return hmac.new(_resolve_secret(secret), _canonical(payload), hashlib.sha256).hexdigest()


def verify_signature(payload, signature, secret=None):
    if signature is None:
        return False
    try:
        if isinstance(payload, dict) and "signature" in payload:
            payload = {key: value for key, value in payload.items()
                       if key != "signature"}
        expected = sign_payload(payload, secret)
    except Exception:
        return False
    if isinstance(signature, bytes):
        signature = signature.decode("ascii", "replace")
    return hmac.compare_digest(expected, str(signature))


def _parse_counts(output):
    tests = failures = errors = 0
    match = re.search(r"Ran\s+(\d+)\s+tests?", output or "")
    if match:
        tests = int(match.group(1))
    for label, attr in (("failures", "failures"), ("errors", "errors")):
        match = re.search(r"%s=(\d+)" % label, output or "")
        if match:
            if attr == "failures":
                failures = int(match.group(1))
            else:
                errors = int(match.group(1))
    return tests, failures, errors


def _auto_command(workspace):
    tests_dir = os.path.join(workspace, "tests")
    if not os.path.isdir(tests_dir):
        return None
    try:
        entries = os.listdir(tests_dir)
    except OSError:
        return None
    if not any(re.match(r"^test_.+\.py$", name) for name in entries):
        return None
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]


def _normalize_command(command):
    if command is None:
        return None
    if isinstance(command, str):
        return [command] if not command.strip() else command
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    return None


def _safe_env(env):
    """A small, non-secret environment for the connector test suite."""
    names = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL",
             "SYSTEMROOT", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")
    base = {name: os.environ[name] for name in names if os.environ.get(name)}
    base["NO_COLOR"] = "1"
    for key, value in dict(env or {}).items():
        base[str(key)] = str(value)
    return base


def run_test_suite(workspace, command=None, timeout=300.0, env=None,
                   secret=None, key_store=None, metadata=None):
    """Run a test suite under a hard timeout and return signed, secret-free evidence.

    ``key_store`` is a connector-controlled state directory. When supplied, a
    persistent HMAC key is used and its reference (``key_id``) travels with
    the evidence so the connector can verify it later. Tests may pass an
    explicit in-memory secret instead.
    """
    workspace = os.path.abspath(str(workspace))
    extra = dict(metadata or {})
    key_id = None
    if secret is None and key_store is not None:
        key_id, secret = ensure_evidence_key(key_store)
    elif secret is None:
        secret = secrets.token_bytes(32)
    else:
        secret = _resolve_secret(secret)
    before = tree_digest(workspace)
    spec = _normalize_command(command)
    auto = False
    if spec is None:
        spec = _auto_command(workspace)
        auto = True
    if spec is None:
        skipped = {
            "status": "skipped", "command": None, "exit_code": None, "tests": 0,
            "failures": 0, "errors": 0, "output_digest": hashlib.sha256(b"").hexdigest(),
            "output_tail": "No explicit test command and no tests/ directory with "
                           "test_*.py files was found; suite skipped.",
            "seconds": 0.0, "tree_sha256": before["sha256"],
            "tree_sha256_after": before["sha256"], "tree_changed": False,
            "timed_out": False, "algorithm": ALGORITHM, "signed": True,
            "key_id": key_id,
        }
        skipped.update(extra)
        skipped["signature"] = sign_payload(skipped, secret)
        return skipped
    started = time.monotonic()
    timed_out = False
    output = ""
    code = None
    stdout = b""
    stderr = b""
    proc = None
    try:
        kwargs = {
            "cwd": workspace,
            "env": _safe_env(env),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": isinstance(spec, str),
        }
        if os.name != "nt":
            # Own process group: a timeout kills the suite's children too.
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(spec, **kwargs)
        try:
            stdout, stderr = proc.communicate(timeout=float(timeout))
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = b"", b""
            code = proc.returncode
        output = ((stdout or b"") + (stderr or b"")).decode(
            "utf-8", "replace")
    except OSError as exc:
        output = "Failed to start test command: %s" % exc
        code = None
    seconds = round(time.monotonic() - started, 4)
    after = tree_digest(workspace)
    tests, failures, errors = _parse_counts(output)
    if timed_out:
        status = "error"
    elif code is None:
        status = "error"
    elif code == 0 and failures == 0 and errors == 0:
        status = "ok"
    else:
        status = "failed"
    output_digest = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
    result = {
        "status": status,
        "command": spec,
        "exit_code": code,
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "output_digest": output_digest,
        "output_tail": redact_secrets(output[-_TEST_OUTPUT_TAIL:]),
        "seconds": seconds,
        "tree_sha256": after["sha256"],
        "tree_sha256_before": before["sha256"],
        "tree_sha256_after": after["sha256"],
        "tree_changed": before["sha256"] != after["sha256"],
        "timed_out": timed_out,
        "auto_detected": auto,
        "algorithm": ALGORITHM,
        "signed": True,
        "key_id": key_id,
    }
    result.update(extra)
    result["signature"] = sign_payload(result, secret)
    return result


def _is_text(path):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    return b"\0" not in chunk


def capture_baseline(workspace, root, max_file_bytes=262144, max_files=400):
    """Copy a bounded set of text files under ``root/files/...`` plus a manifest.

    Binary and oversized files are recorded in the manifest with an
    ``excluded`` reason (and their content hash) instead of a body, so a later
    diff still reports that they changed. Capture happens in a temporary
    sibling (ignored by the workspace walker) and is swapped in only after the
    manifest is durable, so a failure never leaves a half-overwritten baseline.
    """
    workspace = os.path.abspath(str(workspace))
    root = os.path.abspath(str(root))
    parent = os.path.dirname(root) or "."
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".dsh-tmp-baseline-", dir=parent)
    files_dir = os.path.join(staging, "files")
    os.makedirs(files_dir, exist_ok=True)
    try:
        manifest_files = []
        for rel, full in _iter_files(workspace):
            if len(manifest_files) >= int(max_files):
                break
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            excluded = None
            if size > int(max_file_bytes):
                excluded = "oversized"
            elif not _is_text(full):
                excluded = "binary"
            if excluded:
                manifest_files.append({"path": rel, "sha256": _file_sha(full),
                                       "size": size, "excluded": excluded})
                continue
            dest = os.path.join(files_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(full, "rb") as src, open(dest, "wb") as dst:
                dst.write(src.read())
            manifest_files.append({"path": rel, "sha256": _file_sha(full),
                                   "size": size})
        manifest = {
            "root": root,
            "sha256": hashlib.sha256(_canonical(manifest_files)).hexdigest(),
            "files": manifest_files,
        }
        manifest_path = os.path.join(staging, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        old = None
        if os.path.exists(root):
            old = tempfile.mkdtemp(prefix=".dsh-tmp-baseline-old-", dir=parent)
            os.rmdir(old)
            os.replace(root, old)
        try:
            os.replace(staging, root)
        except Exception:
            if old is not None and not os.path.exists(root):
                os.replace(old, root)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"dir": root, "sha256": manifest["sha256"],
            "files": manifest_files}


def load_baseline(root):
    """Load a captured baseline manifest from ``root``; None when unavailable."""
    try:
        root = os.path.abspath(os.path.expanduser(str(root or "")))
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    return {"dir": root, "sha256": manifest.get("sha256"), "files": files}


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if b"\0" in raw:
        return None
    return raw.decode("utf-8", "replace")


def diff_context(workspace, baseline, max_files=20, max_chars=60000,
                 max_file_bytes=262144):
    """Unified diffs for changed/added/deleted files against a baseline.

    Binary/oversized files yield a metadata-only entry (path + reason) instead
    of disappearing from the changed-file list.
    """
    workspace = os.path.abspath(str(workspace))
    files = list((baseline or {}).get("files") or [])
    base_dir = os.path.join(os.path.abspath(str((baseline or {}).get("dir") or "")), "files")
    stats = {"added": 0, "changed": 0, "deleted": 0}
    chunks = []
    changed_files = []
    truncated = False
    limit = int(max_files)
    max_chars = int(max_chars)
    for entry in files:
        if len(changed_files) >= limit:
            truncated = True
            break
        rel = entry.get("path") if isinstance(entry, dict) else str(entry)
        if not rel:
            continue
        excluded = entry.get("excluded") if isinstance(entry, dict) else None
        new_path = os.path.join(workspace, rel.replace("/", os.sep))
        old_path = os.path.join(base_dir, rel.replace("/", os.sep))
        new_exists = os.path.isfile(new_path)
        if excluded:
            kind = "oversized" if excluded == "oversized" else "binary"
            if not new_exists:
                stats["deleted"] += 1
                changed_files.append(rel)
                chunks.append("--- a/%s\n+++ /dev/null\n[%s file deleted; no body]\n"
                              % (rel, kind))
                continue
            try:
                unchanged = (_file_sha(new_path) == entry.get("sha256")
                             and os.path.getsize(new_path) == entry.get("size"))
            except OSError:
                unchanged = False
            if unchanged:
                continue
            stats["changed"] += 1
            changed_files.append(rel)
            chunks.append("--- a/%s\n+++ b/%s\n[%s file changed; no body]\n"
                          % (rel, rel, kind))
            continue
        old_text = _read_text(old_path) if os.path.isfile(old_path) else None
        new_text = _read_text(new_path) if new_exists else None
        if new_exists and new_text is None:
            if old_text is None and not os.path.isfile(old_path):
                stats["added"] += 1
            else:
                stats["changed"] += 1
            changed_files.append(rel)
            chunks.append("--- a/%s\n+++ b/%s\n[binary file changed; no body]\n"
                          % (rel, rel))
            continue
        if old_text is None and not new_exists:
            continue
        if old_text == new_text:
            continue
        if old_text is None:
            stats["added"] += 1
        elif new_text is None:
            stats["deleted"] += 1
        else:
            stats["changed"] += 1
        changed_files.append(rel)
        old_lines = (old_text or "").splitlines(keepends=True)
        new_lines = (new_text or "").splitlines(keepends=True)
        fromfile = "a/%s" % rel if old_text is not None else "/dev/null"
        tofile = "b/%s" % rel if new_text is not None else "/dev/null"
        diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=fromfile,
                                         tofile=tofile, n=3))
        if not diff:
            diff = ["--- %s\n+++ %s\n[content changed but lines unavailable]\n"
                    % (fromfile, tofile)]
        body = "".join(diff)
        if new_text is None and len(body) > 20000:
            body = body[:20000] + "\n[diff truncated]\n"
            truncated = True
        chunks.append(body)
        current = "".join(chunks)
        if len(current) > max_chars:
            truncated = True
            break
    seen = {entry.get("path") if isinstance(entry, dict) else str(entry) for entry in files}
    baseline_root = os.path.abspath(str((baseline or {}).get("dir") or "")) if baseline else ""
    for rel, full in _iter_files(workspace):
        if rel in seen or len(changed_files) >= limit:
            continue
        if baseline_root and os.path.commonpath(
                [os.path.abspath(full), baseline_root]) == baseline_root:
            continue
        try:
            new_size = os.path.getsize(full)
        except OSError:
            continue
        text_new = None
        if new_size <= int(max_file_bytes):
            text_new = _read_text(full)
        if text_new is None:
            reason = "oversized" if new_size > int(max_file_bytes) else "binary"
            stats["added"] += 1
            changed_files.append(rel)
            chunks.append("--- /dev/null\n+++ b/%s\n[%s file added; no body]\n"
                          % (rel, reason))
            if len("".join(chunks)) > max_chars:
                truncated = True
                break
            continue
        stats["added"] += 1
        changed_files.append(rel)
        chunks.append("".join(
            difflib.unified_diff([], text_new.splitlines(keepends=True),
                                 fromfile="/dev/null", tofile="b/%s" % rel, n=3)))
        if len("".join(chunks)) > max_chars:
            truncated = True
            break
    text = "".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[diff context truncated]\n"
        truncated = True
    return {"text": text, "files": changed_files, "stats": stats, "truncated": truncated}


def review_focus_text(baseline, workspace, prior_defects=None, max_chars=60000,
                      round_no=1, prior_signatures=None,
                      max_file_bytes=262144):
    """Bounded R2/R3 reviewer block: connector diff plus targeted-round guidance."""
    max_chars = int(max_chars)
    header = ("## Connector diff (start here)\n\n"
              "The connector computed the authoritative diff below. Read surrounding "
              "context only for changed files; do not read the whole repo.\n\n")
    diff = diff_context(workspace, baseline,
                        max_chars=max(2000, max_chars - len(header) - 1500),
                        max_file_bytes=max_file_bytes)
    body = header + (diff["text"] or "(No text changes were detected.)\n")
    defects = [str(item).strip() for item in (prior_defects or []) if str(item).strip()]
    if defects:
        focus = ("\n## Round %d focus: previously flagged defects\n\n"
                 "Round 1 was a full review; this round is targeted. Verify each previously "
                 "flagged defect against the new diff first, then read only the changed "
                 "files and their immediate dependencies:\n\n%s\n"
                 % (round_no, "\n".join("- %s" % item for item in defects)))
        signatures = [str(item) for item in (prior_signatures or [])
                      if str(item).strip()]
        if signatures:
            focus += ("\nP3-2 defect signatures from the prior round: %s\n"
                      % ", ".join("`%s`" % item[:12] for item in signatures))
        body = focus + body
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[focus context truncated]\n"
    return body


def format_test_evidence(result):
    """Human-readable signed evidence block for the reviewer."""
    result = result or {}
    command = result.get("command")
    if isinstance(command, (list, tuple)):
        command = " ".join(str(part) for part in command)
    return (
        "## Signed test evidence (connector-run)\n\n"
        "- Status: %s\n"
        "- Command: `%s`\n"
        "- Tree SHA-256: `%s`\n"
        "- Tests: %s (failures: %s, errors: %s)\n"
        "- Output SHA-256: `%s`\n"
        "- Connector signature (HMAC-SHA256): `%s`\n"
        "- Key reference: `%s`\n"
        "- Tree changed while the suite ran: %s\n"
        "- Output tail:\n\n```\n%s\n```\n\n"
        "The reviewer may trust this evidence for this exact tree only. Do not trust it if "
        "the tree hash changed. Rerun only the new or defect-related tests; you may rerun "
        "everything if you are suspicious.\n"
        % (result.get("status"), command or "(none)", result.get("tree_sha256"),
           result.get("tests", 0), result.get("failures", 0), result.get("errors", 0),
           result.get("output_digest"), result.get("signature"),
           result.get("key_id") or "(in-memory test key)",
           "YES (rerun the affected tests)" if result.get("tree_changed")
           else "no", str(result.get("output_tail") or "")[:4000])
    )
