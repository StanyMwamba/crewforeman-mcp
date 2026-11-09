"""Consolidated normal-exit / inherited-stdout regression tests.

A fake ``dsh`` writes a deliverable, emits a JSON ``final`` event, and exits 0
while a grandchild keeps the inherited stdout pipe open.  ``run_harness`` must
stop within the bounded post-exit drain instead of waiting for a pipe EOF that
may never arrive.  Four cases:

1. a silent 30 s grandchild (the review's original hang);
2. a grandchild that never exits (thread cleanup and SIGKILL confirmation);
3. a ``final`` event flushed 0.45 s *after* the direct child exits;
4. a grandchild flooding JSONL for 5 s (must not stretch the drain to 5 s).

Each fake records its grandchild pid through ``PIPE_TEST_PIDFILE`` and every
test SIGKILLs the grandchild in ``addCleanup``, so a failing run never leaves a
30 s/forever/flooding process behind.  No real Harness, API, or network.
"""

import os
import signal
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, platforms
from deepseek_mcp.config import load_config
from tests.fakes import base_env


# Grandchild programs are embedded with repr() so the generated fake dsh stays
# one dedented script.  ``\\n`` here is a two-character escape inside the
# generated source, not a literal newline split.
GRANDCHILD_SLEEP_30 = "import time; time.sleep(30)"

GRANDCHILD_NEVER = """
import time
while True:
    time.sleep(5)
"""

GRANDCHILD_LATE_FINAL = """
import json
import time

time.sleep(0.45)
print(json.dumps({"type": "final", "text": "done-late"}), flush=True)
time.sleep(30)
"""

GRANDCHILD_FLOOD = """
import json
import sys
import time

line = json.dumps({"type": "tool_call", "name": "bash"}) + "\\n"
deadline = time.monotonic() + 5.0
try:
    while time.monotonic() < deadline:
        sys.stdout.write(line)
        sys.stdout.flush()
except (BrokenPipeError, OSError):
    pass
"""


class _RunTimedOut(Exception):
    """Raised by the SIGALRM guard if run_harness itself does not return."""


def _alarm_handler(_signum, _frame):
    raise _RunTimedOut("run_harness did not return within the 15 s test guard")


class NormalExitPipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.pidfile = self.root / "grandchild.pid"
        self._grandchild_pid = None
        # LIFO: this runs before TemporaryDirectory.cleanup, while the pidfile
        # that holds the grandchild pid still exists.
        self.addCleanup(self._kill_grandchild)

    def _write_fake_dsh(self, body, name="fake-dsh"):
        path = self.root / name
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(body).lstrip(),
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def _cfg(self, script):
        env = base_env(self.root, fake_dsh=script, sessions=self.sessions)
        env["DSH_MAX_CONCURRENT_CHILDREN"] = "8"
        env["PIPE_TEST_PIDFILE"] = str(self.pidfile)
        return load_config(config_path=self.root / "config.json", env=env)

    def _run_harness(self, script, max_seconds=20):
        cfg = self._cfg(script)
        started = time.monotonic()
        previous = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, 15.0)
        try:
            # _execute_once copies os.environ into the child, so the pidfile
            # variable must be exported while the run is in progress, not only
            # present in the config env used by load_config.
            with mock.patch.dict(
                    os.environ,
                    {"PIPE_TEST_PIDFILE": str(self.pidfile)},
                    clear=False):
                _text, run, _path = harness.run_harness({
                    "prompt": "Do a small task with an inherited stdout grandchild",
                    "subagents": 0,
                    "quality": False,
                    "independent_review": False,
                    "max_seconds": max_seconds,
                    "workspace": str(self.workspace),
                }, cfg)
        except _RunTimedOut as exc:
            self.fail(str(exc))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous)
        elapsed = time.monotonic() - started
        return run, elapsed

    def _read_grandchild_pid(self):
        text = self.pidfile.read_text(encoding="utf-8").strip()
        self.assertTrue(text, "fake dsh did not record the grandchild pid")
        return int(text)

    def _wait_gone(self, pid, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not platforms.pid_alive(pid):
                return True
            time.sleep(0.02)
        return False

    def _kill_grandchild(self):
        pid = self._grandchild_pid
        if pid is None:
            try:
                pid = int(self.pidfile.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return True
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return True
        return self._wait_gone(pid)

    def _assert_deliverable(self, run):
        self.assertIn("deliverable.md", run.get("output_files") or [])
        deliverable = self.workspace / "deliverable.md"
        self.assertTrue(deliverable.is_file())
        self.assertTrue(deliverable.read_text(encoding="utf-8").strip())

    def test_01_lingering_sleep_30_grandchild_is_bounded(self):
        script = self._write_fake_dsh("""
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            Path("deliverable.md").write_text(
                "# Deliverable\\n\\nNon-empty builder output.\\n",
                encoding="utf-8")

            # Inherit stdout: this grandchild holds the read end of the pipe
            # open for 30 s after the direct child has exited.
            grandchild = subprocess.Popen(
                [sys.executable, "-c", __GRANDCHILD__])
            # The grandchild is deliberately left running; mark the handle as
            # reaped so this fake-dsh process does not emit a ResourceWarning
            # at exit (NB6).
            grandchild.returncode = 0
            Path(os.environ["PIPE_TEST_PIDFILE"]).write_text(
                str(grandchild.pid), encoding="utf-8")

            print(json.dumps({"type": "final", "text": "done-sleep30"}),
                  flush=True)
            sys.exit(0)
        """.replace("__GRANDCHILD__", repr(GRANDCHILD_SLEEP_30)),
            name="fake-dsh-sleep30")

        run, elapsed = self._run_harness(script)

        self.assertLess(
            elapsed, 3.0,
            "run_harness waited %.3f s for the 30 s grandchild" % elapsed)
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("exit_code"), 0)
        self.assertEqual(run.get("answer"), "done-sleep30")
        self._assert_deliverable(run)

        # Prove the test held stdout open after the direct child exited.
        self._grandchild_pid = self._read_grandchild_pid()
        if not platforms.pid_alive(self._grandchild_pid):
            self.fail("grandchild %d was not alive after the run"
                      % self._grandchild_pid)

    def test_02_never_exit_grandchild_is_bounded_and_reaped(self):
        baseline = set(threading.enumerate())
        script = self._write_fake_dsh("""
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            Path("deliverable.md").write_text(
                "# Deliverable\\n\\nNon-empty builder output.\\n",
                encoding="utf-8")
            grandchild = subprocess.Popen(
                [sys.executable, "-c", __GRANDCHILD__])
            # The grandchild is deliberately left running; mark the handle as
            # reaped so this fake-dsh process does not emit a ResourceWarning
            # at exit (NB6).
            grandchild.returncode = 0
            Path(os.environ["PIPE_TEST_PIDFILE"]).write_text(
                str(grandchild.pid), encoding="utf-8")
            print(json.dumps({"type": "final", "text": "done-never"}),
                  flush=True)
            sys.exit(0)
        """.replace("__GRANDCHILD__", repr(GRANDCHILD_NEVER)),
            name="fake-dsh-never")

        run, elapsed = self._run_harness(script)

        self.assertLess(
            elapsed, 3.0,
            "run_harness waited %.3f s for the never-exit grandchild" % elapsed)
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("exit_code"), 0)
        self.assertEqual(run.get("answer"), "done-never")
        self._assert_deliverable(run)

        deadline = time.monotonic() + 1.0
        while (set(threading.enumerate()) - baseline
               and time.monotonic() < deadline):
            time.sleep(0.01)
        leaked = set(threading.enumerate()) - baseline
        self.assertFalse(
            leaked, "threads leaked after the normal-exit path: %r"
            % [thread.name for thread in leaked])
        self.assertLessEqual(threading.active_count(), len(baseline))

        pid = self._read_grandchild_pid()
        self._grandchild_pid = pid
        self.assertTrue(
            self._kill_grandchild(),
            "grandchild %d survived SIGKILL/cleanup" % pid)

    def test_03_late_final_after_direct_exit_is_retained(self):
        script = self._write_fake_dsh("""
            import os
            import subprocess
            import sys
            from pathlib import Path

            # The direct child exits 0 immediately; the final answer is flushed
            # by the grandchild 0.45 s later.
            grandchild = subprocess.Popen(
                [sys.executable, "-c", __GRANDCHILD__])
            # The grandchild is deliberately left running; mark the handle as
            # reaped so this fake-dsh process does not emit a ResourceWarning
            # at exit (NB6).
            grandchild.returncode = 0
            Path(os.environ["PIPE_TEST_PIDFILE"]).write_text(
                str(grandchild.pid), encoding="utf-8")
            sys.exit(0)
        """.replace("__GRANDCHILD__", repr(GRANDCHILD_LATE_FINAL)),
            name="fake-dsh-late-final")

        run, elapsed = self._run_harness(script)

        self.assertLess(
            elapsed, 3.0,
            "run_harness took %.3f s for a late final" % elapsed)
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("exit_code"), 0)
        self.assertEqual(
            run.get("answer"), "done-late",
            "the final flushed 0.45 s after the direct child exited was lost")
        self._grandchild_pid = self._read_grandchild_pid()

    def test_04_flooding_grandchild_is_bounded(self):
        script = self._write_fake_dsh("""
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            Path("deliverable.md").write_text(
                "# Deliverable\\n\\nNon-empty builder output.\\n",
                encoding="utf-8")

            # The flood keeps stdout readable for 5 s; it must not extend the
            # post-exit drain beyond its 1 s bound.
            grandchild = subprocess.Popen(
                [sys.executable, "-c", __GRANDCHILD__])
            # The grandchild is deliberately left running; mark the handle as
            # reaped so this fake-dsh process does not emit a ResourceWarning
            # at exit (NB6).
            grandchild.returncode = 0
            Path(os.environ["PIPE_TEST_PIDFILE"]).write_text(
                str(grandchild.pid), encoding="utf-8")
            print(json.dumps({"type": "final", "text": "done-flood"}),
                  flush=True)
            sys.exit(0)
        """.replace("__GRANDCHILD__", repr(GRANDCHILD_FLOOD)),
            name="fake-dsh-flood")

        run, elapsed = self._run_harness(script)

        self.assertLess(
            elapsed, 2.5,
            "run_harness followed the 5 s flood for %.3f s" % elapsed)
        self.assertEqual(run.get("status"), "ok")
        self.assertEqual(run.get("exit_code"), 0)
        self.assertEqual(run.get("answer"), "done-flood")
        self._assert_deliverable(run)
        self._grandchild_pid = self._read_grandchild_pid()


if __name__ == "__main__":
    unittest.main()
