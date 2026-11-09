"""3.13.0 platform primitives: liveness, tree kill, locks, paths, ACLs.

No test sends a real signal to another process or creates a Windows token.
"""

import contextlib
import ctypes
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import platforms


class FakeKernel32:
    def __init__(self):
        self.closed = []
        self.exit_code = 259

    def OpenProcess(self, access, inherit, pid):
        return 0 if pid <= 0 else 1234

    def GetExitCodeProcess(self, handle, ref):
        ref._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class PidAliveTests(unittest.TestCase):
    def test_windows_uses_exit_code_not_signal_zero(self):
        fake = FakeKernel32()
        with mock.patch.object(ctypes, "windll", create=True,
                               new=mock.Mock(kernel32=fake)), \
                mock.patch.object(platforms.os, "kill",
                                  side_effect=AssertionError("signal 0")):
            self.assertTrue(platforms.pid_alive(4321, system="Windows"))
            fake.exit_code = 1
            self.assertFalse(platforms.pid_alive(4321, system="Windows"))
        self.assertTrue(fake.closed)

    def test_posix_branch_uses_signal_zero(self):
        with mock.patch.object(platforms.os, "kill") as kill:
            self.assertTrue(platforms.pid_alive(7, system="Linux"))
        kill.assert_called_once_with(7, 0)

    def test_invalid_pid_is_false(self):
        for value in (0, -1, "x", None):
            self.assertFalse(platforms.pid_alive(value, system="Linux"))


class TerminateTreeTests(unittest.TestCase):
    def test_windows_taskkill_tree(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0)

        result = platforms.terminate_tree(99, system="Windows", runner=runner)
        self.assertEqual(result["method"], "taskkill")
        self.assertTrue(result["terminated"])
        self.assertEqual(calls[0], ["taskkill", "/PID", "99", "/T", "/F"])

    def test_posix_signals_own_group_only(self):
        with mock.patch.object(platforms.os, "getpgid",
                               side_effect=lambda pid: 55 if pid == 55 else 1,
                               create=True), \
                mock.patch.object(platforms.os, "killpg",
                                  create=True) as killpg, \
                mock.patch.object(platforms.os, "kill") as kill, \
                mock.patch.object(platforms, "_pid_alive",
                                  return_value=False):
            result = platforms.terminate_tree(55, system="Linux")
        self.assertTrue(result["terminated"])
        killpg.assert_called_once_with(55, platforms.signal.SIGTERM)
        kill.assert_not_called()

    def test_refuses_self(self):
        result = platforms.terminate_tree(os.getpid(), system="Linux")
        self.assertFalse(result["terminated"])


class FileLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "store.lock"
            with platforms.file_lock(str(path), timeout=1.0) as first:
                self.assertIsNotNone(first)
            with platforms.file_lock(str(path), timeout=1.0) as second:
                self.assertIsNotNone(second)
            self.assertTrue(path.exists())

    def test_bad_parent_is_bounded_timeout(self):
        # An existing *file* cannot become the lock's parent directory.
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / "not-a-dir"
            parent.write_text("x", encoding="utf-8")
            with self.assertRaises(TimeoutError):
                with platforms.file_lock(str(parent / "x.lock"), timeout=0.05):
                    pass


class PermissionTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == "nt",
        "Windows has no POSIX 0600/0700 mode bits; the Windows ACL path is "
        "covered by test_windows_acl_uses_absolute_icacls")
    def test_posix_file_and_dir_modes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "d"
            directory.mkdir()
            path = directory / "f"
            path.write_text("x", encoding="utf-8")
            self.assertTrue(platforms.private_permissions(
                str(directory), system="Linux", is_dir=True))
            self.assertTrue(platforms.private_permissions(
                str(path), system="Linux"))
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(
        os.name == "nt",
        "creating a symlink and reading POSIX mode bits are not portable to "
        "a Windows host")
    def test_symlink_target_is_not_chmodded(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "target"
            target.write_text("x", encoding="utf-8")
            os.chmod(str(target), 0o644)
            link = Path(root) / "link"
            os.symlink(str(target), str(link))
            # follow_symlinks=False must never chmod the link target.
            platforms.private_permissions(str(link), system="Linux")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_windows_acl_uses_absolute_icacls(self):
        with mock.patch.object(platforms, "_windows_user_sid",
                               return_value="S-1-5-21-1"), \
                mock.patch.object(platforms, "_icacls_binary",
                                  return_value=r"C:\Windows\System32\icacls.exe"), \
                mock.patch.object(platforms.os.path, "isfile",
                                  return_value=True), \
                mock.patch.object(platforms.subprocess, "run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout=b"",
                                         stderr=b""),
                               mock.Mock(returncode=0,
                                         stdout=b"S-1-5-21-1:(F)",
                                         stderr=b"")]
            self.assertTrue(platforms.private_permissions(
                r"C:\work\f", system="Windows"))
        first = run.call_args_list[0].args[0]
        self.assertEqual(first[0], r"C:\Windows\System32\icacls.exe")
        self.assertIn("/inheritance:r", first)
        self.assertIn("*S-1-5-21-1:F", first)


class PathAndCommandTests(unittest.TestCase):
    def test_canonical_and_reparse(self):
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / "real"
            real.write_text("x", encoding="utf-8")
            link = Path(root) / "link"
            os.symlink(str(real), str(link))
            self.assertEqual(platforms.canonical_path(str(link)),
                             platforms.canonical_path(str(real)))
            self.assertTrue(platforms.reparse_point(str(link)))
            self.assertFalse(platforms.reparse_point(str(real)))
            self.assertTrue(platforms.inside_path(str(real), root))
            self.assertFalse(platforms.inside_path(str(Path(root) / ".."),
                                                   root))

    def test_split_command_json_shlex_and_windows(self):
        self.assertEqual(platforms.split_command('["a", "b c"]'),
                         ["a", "b c"])
        self.assertEqual(platforms.split_command("a 'b c'", system="Linux"),
                         ["a", "b c"])
        self.assertEqual(
            platforms.split_command('"C:\\Program Files\\x.exe" /q',
                                    system="Windows"),
            ['"C:\\Program Files\\x.exe"', "/q"])

    def test_default_paths_use_platform_temp(self):
        linux = platforms.default_paths(
            system="Linux", home="/h",
            env={"TMPDIR": "", "XDG_CONFIG_HOME": ""})
        self.assertEqual(linux["temp_dir"], tempfile.gettempdir())
        win = platforms.default_paths(
            system="Windows", home=r"C:\Users\u",
            env={"TEMP": r"C:\Temp", "APPDATA": r"C:\Roaming",
                 "LOCALAPPDATA": r"C:\Local"})
        self.assertEqual(win["temp_dir"], r"C:\Temp")

    def test_restart_client_command_delegates(self):
        with mock.patch("deepseek_mcp.restart_helper.resolve_commands",
                        return_value={"close": ["close"], "open": ["open"],
                                      "verify": ["verify"],
                                      "expected_executable": "/x"}) as resolve:
            commands = platforms.restart_client_command(
                system="Linux", app_name="Claude")
        resolve.assert_called_once_with("linux", "Claude")
        self.assertEqual(commands["quit"], ["close"])
        self.assertEqual(commands["open"], ["open"])
        self.assertEqual(commands["verify"], ["verify"])


if __name__ == "__main__":
    unittest.main()
