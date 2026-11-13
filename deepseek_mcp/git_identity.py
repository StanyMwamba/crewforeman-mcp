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

A remote listed in ``git_identity_exceptions`` is matched exactly after URL
normalisation and never skips the metadata scan: it replaces the global
allowlist with only the repository's own repo-local ``(name, email)`` pair,
and every other identity still blocks.

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
from urllib.parse import urlsplit

DEFAULT_IDENTITY = {"name": "crewforeman-mcp", "email": "noreply@example.com"}

# Remote locations are normalised to ``host/path`` (lower-case host, path
# without a trailing ``.git``).  A bare ``owner/repo`` exception is GitHub.com
# shorthand only; there is deliberately no host-agnostic or substring match.
_GITHUB_HOST = "github.com"
_GIT_SUFFIX = ".git"
_REMOTE_SCHEMES = frozenset({
    "http", "https", "ssh", "git", "git+ssh", "ssh+git", "ftp", "ftps",
})
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_RE = re.compile(
    r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^@/:\s]+):(?P<path>[^\s]+)$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# A merge conflict is shown as a combined diff: @@@ with one -range per parent
# and multiple marker columns on each content line.
_COMBINED_HUNK_RE = re.compile(
    r"^@{3,}(?: -\d+(?:,\d+)?)+ \+(\d+)(?:,\d+)? @{3,}")

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


def _strip_git_suffix(path):
    """Strip slashes and a trailing ``.git`` without changing path case."""

    text = str(path or "").strip().strip("/")
    if text.lower().endswith(_GIT_SUFFIX):
        text = text[:-len(_GIT_SUFFIX)]
    return text.strip("/")


def _canonical_url(value, strict=False):
    """Return ``host[/:port]/path`` for a git URL, or ``""`` when invalid."""

    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (parsed.scheme or "").lower() not in _REMOTE_SCHEMES:
        return ""
    if strict and (parsed.query or parsed.fragment):
        return ""
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None:
        host = "%s:%d" % (host, port)
    path = _strip_git_suffix(parsed.path)
    if not path:
        return ""
    return host + "/" + path


def _canonical_scp(value):
    """Return ``host/path`` for ``[user@]host:path``, or ``""``."""

    text = str(value or "").strip()
    if not text or _DRIVE_RE.match(text):
        return ""
    match = _SCP_RE.match(text)
    if not match:
        return ""
    host = match.group("host").strip().lower()
    path = _strip_git_suffix(match.group("path"))
    if not host or not path:
        return ""
    return host + "/" + path


