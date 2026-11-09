#!/usr/bin/env python3
"""Boundary child: attempt one filesystem operation and report the verdict.

Exit codes: 0 operation allowed/completed, 3 denied by the OS boundary,
4 unexpected failure (including a malformed invocation). The parent test runs
this through the real OS wrapper, never through a fake sandbox.
"""

import errno
import os
import sys

DENIED = {errno.EACCES, errno.EPERM, errno.EROFS}


def run(op, target):
    if op == "create":
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    elif op == "modify":
        with open(target, "r+b") as handle:
            handle.write(b"x")
            handle.flush()
    elif op == "append":
        with open(target, "ab") as handle:
            handle.write(b"appended\n")
            handle.flush()
    elif op == "truncate":
        with open(target, "r+b") as handle:
            handle.truncate(0)
    elif op == "rename":
        os.replace(target, target + ".renamed")
    elif op == "delete":
        os.unlink(target)
    elif op == "symlink-create":
        os.symlink(target, target + ".link")
    else:
        raise ValueError("unknown op: %s" % (op,))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: sandbox_child.py OP TARGET\n")
        return 4
    op, target = argv[1], argv[2]
    try:
        run(op, target)
    except OSError as exc:
        if exc.errno in DENIED or isinstance(exc, PermissionError):
            sys.stderr.write("denied: %s %s: %s\n" % (op, target, exc))
            return 3
        sys.stderr.write("unexpected: %s %s: %s\n" % (op, target, exc))
        return 4
    except Exception as exc:  # noqa: BLE001 - verdict must be explicit
        sys.stderr.write("unexpected: %s %s: %s\n" % (op, target, exc))
        return 4
    sys.stderr.write("allowed: %s %s\n" % (op, target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
