"""Regression tests for FIX 1: closed-handle faults in the gate/revision path.

Run from the repo root with:  python -m pytest test_candidate.py -q
"""
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE, _HERE.parents[0] / "repo",
              _HERE.parents[2] / "repo", _HERE.parent):
    if (_cand / "deepseek_mcp" / "harness.py").is_file():
        sys.path.insert(0, str(_cand))
        break

from deepseek_mcp import harness, jobs, platforms, safety, status  # noqa: E402
from deepseek_mcp.config import load_config  # noqa: E402
from tests.fakes import base_env, write_fake_dsh  # noqa: E402


class ClosedHandleRetryTests(unittest.TestCase):
    def test_flush_on_closed_handle_does_not_escape_phase(self):
        calls = []

        def flaky_gate():
            calls.append(1)
            handle = io.StringIO()
            handle.close()
            handle.write("gate evidence")
            handle.flush()  # ValueError: I/O operation on closed file
            return {"verdict": "passed"}

        result, error = harness._call_closed_handle_safe(flaky_gate)
        self.assertIsNone(result)
        self.assertTrue(harness._is_closed_handle_error(error))
        self.assertEqual(len(calls), 2)  # one fresh-session retry

    def test_closed_handle_is_retried_once_on_fresh_call(self):
        calls = []

        def flaky_session():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("flush on a closed handle")
            return {"verdict": "passed"}

        result, error = harness._call_closed_handle_safe(flaky_session)
        self.assertIsNone(error)
        self.assertEqual(result, {"verdict": "passed"})
        self.assertEqual(len(calls), 2)

    def test_non_infrastructure_error_is_not_retried(self):
        calls = []

        def broken_gate():
            calls.append(1)
            raise RuntimeError("tests failed")

        result, error = harness._call_closed_handle_safe(broken_gate)
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(calls), 1)

    def test_closed_handle_detection(self):
        self.assertTrue(harness._is_closed_handle_error(
            'session "session-1": flush on a closed handle'))
        self.assertTrue(harness._is_closed_handle_error(
            ValueError("I/O operation on closed file")))
        self.assertFalse(harness._is_closed_handle_error("tests failed"))


class GateDegradedTerminalStatusTests(unittest.TestCase):
    def _degraded_run(self):
        run = {"id": "20261112-143641-471f46", "status": "error",
               "review_status": "pending", "review_in_flight": True}
        gate = {"verdict": "error", "gate_error":
                'session "session-060fd9bf": flush on a closed handle',
                "gate_error_class": "closed_handle"}
        harness._apply_gate_degraded(run, gate)
        return run, gate

    def test_degraded_run_is_not_an_error(self):
        run, _gate = self._degraded_run()
        self.assertEqual(run["status"], "ok_unreviewed")
        self.assertEqual(run["stop_reason"], "gate_error")
        self.assertFalse(run["review_in_flight"])
        self.assertIn("flush on a closed handle", run["gate_warning"])
        self.assertIn("not independently verified", run["gate_warning"])

    def test_final_status_maps_degraded_gate_to_ok_unreviewed(self):
        final, detail, passed = status.final_status(
            builder_status="ok", review_status="skipped",
            gate_status="gate_error")
        self.assertEqual(final, "ok_unreviewed")
        self.assertFalse(passed)
        self.assertIn("gate could not run", detail)

    def test_jobs_gate_accepts_degraded_gate_run(self):
        run, _gate = self._degraded_run()
        gate_tuple = jobs._review_gate_state_from_run(run)
        self.assertIsNone(jobs._review_gate_error(gate_tuple))
        # Backward compatibility: the historical 5-tuple still refuses a
        # genuine gate_failed run.
        self.assertIsNotNone(jobs._review_gate_error(
            (False, True, False, "gate_failed", "tests failed")))


if __name__ == "__main__":
    unittest.main()