def _canonical_candidate(value):
    """Normalise any remote location for exact comparison.

    A plain local path is prefixed with ``local:`` so a three-segment path
    such as ``github.com/owner/repo`` can never masquerade as a host/path
    canonical form; only URL/SCP candidates carry a host.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return _canonical_url(text, strict=True)
    if _SCP_RE.match(text) and not _DRIVE_RE.match(text):
        return _canonical_scp(text)
    text = text.rstrip("/")
    if text.lower().endswith(_GIT_SUFFIX):
        text = text[:-len(_GIT_SUFFIX)]
    return "local:" + text


def _canonical_exception_entry(entry):
    """Normalise an exception entry, or return ``""`` when it is invalid.

    A full URL/SCP entry keeps its exact normalised host and path.  A bare
    entry is valid only as exactly two path segments and then means the
    GitHub.com shorthand ``github.com/owner/repo``.  Bare remote names,
    email-shaped values, lookalike hosts and 1/3+ segment paths match nothing.
    """

    text = str(entry or "").strip()
    if not text:
        return ""
    if "://" in text:
        return _canonical_url(text, strict=True)
    if _SCP_RE.match(text) and not _DRIVE_RE.match(text):
        return _canonical_scp(text)
    if "@" in text or ":" in text or "\\" in text or text.startswith("/"):
        return ""
    parts = text.split("/")
    if len(parts) != 2:
        return ""
    owner, repo = parts
    if not owner or not repo:
        return ""
    if owner in (".", "..") or repo in (".", ".."):
        return ""
    if not _SAFE_SEGMENT_RE.match(owner) or not _SAFE_SEGMENT_RE.match(repo):
        return ""
    repo = _strip_git_suffix(repo)
    if not repo:
        return ""
    return "%s/%s/%s" % (_GITHUB_HOST, owner, repo)


def _looks_like_location(value):
    """True when *value* is a URL/SCP/path, not a bare remote name."""

    text = str(value or "").strip()
    if not text:
        return False
    if "://" in text:
        return True
    if text.startswith(("/", "./", "../", "~")):
        return True
    if "/" in text or "\\" in text:
        return True
    return bool(_SCP_RE.match(text) and not _DRIVE_RE.match(text))


def allowed_identities(cfg=None):
    """Allowed ``(lower-case name, lower-case email)`` pairs.

    The pair is one unit: a default address with a foreign name is not an
    allowed identity, and a foreign address with an allowed name is not one
    either.
    """

    ident = identity_from_config(cfg)
    return {
        (DEFAULT_IDENTITY["name"].strip().lower(),
         _normalise_email(DEFAULT_IDENTITY["email"])),
        (ident["name"].strip().lower(), _normalise_email(ident["email"])),
    }


def allowed_names(cfg=None):
    """Names accepted by the metadata gate (derived from allowed pairs)."""

    return {name for name, _email in allowed_identities(cfg)}


def allowed_emails(cfg=None):
    """Emails accepted by the metadata gate (derived from allowed pairs).

    Only the configured identity email and the documented default.  Exception
    entries are *remotes*; an email-shaped entry must not widen the global
    allowlist for every push (it would silently permit a personal address).
    """

    return {email for _name, email in allowed_identities(cfg)}


def repo_local_identity(repo):
    """Return the repo-local ``{"name", "email"}`` pair, or ``None``.

    An exception remote may only allow the repository's own configured
    identity.  If either repo-local value is missing there is no such pair,
    so the exception grants nothing and the gate keeps blocking.
    """

    rc, out, _err = _git(repo, ["config", "--local", "--get", "user.name"])
    if rc != 0 or not out.strip():
        return None
    name = out.strip()
    rc, out, _err = _git(repo, ["config", "--local", "--get", "user.email"])
    if rc != 0 or not out.strip():
        return None
    return {"name": name, "email": out.strip()}


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


_PRE_PUSH_SHARED_MARKER = "# CrewForeman pre-push gate"
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
# Strip CR defensively: a CRLF refs stream (or a Windows text-mode reader)
# would otherwise leave a trailing CR on the sha and break the gate's range.
tr -d '\r' > "$refs" || exit 1
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
GATE_ROOT="__CF_SOURCE__"
REPO=$(git rev-parse --show-toplevel 2>/dev/null) || REPO=$(pwd -P)
remote="$1"
while read -r local_ref local_sha remote_ref remote_sha; do
    local_sha=$(printf '%s' "$local_sha" | tr -d '\r')
    remote_sha=$(printf '%s' "$remote_sha" | tr -d '\r')
    [ -z "$local_ref" ] && continue
    [ "$local_sha" = "$zero" ] && continue
    base="$remote_sha"
    # A new branch has no advertised old sha.  Scan the full history; never
    # merge-base against a remote-tracking ref (it can be stale after the
    # remote is re-pointed or rewound, which would hide pushed commits).
    [ "$base" = "$zero" ] && base=""
    # Bootstrap: insert the absolute connector root at sys.path[0] before the
    # import, so a deepseek_mcp package in the pushed repo (cwd) cannot
    # shadow the real gate.
    if ! "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from deepseek_mcp.git_identity import main; sys.exit(main(sys.argv[2:]))' \
            "$GATE_ROOT" --repo "$REPO" --base "$base" --head "$local_sha" \
            --remote "$remote"; then
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

    A previous operator ``pre-push`` hook is moved to ``pre-push.local``
    (once) and the installed gate runs it first, so the connector never
    silently bypasses an operator's policy.  One of our own gate copies (the
    checked-in ``scripts/hooks/pre-push`` under ``core.hooksPath``, or any
    copy carrying the shared marker) is recognised as ours and is never
    moved or overwritten, which would dirty a tracked tree.  Returns
    ``(ok, detail)``.
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
        existing_is_ours = bool(
            existing and _PRE_PUSH_SHARED_MARKER in existing)
        if existing and not existing_is_ours:
            if os.path.exists(local):
                with open(local, "r", encoding="utf-8",
                          errors="replace") as handle:
                    previous = handle.read()
                if previous != existing:
                    return False, "existing_hook_conflict"
            else:
                os.replace(path, local)
            chained = True
        if existing_is_ours and _PRE_PUSH_MARKER not in existing:
            # Checked-in or otherwise unmanaged gate copy: it may be tracked
            # (core.hooksPath=scripts/hooks), so never move or rewrite it.
            return True, "checked_in"
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


# ``git push`` flags the connector may forward.  Anything else is rejected:
# ``--no-verify`` would skip the installed hook, while --force/--all/--tags/
# --mirror/--prune/--repo would rewrite or add refs the gate did not derive
# from the requested refspecs.  ``-n`` is --dry-run for push (not no-verify).
_PUSH_ALLOWED_FLAGS = frozenset({
    "--no-gpg-sign", "--dry-run", "-n", "--porcelain", "--progress",
    "--no-progress", "--verbose", "-v", "--quiet", "-q", "--atomic",
    "--no-atomic", "--thin", "--no-thin", "--ipv4", "--ipv6", "--signed",
    "--no-signed",
})
_PUSH_DELETE_FLAGS = frozenset({"--delete", "-d"})


def validate_push_args(extra_args=None):
    """Return the push arguments after rejecting gate/ref rewriting flags.

    The connector never calls ``git push`` with ``--no-verify``.  Unknown
    options fail closed rather than being forwarded blindly, because a
    positional argument or a ref-expanding flag would push commits the gate
    never scanned.
    """

    result = []
    if isinstance(extra_args, str):
        extra_args = [extra_args]
    for raw in extra_args or ():
        arg = str(raw or "").strip()
        if not arg:
            raise GitIdentityError("empty_push_argument")
        if arg in _PUSH_DELETE_FLAGS or arg in _PUSH_ALLOWED_FLAGS:
            result.append(arg)
            continue
        raise GitIdentityError("unsupported_push_argument:%s" % arg)
    return result


def _refspec_list(refspec):
    if refspec is None:
        return []
    if isinstance(refspec, (list, tuple)):
        items = [str(item).strip() for item in refspec]
    else:
        items = [str(refspec).strip()]
    return [item for item in items if item]


def _resolve_commit(repo, rev):
    rc, out, _err = _git(
        repo, ["rev-parse", "--verify", "--quiet", str(rev) + "^{commit}"])
    if rc != 0 or not out.strip():
        return ""
    return out.strip().splitlines()[0].strip()


def _current_branch_ref(repo):
    rc, out, _err = _git(repo, ["symbolic-ref", "--quiet", "HEAD"])
    if rc == 0 and out.strip().startswith("refs/heads/"):
        return out.strip()
    return ""


def _local_ref_kind(repo, src):
    """Return ``branch``/``tag``/``other`` for a refspec source side."""

    if src == "HEAD":
        return "branch" if _current_branch_ref(repo) else "other"
    if src.startswith("refs/heads/"):
        return "branch"
    if src.startswith("refs/tags/"):
        return "tag"
    if src.startswith("refs/"):
        return "other"
    branch = _resolve_commit(repo, "refs/heads/" + src)
    tag = _resolve_commit(repo, "refs/tags/" + src)
    if branch and tag:
        return "ambiguous"
    if branch:
        return "branch"
    if tag:
        return "tag"
    return "other"


def _destination_ref(repo, src, dst, kind):
    """Best-effort destination ref, or ``""`` when it cannot be derived."""

    if dst:
        if dst.startswith("refs/"):
            return dst
        if kind == "branch":
            return "refs/heads/" + dst
        if kind == "tag":
            return "refs/tags/" + dst
        return ""
    if kind == "branch":
        if src == "HEAD":
            return _current_branch_ref(repo)
        if src.startswith("refs/heads/"):
            return src
        return "refs/heads/" + src
    if kind == "tag":
        if src.startswith("refs/tags/"):
            return src
        return "refs/tags/" + src
    if src.startswith("refs/"):
        return src
    return ""


def _is_ancestor(repo, ancestor, descendant):
    """True when *ancestor* is reachable from *descendant* (fast-forward)."""

    if not ancestor or not descendant:
        return False
    rc, _out, _err = _git(
        repo, ["merge-base", "--is-ancestor", str(ancestor), str(descendant)])
    return rc == 0


def _advertised_old(repo, remote, dst_ref):
    """Old destination sha from ``git ls-remote``, or ``""`` when unknown.

    A remote-tracking ref can be stale after the remote is re-pointed or
    rewound, which would make the gate scan an empty range while ``git push``
    sends the full history.  The destination's advertised value at push time
    is authoritative.  Any error, missing ref, ambiguous candidate or object
    not present locally means "unknown", and the caller scans the full local
    history (never fail-open).
    """

    if not dst_ref or not remote:
        return ""
    if not _looks_like_location(remote):
        # Remote-tracking refs follow the FETCH url.  If this named remote has
        # a different pushurl, the advertised old value for the actual push
        # destination is not known here; scan the full history instead.
        push_urls = _remote_urls(repo, remote, push=True)
        fetch_urls = _remote_urls(repo, remote, push=False)
        if not push_urls or not fetch_urls or set(push_urls) != set(fetch_urls):
            return ""
    rc, out, _err = _git(
        repo, ["ls-remote", "--refs", str(remote), str(dst_ref)])
    if rc != 0 or not out.strip():
        return ""
    matches = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return ""
        sha, ref = fields
        if ref == dst_ref:
            matches.append(sha)
    if len(matches) != 1:
        return ""
    old = _resolve_commit(repo, matches[0])
    return old or ""


def _refspec_ranges(repo, remote, refspec, delete_mode):
    """Parse every refspec into an exact gate range; fail closed on any gap."""

    specs = _refspec_list(refspec)
    if not specs:
        raise GitIdentityError("missing_refspec")
    ranges = []
    deletions = []
    for spec in specs:
        if spec.startswith("-"):
            raise GitIdentityError("invalid_refspec:%s" % spec)
        if spec.startswith("+"):
            raise GitIdentityError("forced_refspec_not_allowed:%s" % spec)
        if "*" in spec:
            raise GitIdentityError("wildcard_refspec_not_allowed:%s" % spec)
        if delete_mode:
            if ":" in spec:
                raise GitIdentityError("delete_refspec_malformed:%s" % spec)
            deletions.append(spec)
            continue
        if ":" in spec:
            src, dst = spec.split(":", 1)
            if not src:
                if not dst:
                    raise GitIdentityError("invalid_refspec:%s" % spec)
                deletions.append(dst)
                continue
            if not dst:
                raise GitIdentityError("invalid_refspec:%s" % spec)
        else:
            src, dst = spec, ""
        head_sha = _resolve_commit(repo, src)
        if not head_sha:
            raise GitIdentityError("refspec_not_resolvable:%s" % spec)
        kind = _local_ref_kind(repo, src)
        if kind == "ambiguous":
            raise GitIdentityError("ambiguous_refspec:%s" % spec)
        dst_ref = _destination_ref(repo, src, dst, kind)
        if not dst_ref:
            # The push is materialised as <scanned-sha>:<dst_ref>; without a
            # destination ref we would have to re-resolve a mutable name.
            raise GitIdentityError("refspec_destination_unresolved:%s" % spec)
        old_sha = _advertised_old(repo, remote, dst_ref)
        if old_sha and not _is_ancestor(repo, old_sha, head_sha):
            # A non-fast-forward would be rejected by git, but the gate must
            # still scan what a forced/rewound destination would receive.
            old_sha = ""
        ranges.append({
            "refspec": spec,
            "src": src,
            "dst": dst,
            "dst_ref": dst_ref,
            "head": head_sha,
            # An unknown or divergent advertised old value scans the whole
            # history of the local commit instead of trusting a stale ref.
            "base": old_sha or "",
        })
    if not ranges and not deletions:
        raise GitIdentityError("no_refspec")
    return ranges, deletions


def push_with_gate(cfg, repo, remote, refspec=None, base="", head="HEAD",
                   extra_args=None):
    """Push only after the connector's metadata/content gate passes.

    This is the connector-controlled push path used by release tooling: a
    violation returns a non-zero status and no ``git push`` runs.  The gate
    range is derived from *every* refspec being pushed (never blindly from
    ``HEAD``), using the destination's advertised ref from ``git ls-remote``
    as the old value and the full history when that value is unknown.  A
    refspec that cannot be parsed, resolved or mapped to a destination ref is
    rejected before any push.

    After the gate passes, each non-delete refspec is materialised as
    ``<scanned-sha>:<dst_ref>`` (deletions as ``:<dst_ref>``), so a concurrent
    ``update-ref`` cannot make ``git push`` send a commit the gate did not
    inspect.

    ``base``/``head`` are accepted for callers of older releases but are not
    trusted: a caller-supplied base could narrow the scan below the actual
    remote old value.
    """

    try:
        remote_text = str(remote or "").strip()
        if not remote_text:
            raise GitIdentityError("missing_remote")
        if remote_text.startswith("-"):
            # Otherwise git would parse the "remote" as an option such as
            # --no-verify or --repo and the push would not be what we gated.
            raise GitIdentityError("invalid_remote:%s" % remote_text)
        remote = remote_text
        extra = validate_push_args(extra_args)
        delete_mode = any(arg in _PUSH_DELETE_FLAGS for arg in extra)
        ranges, deletions = _refspec_ranges(
            repo, remote, refspec, delete_mode)
    except GitIdentityError as exc:
        return 1, "", "identity_gate_blocked:%s" % exc

    messages = []
    for item in ranges:
        try:
            report = check_push(cfg, repo=repo, base=item["base"],
                                head=item["head"], remote=remote)
        except Exception as exc:  # noqa: BLE001 - never push on a gate error
            messages.append("%s: identity_gate_error:%s"
                            % (item["refspec"], exc))
            continue
        if not isinstance(report, dict) or not report.get("ok"):
            if isinstance(report, dict):
                detail = "; ".join(str(m) for m in
                                   (report.get("messages") or []))
            else:
                detail = "identity_gate_missing_report"
            messages.append("%s: %s" % (item["refspec"],
                                        detail or "identity_gate_blocked"))
            continue
        # A report that does not prove what was scanned is not a pass.  The
        # gate must have resolved this exact head and inspected its commits.
        if "head" not in report or "metadata" not in report:
            messages.append("%s: identity_gate_missing_report"
                            % item["refspec"])
            continue
        if str(report.get("head") or "") != item["head"]:
            messages.append("%s: identity_gate_scanned_wrong_head"
                            % item["refspec"])
            continue
        if str(report.get("base") or "") != item["base"]:
            messages.append("%s: identity_gate_scanned_wrong_range"
                            % item["refspec"])
            continue
        if item["head"] != item["base"] and not report.get("metadata"):
            messages.append("%s: identity_gate_scanned_no_commits"
                            % item["refspec"])
    if messages:
        return 1, "", "; ".join(messages)
    # Materialise the refspecs against the exact commits that were scanned.
    # A mutable local ref cannot move the pushed commit out from under the
    # gate between check_push and git push.
    push_refspecs = ["%s:%s" % (item["head"], item["dst_ref"])
                     for item in ranges]
    push_refspecs.extend(":" + dst for dst in deletions)
    push_args = [a for a in extra if a not in _PUSH_DELETE_FLAGS]
    args = ["push"] + push_args + [str(remote)] + push_refspecs
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


def _remote_urls(repo, remote, push=False):
    """Return configured URLs for a remote name, expanding ``pushInsteadOf``.

    ``--push`` is used for the gate because that is where ``git push`` really
    goes; a separate ``pushurl`` must not inherit an exception granted to the
    fetch URL.
    """

    args = ["remote", "get-url"]
    if push:
        args.append("--push")
    args.extend(["--all", str(remote)])
    rc, out, _err = _git(repo, args)
    if rc != 0:
        args = ["remote", "get-url"]
        if push:
            args.append("--push")
        args.append(str(remote))
        rc, out, _err = _git(repo, args)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _effective_remote_url(repo, url):
    """Return git's effective URL after ``insteadOf`` rewriting, or ``""``.

    ``ls-remote --get-url`` expands the rewrite rules without contacting the
    remote, so a literal URL that git will send somewhere else is visible to
    the exception gate.
    """

    rc, out, _err = _git(
        repo, ["ls-remote", "--get-url", str(url)])
    if rc != 0 or not out.strip():
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) != 1:
        return ""
    return lines[0]


def _push_rewritten_urls(repo, url):
    """URLs produced by ``url.<base>.pushInsteadOf`` for a push of *url*.

    ``git ls-remote --get-url`` expands ``insteadOf`` but not the separate
    ``pushInsteadOf`` rule that only applies to pushes.  The gate must see the
    actual push destination, so the longest matching pushInsteadOf rewrite is
    added as a candidate; every candidate must agree or no exception applies.
    """

    text = str(url or "").strip()
    if not text:
        return []
    rc, out, _err = _git(
        repo, ["config", "--get-regexp", r"^url\..*\.pushinsteadof$"])
    if rc != 0 or not out.strip():
        return []
    matches = []
    for line in out.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        key, prefix = fields[0], fields[1]
        if not key.startswith("url.") or not key.endswith(".pushinsteadof"):
            continue
        if not prefix or not text.startswith(prefix):
            continue
        base = key[len("url."):-len(".pushinsteadof")]
        matches.append((len(prefix), base + text[len(prefix):]))
    if not matches:
        return []
    matches.sort(key=lambda item: -item[0])
    return [matches[0][1]]


def _remote_candidates(repo, remote):
    """Return the remote locations for exact matching (never a bare label).

    A configured remote name is resolved with ``git remote get-url --push``
    (already expanded by git).  A direct URL/SCP/path push argument is itself
    a destination, so both the literal value and git's effective value after
    ``insteadOf`` rewriting are candidates; matching still has to be exact,
    and every candidate must agree.  An unresolvable effective URL is kept as
    an empty candidate so the caller fails closed.
    """

    text = str(remote or "").strip()
    values = []
    if text and _looks_like_location(text):
        values.append(text)
        values.append(_effective_remote_url(repo, text))
        values.extend(_push_rewritten_urls(repo, text))
    if text:
        push_urls = _remote_urls(repo, text, push=True)
        values.extend(push_urls)
        for value in push_urls:
            values.extend(_push_rewritten_urls(repo, value))
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def exception_for_remote(cfg, repo, remote):
    """Return the exception entry matching this remote, or ``""``.

    Matching is EXACT on a canonical ``host/path``: a bare ``owner/repo``
    entry can only mean ``github.com/owner/repo``, and a full URL/SCP entry
    must match the full normalised host/path.  There is no suffix, prefix or
    two-segment borrowing, a bare remote name is not an entry, and a direct
    URL push only matches an entry for that exact URL.

    Every destination candidate (multiple pushurls, literal plus
    ``insteadOf``-effective URL) must normalise to the same single location;
    a second/evil destination or an unparseable candidate grants nothing.
    """

    entries = exceptions_from_config(cfg)
    if not entries or not str(remote or "").strip():
        return ""
    candidates = []
    for value in _remote_candidates(repo, remote):
        canonical = _canonical_candidate(value)
        if not canonical:
            return ""
        candidates.append(canonical)
    distinct = sorted(set(candidates))
    if len(distinct) != 1:
        return ""
    canonical_remote = distinct[0]
    for entry in entries:
        canonical = _canonical_exception_entry(entry)
        if canonical and canonical == canonical_remote:
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
             "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B", sha],
        )
        if rc != 0:
            raise GitIdentityError((err or "git_show_failed").strip())
        parts = out.rstrip("\n").split("\x00", 5)
        if len(parts) < 6:
            raise GitIdentityError("unparsable_git_log_record")
        commits.append({
            "sha": parts[0],
            "author_name": parts[1],
            "author_email": parts[2],
            "committer_name": parts[3],
            "committer_email": parts[4],
            "message": parts[5],
        })
    return commits


def scan_metadata(repo, base, head, cfg=None, allowed_pairs=None,
                  extra_emails=None):
    """Return one violation per author/committer pair off the allowlist.

    The allowed unit is a ``(name, email)`` pair, not two independent sets:
    the default address carried by a foreign name is still a foreign
    identity.  *extra_emails* is used only for message/trailer addresses (and
    for an exception remote's repo-local address); it never widens the
    author/committer pair allowlist.
    """

    if allowed_pairs is None:
        pairs = allowed_identities(cfg)
    else:
        pairs = set(allowed_pairs)
    emails = {email for _name, email in pairs}
    for extra in extra_emails or ():
        value = _normalise_email(extra)
        if value:
            emails.add(value)
    violations = []
    for commit in commit_metadata(repo, base, head, cfg):
        for role in ("author", "committer"):
            name = commit[role + "_name"]
            email = _normalise_email(commit[role + "_email"])
            identity = "%s <%s>" % (name, commit[role + "_email"])
            pair = (str(name or "").strip().lower(), email)
            if email not in emails:
                violations.append({
                    "kind": "metadata", "field": "email",
                    "sha": commit["sha"], "role": role,
                    "name": name, "email": commit[role + "_email"],
                    "identity": identity,
                })
            elif pair not in pairs:
                violations.append({
                    "kind": "metadata", "field": "name",
                    "sha": commit["sha"], "role": role,
                    "name": name, "email": commit[role + "_email"],
                    "identity": identity,
                })
        # Co-authored-by / Signed-off-by trailers and message bodies are
        # identity metadata too; scan every address for the same allowlist.
        for address in _EMAIL_RE.findall(commit.get("message") or ""):
            if _normalise_email(address) in emails:
                continue
            violations.append({
                "kind": "metadata", "field": "message",
                "sha": commit["sha"], "role": "message",
                "name": "", "email": address,
                "identity": address,
            })
    return violations


def _added_lines(diff_text):
    """Yield ``(line_number, text)`` for genuinely added content lines.

    ``---``/``+++`` are file headers only outside a hunk.  Once a hunk has
    started, a diff line beginning ``+`` is content even when the content
    itself begins with ``++`` (producing a ``+++...`` diff line); treating
    that as a header used to hide secrets/addresses from the scan.  A new
    ``diff --git`` section resets the header state for the next file.
    """
    line_no = 0
    in_hunk = False
    combined_parents = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --"):
            in_hunk = False
            combined_parents = 0
            continue
        match = _HUNK_RE.match(line)
        combined = False
        if not match:
            match = _COMBINED_HUNK_RE.match(line)
            combined = bool(match)
        if match:
            line_no = int(match.group(1))
            in_hunk = True
            combined_parents = (
                len(re.findall(r" -\d", line.split(" +", 1)[0]))
                if combined else 0)
            continue
        if not in_hunk:
            continue
        if combined:
            if line.startswith("\\"):
                continue
            markers = line[:combined_parents]
            if "+" in markers:
                # Scan the content after the marker columns; the line number
                # is approximate in a combined hunk but violations only need
                # a stable locator.
                yield line_no, line[combined_parents:]
                line_no += 1
            elif line_no:
                line_no += 1
            continue
        if line.startswith("+"):
            yield line_no, line[1:]
            line_no += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        elif line_no:
            line_no += 1


def _mask(value):
    text = str(value or "")
    if len(text) <= 8:
        return "***"
    return text[:4] + "..." + text[-2:]


def _email_allowed_in_content(email, cfg, extra_emails=None,
                              replace_global=False):
    lowered = _normalise_email(email)
    if extra_emails:
        for extra in extra_emails:
            if lowered == _normalise_email(extra):
                return True
    global_emails = allowed_emails(cfg)
    if lowered in global_emails:
        # On an exception remote the only identity address allowed is the
        # repo-local one (checked above); the connector/default address must
        # not slip back in just because its domain is a placeholder.
        return not replace_global
    domain = lowered.rsplit("@", 1)[-1]
    return any(domain == item or domain.endswith("." + item)
               for item in _PLACEHOLDER_DOMAINS)


def _scan_added_diff(out, sha, cfg, violations, extra_emails=None,
                     replace_global=False):
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
                if not _email_allowed_in_content(
                        match.group(0), cfg, extra_emails, replace_global):
                    violations.append({
                        "kind": "content",
                        "rule": "email",
                        "commit": sha[:12],
                        "line": line_no,
                        "match": _mask(match.group(0)),
                    })
                    break


def scan_content(repo, base, head, cfg=None, extra_emails=None,
                 replace_global=False):
    """Scan every pushed commit's added lines, not just the net diff.

    A secret added in one commit and removed in a later one is absent from
    ``base..head`` but remains in the pushed history.  Each commit is scanned
    separately so that such a leak cannot pass the gate.  On an exception
    remote *replace_global* restricts content addresses to the repo-local
    email (passed in *extra_emails*) plus the placeholder domains; the
    globally configured connector/default addresses are not allowed.
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
        _scan_added_diff(out, sha, cfg, violations, extra_emails,
                         replace_global)
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

    ``ok`` is False when commit metadata is off the allowlist or when added
    content contains a secret.  An exception remote does **not** skip the
    metadata scan and does not keep the global connector pairs: the only
    permitted author/committer identity there is the repository's own complete
    repo-local ``(name, email)`` pair, whose email is also the only extra
    message/trailer address.  Missing/incomplete repo-local identity allows
    nothing for metadata.
    """

    report = {
        "ok": True,
        "base": base,
        "head": head,
        "remote": remote or "",
        "identity": identity_from_config(cfg),
        "exception": "",
        "exception_identity": {},
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
        head = out.strip().splitlines()[0].strip()
        report["head"] = head
        report["metadata"] = commit_metadata(repo, base, head, cfg)
        exception = exception_for_remote(cfg, repo, remote)
        report["exception"] = exception
        if exception:
            # Exclusive exception allowlist: on the matched remote the ONLY
            # metadata identity is the repository's own complete repo-local
            # pair.  Global/default connector pairs do not carry over, and an
            # incomplete repo-local identity allows nothing at all.
            local = repo_local_identity(repo)
            report["exception_identity"] = dict(local) if local else {}
            if local:
                pair = (local["name"].strip().lower(),
                        _normalise_email(local["email"]))
                pairs = {pair}
                extra_emails = {pair[1]}
            else:
                pairs = set()
                extra_emails = set()
        else:
            pairs = allowed_identities(cfg)
            extra_emails = set()
        report["violations"].extend(scan_metadata(
            repo, base, head, cfg, allowed_pairs=pairs,
            extra_emails=extra_emails))
        report["violations"].extend(scan_content(
            repo, base, head, cfg, extra_emails=extra_emails,
            replace_global=bool(exception)))
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
