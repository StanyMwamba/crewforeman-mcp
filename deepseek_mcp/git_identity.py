"""Commit-identity enforcement and pre-push privacy gate.

The connector creates commits (pipeline install) and may create commits while
rebasing.  Both must use the configured ``git_identity`` even when the
machine's global git identity belongs to a person, so this module

* writes the configured identity to the repository-local git config and
  returns the ``-c user.name`` / ``-c user.email`` arguments used for the
  actual command (``commit_with_identity`` / ``rebase_with_identity``),
* implements the pre-push gate for commit metadata (author and committer of
  every commit in ``base..head``) and for added file content (home paths,
  private keys, API keys and personal email addresses).

CLI::

    python -m deepseek_mcp.git_identity --repo DIR --base X --head Y \
        [--remote origin] [--config PATH]

The exit code is non-zero when the gate blocks, so a git pre-push hook or CI
can call it directly.  Standard library only; Python 3.9 compatible.
"""

import argparse
import os
import re
import subprocess
import sys

DEFAULT_IDENTITY = {"name": "crewforeman-mcp", "email": "noreply@example.com"}

# Exception entries that contain '@' but look like SCP-style URLs are remote
# locations, not identities.
_REMOTE_MARKERS = ("://", "git@", "ssh:", "git:")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Content rules.  The synthetic example@example.com / user@example.com
# placeholders are deliberately allowed (all of example.com/org/net).
_CONTENT_RULES = (
    ("home_path",
     re.compile(r"(?:/Users/|/home/)[A-Za-z0-9._-]+/")),
    ("home_path",
     re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\")),
    ("private_key",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("api_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("api_key", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("api_key",
     re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|"
                r"auth[_-]?token|password)\b\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']")),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# One `git show` per pushed commit; a bounded, fail-closed cap keeps a push
# from turning the hook into an unbounded history walk.
MAX_SCAN_COMMITS = 10000
# Domains that only ever carry synthetic placeholders in this project.
_PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net",
                        "localhost")


class GitIdentityError(Exception):
    """Raised when git itself cannot be run for the gate."""


# Git prefers these over ``-c user.name`` / ``-c user.email``.  A caller's
# environment must never be able to author or commit with a personal identity
# when the connector is creating a commit or rebase.
_IDENTITY_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def _git(repo, args, timeout=120):
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("LC_ALL", "C")
    for name in _IDENTITY_ENV:
        env.pop(name, None)
    # ``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n`` can inject a competing
    # user.email or core.hooksPath without touching any config file.
    for name in list(env):
        if (name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                     "GIT_INDEX_FILE")
                or name.startswith("GIT_CONFIG_KEY_")
                or name.startswith("GIT_CONFIG_VALUE_")):
            env.pop(name, None)
    try:
        proc = subprocess.run(
            ["git"] + [str(a) for a in args],
            cwd=str(repo) if repo else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "git_not_installed"
    except subprocess.TimeoutExpired:
        return 124, "", "git_timeout"
    except Exception as exc:  # noqa: BLE001 - never crash the caller
        return 125, "", str(exc)


def _cfg_get(cfg, key, default=None):
    """Read *key* from a dict, a Config hybrid, or any mapping-like object."""
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                return default
        except Exception:
            return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return default


def _scan_cap(cfg=None):
    """Maximum commits one gate run will inspect; configurable, fail closed."""
    try:
        value = int(_cfg_get(cfg, "git_identity_max_scan_commits",
                             MAX_SCAN_COMMITS))
    except (TypeError, ValueError):
        value = MAX_SCAN_COMMITS
    return max(1, value)


def identity_from_config(cfg=None):
    """Return ``{"name": ..., "email": ...}`` from the ``git_identity`` key."""

    raw = {}
    candidate = _cfg_get(cfg, "git_identity")
    if isinstance(candidate, dict):
        raw = candidate
    name = str(raw.get("name") or DEFAULT_IDENTITY["name"]).strip()
    email = str(raw.get("email") or DEFAULT_IDENTITY["email"]).strip()
    return {
        "name": name or DEFAULT_IDENTITY["name"],
        "email": email or DEFAULT_IDENTITY["email"],
    }


def exceptions_from_config(cfg=None):
    """Return the configured ``git_identity_exceptions`` entries as a list."""

    value = _cfg_get(cfg, "git_identity_exceptions")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalise_email(value):
    return str(value or "").strip().lower()


def allowed_names(cfg=None):
    """Names accepted by the metadata gate.

    The configured identity name and the documented default are the only
    names; an operator can widen the allowlist through ``git_identity.name``.
    """

    return {DEFAULT_IDENTITY["name"].lower(),
            identity_from_config(cfg)["name"].strip().lower()}


def allowed_emails(cfg=None):
    """Emails accepted by the metadata gate.

    Only the configured identity email and the documented default.  Exception
    entries are *remotes*; an email-shaped entry must not widen the global
    allowlist for every push (it would silently permit a personal address).
    """

    allowed = {_normalise_email(DEFAULT_IDENTITY["email"])}
    allowed.add(_normalise_email(identity_from_config(cfg)["email"]))
    return allowed


def commit_identity_args(cfg=None):
    """``git -c ...`` arguments that pin the identity for one command."""

    ident = identity_from_config(cfg)
    return ["-c", "user.name=" + ident["name"],
            "-c", "user.email=" + ident["email"]]


def apply_identity(repo, cfg=None):
    """Write the identity to the repository-local git config.

    Returns ``(ok, detail)``.  Setting ``--local`` values is what stops a
    global identity from leaking into commits or rebases run later.
    """

    ident = identity_from_config(cfg)
    for key, value in (("user.name", ident["name"]),
                       ("user.email", ident["email"])):
        rc, _out, err = _git(repo, ["config", "--local", key, value])
        if rc != 0:
            return False, (err or "git_config_failed").strip()
    # Any later `git push` from this clone must hit the same gate, even when
    # the operator never ran `git config core.hooksPath scripts/hooks`.
    ok, detail = ensure_pre_push_hook(repo, cfg)
    if not ok:
        return False, detail
    return True, ""


_PRE_PUSH_MARKER = "# CrewForeman pre-push gate (auto-installed)"
_PRE_PUSH_HOOK = r"""#!/bin/sh
# CrewForeman pre-push gate (auto-installed)
#
# The connector installs this hook in every repository where it creates a
# commit or rebase.  It scans the commit METADATA (author and committer
# name+email) and the added file content of every pushed commit, and blocks
# the push when anything is outside the allowlist.
#
# An operator's previous hook is preserved as ``pre-push.local`` in this
# directory and runs first; it is never discarded.
zero="0000000000000000000000000000000000000000"
HOOK_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
refs="$HOOK_DIR/.pre-push-refs.$$"
trap 'rm -f "$refs"' EXIT HUP INT TERM
cat > "$refs" || exit 1
status=0
if [ -x "$HOOK_DIR/pre-push.local" ]; then
    "$HOOK_DIR/pre-push.local" "$@" < "$refs" || status=1
fi
PYTHON="${CF_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "pre-push: no python interpreter found; refusing." >&2
    exit 1
fi
CF_PYTHONPATH="__CF_SOURCE__"
if [ -n "${PYTHONPATH:-}" ]; then
    CF_PYTHONPATH="$CF_PYTHONPATH__CF_SEP__$PYTHONPATH"
fi
export PYTHONPATH="$CF_PYTHONPATH"
remote="$1"
while read local_ref local_sha remote_ref remote_sha; do
    [ "$local_sha" = "$zero" ] && continue
    base="$remote_sha"
    case "$base" in
        "$zero"|"") base=$(git merge-base "$local_sha" origin/main 2>/dev/null) || base="" ;;
    esac
    # An empty base scans the full pushed history (new branch, no origin/main);
    # refusing would make an ordinary push impossible.
    if ! "$PYTHON" -m deepseek_mcp.git_identity \
            --repo . --base "$base" --head "$local_sha" --remote "$remote"; then
        status=1
    fi
done < "$refs"
if [ "$status" -ne 0 ]; then
    echo "pre-push: blocked by the CrewForeman privacy/identity gate." >&2
fi
exit "$status"
"""


def ensure_pre_push_hook(repo, cfg=None):
    """Install the pre-push gate in *repo* without discarding an operator hook.

    A previous ``pre-push`` hook is moved to ``pre-push.local`` (once) and the
    installed gate runs it first, so the connector never silently bypasses an
    operator's policy.  Returns ``(ok, detail)``.
    """

    rc, out, err = _git(repo, ["rev-parse", "--git-path", "hooks"])
    if rc != 0:
        return False, (err or "git_hooks_path_failed").strip()
    hooks = out.strip()
    if not hooks:
        return False, "git_hooks_path_empty"
    if not os.path.isabs(hooks):
        hooks = os.path.join(str(repo), hooks)
    path = os.path.join(hooks, "pre-push")
    local = path + ".local"
    source = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hook = (_PRE_PUSH_HOOK.replace("__CF_SOURCE__", source)
            .replace("__CF_SEP__", os.pathsep))
    try:
        os.makedirs(hooks, exist_ok=True)
        existing = ""
        chained = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                existing = handle.read()
        if existing and _PRE_PUSH_MARKER not in existing:
            if os.path.exists(local):
                with open(local, "r", encoding="utf-8",
                          errors="replace") as handle:
                    previous = handle.read()
                if previous != existing:
                    return False, "existing_hook_conflict"
            else:
                os.replace(path, local)
            chained = True
        if existing.strip() == hook.strip():
            return True, "chained" if chained else "already_installed"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(hook)
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        return True, "chained" if chained else "installed"
    except OSError as exc:
        return False, "hook_install_failed:%s" % (exc,)


def push_with_gate(cfg, repo, remote, refspec=None, base="", head="HEAD",
                   extra_args=None):
    """Push only after the connector's metadata/content gate passes.

    This is the connector-controlled push path used by release tooling: a
    violation returns ``(1, "", message)`` and no ``git push`` runs.
    """

    if not base:
        rc, out, _err = _git(
            repo, ["merge-base", str(head), "refs/remotes/%s/HEAD" % remote])
        if rc == 0 and out.strip():
            base = out.strip()
    report = check_push(cfg, repo=repo, base=base, head=head, remote=remote)
    if not report.get("ok"):
        detail = "; ".join(report.get("messages") or [])
        return 1, "", detail or "identity_gate_blocked"
    args = ["push", str(remote)]
    if refspec:
        if isinstance(refspec, (list, tuple)):
            args.extend(str(item) for item in refspec)
        else:
            args.append(str(refspec))
    if extra_args:
        args.extend(str(item) for item in extra_args)
    return _git(repo, args)


def commit_with_identity(cfg, repo, message, extra_args=None):
    """Apply the identity then ``git commit`` with explicit ``-c`` args."""

    ok, detail = apply_identity(repo, cfg)
    if not ok:
        return 1, "", detail
    args = commit_identity_args(cfg) + ["commit", "-m", str(message)]
    if extra_args:
        args.extend(str(a) for a in extra_args)
    return _git(repo, args)


def rebase_with_identity(cfg, repo, upstream, extra_args=None):
    """Apply the identity then rebase; the committer identity cannot leak."""

    ok, detail = apply_identity(repo, cfg)
    if not ok:
        return 1, "", detail
    args = commit_identity_args(cfg) + ["rebase"] + [str(upstream)]
    if extra_args:
        args.extend(str(a) for a in extra_args)
    return _git(repo, args)


def rebase_continue_with_identity(cfg, repo):
    """Continue an in-progress rebase with the configured identity."""

    ok, detail = apply_identity(repo, cfg)
    if not ok:
        return 1, "", detail
    return _git(repo, commit_identity_args(cfg) + ["rebase", "--continue"])


def _remote_candidates(repo, remote):
    """Return the resolved URLs for *remote* (never the user-local name).

    A remote name is a local label: `git remote add origin <new-url>` would
    let a bare ``origin`` exception exempt an arbitrary destination.  Exact
    URL/owner-repo matching is the safe unit.
    """
    values = []
    if remote:
        rc, out, _err = _git(repo, ["remote", "get-url", str(remote)])
        if rc == 0 and out.strip():
            values.append(out.strip())
    return values


def _remote_path(url):
    """Normalise a git remote URL to a lower-case ``host/path`` string.

    Handles URL and SCP-style forms, and leaves a plain local path alone so
    an exact-match comparison still works for file remotes.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if "://" in text:
        text = text.split("://", 1)[1]
    elif "@" in text and ":" in text.split("@", 1)[1]:
        # SCP-style git@host:owner/repo
        host, path = text.split("@", 1)[1].split(":", 1)
        return (host + "/" + path).strip("/").lower()
    if "@" in text:
        text = text.split("@", 1)[1]
    return text.lower().strip("/")


def exception_for_remote(cfg, repo, remote):
    """Return the exception entry matching this remote, or ``""``.

    Matching is EXACT, not a substring: an entry must be ``owner/repo`` or a
    full URL.  A bare remote name is not accepted because it is a local label
    that can be re-pointed at another URL; a lookalike path cannot borrow an
    exception either.
    """

    entries = exceptions_from_config(cfg)
    if not entries or not remote:
        return ""
    values = list(_remote_candidates(repo, remote))
    candidates = set()
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        lowered = raw.rstrip("/").lower()
        candidates.add(lowered)
        if lowered.endswith(".git"):
            candidates.add(lowered[:-4])
        path = _remote_path(raw)
        if path:
            candidates.add(path)
            # owner/repo is the last two path segments (GitHub/GitLab style).
            segments = [part for part in path.split("/") if part]
            if len(segments) >= 2:
                candidates.add("/".join(segments[-2:]))
    for entry in entries:
        needle = entry.strip().lower().rstrip("/")
        if needle.endswith(".git"):
            needle = needle[:-4]
        if "://" in needle or "@" in needle:
            parsed = _remote_path(needle)
            if parsed:
                needle = parsed
        if needle in candidates:
            return entry
    return ""


def _rev_list(repo, base, head):
    if not head:
        raise GitIdentityError("missing_head")
    if base:
        rc, out, err = _git(repo, ["rev-list", "--reverse", base + ".." + head])
    else:
        rc, out, err = _git(repo, ["rev-list", "--reverse", head])
    if rc != 0:
        raise GitIdentityError((err or "git_rev_list_failed").strip())
    return out.split()


def commit_metadata(repo, base, head, cfg=None):
    """Author and committer metadata for every commit in ``base..head``."""

    commits = []
    shas = _rev_list(repo, base, head)
    if len(shas) > _scan_cap(cfg):
        raise GitIdentityError(
            "too_many_commits:%d (raise git_identity_max_scan_commits)"
            % len(shas))
    for sha in shas:
        rc, out, err = _git(
            repo,
            ["show", "-s", "--no-color",
             "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", sha],
        )
        if rc != 0:
            raise GitIdentityError((err or "git_show_failed").strip())
        parts = out.rstrip("\n").split("\x00")
        if len(parts) < 5:
            raise GitIdentityError("unparsable_git_log_record")
        commits.append({
            "sha": parts[0],
            "author_name": parts[1],
            "author_email": parts[2],
            "committer_name": parts[3],
            "committer_email": parts[4],
        })
    return commits


def scan_metadata(repo, base, head, cfg=None):
    """Return one violation per author/committer name or email off the allowlist."""

    allowed = allowed_emails(cfg)
    names = allowed_names(cfg)
    violations = []
    for commit in commit_metadata(repo, base, head, cfg):
        for role in ("author", "committer"):
            name = commit[role + "_name"]
            email = _normalise_email(commit[role + "_email"])
            identity = "%s <%s>" % (name, commit[role + "_email"])
            if email not in allowed:
                violations.append({
                    "kind": "metadata", "field": "email",
                    "sha": commit["sha"], "role": role,
                    "name": name, "email": commit[role + "_email"],
                    "identity": identity,
                })
            elif str(name or "").strip().lower() not in names:
                violations.append({
                    "kind": "metadata", "field": "name",
                    "sha": commit["sha"], "role": role,
                    "name": name, "email": commit[role + "_email"],
                    "identity": identity,
                })
    return violations


def _added_lines(diff_text):
    line_no = 0
    for line in diff_text.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            line_no = int(match.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            yield line_no, line[1:]
            line_no += 1
        elif line.startswith("-"):
            continue
        elif line_no:
            line_no += 1


def _mask(value):
    text = str(value or "")
    if len(text) <= 8:
        return "***"
    return text[:4] + "..." + text[-2:]


def _email_allowed_in_content(email, cfg):
    lowered = _normalise_email(email)
    if lowered in allowed_emails(cfg):
        return True
    domain = lowered.rsplit("@", 1)[-1]
    return any(domain == item or domain.endswith("." + item)
               for item in _PLACEHOLDER_DOMAINS)


def _scan_added_diff(out, sha, cfg, violations):
    """Append violations for the added lines of one commit diff."""
    for line_no, text in _added_lines(out):
        # A '+' line is scanned once per rule; report the first hit per rule
        # so one long line cannot flood the report.
        for rule, pattern in _CONTENT_RULES:
            match = pattern.search(text)
            if match:
                violations.append({
                    "kind": "content",
                    "rule": rule,
                    "commit": sha[:12],
                    "line": line_no,
                    "match": _mask(match.group(0)),
                })
                break
        else:
            for match in _EMAIL_RE.finditer(text):
                if not _email_allowed_in_content(match.group(0), cfg):
                    violations.append({
                        "kind": "content",
                        "rule": "email",
                        "commit": sha[:12],
                        "line": line_no,
                        "match": _mask(match.group(0)),
                    })
                    break


def scan_content(repo, base, head, cfg=None):
    """Scan every pushed commit's added lines, not just the net diff.

    A secret added in one commit and removed in a later one is absent from
    ``base..head`` but remains in the pushed history.  Each commit is scanned
    separately so that such a leak cannot pass the gate.
    """

    if not head:
        return []
    violations = []
    commits = _rev_list(repo, base, head)
    # A new branch can carry a long history; bound the work and fail closed
    # rather than silently scanning only part of it.
    if len(commits) > _scan_cap(cfg):
        return [{
            "kind": "error", "sha": "", "role": "", "name": "",
            "email": "",
            "detail": ("too_many_commits:%d (raise "
                       "git_identity_max_scan_commits)" % len(commits)),
        }]
    for sha in commits:
        rc, out, err = _git(
            repo, ["show", "--no-color", "--unified=0", "--format=", sha])
        if rc != 0:
            raise GitIdentityError((err or "git_show_failed").strip())
        _scan_added_diff(out, sha, cfg, violations)
    return violations


def _violation_message(violation):
    if violation["kind"] == "metadata":
        return ("commit %s %s identity %s <%s> is not on the allowlist"
                % (violation["sha"][:12], violation["role"],
                   violation["name"], violation["email"]))
    return ("line %s: %s looks like a secret (%s)"
            % (violation["line"], violation["rule"], violation["match"]))


def check_push(cfg=None, repo=".", base="", head="HEAD", remote=None):
    """Run the full pre-push gate and return a JSON-friendly report.

    ``ok`` is False when commit metadata is off the allowlist (unless the
    remote matches an exception) or when added content contains a secret.
    """

    report = {
        "ok": True,
        "base": base,
        "head": head,
        "remote": remote or "",
        "identity": identity_from_config(cfg),
        "exception": "",
        "metadata": [],
        "violations": [],
        "messages": [],
    }
    try:
        # A gate must fail closed on an unresolvable head: an empty range used
        # to report OK with zero commits scanned.
        if not str(head or "").strip():
            raise GitIdentityError("missing_head")
        rc, out, err = _git(
            repo, ["rev-parse", "--verify", str(head) + "^{commit}"])
        if rc != 0 or not out.strip():
            raise GitIdentityError(
                (err or "head_not_resolvable").strip())
        head = out.strip()
        report["head"] = head
        report["metadata"] = commit_metadata(repo, base, head, cfg)
        exception = exception_for_remote(cfg, repo, remote)
        report["exception"] = exception
        if not exception:
            report["violations"].extend(scan_metadata(repo, base, head, cfg))
        report["violations"].extend(scan_content(repo, base, head, cfg))
    except GitIdentityError as exc:
        report["violations"].append({
            "kind": "error",
            "sha": "",
            "role": "",
            "name": "",
            "email": "",
            "detail": str(exc),
        })
    except Exception as exc:  # noqa: BLE001 - fail closed, never crash a push
        report["violations"].append({
            "kind": "error",
            "sha": "",
            "role": "",
            "name": "",
            "email": "",
            "detail": str(exc),
        })

    report["ok"] = not report["violations"]
    if not report["ok"]:
        for violation in report["violations"]:
            if violation["kind"] == "error":
                report["messages"].append(
                    "git-identity gate could not verify the range: "
                    + violation.get("detail", ""))
            else:
                report["messages"].append(_violation_message(violation))
    return report


def _cli_config(path=None):
    try:
        from .config import load_config
    except Exception:  # pragma: no cover - direct script execution
        return {}
    try:
        cfg = load_config(path) if path else load_config()
    except TypeError:
        cfg = load_config()
    except Exception:
        return {}
    # ``load_config`` returns a Config hybrid (mapping-like, not a dict); the
    # gate must read it or a configured identity/exception list is ignored.
    return cfg if cfg is not None else {}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m deepseek_mcp.git_identity",
        description="Block pushes whose commit metadata or content leaks "
                    "a personal identity or secret.",
    )
    parser.add_argument("--repo", default=".", help="repository path")
    parser.add_argument("--base", default="", help="range base (exclusive)")
    parser.add_argument("--head", default="HEAD", help="range head")
    parser.add_argument("--remote", default=None,
                        help="remote name, checked against exceptions")
    parser.add_argument("--config", default=None,
                        help="optional config.json path")
    args = parser.parse_args(argv)

    cfg = _cli_config(args.config)
    report = check_push(cfg, repo=args.repo, base=args.base, head=args.head,
                        remote=args.remote)
    if report["ok"]:
        print("git-identity gate: OK (%d commit(s), %d exception)"
              % (len(report["metadata"]), 1 if report["exception"] else 0))
        return 0
    for message in report["messages"]:
        print("git-identity gate: BLOCKED: " + message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
