import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import clients, restart_helper
from deepseek_mcp.config import Config
import tests

REPO = Path(__file__).resolve().parent.parent


def _py(code, *args):
    return [sys.executable, "-c", code] + [str(arg) for arg in args]


class RestartHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.status = self.root / "restart-status.json"

    def test_retries_open_until_verify_succeeds(self):
        counter = self.root / "opens.txt"
        args = {
            "app_name": "FakeApp",
            "system": "linux",
            "attempts": 3,
            "retry_seconds": 0.01,
            "status_path": str(self.status),
            "close": _py("pass"),
            "open": _py(
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                "old=p.read_text() if p.exists() else '0'; "
                "p.write_text(str(int(old or '0') + 1))", counter),
            "verify": _py(
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                "old=p.read_text() if p.exists() else '0'; "
                "sys.exit(0 if int(old or '0') >= 2 else 1)", counter),
        }
        record = restart_helper.run_once(args)
        self.assertEqual(record["state"], "reopened")
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(counter.read_text(encoding="utf-8"), "2")
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "reopened")
        self.assertEqual(data["attempts"], 2)
        self.assertTrue(data["started_at"])
        self.assertTrue(data["updated_at"])
        self.assertEqual(data["status_path"], str(self.status))
        self.assertEqual(len(data["attempts_detail"]), 2)

    def test_spawned_helper_survives_and_writes_reopened_status(self):
        import time
        marker = self.root / "opened.txt"
        args = {
            "app_name": "FakeApp",
            "system": "linux",
            "attempts": 2,
            "retry_seconds": 0.01,
            "initial_delay": 0.01,
            "status_path": str(self.status),
            "close": _py("pass"),
            "open": _py(
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('open')",
                marker),
            "verify": _py(
                "import pathlib,sys; "
                "sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 1)",
                marker),
        }
        pid = restart_helper.spawn_restart_helper(args)
        self.assertGreater(pid, 0)
        record = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            record = restart_helper.read_status(self.status)
            if record.get("state") in ("reopened", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(record.get("state"), "reopened", record)
        self.assertTrue(marker.is_file())
        self.assertEqual(record.get("helper_pid"), pid)
        self.assertTrue(record.get("attempts_detail"))

    def test_all_attempts_fail_records_failed_status(self):
        args = {
            "app_name": "FakeApp",
            "system": "linux",
            "attempts": 3,
            "retry_seconds": 0.01,
            "status_path": str(self.status),
            "close": _py("pass"),
            "open": _py("pass"),
            "verify": _py("import sys; sys.exit(1)"),
        }
        record = restart_helper.run_once(args)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["attempts"], 3)
        self.assertTrue(record["error"])
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["attempts"], 3)
        self.assertIn("verification failed", data["error"])

    def test_parent_spawns_detached_helper_and_writes_scheduled_status(self):
        cfg = Config({"state_dir": str(self.root),
                      "restart_status_path": str(self.status)})

        def runner(argv, timeout=15):
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch(
                "deepseek_mcp.clients.restart_helper.spawn_restart_helper",
                return_value=4321) as spawn:
            message = clients.restart_client_app(
                {"app_name": "FakeApp", "wait_seconds": 0},
                cfg=cfg, system="linux", runner=runner)
        self.assertIn("scheduled", message)
        self.assertIn(str(self.status), message)
        spawn.assert_called_once()
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "scheduled")
        self.assertEqual(data["helper_pid"], 4321)
        self.assertEqual(data["status_path"], str(self.status))

    def test_config_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.restart_helper_attempts, 3)
        self.assertEqual(cfg.restart_helper_retry_seconds, 2.0)
        self.assertEqual(cfg.restart_status_path, "")


