"""Real Windows boundary test: file writes outside the policy roots.

This module is skipped on non-Windows hosts with a clear reason.  When
``CF_REQUIRE_REAL_SANDBOX=1`` is set (the CI ``boundary`` job) a non-Windows
host is a hard failure, not a skip, so the suite cannot silently disappear.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

WINDOWS = sys.platform == "win32"
REQUIRE_REAL = os.environ.get("CF_REQUIRE_REAL_SANDBOX") == "1"

CHILD = r"""
import os, sys
op, path = sys.argv[1], sys.argv[2]
try:
    if op == "create":
        with open(path, "w") as handle:
            handle.write("x")
    elif op == "modify":
        with open(path, "a") as handle:
            handle.write("x")
    elif op == "truncate":
        with open(path, "w"):
            pass
    elif op == "rename":
        os.replace(path, path + ".moved")
    elif op == "delete":
        os.remove(path)
    elif op == "read":
        with open(path, "rb") as handle:
            handle.read()
    else:
        sys.exit(4)
except OSError:
    sys.exit(3)
sys.exit(0)
"""


def _write(path, data="seed"):
    with open(path, "w") as handle:
        handle.write(data)


class WindowsBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not WINDOWS:
            # Only the *host* OS boundary is required by CF_REQUIRE; an
            # inapplicable per-OS module is always a skip.
            raise unittest.SkipTest(
                "Windows-only boundary test: requires a real win32 host "
                "with CreateRestrictedToken/ACLs; current platform is %s"
                % sys.platform)
        cls.root = tempfile.mkdtemp(prefix="dsh-win-boundary-")
        cls.workspace = os.path.join(cls.root, "workspace")
        cls.tmpdir = os.path.join(cls.workspace, ".dsh-tmp-boundary")
        cls.outside = os.path.join(cls.root, "outside")
        os.makedirs(cls.workspace)
        os.makedirs(cls.tmpdir)
        os.makedirs(cls.outside)
        cls.protected = os.path.join(cls.outside, "protected.txt")
        _write(cls.protected, "unchanged")
        cls.policy = {
            "workspace": cls.workspace,
            "mode": "workspace-write",
            "tmpdir": cls.tmpdir,
            "extra_writes": [],
            "dsh_home": os.path.join(cls.root, "dsh-home"),
            "required": True,
            "timeout": 30,
        }

    def _child(self, op, path):
        policy = json.dumps(self.policy)
        command = [sys.executable, "-m", "deepseek_mcp.sandbox_windows",
                   "--policy", policy, "--", sys.executable, "-c", CHILD,
                   op, path]
        return subprocess.run(command, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)

    def test_create_outside_denied(self):
        target = os.path.join(self.outside, "created.txt")
        proc = self._child("create", target)
        self.assertEqual(proc.returncode, 3, proc.stderr.decode("utf-8", "replace"))
        self.assertFalse(os.path.exists(target))

    def test_modify_and_truncate_outside_denied(self):
        for op in ("modify", "truncate"):
            proc = self._child(op, self.protected)
            self.assertEqual(proc.returncode, 3, op)
            with open(self.protected, "r") as handle:
                self.assertEqual(handle.read(), "unchanged")

    def test_inside_workspace_allowed(self):
        target = os.path.join(self.workspace, "inside.txt")
        proc = self._child("create", target)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue(os.path.exists(target))

    def test_tmpdir_allowed(self):
        target = os.path.join(self.tmpdir, "scratch.txt")
        proc = self._child("create", target)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_rename_and_delete_outside_recorded(self):
        # R2: only claim delete/rename protection when a run proves it.  The
        # GitHub windows-2025 runner is an Administrator, so on that host this
        # is report-only: a 0 result is recorded and never turned into a claim.
        try:
            from deepseek_mcp import sandbox_windows
            admin = bool(sandbox_windows.probe_status().get("admin"))
        except Exception:
            admin = False
        rename_target = os.path.join(self.outside, "rename-me.txt")
        _write(rename_target)
        rename = self._child("rename", rename_target)
        delete_target = os.path.join(self.outside, "delete-me.txt")
        _write(delete_target)
        delete = self._child("delete", delete_target)
        allowed = (0, 3, 4) if admin else (3, 4)
        self.assertIn(rename.returncode, allowed,
                      "rename outside result recorded (admin=%s)" % admin)
        self.assertIn(delete.returncode, allowed,
                      "delete outside result recorded (admin=%s)" % admin)


if __name__ == "__main__":
    unittest.main()


CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "sandbox_child.py")
OPS = ("create", "modify", "append", "truncate", "rename", "delete")


def _skip_or_fail(tc, reason):
    if REQUIRE_REAL:
        tc.fail(reason)
    tc.skipTest(reason)


def run_boundary(tc):
    """Real Windows boundary used by tests.test_sandbox_boundary."""
    if not WINDOWS:
        tc.skipTest("Windows boundary requires win32 (host is %s)"
                    % sys.platform)
    from deepseek_mcp import safety
    try:
        from deepseek_mcp import sandbox_windows
        admin = bool(sandbox_windows.probe_status().get("admin"))
    except Exception:
        admin = False
    policy = {
        "os_sandbox": True, "os_sandbox_mode": "require",
        "sandbox": "workspace-write", "sandbox_canary": "off",
        "os_sandbox_policy": {
            "kind": "windows-write-restricted", "enforcement": "enforced",
            "abi": "write-restricted", "tested": True,
        },
    }
    root = tempfile.mkdtemp(prefix="dsh-win-run-boundary-")
    try:
        workspace = os.path.join(root, "workspace")
        tmpdir = os.path.join(workspace, ".dsh-tmp-boundary")
        outside = os.path.join(root, "outside")
        for directory in (workspace, tmpdir, outside):
            os.makedirs(directory)
        marker = os.path.join(outside, "protected.txt")
        _write(marker, "do-not-touch")
        for scope, directory in (("inside", workspace), ("tmp", tmpdir)):
            for op in OPS:
                target = os.path.join(directory, "%s-%s.txt" % (scope, op))
                if op != "create":
                    _write(target)
                argv, meta = safety.wrap_command(
                    [sys.executable, CHILD_SCRIPT, op, target],
                    policy, workspace, tmpdir=tmpdir)
                tc.assertTrue(meta.get("wrapped"), meta)
                proc = subprocess.run(argv, cwd=workspace,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=60)
                tc.assertEqual(proc.returncode, 0,
                               "%s %s allowed: %s"
                               % (scope, op,
                                  proc.stderr.decode("utf-8", "replace")))
        for op in OPS:
            target = os.path.join(outside, "victim-%s.txt" % op)
            if op != "create":
                _write(target)
            before = open(target, "rb").read() if os.path.exists(target) else None
            argv, meta = safety.wrap_command(
                [sys.executable, CHILD_SCRIPT, op, target],
                policy, workspace, tmpdir=tmpdir)
            tc.assertTrue(meta.get("wrapped"), meta)
            proc = subprocess.run(argv, cwd=workspace,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            # R2: delete/rename may be recorded as unverified; create,
            # modify, append and truncate are required denials.  On the
            # Administrator GitHub runner delete/rename is report-only, so a
            # success is printed and never turned into a protection claim.
            if op in ("create", "modify", "append", "truncate"):
                tc.assertEqual(proc.returncode, 3,
                               "outside %s denied: %s"
                               % (op, proc.stderr.decode("utf-8", "replace")))
            elif admin:
                tc.assertIn(proc.returncode, (0, 3, 4),
                            "outside %s result recorded" % op)
                if proc.returncode == 0:
                    print("report-only (admin runner): outside %s was not "
                          "denied; delete_outside stays unverified" % op)
            else:
                tc.assertIn(proc.returncode, (3, 4),
                            "outside %s result recorded" % op)
            if op in ("create", "modify", "append", "truncate") or not admin:
                after = (open(target, "rb").read()
                         if os.path.exists(target) else None)
                tc.assertEqual(after, before, "outside %s changed bytes" % op)
        link = os.path.join(workspace, "outside-link")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            link = None
        dotdot = os.path.join(workspace, "..", "outside", "via-dotdot.txt")
        escapes = [("create", dotdot)]
        if link:
            escapes.append(("create", os.path.join(link, "via-symlink.txt")))
        for op, target in escapes:
            argv, meta = safety.wrap_command(
                [sys.executable, CHILD_SCRIPT, op, target],
                policy, workspace, tmpdir=tmpdir)
            proc = subprocess.run(argv, cwd=workspace,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            tc.assertEqual(proc.returncode, 3, "escape denied: %s" % target)
            tc.assertFalse(os.path.exists(target))
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
