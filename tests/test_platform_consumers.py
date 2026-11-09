"""Focused section 10 consumer fixes (B6): rows 5, 7, 8, 10, 11 and 12.

Each test mocks the shared ``platforms`` primitive so the suite runs on POSIX
while still exercising the Windows-fake branch.  ``os.kill`` is patched to
raise in the process-tree tests: no consumer may fall back to raw signalling.
"""

import inspect
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import authority, credentials, failure_notes, gate, progress
from deepseek_mcp import platforms, review_context


class FakeProc(object):
    pid = 4242
    returncode = -9

    def __init__(self):
        self.timeouts = 0
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeouts == 0:
            self.timeouts += 1
            raise subprocess.TimeoutExpired("cmd", timeout)
        return b"", b""

    def kill(self):
        self.killed = True


class FakeLock(object):
    def __init__(self, path, timeout):
        self.path = path
        self.timeout = timeout
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False


class PlatformConsumerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # ---- row 5: process trees through platforms.terminate_tree -------------
    def test_row5_gate_timeout_uses_terminate_tree_and_never_os_kill(self):
        proc = FakeProc()
        with mock.patch.object(gate.subprocess, "Popen", return_value=proc), \
                mock.patch.object(gate.platforms, "terminate_tree",
                                  return_value={"terminated": True}) as killed, \
                mock.patch.object(gate.os, "kill",
                                  side_effect=AssertionError("os.kill called")):
            result = gate._local_run("python -V", str(self.workspace), 1.0)
        killed.assert_called_once()
        self.assertEqual(killed.call_args.args[0], FakeProc.pid)
        self.assertFalse(proc.killed)
        self.assertEqual(result["exit_code"], -9)

    def test_row5_review_context_helper_uses_terminate_tree(self):
        with mock.patch.object(review_context.platforms, "terminate_tree",
                               return_value={"terminated": True}) as killed, \
                mock.patch.object(review_context.os, "kill",
                                  side_effect=AssertionError("os.kill called")):
            self.assertTrue(review_context._terminate_tree(4242,
                                                           system="Windows"))
        killed.assert_called_once_with(4242, system="Windows",
                                       timeout=0.5)
        for module in (gate, review_context):
            source = inspect.getsource(module)
            self.assertNotIn("os.killpg", source)
            self.assertNotIn("os.getpgid", source)

    # ---- row 7: Windows ACL and review-context private state --------------
    def test_row7_windows_acl_records_private_false_and_posix_keeps_0600(self):
        secret = self.workspace / "credentials.yaml"
        secret.write_text("TOKEN: x\n", encoding="utf-8")
        with mock.patch.object(credentials.platforms, "private_permissions",
                               return_value=False) as checker:
            status = credentials.private_status(secret, system="Windows")
        self.assertFalse(status["private"])
        self.assertEqual(status["method"], "acl")
        self.assertIn("private=false", status["reason"])
        checker.assert_called_once_with(str(secret), system="Windows")
        with mock.patch.object(credentials.platforms, "private_permissions",
                               return_value=True):
            self.assertTrue(
                credentials.private_status(secret, system="Windows")["private"])
    @unittest.skipIf(
        os.name == "nt",
        "Windows st_mode does not carry POSIX 0600/0644 permission bits")
    def test_row7_posix_mode_keeps_0600(self):
        secret = self.workspace / "credentials.yaml"
        secret.write_text("TOKEN: x\n", encoding="utf-8")
        os.chmod(secret, 0o600)
        self.assertTrue(credentials._owner_only(secret, system="Linux"))
        os.chmod(secret, 0o644)
        self.assertFalse(credentials._owner_only(secret, system="Linux"))

    def test_row7_review_context_uses_private_permissions_or_records_false(self):
        path = self.workspace / "state.json"
        path.write_text("{}", encoding="utf-8")
        review_context.PRIVATE_FAILURES[:] = []
        with mock.patch.object(review_context.platforms,
                               "private_permissions", return_value=False) as pp:
            self.assertFalse(review_context._private(path))
        pp.assert_called_once_with(str(path), system=None, is_dir=False)
        self.assertEqual(review_context.PRIVATE_FAILURES[-1]["private"], False)
        self.assertNotIn("os.chmod(path, 0o600)", inspect.getsource(
            review_context))

    # ---- row 8: failure-notes lock delegates to platforms.file_lock -------
    def test_row8_lock_delegates_and_maps_timeout(self):
        lock_path = self.workspace / "notes.lock"
        seen = {}

        def fake_file_lock(path, timeout=10.0):
            seen["path"] = path
            seen["timeout"] = timeout
            return FakeLock(path, timeout)

        with mock.patch.object(failure_notes.platforms, "file_lock",
                               create=True, side_effect=fake_file_lock):
            with failure_notes._Lock(lock_path, timeout=0.25) as held:
                self.assertIsInstance(held, failure_notes._Lock)
        self.assertEqual(seen["timeout"], 0.25)
        self.assertEqual(seen["path"], str(lock_path))

        def timeout_lock(path, timeout=10.0):
            raise TimeoutError("bounded")

        with mock.patch.object(failure_notes.platforms, "file_lock",
                               create=True, side_effect=timeout_lock):
            with self.assertRaises(failure_notes._LockTimeout):
                with failure_notes._Lock(lock_path, timeout=0.1):
                    pass
        source = inspect.getsource(failure_notes)
        self.assertNotIn("import fcntl", source)
        self.assertNotIn("import msvcrt", source)

    # ---- rows 10/12: reparse/symlink escapes refused before write ---------
    def test_row10_progress_and_authority_refuse_escape_before_write(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "link"
        try:
            os.symlink(str(outside), str(link))
        except (OSError, NotImplementedError) as exc:
            if os.name == "nt":
                self.skipTest(
                    "creating a symlink needs Developer Mode or the "
                    "SeCreateSymbolicLinkPrivilege: %s" % exc)
            self.fail("symlink support is required for the escape test: %s" % exc)
        target = link / "state.json"
        with mock.patch.object(progress.platforms, "reparse_point",
                               create=True, return_value=True):
            with self.assertRaises(progress.ProgressPathError):
                progress.write_json(target, {"x": 1}, root=self.workspace,
                                    system="Windows")
        self.assertFalse((outside / "state.json").exists())

        with mock.patch.object(authority.platforms, "reparse_point",
                               create=True, return_value=True):
            with self.assertRaises(authority.WorkspaceRootError):
                authority.require_safe_path(str(target), str(self.workspace),
                                            system="Windows")
        self.assertFalse((outside / "state.json").exists())

        # POSIX realpath check also refuses an escape without the Windows hook.
        with self.assertRaises(progress.ProgressPathError):
            progress.write_json(target, {"x": 1}, root=self.workspace)

    def test_row11_progress_json_is_utf8_lf(self):
        target = self.workspace / "state.json"
        progress.write_json(target, {"text": "héllo"}, root=self.workspace)
        raw = target.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIn("héllo".encode("utf-8"), raw)
        self.assertEqual(json.loads(raw.decode("utf-8"))["text"], "héllo")

    # ---- row 12: authority command parsing delegates, no shell=True -------
    def test_row12_authority_split_command_delegates(self):
        with mock.patch.object(authority.platforms, "split_command",
                               create=True, return_value=["a", "b"]) as split:
            self.assertEqual(authority.split_command('a "b c"',
                                                     system="Windows"),
                             ["a", "b"])
        split.assert_called_once_with('a "b c"', system="Windows")
        self.assertNotIn("shell=True", inspect.getsource(authority))


if __name__ == "__main__":
    unittest.main()
