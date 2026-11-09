"""v3.4.0: child-session progress must keep a healthy silent wave alive."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, platforms, progress, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh


class ChildActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.directory = self.sessions / progress.encode_workspace(self.workspace)
        self.directory.mkdir(parents=True)

    def _write(self, name="session.v4.jsonl", text="{}\n"):
        path = self.directory / "session-test" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _write_session(self, session_id, text="{}\n"):
        path = self.directory / session_id / "session.v4.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _activity(self, **kwargs):
        return progress.ChildActivity(self.sessions, self.workspace,
                                      since=time.time() - 1, **kwargs)

    def test_project_key_matches_installed_harness_fixtures(self):
        # Expected strings are literal outputs of the installed Harness
        # projectKey (Node: collapse separators, ~XXXX escapes, 251 cap).
        cases = {
            "/tmp/My Drive/wörk~x/ä": "--tmp-My~0020Drive-w~00F6rk~007Ex-~00E4--",
            "/a//b\\\\c:d": "--a-b-c-d--",
            "---x": "--x--",
            "/x/😀": "--x-~D83D~DE00--",
            "///": "--root--",
            "/" + "a" * 300: "--" + "a" * 251 + "--",
        }
        for cwd, expected in cases.items():
            self.assertEqual(progress.project_key(cwd), expected, cwd)
        self.assertEqual(progress.encode_workspace(self.workspace),
                         harness.encode_workspace(self.workspace))
        with self.assertRaises(ValueError):
            progress.project_key("")

    def test_workspace_with_space_and_non_ascii_is_found(self):
        workspace = self.root / "My Drive" / "wörk"
        workspace.mkdir(parents=True)
        # Temp roots are ASCII here, so pin the tricky tail literally rather
        # than comparing two copies of the implementation under test.
        base = progress.project_key(str(self.root.resolve()))
        expected = base[:-2] + "-My~0020Drive-w~00F6rk--"
        directory = self.sessions / expected
        path = directory / "child-session" / "session.v4.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        activity = progress.ChildActivity(self.sessions, workspace,
                                          since=time.time() - 1)
        first = activity.scan()
        self.assertEqual(first["sessions"], 1, expected)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type": "tool_call"}\n')
        self.assertTrue(activity.scan()["changed"])

    def test_first_scan_is_only_a_baseline_then_growth_counts(self):
        path = self._write()
        activity = self._activity()
        first = activity.scan()
        self.assertFalse(first["changed"])
        self.assertEqual(first["sessions"], 1)
        self.assertGreaterEqual(first["active"], 1)
        self.assertFalse(activity.scan()["changed"])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type": "tool_call"}\n')
        grown = activity.scan()
        self.assertTrue(grown["changed"])
        self.assertEqual(grown["sessions"], 1)
        self.assertFalse(activity.scan()["changed"])
        self.assertEqual(activity.events, 1)

    def test_new_session_after_baseline_counts_as_child_start(self):
        activity = self._activity()
        activity.scan()
        self._write(name="session.v4.jsonl")
        update = activity.scan()
        self.assertTrue(update["changed"])
        self.assertEqual(update["sessions"], 1)

    def test_old_logs_are_ignored(self):
        path = self._write()
        old = time.time() - 3600
        os.utime(str(path), (old, old))
        update = self._activity().scan()
        self.assertEqual(update["sessions"], 0)
        self.assertFalse(update["changed"])

    def test_parent_session_log_is_excluded(self):
        parent = self._write()
        parent.parent.rename(self.directory / "parent-session")
        parent = self.directory / "parent-session" / "session.v4.jsonl"
        child = self.directory / "child-session" / "session.v4.jsonl"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("{}\n", encoding="utf-8")
        activity = self._activity()
        activity.exclude_ids = {"parent-session"}
        first = activity.scan()
        self.assertEqual(first["sessions"], 1)
        self.assertFalse(first["changed"])
        with open(child, "a", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertTrue(activity.scan()["changed"])
        with open(parent, "a", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertFalse(activity.scan()["changed"])

    def test_include_ids_ignore_a_concurrent_run(self):
        mine = self._write_session("this-run-child")
        other = self._write_session("another-run-parent")
        activity = self._activity()
        activity.scan()
        activity.add_ids({"this-run-child"})
        with open(other, "a", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertFalse(activity.scan()["changed"])
        self.assertEqual(activity.scan()["sessions"], 1)
        with open(mine, "a", encoding="utf-8") as handle:
            handle.write("{}\n")
        update = activity.scan()
        self.assertTrue(update["changed"])
        self.assertEqual(update["sessions"], 1)

    def test_symlinked_session_directory_is_ignored(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "session.v4.jsonl").write_text("{}\n", encoding="utf-8")
        try:
            os.symlink(str(outside), str(self.directory / "link-session"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(self._activity().scan()["sessions"], 0)

    def test_missing_directory_is_safe(self):
        missing = progress.ChildActivity(self.sessions, self.root / "other",
                                         since=time.time())
        self.assertEqual(missing.scan()["sessions"], 0)
        self.assertFalse(missing.scan()["changed"])

    def test_concurrent_scans_do_not_crash(self):
        path = self._write()
        activity = self._activity()
        activity.scan()
        results = []

        def reader():
            results.append(activity.scan()["sessions"])

        import threading
        threads = [threading.Thread(target=reader) for _ in range(2)]
        for thread in threads:
            thread.start()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type": "final"}\n')
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(value == 1 for value in results))


class StallWatchTests(unittest.TestCase):
    def test_cap_alone_enables_the_watch(self):
        watch = harness._StallWatch(None, max_seconds=1.0)
        self.assertTrue(watch.enabled)
        now = [0.0]
        watch = harness._StallWatch(None, clock=lambda: now[0], max_seconds=1.0,
                                    grace_seconds=0)
        now[0] = 1.1
        self.assertTrue(watch.poll())
        self.assertTrue(watch.cap_hit())
        self.assertIn("absolute watchdog cap", watch.error("builder"))

    def test_recent_progress_extends_the_cap_instead_of_killing(self):
        now = [0.0]
        watch = harness._StallWatch(None, clock=lambda: now[0], max_seconds=1.0,
                                    grace_seconds=120, extension_seconds=300)
        now[0] = 1.1
        self.assertIsNone(watch.poll())
        self.assertFalse(watch.cap_hit())
        self.assertEqual(watch.cap_extensions(), 1)
        # Progress at the old deadline buys another extension.
        now[0] = 301.2
        watch.touch("workspace", "files changed")
        self.assertIsNone(watch.poll())
        self.assertEqual(watch.cap_extensions(), 2)
        # Silence beyond the grace window is finally fatal.
        now[0] = 602.0
        self.assertEqual(watch.poll(), "cap")
        self.assertTrue(watch.cap_hit())

    def test_parent_only_cap_does_not_claim_child_activity(self):
        now = [0.0]
        watch = harness._StallWatch(None, clock=lambda: now[0], max_seconds=1.0,
                                    grace_seconds=0)
        now[0] = 1.1
        self.assertTrue(watch.poll())
        text = watch.error("builder", child_events=0)
        self.assertIn("absolute watchdog cap", text)
        self.assertIn("parent-only activity capped", text)
        self.assertNotIn("activity kept", text)
        self.assertIn("progress events: 0", text)

    def test_cap_with_child_events_names_child_activity(self):
        now = [0.0]
        watch = harness._StallWatch(None, clock=lambda: now[0], max_seconds=1.0,
                                    grace_seconds=0)
        now[0] = 1.1
        self.assertTrue(watch.poll())
        text = watch.error("builder", child_events=3)
        self.assertIn("child/workspace activity kept builder alive", text)
        self.assertIn("progress events: 3", text)

    def test_disabled_when_neither_limit_is_set(self):
        self.assertFalse(harness._StallWatch(None, max_seconds=None).enabled)


class ChildProgressIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.env = base_env(self.root, fake_dsh=self.dsh, sessions=self.sessions)
        self.env.update({
            "FAKE_DSH_MODE": "tick",
            "FAKE_DSH_TICK_SECONDS": "2.2",
            "FAKE_DSH_TICK_INTERVAL": "0.15",
            "FAKE_DSH_SESSIONS": str(self.sessions),
            "DSH_STALL_SECONDS": "1.0",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
            "FAKE_DSH_ENV_OUT": str(self.root / "env.json"),
        })

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def _run(self, max_seconds=None, **extra):
        env = dict(os.environ)
        env.update(self.env)
        env.update(extra)
        args = {
            "prompt": "Do a small task", "subagents": 0, "quality": False,
            "workspace": str(self.workspace),
        }
        if max_seconds is not None:
            args["max_seconds"] = max_seconds
        with mock.patch.dict(os.environ, env, clear=False):
            return harness.run_harness(args, self.cfg(**extra))

    def test_child_activity_prevents_a_false_parent_stall(self):
        _text, run, _path = self._run(DSH_CHILD_PROGRESS="1")
        self.assertEqual(run["status"], "ok")
        self.assertFalse(run["stalled"])
        self.assertGreaterEqual(run["child_progress_events"], 4)
        self.assertGreaterEqual(run["builder"]["child_progress_events"], 4)
        self.assertTrue(run["builder"]["tmpdir_isolated"])

    def test_silent_wave_with_space_and_non_ascii_workspace_does_not_stall(self):
        self.workspace = self.root / "My Drive" / "wörk"
        self.workspace.mkdir(parents=True)
        _text, run, _path = self._run(DSH_CHILD_PROGRESS="1")
        self.assertEqual(run["status"], "ok")
        self.assertFalse(run["stalled"])
        self.assertGreaterEqual(run["builder"]["child_progress_events"], 4)

    def test_without_child_progress_the_same_run_stalls(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(DSH_CHILD_PROGRESS="0")
        self.assertIn("stall", str(caught.exception).lower())

    def test_tmpdir_is_workspace_local_and_cleaned_up(self):
        _text, _run, _path = self._run(DSH_CHILD_PROGRESS="1")
        data = json.loads((self.root / "env.json").read_text(encoding="utf-8"))
        self.assertTrue(str(data["TMPDIR"]).startswith(str(self.workspace)))
        self.assertFalse(Path(data["TMPDIR"]).exists())
        self.assertEqual(list(self.workspace.glob(".dsh-tmp-*")), [])

    def test_child_activity_keeps_a_long_wave_past_the_cap(self):
        # The cap is progress-gated: a wave that keeps producing child-session
        # events is extended, not killed at the wall-clock cap.
        with mock.patch.object(harness, "_stall_max_seconds", return_value=1.2):
            _text, run, _path = self._run(DSH_CHILD_PROGRESS="1",
                                          FAKE_DSH_TICK_SECONDS="3.0",
                                          DSH_STALL_SECONDS="0.5")
        self.assertEqual(run["status"], "ok")
        self.assertFalse(run["builder"]["stall_cap_hit"])
        self.assertGreaterEqual(run["builder"]["stall_cap_extensions"], 1)
        self.assertGreaterEqual(run["builder"]["child_progress_events"], 1)

    def test_cap_alone_is_enabled_and_does_not_kill_progress(self):
        extra = {
            "DSH_CHILD_PROGRESS": "1",
            "FAKE_DSH_TICK_SECONDS": "2.5",
            "DSH_STALL_SECONDS": "0",
        }
        with mock.patch.object(harness, "_stall_max_seconds", return_value=1.2):
            _text, run, _path = self._run(**extra)
        self.assertEqual(run["status"], "ok")
        self.assertFalse(run["builder"]["stall_cap_hit"])
        self.assertGreaterEqual(run["builder"]["stall_cap_extensions"], 1)

    def test_time_limit_kills_grandchild_process_group(self):
        pidfile = self.root / "child.pid"
        with self.assertRaises(RuntimeError):
            self._run(max_seconds=0.8,
                      FAKE_DSH_MODE="tree",
                      FAKE_DSH_CHILD_PID=str(pidfile),
                      DSH_CHILD_PROGRESS="0",
                      DSH_STALL_SECONDS="5")
        pid = int(pidfile.read_text(encoding="utf-8"))
        alive = True
        for _ in range(40):
            if not platforms.pid_alive(pid):
                alive = False
                break
            time.sleep(0.1)
        self.assertFalse(alive, "grandchild survived the process-group kill")

    def test_another_runs_session_log_does_not_refresh_the_watch(self):
        encoded = self.sessions / progress.encode_workspace(self.workspace)
        other = encoded / "another-run-parent" / "session.v4.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("{}\n", encoding="utf-8")
        stop = threading.Event()

        def chatter():
            while not stop.wait(0.1):
                try:
                    with open(other, "a", encoding="utf-8") as handle:
                        handle.write("{}\n")
                except OSError:
                    return

        thread = threading.Thread(target=chatter, daemon=True)
        thread.start()
        try:
            started = time.time()
            with self.assertRaises(RuntimeError) as caught:
                self._run(DSH_CHILD_PROGRESS="1",
                          FAKE_DSH_MODE="catalog_sleep",
                          FAKE_DSH_SLEEP="6",
                          FAKE_DSH_CATALOG_CHILD="this-run-child",
                          DSH_STALL_SECONDS="0.8",
                          DSH_STALL_MAX_SECONDS="5")
            elapsed = time.time() - started
        finally:
            stop.set()
            thread.join(timeout=2)
        self.assertIn("stall", str(caught.exception).lower())
        self.assertLess(elapsed, 4.5,
                        "another run's growing log refreshed this run's watchdog")


if __name__ == "__main__":
    unittest.main()
