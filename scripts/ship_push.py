#!/usr/bin/env python3
"""Fast-forward push through the CrewForeman identity/content gate.

Release tooling must not call ``git push`` directly: this wrapper runs
``git_identity.push_with_gate`` first, which derives the exact range from the
refspecs being pushed (author/committer metadata and added content) and
returns before the push on any violation.  The installed pre-push hook is a
second, cooperative layer; ``--no-verify`` can skip a hook but not this
wrapper, and this wrapper rejects ``--no-verify`` outright.

Usage:
    python3 scripts/ship_push.py --repo . --remote origin \
        --refspec main:main

``--base`` is accepted for older callers but is not trusted: the gate derives
the old value from the destination's advertised ref and scans the full
history when that value is unknown.
"""

import argparse
import sys
from pathlib import Path

# The gate must come from the connector's own source tree, never from the
# repository being pushed (a repo-root ``deepseek_mcp`` package would shadow
# it).  This file lives in ``<connector>/scripts/ship_push.py``.
CONNECTOR_ROOT = Path(__file__).resolve().parents[1]

# Tokens that would suppress or outrun the connector's own gate.  They are
# rejected before argparse so they can never be forwarded to git push.
_FORBIDDEN_TOKENS = (
    "--no-verify",
    "--force",
    "--force-with-lease",
    "--force-if-includes",
    "--all",
    "--branches",
    "--tags",
    "--mirror",
    "--follow-tags",
    "--prune",
)


def _guard_argv(argv):
    for token in argv:
        for forbidden in _FORBIDDEN_TOKENS:
            if token == forbidden or token.startswith(forbidden + "="):
                return ("ship-push: refusing %s: the identity gate cannot be "
                        "skipped or allowed to outrun the refspec" % token)
    return ""


def _load_config():
    try:
        from deepseek_mcp.config import load_config
    except Exception:
        return {}
    try:
        return load_config()
    except Exception:
        return {}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    guard = _guard_argv(argv)
    if guard:
        print(guard, file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--refspec", default="main:main")
    parser.add_argument("--base", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--extra-arg", "--push-arg", dest="extra_args",
                        action="append", default=[],
                        help="benign git push flag (validated by the gate)")
    args = parser.parse_args(argv)
    repo = str(Path(args.repo).resolve())
    # Import the hard gate from the connector source root, never from the
    # pushed repository.  Insert the connector root at sys.path[0] (a repo
    # path on PYTHONPATH or earlier must not win) and verify the module that
    # was actually loaded lives under that root.
    sys.path.insert(0, str(CONNECTOR_ROOT))
    try:
        from deepseek_mcp import git_identity
    except Exception as exc:  # noqa: BLE001 - fail closed
        print("ship-push: cannot import the connector gate: %s" % exc,
              file=sys.stderr)
        return 2
    module_file = getattr(git_identity, "__file__", "") or ""
    shadowed = not module_file
    if module_file:
        try:
            Path(module_file).resolve().relative_to(CONNECTOR_ROOT)
        except (ValueError, OSError):
            shadowed = True
    if shadowed:
        print("ship-push: refusing a shadowed identity gate (%s)"
              % (module_file or "<unknown>"), file=sys.stderr)
        return 2

    try:
        extra_args = git_identity.validate_push_args(args.extra_args)
    except git_identity.GitIdentityError as exc:
        print("ship-push: refusing push argument: %s" % exc,
              file=sys.stderr)
        return 2

    cfg = _load_config()
    if args.config:
        try:
            from deepseek_mcp.config import load_config
            cfg = load_config(args.config)
        except Exception as exc:  # noqa: BLE001 - fail closed
            print("ship-push: cannot read %s: %s" % (args.config, exc),
                  file=sys.stderr)
            return 2
    rc, out, err = git_identity.push_with_gate(
        cfg, repo, args.remote, args.refspec, base=args.base,
        extra_args=extra_args)
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
