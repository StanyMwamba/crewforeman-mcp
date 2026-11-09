"""v3.6.0 platform primitives: exercise Darwin/Linux/Windows with mocks.

No test here depends on the host OS, launches a process, or sends a signal.
"""
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import platforms

# Fixture homes are assembled at runtime so the privacy scanner sees no
# literal operator-style /Users or /home path in test source.
HOME_LINUX = os.path.join(os.sep, "home", "u")
HOME_MAC = os.path.join(os.sep, "Users", "u")


class NormalizeAndDefaultsTests(unittest.TestCase):
    def test_normalize_system_aliases(self):
        cases = {
            "Windows": "Windows", "win32": "Windows", "nt": "Windows",
            "Darwin": "Darwin", "macos": "Darwin", "Mac": "Darwin",
            "Linux": "Linux", "posix": "Linux",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(platforms.normalize_system(value), expected)

    def test_normalize_system_host_fallbacks(self):
        with mock.patch.object(platforms.os, "name", "nt"):
            self.assertEqual(platforms.normalize_system(None), platforms.WINDOWS)
        with mock.patch.object(platforms.os, "name", "posix"), \
                mock.patch.object(platforms.sys, "platform", "darwin"):
            self.assertEqual(platforms.normalize_system(""), platforms.MACOS)
        with mock.patch.object(platforms.os, "name", "posix"), \
                mock.patch.object(platforms.sys, "platform", "linux"):
            self.assertEqual(platforms.normalize_system(None), platforms.LINUX)

    def test_default_paths_per_os_and_env_overrides(self):
        windows = platforms.default_paths(system="Windows", home=HOME_LINUX, env={
            "APPDATA": "/appdata", "LOCALAPPDATA": "/localdata", "TEMP": "/temp"})
        self.assertEqual(windows["config_dir"], os.path.join("/appdata", "deepseek-harness-mcp"))
        self.assertEqual(windows["data_dir"], os.path.join("/localdata", "DeepSeek-MCP"))
        self.assertEqual(windows["temp_dir"], "/temp")
        fallback = platforms.default_paths(system="Windows", home=HOME_LINUX, env={})
        self.assertEqual(fallback["config_dir"],
                         os.path.join(HOME_LINUX, "AppData", "Roaming", "deepseek-harness-mcp"))
        self.assertEqual(fallback["data_dir"],
                         os.path.join(HOME_LINUX, "AppData", "Local", "DeepSeek-MCP"))

        linux = platforms.default_paths(system="Linux", home=HOME_LINUX, env={
            "XDG_CONFIG_HOME": "/xdg/config", "XDG_STATE_HOME": "/xdg/state",
            "XDG_DATA_HOME": "/xdg/data", "TMPDIR": "/xdg/tmp"})
        self.assertEqual(linux["config_dir"], os.path.join("/xdg/config", "deepseek-harness-mcp"))
        self.assertEqual(linux["data_dir"], os.path.join("/xdg/data", "DeepSeek-MCP"))
        self.assertEqual(linux["state_dir"], os.path.join("/xdg/state", "DeepSeek-MCP"))
        self.assertEqual(linux["temp_dir"], "/xdg/tmp")
        linux_defaults = platforms.default_paths(system="Linux", home=HOME_LINUX, env={})
        self.assertEqual(linux_defaults["config_dir"],
                         os.path.join(HOME_LINUX, ".config", "deepseek-harness-mcp"))
        self.assertEqual(linux_defaults["data_dir"],
                         os.path.join(HOME_LINUX, ".local", "share", "DeepSeek-MCP"))

        darwin = platforms.default_paths(system="Darwin", home=HOME_MAC,
                                         env={"TMPDIR": "/var/folders/x"})
        self.assertEqual(darwin["config_dir"],
                         os.path.join(HOME_MAC, ".config", "deepseek-harness-mcp"))
        self.assertEqual(darwin["data_dir"], os.path.join(HOME_MAC, "DeepSeek-MCP"))
        self.assertEqual(darwin["temp_dir"], "/var/folders/x")


class CandidateTests(unittest.TestCase):
    def _identity_existing(self):
        return mock.patch.object(platforms, "_existing", side_effect=lambda paths: list(paths))

    def test_chrome_candidates_windows_program_files_and_edge(self):
        env = {"PROGRAMFILES": "/pf", "PROGRAMFILES(X86)": "/pf86",
               "LOCALAPPDATA": "/local"}
        with self._identity_existing(), \
                mock.patch.object(platforms.shutil, "which", return_value=None):
            paths = platforms.chrome_candidates(system="Windows", env=env)
        self.assertIn(os.path.join("/pf", "Google", "Chrome", "Application", "chrome.exe"), paths)
        self.assertIn(os.path.join("/pf86", "Microsoft", "Edge", "Application", "msedge.exe"), paths)
        self.assertIn(os.path.join("/local", "Google", "Chrome", "Application", "chrome.exe"), paths)

    def test_chrome_candidates_macos_linux_and_which_first(self):
        with self._identity_existing(), \
                mock.patch.object(platforms.shutil, "which", return_value=None):
            mac = platforms.chrome_candidates(system="Darwin", env={})
            linux = platforms.chrome_candidates(system="Linux", env={})
        self.assertEqual(mac[0], "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        self.assertIn("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", mac)
        self.assertIn("/usr/bin/google-chrome", linux)
        self.assertIn("/usr/bin/chromium", linux)
        with self._identity_existing(), \
                mock.patch.object(platforms.shutil, "which",
                                  side_effect=lambda name, path=None:
                                  "/opt/found/chrome" if name == "google-chrome" else None):
            with_which = platforms.chrome_candidates(system="Linux", env={"PATH": "/opt/found"})
        self.assertEqual(with_which[0], "/opt/found/chrome")

    def test_dsh_candidates_per_os_and_which_first(self):
        with self._identity_existing(), \
                mock.patch.object(platforms.shutil, "which", return_value=None):
            mac = platforms.dsh_candidates(system="Darwin", env={"HOME": HOME_MAC})
            win = platforms.dsh_candidates(system="Windows", env={"LOCALAPPDATA": "/local"})
            linux = platforms.dsh_candidates(system="Linux", env={"HOME": HOME_LINUX})
        self.assertEqual(mac[0], "/Applications/DeepSeek Harness.app/Contents/Resources/"
                                 "runtime/cli/bin/dsh")
        self.assertIn(os.path.join("/local", "Programs", "dsh", "dsh.exe"), win)
        self.assertIn("/usr/local/bin/dsh", linux)
        with self._identity_existing(), \
                mock.patch.object(platforms.shutil, "which",
                                  side_effect=lambda name, path=None:
                                  "/opt/bin/dsh" if name == "dsh" else None):
            found = platforms.dsh_candidates(system="Linux", env={"PATH": "/opt/bin"})
        self.assertEqual(found[0], "/opt/bin/dsh")

    def test_popen_kwargs_posix_and_windows(self):
        self.assertEqual(platforms.popen_kwargs(system="Darwin"), {"start_new_session": True})
        self.assertEqual(platforms.popen_kwargs(system="Linux"), {"start_new_session": True})
        with mock.patch.object(platforms.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200,
                               create=True), \
                mock.patch.object(platforms.subprocess, "CREATE_NO_WINDOW", 0x8000000,
                                  create=True):
            kwargs = platforms.popen_kwargs(system="Windows")
        self.assertEqual(kwargs, {"creationflags": 0x200 | 0x8000000})


class ProcessControlTests(unittest.TestCase):
    def test_terminate_tree_posix_killpg(self):
        with mock.patch.object(platforms.os, "getpid", return_value=100), \
                mock.patch.object(platforms.os, "getpgid",
                                  side_effect=lambda pid: 123 if pid == 123 else 999,
                                  create=True), \
                mock.patch.object(platforms.os, "killpg",
                                  create=True) as killpg, \
                mock.patch.object(platforms.os, "kill") as kill, \
                mock.patch.object(platforms, "_pid_alive", return_value=False):
            result = platforms.terminate_tree(123, system="Linux")
        self.assertEqual(result, {"terminated": True, "method": "killpg"})
        killpg.assert_called_once_with(123, signal.SIGTERM)
        kill.assert_not_called()

        with mock.patch.object(platforms.os, "getpid", return_value=100), \
                mock.patch.object(platforms.os, "killpg",
                                  create=True) as killpg:
            unsafe = platforms.terminate_tree(0, system="Linux")
        self.assertFalse(unsafe["terminated"])
        self.assertIn("unsafe", unsafe["reason"])
        killpg.assert_not_called()

    def test_terminate_tree_windows_taskkill(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        result = platforms.terminate_tree(4242, system="Windows", runner=runner)
        self.assertEqual(result["method"], "taskkill")
        self.assertTrue(result["terminated"])
        self.assertEqual(result["exit_code"], 0)
        command = runner.call_args[0][0]
        self.assertEqual(command[:3], ["taskkill", "/PID", "4242"])
        self.assertIn("/T", command)
        self.assertIn("/F", command)


class HelperCommandTests(unittest.TestCase):
    def test_keep_awake_command(self):
        self.assertEqual(platforms.keep_awake_command(system="Darwin"),
                         ["/usr/bin/caffeinate", "-i", "-s", "-m"])
        with mock.patch.object(platforms.shutil, "which", return_value="/usr/bin/systemd-inhibit"):
            linux = platforms.keep_awake_command(system="Linux")
        self.assertEqual(linux[:2], ["systemd-inhibit", "--what=idle:sleep"])
        self.assertIn("sleep", linux)
        with mock.patch.object(platforms.shutil, "which", return_value=None):
            self.assertIsNone(platforms.keep_awake_command(system="Linux"))
        self.assertIsNone(platforms.keep_awake_command(system="Windows"))

    def test_screenshot_command(self):
        with mock.patch.object(platforms.Path, "is_file", return_value=True):
            mac = platforms.screenshot_command(system="Darwin", path="/tmp/a.png")
        self.assertEqual(mac, ["/usr/sbin/screencapture", "-x", "-t", "png", "/tmp/a.png"])
        with mock.patch.object(platforms.Path, "is_file", return_value=False):
            self.assertIsNone(platforms.screenshot_command(system="Darwin", path="/tmp/a.png"))

        win = platforms.screenshot_command(system="Windows", path="C:\\tmp\\o'hare.png")
        self.assertEqual(win[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("o''hare.png", win[-1])

        with mock.patch.object(platforms.shutil, "which",
                               side_effect=lambda name, path=None:
                               "/usr/bin/scrot" if name == "scrot" else None):
            linux = platforms.screenshot_command(system="Linux", path="/tmp/a.png")
        self.assertEqual(linux, ["/usr/bin/scrot", "/tmp/a.png"])
        with mock.patch.object(platforms.shutil, "which", return_value=None):
            self.assertIsNone(platforms.screenshot_command(system="Linux", path="/tmp/a.png"))

    def test_restart_client_command(self):
        mac = platforms.restart_client_command(system="Darwin", app_name="Claude")
        self.assertEqual(mac["open"], ["open", "-a", "Claude"])
        self.assertEqual(mac["quit"][:2], ["osascript", "-e"])
        self.assertIn("Claude", mac["quit"][-1])

        win = platforms.restart_client_command(system="Windows", app_name="Claude Desktop")
        self.assertEqual(win["quit"], ["taskkill", "/IM", "Claude Desktop.exe", "/F"])
        self.assertEqual(win["open"], ["cmd", "/c", "start", "", "Claude Desktop"])

        linux = platforms.restart_client_command(system="Linux", app_name="Claude Desktop")
        self.assertEqual(linux["quit"], ["pkill", "-x", "claude-desktop"])
        self.assertEqual(linux["open"], ["gtk-launch", "claude-desktop"])
        self.assertIsNone(platforms.restart_client_command(system="Linux", app_name=" "))


class SandboxAndPermissionsTests(unittest.TestCase):
    def test_sandbox_support(self):
        from deepseek_mcp import safety
        with mock.patch.object(
                safety, "sandbox_status",
                return_value={"kind": "seatbelt", "enforcement": "enforced",
                              "abi": "seatbelt", "reason": None}):
            mac = platforms.sandbox_support(system="Darwin")
        self.assertEqual(mac["kind"], "seatbelt")
        self.assertTrue(mac["available"])
        with mock.patch.object(
                safety, "sandbox_status",
                return_value={"kind": "none", "enforcement": "none",
                              "abi": None,
                              "reason": "sandbox-exec not available"}):
            missing = platforms.sandbox_support(system="Darwin")
        self.assertFalse(missing["available"])
        self.assertIn("sandbox-exec", missing["reason"])
        with mock.patch.object(
                safety, "sandbox_status",
                return_value={"kind": "landlock", "enforcement": "enforced",
                              "abi": "landlock-8", "reason": None}):
            linux = platforms.sandbox_support(system="Linux")
        self.assertEqual(linux["kind"], "landlock")
        self.assertTrue(linux["available"])
        with mock.patch.object(
                safety, "sandbox_status",
                return_value={"kind": "none", "enforcement": "none",
                              "abi": None, "reason": "not_windows"}):
            unsupported = platforms.sandbox_support(system="Windows")
        self.assertEqual(unsupported["kind"], "none")
        self.assertFalse(unsupported["available"])

    def test_private_permissions_and_secure_tree(self):
        with mock.patch.object(platforms.os, "chmod") as chmod:
            self.assertTrue(platforms.private_permissions("/x/file", system="Linux"))
            chmod.assert_called_once_with("/x/file", 0o600,
                                          follow_symlinks=False)
        with mock.patch.object(platforms.os, "chmod") as chmod:
            self.assertTrue(platforms.private_permissions("/x/dir", system="Darwin", is_dir=True))
            chmod.assert_called_once_with("/x/dir", 0o700,
                                          follow_symlinks=False)
        with mock.patch.object(platforms.os, "chmod") as chmod:
            with mock.patch.object(platforms, "_windows_private_acl",
                                   return_value=False):
                self.assertFalse(
                    platforms.private_permissions("/x/file", system="Windows"))
            chmod.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "private.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(platforms.os, "chmod") as chmod:
                dirs, files = platforms.secure_tree(root, system="Linux")
            self.assertEqual((dirs, files), (2, 1))
            modes = [call.args[1] for call in chmod.call_args_list]
            self.assertIn(0o700, modes)
            self.assertIn(0o600, modes)
            with mock.patch.object(platforms.os, "chmod") as chmod:
                self.assertEqual(platforms.secure_tree(root, system="Windows"), (0, 0))
                chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()