class GateClosedHandleIntegrationTest(unittest.TestCase):
    """A closed-handle gate revision degrades through the real run record."""

    def test_run_harness_degrades_instead_of_erroring(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        error = ValueError('session "s": flush on a closed handle')
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("deepseek_mcp.gate.run_gate", side_effect=error):
            _text, run, _path = harness.run_harness({
                "prompt": "small task", "subagents": 0, "quality": True,
                "independent_review": True, "quality_rounds": 1,
                "workspace": str(workspace),
            }, cfg)
        self.assertEqual(run["status"], "ok_unreviewed", run.get("error"))
        self.assertEqual(run["stop_reason"], "gate_error")
        self.assertEqual(run["review_status"], "skipped")
        self.assertIn("flush on a closed handle", run["gate_error"])
        self.assertTrue(run["gate_warning"])


class StopDrainIntegrationTest(unittest.TestCase):
    """The stop path must drain a child that is still flushing."""

    def test_stop_keeps_draining_until_the_child_exits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        child_env = dict(env)
        child_env.update({
            "FAKE_DSH_MODE": "drain",
            "FAKE_DSH_DRAIN_SECONDS": "1.2",
            "FAKE_DSH_CLOSED_HANDLE_STDERR": "1",
        })
        with mock.patch.dict(os.environ, child_env, clear=False):
            result = harness._execute_once_impl(
                cfg, workspace, "drain after stop", limit=0.4,
                policy=safety.resolve({}, cfg), dsh_bin=str(dsh))
        self.assertEqual(result["stop_reason"], "time_limit")
        if os.name == "nt":
            # Windows has no SIGTERM: the stop path uses taskkill /F, so the
            # fake cannot flush a final event after the stop. The bounded-kill
            # test below covers that path; here we only require the available
            # stream to have been read and the teardown noise stripped.
            self.assertGreaterEqual(result["events"], 1)
        else:
            self.assertEqual(result["answer"], "drain complete")
            self.assertGreaterEqual(result["events"], 3,
                                    "events written after the stop were lost")
        self.assertNotIn("flush on a closed handle", result["error"] or "")
        self.assertNotIn("flush on a closed handle",
                         result.get("stderr_tail") or "")

    def test_a_child_that_outlives_the_drain_window_is_killed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        dsh = write_fake_dsh(root / "dsh")
        env = base_env(root, fake_dsh=dsh, sessions=root / "sessions")
        cfg = load_config(config_path=root / "config.json", env=env)
        child_env = dict(env)
        child_env.update({
            "FAKE_DSH_MODE": "drain",
            "FAKE_DSH_DRAIN_SECONDS": "10",
        })
        started = time.monotonic()
        with mock.patch.dict(os.environ, child_env, clear=False):
            result = harness._execute_once_impl(
                cfg, workspace, "drain forever", limit=0.3,
                policy=safety.resolve({}, cfg), dsh_bin=str(dsh))
        elapsed = time.monotonic() - started
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertLess(elapsed, 6.0,
                        "a child that ignores SIGTERM must be killed")
        self.assertNotIn("flush on a closed handle", result["error"] or "")


class GrandchildShutdownOrderingTests(unittest.TestCase):
    """FIX C: never close the read end while the child's group can write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.root / "sessions")
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)

    def _run(self, env=None, label="builder", limit=None, policy=None):
        child_env = dict(self.env)
        child_env.update(env or {})
        with mock.patch.dict(os.environ, child_env, clear=False):
            return harness._execute_once_impl(
                self.cfg, self.workspace, "grandchild shutdown",
                limit=limit,
                policy=policy or safety.resolve({}, self.cfg),
                dsh_bin=str(self.dsh), label=label)

    def test_late_grandchild_event_survives_until_the_group_exits(self):
        # Old ordering: the direct child exits immediately, the connector
        # starts EXIT_DRAIN_SECONDS (1 s), breaks and closes stdout.  At 2.5 s
        # the grandchild writes into the closed pipe, gets EPIPE and its late
        # event is lost.
        started = time.monotonic()
        result = self._run({
            "FAKE_DSH_MODE": "linger_group",
            "FAKE_DSH_LINGER_SECONDS": "2.5",
        })
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0,
                        "a lingering group must not pin the reader forever")
        if os.name == "nt":
            # Windows may not inherit/hold the anonymous pipe after the direct
            # child exits; the bounded-return Windows variant is the contract.
            self.assertNotIn("flush on a closed handle",
                             (result.get("error") or "")
                             + (result.get("stderr_tail") or ""))
            return
        self.assertEqual(result.get("answer"), "linger direct complete")
        self.assertEqual(
            (result.get("tools_used") or {}).get("late"), 1,
            "the late grandchild event was lost: stdout was released too early")
        combined = ((result.get("error") or "") + "\n"
                    + (result.get("stderr_tail") or ""))
        self.assertNotIn("flush on a closed handle", combined)
        self.assertFalse(result.get("stream_abandoned"))

    def test_reviewer_home_is_removed_only_after_the_group_is_gone(self):
        # The grandchild writes its session log under the reviewer DSH_HOME at
        # 2.5 s.  The old path deleted that home at ~2 s, so the write recreated
        # the directory after cleanup.  The fixed path removes it only after
        # the whole group is gone.
        result = self._run({
            "FAKE_DSH_MODE": "linger_group_home",
            "FAKE_DSH_LINGER_SECONDS": "2.5",
            "FAKE_DSH_LINGER_HOME": "1",
        }, label="reviewer late", policy=safety.resolve(
            {}, self.cfg, reviewer=True))
        if os.name == "nt":
            self.assertIsInstance(result, dict)
            return
        self.assertEqual((result.get("tools_used") or {}).get("late"), 1)
        homes = self.root / "state" / "reviewer-homes"
        leftovers = sorted(path.name for path in
                           homes.glob("review-*") if path.is_dir())
        self.assertEqual(
            leftovers, [],
            "reviewer_home was removed while the session logger was alive: %r"
            % leftovers)

    def test_stubborn_group_is_killed_within_the_shutdown_bound(self):
        pidfile = self.root / "stubborn.pid"
        started = time.monotonic()
        result = self._run({
            "FAKE_DSH_MODE": "group_stubborn",
            "FAKE_DSH_GRANDCHILD_PID": str(pidfile),
        }, limit=0.3)
        elapsed = time.monotonic() - started
        self.assertEqual(result.get("stop_reason"), "time_limit")
        self.assertLess(elapsed, 8.0,
                        "a group that ignores SIGTERM must be force-killed")
        pid = int(pidfile.read_text(encoding="utf-8"))
        for _ in range(40):
            if not platforms.pid_alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(platforms.pid_alive(pid),
                         "grandchild survived the bounded group kill")

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX process groups")
    def test_group_that_never_dies_abandons_the_stream_and_cleanup(self):
        parent_file = self.root / "never-parent.pid"
        child_file = self.root / "never-child.pid"

        def cleanup():
            for path in (parent_file, child_file):
                try:
                    pid = int(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        self.addCleanup(cleanup)
        started = time.monotonic()
        with mock.patch.object(
                harness, "_process_group_alive", return_value=True), \
                mock.patch.object(harness, "_signal_harness_group"), \
                mock.patch.multiple(
                    harness, STOP_GRACE_SECONDS=0.05,
                    STOP_DRAIN_HARD_SECONDS=0.3,
                    POST_GROUP_DRAIN_SECONDS=0.1,
                    STALL_POLL_SECONDS=0.02):
            result = self._run({
                "FAKE_DSH_MODE": "group_stubborn",
                "FAKE_DSH_PARENT_PID": str(parent_file),
                "FAKE_DSH_GRANDCHILD_PID": str(child_file),
            }, limit=0.15)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(result.get("stop_reason"), "time_limit")
        # The daemon pump keeps the read end open; the main thread records the
        # abandonment instead of closing stdout and risking EPIPE.
        self.assertTrue(result.get("stream_abandoned"))
        self.assertTrue(result.get("cleanup_deferred"))

    def test_abandoned_group_preserves_stderr_text(self):
        # stderr is a regular temp file, separate from the child's inherited
        # fd: it must be read even when the stdout stream is abandoned.
        # Force a captured group on every host.  Windows has no pgid, so
        # without this the fake's stdout reaches EOF and the stream is
        # correctly *not* abandoned; the test needs the group-alive path.
        with mock.patch.object(
                harness, "_process_group", return_value=987654321), \
                mock.patch.object(
                    harness, "_process_group_alive", return_value=True), \
                mock.patch.object(harness, "_terminate_process"), \
                mock.patch.multiple(
                    harness, EXIT_GROUP_DRAIN_SECONDS=0.25,
                    POST_GROUP_DRAIN_SECONDS=0.1,
                    STOP_GRACE_SECONDS=0.05,
                    STALL_POLL_SECONDS=0.02):
            result = self._run({"FAKE_DSH_MODE": "stderr_boom"})
        self.assertTrue(result.get("stream_abandoned"))
        self.assertTrue(result.get("cleanup_deferred"))
        self.assertIn("fatal: boom", result.get("stderr_tail") or "")
        self.assertIn("fatal: boom", result.get("error") or "")

    def test_no_pgid_late_event_waits_for_real_eof(self):
        # With no capturable process group the old fallback declared the group
        # gone at the direct child's exit and stopped the pump 0.5 s later.
        # Only a real pipe EOF may end the drain on this path.
        started = time.monotonic()
        with mock.patch.object(harness, "_process_group", return_value=None):
            result = self._run({
                "FAKE_DSH_MODE": "linger_group",
                "FAKE_DSH_LINGER_SECONDS": "2.5",
            })
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0)
        self.assertEqual(
            (result.get("tools_used") or {}).get("late"), 1,
            "a late grandchild event was lost when no pgid was available")
        combined = ((result.get("error") or "") + "\n"
                    + (result.get("stderr_tail") or ""))
        self.assertNotIn("flush on a closed handle", combined)
        self.assertFalse(result.get("stream_abandoned"))
        self.assertFalse(result.get("cleanup_deferred"))

    def test_no_pgid_without_eof_defers_cleanup(self):
        pidfile = self.root / "no-pgid-child.pid"

        def cleanup():
            try:
                pid = int(pidfile.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if platforms.pid_alive(pid):
                platforms.terminate_tree(pid, timeout=1.0)

        self.addCleanup(cleanup)
        with mock.patch.object(harness, "_process_group", return_value=None), \
                mock.patch.object(harness, "_terminate_process"), \
                mock.patch.multiple(
                    harness, EXIT_GROUP_DRAIN_SECONDS=0.25,
                    POST_GROUP_DRAIN_SECONDS=0.1,
                    STOP_GRACE_SECONDS=0.05,
                    STALL_POLL_SECONDS=0.02):
            result = self._run({
                "FAKE_DSH_MODE": "linger_group_home",
                "FAKE_DSH_LINGER_SECONDS": "30",
                "FAKE_DSH_LINGER_HOME": "1",
                "FAKE_DSH_GRANDCHILD_PID": str(pidfile),
            }, label="reviewer late", policy=safety.resolve(
                {}, self.cfg, reviewer=True))
        self.assertTrue(result.get("stream_abandoned"))
        self.assertTrue(result.get("cleanup_deferred"))
        homes = self.root / "state" / "reviewer-homes"
        leftovers = [path for path in homes.glob("review-*")
                     if path.is_dir()]
        self.assertTrue(
            leftovers,
            "reviewer_home was removed without a real stdout EOF")

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX process groups")
    def test_captured_pgid_kills_grandchild_after_the_leader_exited(self):
        # The old _terminate_process returned immediately when proc.poll()
        # was set, so a grandchild that ignored SIGTERM survived.  The caller
        # now passes the pgid captured before the leader exited.
        pidfile = self.root / "orphan.pid"
        child_code = (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "grand = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)'])\n"
            "open(sys.argv[1], 'w').write(str(grand.pid))\n"
            "sys.exit(0)\n")
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(pidfile)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **platforms.popen_kwargs())
        pgid = os.getpgid(proc.pid)
        proc.wait(timeout=5)
        pid = int(pidfile.read_text(encoding="utf-8"))

        def cleanup():
            if platforms.pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        self.addCleanup(cleanup)
        self.assertTrue(platforms.pid_alive(pid),
                        "fake grandchild did not survive its leader")
        harness._terminate_process(proc, grace=0.3, pgid=pgid)
        for _ in range(40):
            if not platforms.pid_alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(platforms.pid_alive(pid),
                         "captured pgid did not reach the orphaned grandchild")


if __name__ == "__main__":
    unittest.main(verbosity=2)