class MacOSDefaultCommandTests(unittest.TestCase):
    """macOS keeps osascript verify and adds an ancestor-aware process check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.status = Path(self.tmp.name) / "restart-status.json"
        # Deterministic process-check decisions: the real helper asks ps for
        # its ancestor PIDs; these tests use fake PIDs.
        patcher = mock.patch.object(restart_helper, "_ancestor_pids",
                                    return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_macos_verify_osascript_and_process_check_includes_ancestors(self):
        commands = restart_helper.resolve_commands("macos", "Claude")
        self.assertEqual(commands["verify"],
                         ["osascript", "-e",
                          'application "Claude" is running'])
        self.assertEqual(commands["process_check"],
                         ["pgrep", "-a", "-x", "Claude"])
        # Plain -x excludes ancestors and can miss a running Claude.
        self.assertEqual(commands["process_check"][:2], ["pgrep", "-a"])
        self.assertNotEqual(commands["process_check"],
                            ["pgrep", "-x", "Claude"])
        self.assertEqual(restart_helper.macos_process_check("Claude"),
                         ["pgrep", "-a", "-x", "Claude"])

    def test_run_once_executes_verify_and_process_check(self):
        seen = []

        def runner(argv, timeout=None):
            seen.append(list(argv))
            if list(argv) == ["pgrep", "-a", "-x", "Claude"]:
                return subprocess.CompletedProcess(argv, 0, "4242 Claude\n", "")
            # macOS osascript verify requires stdout "true".
            return subprocess.CompletedProcess(argv, 0, "true", "")

        record = restart_helper.run_once({
            "app_name": "Claude", "system": "macos", "attempts": 1,
            "retry_seconds": 0, "status_path": str(self.status),
            "close": _py("pass"), "open": _py("pass"), "runner": runner,
        })
        self.assertEqual(record["state"], "reopened")
        self.assertEqual(record["process_check"],
                         ["pgrep", "-a", "-x", "Claude"])
        # Both the authoritative osascript verify and the process check are
        # executed in the same attempt.
        self.assertIn(["osascript", "-e",
                       'application "Claude" is running'], seen)
        self.assertIn(["pgrep", "-a", "-x", "Claude"], seen)
        detail = record["attempts_detail"][0]
        self.assertEqual(detail["verified_by"], "both")
        self.assertEqual(detail["verify_returncode"], 0)
        self.assertEqual(detail["process_check_returncode"], 0)
        self.assertEqual(detail["process_check_output"], "4242 Claude")
        self.assertTrue(detail["process_check_ok"])
        self.assertTrue(record["process_check_ok"])
        self.assertEqual(record["verified_by"], "both")
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["process_check"],
                         ["pgrep", "-a", "-x", "Claude"])
        self.assertTrue(data["process_check_ok"])
        self.assertEqual(data["attempts_detail"][0]["verified_by"], "both")

    def test_pgrep_alone_does_not_override_failed_osascript(self):
        # Regression for the reviewed false positive: osascript said the app
        # was not running while pgrep found a (non-ancestor) process.  pgrep
        # is corroboration only and must not turn the attempt into a reopen.
        def runner(argv, timeout=None):
            if list(argv) == ["pgrep", "-a", "-x", "Claude"]:
                return subprocess.CompletedProcess(argv, 0, "4242 Claude\n", "")
            if list(argv)[:1] == ["osascript"]:
                return subprocess.CompletedProcess(argv, 1, "false", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        record = restart_helper.run_once({
            "app_name": "Claude", "system": "macos", "attempts": 1,
            "retry_seconds": 0, "status_path": str(self.status),
            "close": _py("pass"), "open": _py("pass"), "runner": runner,
        })
        self.assertEqual(record["state"], "failed")
        self.assertNotEqual(record.get("verified_by"), "process_check")
        detail = record["attempts_detail"][0]
        self.assertFalse(detail["verified"])
        self.assertEqual(detail["verified_by"], "")
        self.assertEqual(detail["verify_returncode"], 1)
        self.assertEqual(detail["process_check_returncode"], 0)
        self.assertEqual(detail["process_check_output"], "4242 Claude")
        self.assertTrue(detail["process_check_ok"])
        self.assertTrue(detail["process_check_ancestry_known"])
        self.assertIn("osascript did not confirm", record["error"])
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "failed")

    def test_ancestor_lookup_failure_is_fail_closed(self):
        # In the Harness sandbox /bin/ps can be denied, so the ancestor walk
        # is unknown.  The only pgrep match is then an old Claude ancestor;
        # it must not be accepted as proof that the app reopened.
        with mock.patch.object(restart_helper, "_ancestor_pids",
                               return_value=None):
            def runner(argv, timeout=None):
                if list(argv) == ["pgrep", "-a", "-x", "Claude"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "123 Claude\n", "")
                if list(argv)[:1] == ["osascript"]:
                    return subprocess.CompletedProcess(argv, 1, "false", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            record = restart_helper.run_once({
                "app_name": "Claude", "system": "macos", "attempts": 1,
                "retry_seconds": 0, "status_path": str(self.status),
                "close": _py("pass"), "open": _py("pass"), "runner": runner,
            })
        self.assertEqual(record["state"], "failed")
        self.assertNotEqual(record.get("verified_by"), "process_check")
        detail = record["attempts_detail"][0]
        self.assertFalse(detail["verified"])
        self.assertEqual(detail["verified_by"], "")
        self.assertFalse(detail["process_check_ok"])
        self.assertFalse(detail["process_check_ancestry_known"])
        self.assertEqual(detail["process_check_pids"], [123])
        self.assertEqual(detail["process_check_matches"], [])
        self.assertIn("ancestor lookup failed", record["error"])

    def test_osascript_alone_verifies_when_process_check_is_empty(self):
        def runner(argv, timeout=None):
            if list(argv) == ["pgrep", "-a", "-x", "Claude"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            if list(argv)[:1] == ["osascript"]:
                return subprocess.CompletedProcess(argv, 0, "true", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        record = restart_helper.run_once({
            "app_name": "Claude", "system": "macos", "attempts": 1,
            "retry_seconds": 0, "status_path": str(self.status),
            "close": _py("pass"), "open": _py("pass"), "runner": runner,
        })
        self.assertEqual(record["state"], "reopened")
        detail = record["attempts_detail"][0]
        self.assertEqual(detail["verified_by"], "osascript")
        self.assertEqual(detail["verify_returncode"], 0)
        self.assertEqual(detail["process_check_returncode"], 1)

    def test_both_checks_failing_fails_the_attempt(self):
        def runner(argv, timeout=None):
            if list(argv) == ["pgrep", "-a", "-x", "Claude"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            if list(argv)[:1] == ["osascript"]:
                return subprocess.CompletedProcess(argv, 1, "false", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        record = restart_helper.run_once({
            "app_name": "Claude", "system": "macos", "attempts": 1,
            "retry_seconds": 0, "status_path": str(self.status),
            "close": _py("pass"), "open": _py("pass"), "runner": runner,
        })
        self.assertEqual(record["state"], "failed")
        detail = record["attempts_detail"][0]
        self.assertFalse(detail["verified"])
        self.assertEqual(detail["verified_by"], "")
        self.assertEqual(detail["verify_returncode"], 1)
        self.assertEqual(detail["process_check_returncode"], 1)
        self.assertIn("verification failed", record["error"])
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "failed")

    def test_process_check_ok_requires_zero_exit_and_numeric_pid(self):
        self.assertTrue(restart_helper._process_check_ok(
            0, "123 Claude\n", ancestors=set()))
        self.assertTrue(restart_helper._process_check_ok(
            0, "  456  Claude\n", ancestors=set()))
        self.assertFalse(restart_helper._process_check_ok(
            0, "", ancestors=set()))
        self.assertFalse(restart_helper._process_check_ok(
            0, "no pid here", ancestors=set()))
        self.assertFalse(restart_helper._process_check_ok(
            1, "123 Claude\n", ancestors=set()))
        # Unknown ancestry (ps denied) is fail closed, not "no ancestors".
        with mock.patch.object(restart_helper, "_ancestor_pids",
                               return_value=None):
            self.assertFalse(restart_helper._process_check_ok(
                0, "123 Claude\n"))

    def test_process_check_ignores_ancestor_claude(self):
        # pgrep -a includes ancestors; an old Claude that is still our
        # ancestor must not by itself prove the reopened app is running.
        self.assertFalse(restart_helper._process_check_ok(
            0, "123 Claude\n", ancestors={123}))
        self.assertTrue(restart_helper._process_check_ok(
            0, "123 Claude\n9999 Claude\n", ancestors={123}))


class LinuxDefaultCommandTests(unittest.TestCase):
    """The real Linux defaults must use exact names, never full cmdlines."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.status = self.root / "restart-status.json"
        if os.name != "posix":
            self.skipTest("Linux restart defaults are POSIX-only")
        # Ubuntu 26.04 ships a multicall coreutils whose applets are hard
        # links: executing a symlink named ``cfp32fakeapp`` fails with
        # "Requested utility does not match executable name".  Build a tiny
        # real executable with the app name instead; fall back to the old
        # symlink only when no C compiler exists.
        self.fake_app = self.root / "cfp32fakeapp"
        self.expected_exe = self.fake_app
        compiler = (shutil.which("cc") or shutil.which("gcc")
                    or shutil.which("clang"))
        if compiler:
            source_file = self.root / "cfp32fakeapp.c"
            source_file.write_text(
                "#include <stdlib.h>\n#include <unistd.h>\n"
                "int main(int argc, char **argv) {\n"
                "  sleep(argc > 1 ? atoi(argv[1]) : 60);\n"
                "  return 0;\n}\n", encoding="utf-8")
            subprocess.run([compiler, "-O0", "-o", str(self.fake_app),
                            str(source_file)], check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        elif sys.platform.startswith("linux"):
            # No compiler in a minimal VM.  A script cannot own the process
            # name, so set the task comm explicitly; identity is the
            # interpreter that /proc/<pid>/exe really points at.
            self.fake_app.write_text(
                "#!%s\nimport ctypes, sys, time\n"
                "ctypes.CDLL(None).prctl(15, b'cfp32fakeapp', 0, 0, 0)\n"
                "time.sleep(int(sys.argv[1]) if len(sys.argv) > 1 else 60)\n"
                % sys.executable, encoding="utf-8")
            self.fake_app.chmod(0o755)
            self.expected_exe = Path(sys.executable)
        else:
            source = shutil.which("sleep") or "/bin/sleep"
            os.symlink(os.path.realpath(source), str(self.fake_app))
        self.decoys = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.decoys:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                pass
        try:
            subprocess.run(["pkill", "-x", "cfp32fakeapp"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass

    def _which(self, name):
        return str(self.fake_app) if name == "cfp32fakeapp" else None

    def _launch_open(self):
        # Detached on purpose (the reopen must survive the opener); mark the
        # handle as reaped so this short-lived opener script does not emit a
        # ResourceWarning at exit (NB6).
        return _py(
            "import subprocess, sys; "
            "p = subprocess.Popen([sys.argv[1], '60'], start_new_session=True, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "p.returncode = 0",
            self.fake_app)

    def test_defaults_use_exact_process_name_not_full_cmdline(self):
        commands = restart_helper.resolve_commands(
            "linux", "cfp32fakeapp", which=self._which)
        self.assertEqual(commands["close"][:2], ["pkill", "-x"])
        self.assertEqual(commands["verify"][:2], ["pgrep", "-x"])
        self.assertEqual(commands["close"][2], "cfp32fakeapp")
        self.assertEqual(commands["verify"][2], "cfp32fakeapp")
        for argv in (commands["close"], commands["verify"]):
            self.assertNotIn("-f", argv)
            self.assertNotIn("--full", argv)
        self.assertEqual(commands["expected_executable"], str(self.fake_app))

    def test_default_close_ignores_helper_shaped_decoy_and_reopens(self):
        # A process whose *command line* contains the app name (as the
        # detached helper's does via --app-name) must not be matched by
        # pkill -x.  It is the closest stand-in for the helper itself.
        decoy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             "--app-name", "cfp32fakeapp"],
            start_new_session=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.decoys.append(decoy)
        record = restart_helper.run_once({
            "app_name": "cfp32fakeapp", "system": "linux", "attempts": 2,
            "retry_seconds": 0.2, "status_path": str(self.status),
            "expected_exe": str(self.expected_exe),
            "open": self._launch_open(),
            "which": self._which,
        })
        self.assertEqual(record["state"], "reopened", record)
        self.assertEqual(decoy.poll(), None,
                         "the full-cmdline decoy (helper-shaped) was killed")
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "reopened")
        self.assertTrue(data["attempts_detail"][0]["verified"])
        self.assertEqual(data["attempts_detail"][0]["executable"],
                         str(self.expected_exe))

        # Now the failure path: the default close kills the running fake app,
        # the opener does nothing, and default verify must report failure.
        failed = restart_helper.run_once({
            "app_name": "cfp32fakeapp", "system": "linux", "attempts": 1,
            "retry_seconds": 0.2, "status_path": str(self.status),
            "expected_exe": str(self.expected_exe),
            "open": _py("pass"), "which": self._which,
        })
        self.assertEqual(failed["state"], "failed", failed)
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "failed")
        self.assertIn("verification failed", data["error"])
        self.assertEqual(decoy.poll(), None)

    def test_default_verify_checks_executable_identity(self):
        # pgrep -x matches the process name, but verification must also reject
        # a same-named process that is running a different executable.
        proc = subprocess.Popen(
            ["/bin/sleep", "60"], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.decoys.append(proc)
        code, out, err = restart_helper._execute(["pgrep", "-x", "sleep"])
        self.assertEqual(code, 0, err)
        self.assertFalse(restart_helper._verify_ok(
            "linux", code, out, err, expected_executable="/bin/cat"))
        self.assertTrue(restart_helper._verify_ok(
            "linux", code, out, err, expected_executable="/bin/sleep"))


class ExecutableIdentityTests(unittest.TestCase):
    """Same-basename executables must never satisfy identity checks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.status = self.root / "restart-status.json"
        if os.name != "posix":
            self.skipTest("process executable identity is POSIX-only")
        self.real = shutil.which("sleep") or "/bin/sleep"
        self.procs = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.procs:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                pass

    def _spawn(self, path):
        proc = subprocess.Popen(
            [str(path), "60"], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def _wait_for_pgrep(self, name):
        deadline = time.time() + 10
        code, out, err = 1, "", ""
        while time.time() < deadline:
            code, out, err = restart_helper._execute(["pgrep", "-x", name])
            if code == 0:
                break
            time.sleep(0.05)
        return code, out, err

    def _copy(self, directory, name):
        path = self.root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.real, str(path))
        os.chmod(str(path), 0o755)
        return path

    def test_same_basename_copy_is_not_the_same_executable(self):
        impostor = self._copy("impostor", "sleep")
        self.assertFalse(restart_helper._same_executable(
            str(impostor), "/bin/sleep"))
        self.assertTrue(restart_helper._same_executable(
            "/bin/sleep", "/bin/sleep"))

    def test_symlink_to_real_executable_is_the_same(self):
        link = self.root / "cfp32sleep-link"
        os.symlink(os.path.realpath(self.real), str(link))
        self.assertTrue(restart_helper._same_executable(
            str(link), self.real))

    def test_verify_rejects_same_basename_impostor(self):
        # A copy of /bin/sleep cannot be executed on this macOS host (the
        # signed platform binary is killed when run from a copied path), so a
        # real sleep process stands in while the identity layer reports the
        # copied same-basename path.
        impostor = self._copy("impostor", "sleep")
        self._spawn(self.real)
        code, out, err = self._wait_for_pgrep("sleep")
        self.assertEqual(code, 0, err)
        with mock.patch.object(restart_helper, "_process_exe",
                               return_value=str(impostor)):
            self.assertFalse(restart_helper._verify_ok(
                "linux", code, out, err, expected_executable="/bin/sleep"))
        self.assertTrue(restart_helper._verify_ok(
            "linux", code, out, err, expected_executable="/bin/sleep"))

    def test_run_once_default_verify_rejects_impostor_process(self):
        impostor = self._copy("impostor", "sleep")
        self._spawn(self.real)
        code, out, err = self._wait_for_pgrep("sleep")
        self.assertEqual(code, 0, err)
        with mock.patch.object(restart_helper, "_process_exe",
                               return_value=str(impostor)):
            record = restart_helper.run_once({
                "app_name": "sleep", "system": "linux", "attempts": 1,
                "retry_seconds": 0.1, "status_path": str(self.status),
                "expected_exe": "/bin/sleep",
                "close": _py("pass"), "open": _py("pass"),
            })
        self.assertEqual(record["state"], "failed", record)
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "failed")
        self.assertIn("verification failed", data["error"])


class DetachedSurvivalTests(unittest.TestCase):
    def test_helper_survives_termination_of_its_parent(self):
        tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        status = root / "restart-status.json"
        script = root / "parent.py"
        script.write_text(
            "import sys, time\n"
            "from deepseek_mcp import restart_helper\n"
            "restart_helper.spawn_restart_helper({\n"
            "    'app_name': 'FakeApp', 'system': 'linux', 'attempts': 2,\n"
            "    'retry_seconds': 0.05, 'initial_delay': 2.0,\n"
            "    'status_path': sys.argv[1],\n"
            "    'close': [sys.executable, '-c', 'pass'],\n"
            "    'open': [sys.executable, '-c', 'pass'],\n"
            "    'verify': [sys.executable, '-c', 'pass'],\n"
            "})\n"
            "print('spawned', flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        parent = subprocess.Popen(
            [sys.executable, str(script), str(status)], cwd=str(REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        deadline = time.time() + 10
        record = {}
        while time.time() < deadline:
            record = restart_helper.read_status(status)
            if record.get("helper_pid"):
                break
            time.sleep(0.02)
        self.assertTrue(record.get("helper_pid"), record)
        helper_pid = int(record["helper_pid"])
        self.assertNotEqual(helper_pid, parent.pid)
        # Kill the parent while the helper is still in its initial delay.
        parent.kill()
        parent.wait(timeout=10)
        deadline = time.time() + 20
        while time.time() < deadline:
            record = restart_helper.read_status(status)
            if record.get("state") in ("reopened", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(record.get("state"), "reopened", record)
        self.assertEqual(int(record.get("helper_pid")), helper_pid)
        self.assertGreaterEqual(int(record.get("attempts") or 0), 1)


class ReleaseSafetyDocsTests(unittest.TestCase):
    def test_operations_documents_post_acceptance_push_and_future_flag(self):
        text = (REPO / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        for needle in (
                "post-acceptance",
                "review_status=accepted",
                "review_passed=true",
                'git push origin "$REVIEWED_SHA:refs/heads/main"',
                "HEAD unchanged since review",
                "clean working tree",
                "forbid_network_side_effects_until_accepted",
                "never run `git push`",
                "same-job guard is **not implemented**",
        ):
            self.assertIn(needle, text)

    def test_changelog_has_unreleased_restart_and_release_safety(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## 3.9.2", text)
        self.assertIn("restart helper", text.lower())
        self.assertIn("forbid_network_side_effects_until_accepted", text)


if __name__ == "__main__":
    unittest.main()
