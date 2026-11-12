"""3.13.0 platform primitives: liveness, tree kill, locks, paths, ACLs.

No test sends a real signal to another process or creates a Windows token.
"""

import contextlib
import ctypes
import os
import stat
import sys
import tempfile
import threading
import time
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


class FileLockConcurrencyTests(unittest.TestCase):
    def test_same_process_threads_are_serialized(self):
        threads = 8
        iterations = 6
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "threads.lock"
            guard = threading.Lock()
            state = {"inside": 0, "max": 0, "entries": 0}
            errors = []

            def worker():
                try:
                    for _ in range(iterations):
                        with platforms.file_lock(str(path), timeout=10):
                            with guard:
                                state["inside"] += 1
                                inside = state["inside"]
                                state["entries"] += 1
                                state["max"] = max(state["max"], inside)
                            try:
                                self.assertEqual(inside, 1)
                                time.sleep(0.002)
                            finally:
                                with guard:
                                    state["inside"] -= 1
                except BaseException as exc:  # reported below
                    errors.append(exc)

            started = time.monotonic()
            workers = [threading.Thread(target=worker)
                       for _ in range(threads)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join()
            elapsed = time.monotonic() - started

            self.assertEqual(errors, [])
            self.assertEqual(state["entries"], threads * iterations)
            self.assertEqual(state["max"], 1)
            self.assertLess(elapsed, 5.0)

    def test_same_process_open_window_is_serialized(self):
        # The RLock must cover os.open itself.  On Windows a second open of
        # the same lock file can fail transiently; the OS lock is acquired
        # only after the open, so it cannot protect that window.  Sleep
        # inside the patched open to make an unserialized window visible;
        # unfixed code shows max concurrency > 1 here.
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "open-window.lock"
            guard = threading.Lock()
            state = {"active": 0, "max": 0, "opens": 0}
            real_open = os.open

            def observed_open(file, flags, mode=0o777, *args, **kwargs):
                with guard:
                    state["active"] += 1
                    state["opens"] += 1
                    state["max"] = max(state["max"], state["active"])
                try:
                    time.sleep(0.005)
                    return real_open(file, flags, mode, *args, **kwargs)
                finally:
                    with guard:
                        state["active"] -= 1

            def worker():
                with platforms.file_lock(str(path), timeout=10):
                    time.sleep(0.001)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            with mock.patch.object(platforms.os, "open", new=observed_open):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(state["opens"], len(threads))
            self.assertEqual(state["max"], 1,
                             "lock-file open window was not serialized")

    def test_transient_permission_error_on_open_is_retried(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "transient.lock"
            real_open = os.open
            attempts = []

            def flaky_open(file, flags, mode=0o777, *args, **kwargs):
                attempts.append(file)
                if len(attempts) == 1:
                    raise PermissionError(13, "Permission denied", str(path))
                return real_open(file, flags, mode, *args, **kwargs)

            patched = mock.Mock(side_effect=flaky_open)
            acquired = []
            with mock.patch.object(platforms.os, "open", new=patched):
                with platforms.file_lock(str(path), timeout=2.0) as handle:
                    self.assertIsNotNone(handle)
                    acquired.append(True)

            self.assertEqual(acquired, [True])
            self.assertEqual(patched.call_count, 2)

    def test_persistent_permission_error_raises_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "persistent.lock"

            def denied_open(file, flags, mode=0o777, *args, **kwargs):
                raise PermissionError(13, "Permission denied", str(path))

            patched = mock.Mock(side_effect=denied_open)
            started = time.monotonic()
            with mock.patch.object(platforms.os, "open", new=patched):
                with self.assertRaises(TimeoutError) as caught:
                    with platforms.file_lock(str(path), timeout=0.25):
                        pass
            elapsed = time.monotonic() - started

            self.assertIn("cannot open lock", str(caught.exception))
            self.assertGreaterEqual(elapsed, 0.12)
            # Budget (0.25s) plus scheduling slack, not 5x.
            self.assertLess(elapsed, 0.35)
            self.assertGreaterEqual(patched.call_count, 2)

    def test_process_lock_released_when_open_fails(self):
        # The outer finally must release the process RLock even when
        # _open_lock_file raises.  Assert the release call directly: a
        # same-thread reacquire cannot prove it (RLock is reentrant) and a
        # later cross-thread probe cannot either, because an unreferenced
        # leaked lock is reaped from the weak registry.
        class TrackingLock(object):
            def __init__(self):
                self._lock = threading.RLock()
                self.releases = 0

            def acquire(self, *args, **kwargs):
                return self._lock.acquire(*args, **kwargs)

            def release(self):
                self.releases += 1
                return self._lock.release()

        tracker = TrackingLock()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "release.lock"
            with mock.patch.object(platforms, "_same_process_lock",
                                   return_value=tracker), \
                    mock.patch.object(
                        platforms.os, "open",
                        side_effect=PermissionError(13, "denied",
                                                    str(path))):
                with self.assertRaises(TimeoutError):
                    with platforms.file_lock(str(path), timeout=0.1):
                        pass
        self.assertEqual(tracker.releases, 1,
                         "process RLock was not released on failed open")

    def test_lock_is_released_when_body_raises(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "raised.lock"
            with self.assertRaises(RuntimeError):
                with platforms.file_lock(str(path), timeout=1.0):
                    raise RuntimeError("boom")
            # A leaked RLock would make this bounded reacquire time out.
            with platforms.file_lock(str(path), timeout=1.0) as handle:
                self.assertIsNotNone(handle)

    def test_windows_branch_uses_msvcrt_lock_and_unlock(self):
        # The Windows branch cannot run on this host, so inject a fake
        # msvcrt module and prove the branch still calls locking() at offset
        # 0 with LK_NBLCK on acquire and LK_UNLCK on release.
        class FakeMsvcrt(object):
            LK_NBLCK = 2
            LK_UNLCK = 0

            def __init__(self):
                self.calls = []

            def locking(self, fd, mode, nbytes):
                self.calls.append((mode, nbytes,
                                   os.lseek(fd, 0, os.SEEK_CUR)))

        fake = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "windows.lock"
            with mock.patch.dict(sys.modules, {"msvcrt": fake}):
                with platforms.file_lock(str(path), timeout=1.0,
                                         system="Windows") as handle:
                    self.assertIsNotNone(handle)
        self.assertEqual(fake.calls,
                         [(FakeMsvcrt.LK_NBLCK, 1, 0),
                          (FakeMsvcrt.LK_UNLCK, 1, 0)])


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
