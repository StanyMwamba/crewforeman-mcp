"""Tests for the crosscheck reproduction gate (fix 6)."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from deepseek_mcp import crosscheck


def python_command(code):
    """Build a quoted command line for the running interpreter.

    ``list2cmdline`` produces the platform's native quoting; the gate splits
    POSIX strings with shlex and Windows strings with its Windows parser.
    """
    return subprocess.list2cmdline([sys.executable or "python", "-c", code])


class StubRunner(object):
    def __init__(self, result=(0, ""), exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def __call__(self, command, cwd, timeout):
        self.calls.append((command, cwd, timeout))
        if self.exc is not None:
            raise self.exc
        return self.result


class CrosscheckReproGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def finding(self, **overrides):
        row = {
            "id": "F1",
            "part_id": "p1",
            "severity": "blocking",
            "lens": "breaker",
            "text": "crash on empty input",
            "file": "app.py",
            "line": 1,
        }
        row.update(overrides)
        return row

    def test_passing_repro_is_kept(self):
        runner = StubRunner((0, "traceback\nboom observed\n"))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "boom observed"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=runner)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(filtered), 0)
        self.assertEqual(kept[0]["repro_status"], "verified")
        self.assertEqual(stats["real"], 1)
        self.assertEqual(stats["false_alarm_filtered"], 0)
        self.assertEqual(runner.calls, [("python repro.py", self.ws, 10.0)])

    def test_failing_repro_is_dropped_and_counted(self):
        runner = StubRunner((2, "no repro"))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "expected"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=runner)
        self.assertEqual(kept, [])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["status"], "false_alarm")
        self.assertEqual(filtered[0]["filtered_by"], "connector_repro")
        self.assertEqual(filtered[0]["false_alarm_reason"],
                         "repro_failed:exit=2")
        self.assertEqual(stats["false_alarm_filtered"], 1)
        parts = [{"id": "p1", "status": "built",
                  "builder_crew": "crew-1", "checker_crew": "crew-2"}]
        evaluation = crosscheck.evaluate(
            parts, kept + filtered, [], {"status": "passed"})
        self.assertEqual(evaluation["conditions"]["blocking"], 1.0)
        self.assertEqual(evaluation["counts"]["blocking_open"], 0)

    def test_bug_demo_with_non_zero_exit_is_kept(self):
        # A failing test / traceback demonstrates the bug; exit 2 is evidence,
        # not a reason to dismiss the finding.
        runner = StubRunner((2, "Traceback ... AssertionError: boom"))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "AssertionError: boom"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=runner)
        self.assertEqual(len(kept), 1)
        self.assertEqual(filtered, [])
        self.assertEqual(kept[0]["repro_status"], "verified")
        self.assertEqual(stats["false_alarm_filtered"], 0)

    def test_declared_exit_code_is_enforced(self):
        runner = StubRunner((0, "AssertionError: boom"))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "AssertionError: boom",
                                   "exit_code": 2})
        kept, filtered, _stats = crosscheck.verify_findings(
            [item], self.ws, runner=runner)
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["false_alarm_reason"],
                         "exit_code_mismatch:0")

    def test_real_runner_does_not_use_a_shell(self):
        marker = Path(self.ws) / "pwned"
        command = python_command("print('hello')") + " ; > " + (
            subprocess.list2cmdline([str(marker)]))
        item = self.finding(repro={"command": command, "output": "hello"})
        kept, filtered, _stats = crosscheck.verify_findings([item], self.ws)
        self.assertEqual(len(kept), 1, filtered)
        self.assertFalse(marker.exists(), "shell metacharacters executed")

    def test_real_runner_scrubs_the_environment(self):
        os.environ["DEEPSEEK_API_KEY"] = "unit-test-not-a-real-key"
        try:
            command = python_command(
                "import os;print(bool(os.environ.get('DEEPSEEK_API_KEY')))")
            item = self.finding(repro={"command": command, "output": "False"})
            kept, filtered, _stats = crosscheck.verify_findings([item], self.ws)
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        self.assertEqual(len(kept), 1, filtered)

    def test_real_runner_timeout_is_unverified_and_bounded(self):
        command = python_command("import time;time.sleep(30)")
        item = self.finding(repro={"command": command, "output": "never"})
        started = time.monotonic()
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["repro_status"], "unverified")
        self.assertEqual(filtered[0]["false_alarm_reason"], "repro_timeout")
        self.assertEqual(stats["unverified"], 1)
        self.assertEqual(stats["false_alarm_filtered"], 1)
        self.assertLess(elapsed, 8.0, "timeout did not bound the process")

    def test_timeout_is_capped_at_the_connector_limit(self):
        runner = StubRunner((0, "boom"))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "boom"})
        crosscheck.verify_findings(
            [item], self.ws, runner=runner, timeout=9999)
        self.assertEqual(runner.calls[0][2], crosscheck.REPRO_TIMEOUT)

    def test_windows_argv_splitter_keeps_quotes_and_backslashes(self):
        argv = crosscheck._windows_argv(
            r'"C:\Program Files\Python\python.exe" -c "print(1)"')
        self.assertEqual(argv, [r"C:\Program Files\Python\python.exe",
                                "-c", "print(1)"])

    def test_output_mismatch_with_exit_zero_is_dropped(self):
        runner = StubRunner((0, "something else"))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "boom"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=runner)
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["false_alarm_reason"], "output_mismatch")
        self.assertEqual(stats["false_alarm_filtered"], 1)

    def test_missing_repro_is_dropped_and_counted(self):
        runner = StubRunner()
        kept, filtered, stats = crosscheck.verify_findings(
            [self.finding()], self.ws, runner=runner)
        self.assertEqual(kept, [])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["false_alarm_reason"], "missing_repro")
        self.assertEqual(stats["false_alarm_filtered"], 1)
        self.assertEqual(runner.calls, [])

    def test_spec_finding_quote_verified_against_workspace(self):
        Path(self.ws, "spec.txt").write_text(
            "alpha\nneedle line\ngamma\n", encoding="utf-8")
        item = self.finding(
            lens="spec",
            repro={"file": "spec.txt", "line": 2,
                   "quote": "needle line"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner())
        self.assertEqual(len(kept), 1)
        self.assertEqual(filtered, [])
        self.assertEqual(kept[0]["repro_status"], "verified")
        self.assertEqual(stats["spec"], 1)

    def test_spec_finding_wrong_quote_dropped(self):
        Path(self.ws, "spec.txt").write_text(
            "alpha\nneedle line\ngamma\n", encoding="utf-8")
        item = self.finding(
            lens="spec",
            repro={"file": "spec.txt", "line": 2,
                   "quote": "text that is absent"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner())
        self.assertEqual(kept, [])
        self.assertEqual(len(filtered), 1)
        self.assertTrue(filtered[0]["false_alarm_reason"].startswith(
            "quote_not_found:"))
        self.assertEqual(stats["false_alarm_filtered"], 1)

    def test_spec_quote_at_wrong_line_is_dropped(self):
        Path(self.ws, "spec.txt").write_text(
            "needle line\nalpha\nbeta\n", encoding="utf-8")
        item = self.finding(
            lens="spec",
            repro={"file": "spec.txt", "line": 3,
                   "quote": "needle line"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner())
        self.assertEqual(kept, [])
        self.assertTrue(filtered[0]["false_alarm_reason"].startswith(
            "quote_not_at_line:"))
        self.assertEqual(stats["false_alarm_filtered"], 1)

    def test_spec_finding_without_quote_is_dropped(self):
        Path(self.ws, "spec.txt").write_text(
            "alpha\nneedle line\ngamma\n", encoding="utf-8")
        item = self.finding(
            lens="spec", repro={"file": "spec.txt", "line": 2})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner())
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["false_alarm_reason"],
                         "spec_finding_needs_file_line_quote")
        self.assertEqual(stats["false_alarm_filtered"], 1)

    def test_spec_quote_outside_the_workspace_is_dropped(self):
        item = self.finding(
            lens="spec",
            repro={"file": "../outside.txt", "line": 1,
                   "quote": "anything"})
        kept, filtered, _stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner())
        self.assertEqual(kept, [])
        self.assertTrue(filtered[0]["false_alarm_reason"].startswith(
            "quote_file_missing:"))

    def test_timeout_is_unverified_with_reason(self):
        runner = StubRunner(exc=subprocess.TimeoutExpired("python repro.py", 5))
        item = self.finding(repro={"command": "python repro.py",
                                   "output": "boom"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=runner)
        self.assertEqual(kept, [])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["repro_status"], "unverified")
        self.assertEqual(filtered[0]["false_alarm_reason"], "repro_timeout")
        self.assertEqual(stats["unverified"], 1)
        self.assertEqual(stats["false_alarm_filtered"], 1)

    def test_runner_called_once_per_unique_finding(self):
        runner = StubRunner((0, "boom"))
        rows = [
            self.finding(id="F1", repro={"command": "python repro.py",
                                         "output": "boom"}),
            self.finding(id="F1", repro={"command": "python repro.py",
                                         "output": "boom"}),
        ]
        kept, filtered, stats = crosscheck.verify_findings(
            rows, self.ws, runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(stats["raw_findings"], 2)
        self.assertEqual(stats["unique"], 1)
        self.assertEqual(stats["real"], 2)
        self.assertEqual(len(kept) + len(filtered), 2)


    def test_recorded_numbers_and_dropped_blocking_never_open(self):
        Path(self.ws, "spec.txt").write_text(
            "alpha\nneedle line\ngamma\n", encoding="utf-8")
        calls = []

        def runner(command, cwd, timeout):
            calls.append(command)
            if command == "hang":
                raise subprocess.TimeoutExpired(command, timeout)
            return (0, "boom observed" if command == "keep" else "other")

        findings = [
            self.finding(id="F1", status="fixed",
                         repro={"command": "keep", "output": "boom observed"}),
            self.finding(id="F2", repro={"command": "drop",
                                         "output": "boom observed"}),
            self.finding(id="F3", lens="spec", severity="minor",
                         repro={"file": "spec.txt", "line": 2}),
            self.finding(id="F4", lens="spec", severity="minor",
                         repro={"file": "spec.txt", "line": 2,
                                "quote": "needle line"}),
            self.finding(id="F5", repro={"command": "hang",
                                         "output": "boom observed"}),
        ]
        kept, filtered, stats = crosscheck.verify_findings(
            findings, self.ws, runner=runner)
        self.assertEqual(calls, ["keep", "drop", "hang"])
        self.assertEqual(stats, {
            "raw_findings": 5, "unique": 5, "real": 2, "false_alarm": 3,
            "false_alarm_filtered": 3, "unverified": 1,
            "breaker": 3, "spec": 2})
        parts = [{"id": "p1", "status": "built",
                  "builder_crew": "crew-1", "checker_crew": "crew-2"}]
        evaluation = crosscheck.evaluate(
            parts, kept + filtered, [], {"status": "passed"})
        self.assertEqual(evaluation["counts"]["blocking_open"], 0)
        self.assertEqual(evaluation["conditions"]["blocking"], 1.0)

    def test_duplicate_id_with_different_repro_runs_both(self):
        """P3-1: the cache must key on the repro, not just the finding id."""
        calls = []

        def runner(command, cwd, timeout):
            calls.append(command)
            return (0, "boom") if command == "good" else (0, "NOTHING")

        base = {"id": "D1", "part_id": "p1", "severity": "blocking",
                "lens": "breaker", "text": "t"}
        kept, filtered, stats = crosscheck.verify_findings([
            dict(base, text="first", repro={"command": "good",
                                            "output": "boom"}),
            dict(base, text="second", repro={"command": "NEVER-RUN",
                                             "output": "NEVER-OBSERVED"}),
        ], self.ws, runner=runner)
        self.assertEqual(calls, ["good", "NEVER-RUN"])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["false_alarm_reason"], "output_mismatch")

    def test_duplicate_id_decoy_cannot_hide_a_real_finding(self):
        calls = []

        def runner(command, cwd, timeout):
            calls.append(command)
            return (0, "boom") if command == "real" else (0, "NOTHING")

        base = {"id": "D2", "part_id": "p1", "severity": "blocking",
                "lens": "breaker"}
        kept, filtered, stats = crosscheck.verify_findings([
            dict(base, text="decoy", repro={"command": "bad",
                                            "output": "boom"}),
            dict(base, text="real", repro={"command": "real",
                                           "output": "boom"}),
        ], self.ws, runner=runner)
        self.assertEqual(sorted(calls), ["bad", "real"])
        self.assertEqual([row["text"] for row in kept], ["real"])

    def test_non_finite_line_is_dropped_not_raised(self):
        spec = Path(self.ws, "spec.txt")
        spec.write_text("alpha\nneedle line\n", encoding="utf-8")
        item = self.finding(lens="spec", file="spec.txt", line=2,
                            repro={"file": "spec.txt", "line": float("inf"),
                                   "quote": "needle line"})
        kept, filtered, stats = crosscheck.verify_findings([item], self.ws)
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["false_alarm_reason"],
                         "spec_finding_needs_file_line_quote")

    def test_non_finite_exit_code_is_dropped_not_raised(self):
        item = self.finding(executable=True,
                            repro={"command": "python repro.py",
                                   "output": "boom",
                                   "exit_code": float("inf")})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner((0, "boom")))
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["false_alarm_reason"],
                         "invalid_exit_code")

    def test_fix_task_does_not_resurrect_a_dropped_finding(self):
        record = {
            "parts": [{"id": "p1", "status": "built",
                       "builder_crew": "crew-1", "checker_crew": "crew-2"}],
            "findings": [{
                "id": "F1", "part_id": "p1", "severity": "blocking",
                "lens": "breaker", "text": "dropped", "file": "a.py",
                "line": 1, "status": "false_alarm",
                "filtered_by": "connector_repro", "repro_status": "failed",
            }],
            "requirements": [],
        }
        evaluation = crosscheck.evaluate(
            record["parts"], record["findings"], [], {"status": "failed"})
        task = crosscheck.build_fix_task({}, record, evaluation, 2, "brief")
        self.assertNotIn("- F1", task)
        self.assertEqual(evaluation["counts"]["blocking_open"], 0)

    def test_marker_before_a_long_noisy_log_is_still_found(self):
        """P3-4: the 20 KB record tail must not decide the match."""
        marker = "NEEDLE"
        code = "import sys; sys.stdout.write(%r + 'x' * 25000)" % marker
        command = python_command(code)
        item = self.finding(executable=True,
                            repro={"command": command, "output": marker})
        kept, filtered, stats = crosscheck.verify_findings([item], self.ws)
        self.assertEqual(len(kept), 1, filtered)
        self.assertEqual(stats["false_alarm_filtered"], 0)

    def test_real_runner_uses_a_disposable_workspace_copy(self):
        victim = Path(self.ws) / "victim.txt"
        victim.write_text("original", encoding="utf-8")
        command = python_command(
            "import pathlib; pathlib.Path('victim.txt').write_text('mutated'); "
            "print('workspace changed')")
        item = self.finding(executable=True,
                            repro={"command": command,
                                   "output": "workspace changed"})
        kept, filtered, _stats = crosscheck.verify_findings([item], self.ws)
        self.assertEqual(len(kept), 1, filtered)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")

    def test_repro_home_is_isolated_from_the_operator(self):
        command = python_command(
            "import os; print('HOME=' + os.environ.get('HOME', ''))")
        item = self.finding(executable=True,
                            repro={"command": command, "output": "HOME="})
        kept, filtered, _stats = crosscheck.verify_findings([item], self.ws)
        self.assertEqual(len(kept), 1, filtered)
        output = kept[0]["repro_result"]["output"].strip()
        self.assertIn("HOME=", output)
        self.assertNotEqual(output, "HOME=" + os.path.expanduser("~"))

    def test_too_short_expected_output_is_dropped(self):
        item = self.finding(executable=True,
                            repro={"command": "python repro.py",
                                   "output": "1"})
        kept, filtered, stats = crosscheck.verify_findings(
            [item], self.ws, runner=StubRunner((0, "anything 1")))
        self.assertEqual(kept, [])
        self.assertEqual(filtered[0]["false_alarm_reason"],
                         "repro_output_too_short")


if __name__ == "__main__":
    unittest.main()
