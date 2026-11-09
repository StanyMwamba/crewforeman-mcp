"""Real Linux boundary test for the Landlock launcher.

The test is skipped on non-Linux hosts and on Linux kernels without a usable
Landlock ABI >= 3, always with an explicit reason.  It runs the launcher in a
fresh Python process (never the test process) and proves two things: a create
outside the workspace is denied, and ``exec`` preserves the PID.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from deepseek_mcp import sandbox_linux as sl

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_LINUX = sys.platform.startswith("linux")
_PROBE = sl.probe_abi() if IS_LINUX else None


def _skip_reason():
    if not IS_LINUX:
        return "Linux-only Landlock boundary test (host is %s)" % sys.platform
    if not _PROBE or not _PROBE.get("ok"):
        return "Landlock unavailable on this host: %s" % (
            (_PROBE or {}).get("reason") or "probe failed"
        )
    if int(_PROBE.get("abi") or 0) < 3:
        return "Landlock ABI %s < 3 (truncate boundary unavailable)" % (
            _PROBE.get("abi"),
        )
    return ""


CHILD = r"""
import os
import sys

outside, pidfile = sys.argv[1], sys.argv[2]
denied = False
try:
    with open(os.path.join(outside, "escaped.txt"), "w") as handle:
        handle.write("escaped\n")
except OSError:
    denied = True
with open(pidfile, "w") as handle:
    handle.write(str(os.getpid()))
