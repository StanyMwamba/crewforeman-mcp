#!/usr/bin/env python3
"""Fast-forward push through the CrewForeman identity/content gate.

Release tooling must not call ``git push`` directly: this wrapper runs
``git_identity.push_with_gate`` first, which scans every commit in
``base..head`` (author/committer metadata and added content) and returns
before the push on any violation.  The installed pre-push hook is a second,
cooperative layer; ``--no-verify`` can skip a hook but not this wrapper.

Usage:
    python3 scripts/ship_push.py --repo . --remote origin \
        --refspec main:main --base origin/main
"""

import argparse
import sys
from pathlib import Path


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--refspec", default="main:main")
    parser.add_argument("--base", default="")
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)
    repo = str(Path(args.repo).resolve())
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from deepseek_mcp import git_identity

    cfg = _load_config()
    if args.config:
        try:
            from deepseek_mcp.config import load_config
            cfg = load_config(args.config)
        except Exception as exc:  # noqa: BLE001 - fail closed
            print("ship-push: cannot read %s: %s" % (args.config, exc),
                  file=sys.stderr)
            return 2
    base = args.base
    if not base:
        rc, out, _err = git_identity._git(
            repo, ["merge-base", "HEAD", "refs/remotes/%s/HEAD" % args.remote])
        base = out.strip() if rc == 0 else ""
    rc, out, err = git_identity.push_with_gate(
        cfg, repo, args.remote, args.refspec, base=base)
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
