"""Real macOS boundary: Seatbelt create/modify/truncate/rename/delete.

Nothing here fakes the sandbox.  It is skipped only when Seatbelt is genuinely
unavailable (or CF_ALLOW_NESTED_SANDBOX=1), and that skip is a hard failure in
CI via CF_REQUIRE_REAL_SANDBOX=1.

Both fixture roots live directly under ``/private/tmp`` instead of the process
``TMPDIR``: the test harness redirects ``TMPDIR`` into a per-case tree, and a
boundary target inside a redirected temp root is one grant away from being
legitimately writable.  ``/private/tmp`` is outside the repository, the case
tree, the workspace and the DSH runtime grants.  The roots are unique per run
and removed in ``finally``.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CHILD = Path(__file__).resolve().parent / "sandbox_child.py"
OPS = ("create", "modify", "append", "truncate", "rename", "delete")


def _require_real():
    return os.environ.get("CF_REQUIRE_REAL_SANDBOX") == "1"


def skip_or_fail(tc, reason):
    if _require_real():
        tc.fail(reason)
    tc.skipTest(reason)


def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _real(path):
    return os.path.realpath(str(path))


def _within(path, parent):
    """True when *path* is *parent* or below it (after resolving symlinks)."""
    try:
        parent = _real(parent)
        return os.path.commonpath([_real(path), parent]) == parent
    except ValueError:
        return False


def _fresh_dir(prefix):
    """A unique directory outside the workspace, case tree and grants."""
    base = "/private/tmp"
    if not (os.path.isdir(base) and os.access(base, os.W_OK | os.X_OK)):
        base = None
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _run(tc, workspace, tmpdir, op, target):
    from deepseek_mcp import safety
    policy = {"os_sandbox": True, "os_sandbox_mode": "require",
              "sandbox": "workspace-write",
              "os_sandbox_policy": {"kind": "seatbelt"}}
    argv, meta = safety.wrap_command(
        [sys.executable, str(CHILD), op, str(target)],
        policy, str(workspace), tmpdir=str(tmpdir))
    if meta.get("blocked"):
        skip_or_fail(tc, "boundary blocked: %s" % meta.get("reason"))
    tc.assertTrue(meta.get("wrapped"), "not wrapped: %s" % (meta,))
    tc.assertIn("sandbox-exec", Path(argv[0]).name)
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=60)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def run_boundary(tc):
    if sys.platform != "darwin":
        tc.skipTest("macOS boundary requires darwin")
    if os.environ.get("CF_ALLOW_NESTED_SANDBOX") == "1":
        skip_or_fail(tc, "CF_ALLOW_NESTED_SANDBOX=1 (developer host)")
    from deepseek_mcp import safety
    usable = getattr(safety, "seatbelt_usable", lambda force=False: False)(False)
    if not usable:
        skip_or_fail(tc, "sandbox-exec/Seatbelt unavailable on this host")

    root = _fresh_dir("dsh-boundary-")
    outside = None
    try:
        outside = _fresh_dir("dsh-boundary-outside-")
        workspace = root / "workspace"
        workspace.mkdir()
        tmpdir = workspace / ".dsh-tmp-boundary"
        tmpdir.mkdir()

        # The boundary target must be outside every granted root.  The
        # workspace grant is a subpath of ``workspace`` and the only other
        # writable roots are its tmpdir and the operator's DSH runtime/extra
        # writes; a sibling under /private/tmp is under none of them.
        tc.assertNotEqual(_real(outside), _real(workspace))
        tc.assertFalse(_within(outside, workspace),
                       "outside root is inside the workspace: %s" % outside)
        tc.assertFalse(_within(outside, tmpdir),
                       "outside root is inside the tmp grant: %s" % outside)

        marker = outside / "protected.txt"
        marker.write_text("do-not-touch\n", encoding="utf-8")
        marker_hash = _sha(marker)

        # Inside and temp writes must be allowed.
        for scope, directory in (("inside", workspace), ("tmp", tmpdir)):
            for op in OPS:
                target = directory / ("%s-%s.txt" % (scope, op))
                if op == "create":
                    if target.exists():
                        target.unlink()
                else:
                    target.write_text("original\n", encoding="utf-8")
                rc, err = _run(tc, workspace, tmpdir, op, target)
                tc.assertEqual(rc, 0, "%s %s should be allowed: %s" % (scope, op, err))

        # Outside writes must be denied and leave bytes untouched.  The create
        # target is deliberately absent: sandbox_child opens it with
        # O_CREAT|O_EXCL, so a pre-created victim would fail with EEXIST (17)
        # before Seatbelt evaluated the write, and the child would report an
        # unexpected failure (4) instead of the denial code (3).  Only an
        # absent target really proves that the boundary denied the create.
        for op in OPS:
            victim = outside / ("victim-%s.txt" % op)
            renamed = Path(str(victim) + ".renamed")
            if op == "create":
                if victim.exists():
                    victim.unlink()
                tc.assertFalse(victim.exists(),
                               "create probe target must not exist: %s" % victim)
                before = None
                entries = {p.name for p in outside.iterdir()}
            else:
                victim.write_text("original\n", encoding="utf-8")
                before = _sha(victim)
            rc, err = _run(tc, workspace, tmpdir, op, victim)
            tc.assertEqual(rc, 3, "outside %s should be denied: %s" % (op, err))
            if op == "create":
                tc.assertFalse(victim.exists(),
                               "denied outside create left %s" % victim)
                tc.assertIsNone(_sha(victim),
                                "outside create produced bytes: %s" % victim)
                tc.assertEqual({p.name for p in outside.iterdir()}, entries,
                               "outside create changed the directory")
            else:
                tc.assertEqual(_sha(victim), before, "outside %s mutated target" % op)
            tc.assertFalse(renamed.exists(),
                           "outside %s left %s" % (op, renamed))

        symlink_victim = outside / "symlink-victim.txt"
        symlink_victim.write_text("original\n", encoding="utf-8")
        symlink_hash = _sha(symlink_victim)
        link = workspace / "escape-link"
        os.symlink(symlink_victim, link)
        rc, err = _run(tc, workspace, tmpdir, "append", link)
        tc.assertEqual(rc, 3, "symlink escape should be denied: %s" % err)
        tc.assertEqual(_sha(symlink_victim), symlink_hash,
                       "symlink escape mutated target")

        dir_link = workspace / "escape-dir-link"
        os.symlink(outside, dir_link)
        via_link = dir_link / "via-symlink.txt"
        rc, err = _run(tc, workspace, tmpdir, "create", via_link)
        tc.assertEqual(rc, 3, "symlink directory escape should be denied: %s" % err)
        tc.assertFalse(via_link.exists(),
                       "symlink directory escape created %s" % via_link)

        # ``..`` components must resolve to the real outside directory; the
        # lexical path is relative to the workspace so the two roots can sit
        # at different depths under /private/tmp.
        dotdot = outside / "dotdot-created.txt"
        rc, err = _run(tc, workspace, tmpdir, "create",
                       workspace / os.path.relpath(str(dotdot), str(workspace)))
        tc.assertEqual(rc, 3, "'..' escape should be denied: %s" % err)
        tc.assertFalse(dotdot.exists(), "'..' escape created %s" % dotdot)

        rc, err = _run(tc, workspace, tmpdir, "append",
                       workspace / os.path.relpath(str(marker), str(workspace)))
        tc.assertEqual(rc, 3, "'..' modify should be denied: %s" % err)
        tc.assertEqual(_sha(marker), marker_hash, "protected marker mutated")
    finally:
        if outside is not None:
            shutil.rmtree(str(outside), ignore_errors=True)
        shutil.rmtree(str(root), ignore_errors=True)


class MacSandboxBoundaryTest(unittest.TestCase):
    def test_real_seatbelt_boundary(self):
        run_boundary(self)


if __name__ == "__main__":
    unittest.main()
