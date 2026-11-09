"""R1 tests for the v3.2.0 stall watchdog.

Unit tests drive ``_StallWatch`` with an injected clock; one integration test
runs a self-contained fake ``dsh`` that emits two tool calls (including
``workflow``) and then goes silent, so the real monitor thread stops it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from deepseek_mcp import harness
from deepseek_mcp.config import Config, load_config
from tests.fakes import base_env, write_fake_script


class FakeClock:
    """Deterministic monotonic clock for watchdog unit tests."""

    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class StallWatchTests(unittest.TestCase):
    def test_poll_fires_exactly_once_at_threshold(self):
        clock = FakeClock(10.0)
        watch = harness._StallWatch(1.0, clock=clock)
        self.assertTrue(watch.enabled)
        self.assertEqual(watch.detail(), "process start")
        self.assertIsNone(watch.elapsed())
        self.assertIsNone(watch.last_at())

        clock.advance(0.5)
        self.assertFalse(watch.poll())
        clock.advance(0.5)
        self.assertTrue(watch.poll())

        self.assertEqual(watch.elapsed(), 1.0)
        self.assertEqual(watch.last_at(), 0.0)
        self.assertEqual(watch.detail(), "process start")
        message = watch.error("builder")
        self.assertTrue(
            message.startswith("stall: no progress event for 1.0 s in builder"))
        self.assertIn("process start at 0.0 s", message)

        # The trigger is edge-like: a second poll must not report a new stall.
        self.assertFalse(watch.poll())
        self.assertEqual(watch.elapsed(), 1.0)

    def test_touch_resets_the_window_and_detail(self):
        clock = FakeClock(100.0)
        watch = harness._StallWatch(1.0, clock=clock)

        clock.advance(0.9)
        self.assertFalse(watch.poll())
        watch.touch("tool", "tool 'workflow' (1 tool call(s))")

        # The 0.9 s before the event no longer counts towards the threshold.
        clock.advance(0.5)
        self.assertFalse(watch.poll())
        clock.advance(0.6)
        self.assertTrue(watch.poll())

        self.assertEqual(watch.elapsed(), 1.1)
        self.assertEqual(watch.last_at(), 0.9)
        self.assertEqual(watch.detail(), "tool 'workflow' (1 tool call(s))")
        self.assertIn("tool 'workflow' (1 tool call(s))", watch.error("builder"))
        self.assertFalse(watch.poll())

    def test_fake_clock_touches_keep_the_process_alive_past_threshold(self):
        clock = FakeClock(0.0)
        watch = harness._StallWatch(1.0, clock=clock)
        for _index in range(5):
            clock.advance(0.75)
            self.assertFalse(watch.poll())
            watch.touch("tool", "tool 'workflow'")
        # Five touches span 3.75 s, far past the 1 s threshold, with no stall.
        self.assertFalse(watch.poll())
        clock.advance(1.0)
        self.assertTrue(watch.poll())
        self.assertEqual(watch.elapsed(), 1.0)
        # ``last_at()`` is rounded to one decimal like the run record field.
        self.assertEqual(watch.last_at(), 3.8)

    def test_disabled_watch_never_polls(self):
        clock = FakeClock(0.0)
        watch = harness._StallWatch(None, clock=clock)
        self.assertFalse(watch.enabled)
        for _index in range(10):
            clock.advance(100.0)
            self.assertFalse(watch.poll())
        self.assertIsNone(watch.elapsed())
        self.assertIsNone(watch.last_at())


class StallSecondsTests(unittest.TestCase):
    def test_default_is_480_and_invalid_values_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(config_path=Path(tmp) / "missing.json", env={})
        self.assertEqual(harness._stall_seconds(cfg), 480.0)

        for raw in (None, 0, -5, "abc", "", "not-a-number"):
            self.assertIsNone(
                harness._stall_seconds(Config({"stall_seconds": raw})),
                "stall_seconds=%r should disable the watchdog" % (raw,))

        self.assertEqual(
            harness._stall_seconds(Config({"stall_seconds": "1.5"})), 1.5)


class StallContractTests(unittest.TestCase):
    def test_stop_reason_maps_stall_and_time_limit(self):
        self.assertIsNone(harness._stop_reason({"status": "ok"}))
        self.assertEqual(
            harness._stop_reason({"status": "error", "stalled": True}), "stalled")
        self.assertEqual(
            harness._stop_reason({"status": "error", "timed_out": True}),
            "time_limit")
        self.assertEqual(
            harness._stop_reason({"status": "error",
                                  "error": "time limit reached"}),
            "time_limit")
        self.assertEqual(
            harness._stop_reason({"status": "error",
                                  "error": "stall: no progress event"}),
            "stalled")
        self.assertIsNone(
            harness._stop_reason({"status": "error", "error": "plain crash"}))

    def test_apply_execution_copies_stall_fields(self):
        run = {}
        harness._apply_execution(run, {
            "status": "error",
            "error": "stall: no progress event",
            "stalled": True,
            "stalled_step": "builder: last event was tool 'workflow' at 0.0s",
            "stall_elapsed": 1.2,
            "timed_out": False,
            "stop_reason": "stalled",
            "output_files": ["candidates/wave1/candidate-1.md"],
            "seconds": 1.2,
        })
        self.assertEqual(run["status"], "error")
        self.assertIs(run["stalled"], True)
        self.assertIn("workflow", run["stalled_step"])
        self.assertEqual(run["stall_elapsed"], 1.2)
        self.assertIs(run["timed_out"], False)
        self.assertEqual(run["stop_reason"], "stalled")
        self.assertEqual(run["output_files"],
                         ["candidates/wave1/candidate-1.md"])

    def test_terminate_process_stops_a_sleeping_child(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

        self.addCleanup(cleanup)
        started = time.time()
        harness._terminate_process(proc, grace=1.0)
        self.assertIsNotNone(proc.poll())
        self.assertLess(time.time() - started, 3.0)


class HarnessStallIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Retry teardown: a Windows grandchild that inherited stdout may still
        # be exiting, and a live process can hold a directory for a moment.
        # The test assertion is about returning immediately, not about the
        # cleanup being instant.
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup_tmp, self.tmp)
        self.root = Path(self.tmp)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"

    @staticmethod
    def _cleanup_tmp(path):
        for _ in range(60):
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                return
            time.sleep(0.1)

    def _write_fake_dsh(self, body, name="fake-dsh"):
        path = self.root / name
        script = "#!/usr/bin/env python3\n" + textwrap.dedent(body).lstrip()
        return write_fake_script(path, script)

    def _cfg(self, script, **env_overrides):
        env = base_env(self.root, fake_dsh=script, sessions=self.sessions)
        env["DSH_MAX_CONCURRENT_CHILDREN"] = "8"
        env.update(env_overrides)
        return load_config(config_path=self.root / "config.json", env=env)

    def _run_expecting_error(self, script, args, **env_overrides):
        cfg = self._cfg(script, **env_overrides)
        started = time.time()
        with self.assertRaises(RuntimeError) as caught:
            harness.run_harness(args, cfg)
        elapsed = time.time() - started
        saved = sorted(Path(cfg.log_dir).glob("*.json"))
        self.assertEqual(len(saved), 1, "run_harness should save exactly one run")
        run = json.loads(saved[0].read_text(encoding="utf-8"))
        return run, caught.exception, elapsed, cfg

    def _stall_args(self):
        return {
            "prompt": "Do a small stalled task",
            "subagents": 0,
            "quality": False,
            "independent_review": False,
            "max_seconds": 20,
            "workspace": str(self.workspace),
        }

    def test_stall_stops_builder_and_saves_stalled_run(self):
        script = self._write_fake_dsh("""
            import json
            import sys
            import time

            def emit(payload):
                print(json.dumps(payload), flush=True)

            emit({"type": "tool_call", "name": "bash"})
            emit({"type": "tool_call", "name": "workflow"})
            time.sleep(5)
            sys.exit(0)
        """)
        run, error, elapsed, _cfg = self._run_expecting_error(
            script, self._stall_args(), DSH_STALL_SECONDS="1")

        self.assertLess(elapsed, 8.0)
        self.assertEqual(run["status"], "error")
        self.assertIs(run["stalled"], True)
        self.assertIsInstance(run["stalled_step"], str)
        self.assertIn("workflow", run["stalled_step"])
        self.assertGreaterEqual(run["stall_elapsed"], 0.9)
        self.assertIs(run["timed_out"], False)
        self.assertEqual(run["stop_reason"], "stalled")
        self.assertIn("stall", run["error"].lower())
        self.assertNotIn("time limit reached", run["error"].lower())
        self.assertIn("stall", str(error).lower())
        self.assertNotIn("time limit reached", str(error).lower())
        self.assertEqual(run["tools_used"], {"bash": 1, "workflow": 1})
        self.assertFalse(run["rescued"])

    def test_error_spam_does_not_reset_the_stall_watch(self):
        script = self._write_fake_dsh("""
            import json
            import time

            def emit(payload):
                print(json.dumps(payload), flush=True)

            emit({"type": "tool_call", "name": "workflow"})
            index = 0
            while True:
                emit({"type": "error",
                      "message": "minor fake error %d" % index})
                index += 1
                time.sleep(0.2)
        """, name="fake-dsh-error-spam")
        run, _error, elapsed, _cfg = self._run_expecting_error(
            script, self._stall_args(), DSH_STALL_SECONDS="1")

        # Error events are diagnostics, not progress: endless error spam must
        # not postpone the watchdog past its 1 s window.
        self.assertLess(elapsed, 8.0)
        self.assertEqual(run["status"], "error")
        self.assertIs(run["stalled"], True)
        self.assertGreaterEqual(run["stall_elapsed"], 0.9)
        self.assertEqual(run["stop_reason"], "stalled")
        self.assertIs(run["timed_out"], False)
        self.assertIn("stall", run["error"].lower())
        self.assertNotIn("time limit reached", run["error"].lower())


    def test_grandchild_with_inherited_stdout_does_not_block_stall(self):
        # The direct child ignores SIGTERM and leaves a grandchild holding the
        # inherited stdout pipe. The run must still return after the 2 s grace,
        # not when the grandchild exits.
        script = self._write_fake_dsh("""
            import json
            import signal
            import subprocess
            import sys
            import time

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print(json.dumps({"type": "tool_call", "name": "workflow"}), flush=True)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
            child.returncode = 0
            time.sleep(30)
        """, name="fake-dsh-grandchild")
        run, _error, elapsed, _cfg = self._run_expecting_error(
            script, self._stall_args(), DSH_STALL_SECONDS="1")

        self.assertLess(elapsed, 6.0)
        self.assertEqual(run["status"], "error")
        self.assertIs(run["stalled"], True)
        self.assertEqual(run["stop_reason"], "stalled")


    def test_normal_exit_with_grandchild_stdout_returns_immediately(self):
        # A normal exit 0 + final must not wait for a grandchild that inherited
        # stdout; only stop reasons and true EOF may end the reader early.
        script = self._write_fake_dsh("""
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            Path("deliverable.md").write_text("# done\\n", encoding="utf-8")
            # Keep the inherited-stdout grandchild out of the workspace: on
            # Windows a live process cwd blocks TemporaryDirectory cleanup.
            # The product sets TEMP to its workspace tmpdir, so
            # tempfile.gettempdir() is still inside the tree being cleaned.
            # Use the filesystem root as the grandchild's cwd instead.
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                cwd=os.path.abspath(os.sep))
            child.returncode = 0
            print(json.dumps({"type": "final", "text": "done"}), flush=True)
            sys.exit(0)
        """, name="fake-dsh-normal-grandchild")
        cfg = self._cfg(script)
        started = time.time()
        _text, run, _path = harness.run_harness({
            "prompt": "Do a small task",
            "subagents": 0,
            "quality": False,
            "independent_review": False,
            "max_seconds": 20,
            "workspace": str(self.workspace),
        }, cfg)
        elapsed = time.time() - started

        self.assertLess(elapsed, 2.0)
        self.assertEqual(run["status"], "ok")
        self.assertIn("deliverable.md", run["output_files"])


if __name__ == "__main__":
    unittest.main()