sys.exit(0 if denied else 4)
"""


@unittest.skipUnless(
    IS_LINUX and _PROBE and _PROBE.get("ok") and int(_PROBE.get("abi") or 0) >= 3,
    _skip_reason() or "Linux Landlock ABI >= 3 required",
)
class LandlockBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.workspace = os.path.join(self.root, "workspace")
        self.outside = os.path.join(self.root, "outside")
        self.tmpdir = os.path.join(self.root, "tmp")
        os.makedirs(self.workspace)
        os.makedirs(self.outside)
        os.makedirs(self.tmpdir)
        self.pidfile = os.path.join(self.workspace, "child.pid")
        self.policy = {
            "workspace": self.workspace,
            "mode": "workspace-write",
            "tmpdir": self.tmpdir,
            "dsh_home": os.path.join(self.root, "dsh-home"),
            "min_abi": 3,
            "required": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _run(self):
        command = [
            sys.executable,
            "-m",
            "deepseek_mcp.sandbox_linux",
            "--policy",
            json.dumps(self.policy),
            "--",
            sys.executable,
            "-c",
            CHILD,
            self.outside,
            self.pidfile,
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_outside_create_denied_and_exec_preserves_pid(self):
        proc = self._run()
        _stdout, stderr = proc.communicate(timeout=60)
        stderr = stderr.decode("utf-8", "replace")
        self.assertEqual(
            proc.returncode,
            0,
            "launcher/child failed: code=%s stderr=%s"
            % (proc.returncode, stderr),
        )
        self.assertFalse(os.path.exists(os.path.join(self.outside,
                                                     "escaped.txt")))
        with open(self.pidfile) as handle:
            child_pid = int(handle.read().strip())
        # exec replaces the launcher image, so the child keeps the PID the
        # connector spawned.
        self.assertEqual(child_pid, proc.pid)

    def test_inside_workspace_write_allowed(self):
        proc = self._run()
        _stdout, stderr = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0,
                         stderr.decode("utf-8", "replace"))
        self.assertTrue(os.path.isfile(self.pidfile))


if __name__ == "__main__":
    unittest.main()


CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "sandbox_child.py")
OPS = ("create", "modify", "append", "truncate", "rename", "delete")


def _require_real():
    return os.environ.get("CF_REQUIRE_REAL_SANDBOX") == "1"


def _skip_or_fail(tc, reason):
    if _require_real():
        tc.fail(reason)
    tc.skipTest(reason)


def _child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = (REPO_ROOT + os.pathsep
                         + env.get("PYTHONPATH", "")).rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _sha(path):
    import hashlib
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def run_boundary(tc):
    """Real Landlock boundary used by tests.test_sandbox_boundary."""
    from deepseek_mcp import safety
    if not IS_LINUX:
        tc.skipTest("Linux Landlock boundary requires Linux")
    if not _PROBE or not _PROBE.get("ok"):
        _skip_or_fail(tc, "Landlock unavailable: %s"
                     % ((_PROBE or {}).get("reason") or "probe failed"))
    abi = int(_PROBE.get("abi") or 0)
    if abi < 3:
        _skip_or_fail(tc, "Landlock ABI %d < 3" % abi)
    policy = {
        "os_sandbox": True,
        "os_sandbox_mode": "require",
        "sandbox": "workspace-write",
        "sandbox_canary": "off",
        "os_sandbox_policy": {
            "kind": "landlock", "enforcement": "enforced",
            "abi": "landlock-%d" % abi, "tested": True,
        },
    }
    with tempfile.TemporaryDirectory(prefix="dsh-landlock-boundary-") as root:
        root = os.path.realpath(root)
        workspace = os.path.join(root, "workspace")
        tmpdir = os.path.join(workspace, ".dsh-tmp-boundary")
        outside = os.path.join(root, "outside")
        for directory in (workspace, tmpdir, outside):
            os.makedirs(directory)
        marker = os.path.join(outside, "protected.txt")
        with open(marker, "w") as handle:
            handle.write("do-not-touch\n")
        marker_hash = _sha(marker)
        for scope, directory in (("inside", workspace), ("tmp", tmpdir)):
            for op in OPS:
                target = os.path.join(directory, "%s-%s.txt" % (scope, op))
                if op != "create":
                    with open(target, "w") as handle:
                        handle.write("original\n")
                argv, meta = safety.wrap_command(
                    [sys.executable, CHILD_SCRIPT, op, target],
                    policy, workspace, tmpdir=tmpdir)
                tc.assertTrue(meta.get("wrapped"), meta)
                proc = subprocess.run(argv, cwd=workspace, env=_child_env(),
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=60)
                tc.assertEqual(
                    proc.returncode, 0,
                    "%s %s should be allowed: %s"
                    % (scope, op, proc.stderr.decode("utf-8", "replace")))
                if op == "create":
                    tc.assertTrue(os.path.exists(target))
                if op == "rename":
                    tc.assertTrue(os.path.exists(target + ".renamed"))
                if op == "delete":
                    tc.assertFalse(os.path.exists(target))
        for op in ("create", "modify", "append", "truncate", "rename", "delete"):
            target = os.path.join(outside, "victim-%s.txt" % op)
            if op != "create":
                with open(target, "w") as handle:
                    handle.write("protected\n")
            before = _sha(target)
            argv, meta = safety.wrap_command(
                [sys.executable, CHILD_SCRIPT, op, target],
                policy, workspace, tmpdir=tmpdir)
            tc.assertTrue(meta.get("wrapped"), meta)
            proc = subprocess.run(argv, cwd=workspace, env=_child_env(),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            tc.assertEqual(
                proc.returncode, 3,
                "outside %s must be denied, got %s: %s"
                % (op, proc.returncode,
                   proc.stderr.decode("utf-8", "replace")))
            if op == "delete":
                tc.assertTrue(os.path.exists(target))
            tc.assertEqual(_sha(target), before, "outside %s changed bytes"
                           % op)
        # Symlink and ``..`` escapes must be denied by the same path rules.
        link = os.path.join(workspace, "outside-link")
        os.symlink(outside, link)
        for op, target in (
                ("create", os.path.join(link, "via-symlink.txt")),
                ("create", os.path.join(workspace, "..", "outside",
                                        "via-dotdot.txt"))):
            argv, meta = safety.wrap_command(
                [sys.executable, CHILD_SCRIPT, op, target],
                policy, workspace, tmpdir=tmpdir)
            proc = subprocess.run(argv, cwd=workspace, env=_child_env(),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            tc.assertEqual(proc.returncode, 3,
                           "%s must be denied: %s"
                           % (target, proc.stderr.decode("utf-8", "replace")))
            tc.assertFalse(os.path.exists(target))
        tc.assertEqual(_sha(marker), marker_hash)
