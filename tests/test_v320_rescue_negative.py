"""Negative-path tests for the v3.2.0 rescue flow (R2).

These lock the cases where the stall/time-limit rescue must *not* silently turn
a failed run into a success: no usable files, an empty account balance, a
failed finisher, and ordinary (non-stop) builder errors.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_script


# A self-contained fake Harness CLI. It branches on the task text so the
# builder, the rescue finisher and the independent reviewer are distinguishable,
# and appends one JSON line per invocation to FAKE_DSH_LOG for call counting.
FAKE_DSH = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

task = sys.argv[-1] if len(sys.argv) else ""
workspace = Path.cwd()
if "FINISHER" in task:
    kind = "finisher"
elif "INDEPENDENT REVIEWER" in task:
    kind = "reviewer"
else:
    kind = "builder"

log_path = os.environ.get("FAKE_DSH_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "task": task}) + "\n")


def emit(payload):
    print(json.dumps(payload), flush=True)


def write_candidate():
    path = workspace / "candidates" / "wave1" / "candidate-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Candidate one\n\nPartial but useful.\n", encoding="utf-8")


if kind == "finisher":
    finisher_mode = os.environ.get("FAKE_DSH_FINISHER_MODE", "ok")
    if finisher_mode == "noop":
        emit({"type": "final", "text": "Kept the existing candidate; nothing new written."})
        sys.exit(0)
    if finisher_mode == "empty":
        (workspace / "final.md").write_bytes(b"")
        emit({"type": "final", "text": "Finisher wrote a zero-byte final.md"})
        sys.exit(0)
    if finisher_mode == "error":
        emit({"type": "error", "message": "fake finisher failure"})
        sys.exit(1)
    (workspace / "final.md").write_text("# Final deliverable\n", encoding="utf-8")
    emit({"type": "final", "text": "Finisher wrote final.md"})
    sys.exit(0)

emit({"type": "tool_call", "name": "bash"})
emit({"type": "tool_call", "name": "read"})
mode = os.environ.get("FAKE_DSH_BUILDER_MODE", "ok")
if mode == "stall_empty":
    time.sleep(2)
    sys.exit(0)
if mode == "stall_candidate":
    write_candidate()
    time.sleep(2)
    sys.exit(0)
if mode == "quota_candidate":
    write_candidate()
    emit({"type": "error", "message": "QUOTA: Insufficient Balance"})
    sys.exit(1)
if mode == "error_candidate":
    write_candidate()
    emit({"type": "error", "message": "fake ordinary builder failure"})
    sys.exit(1)
(workspace / "deliverable.md").write_text("# Deliverable\n", encoding="utf-8")
emit({"type": "final", "text": "Deliverable written."})
sys.exit(0)
'''


class RescueNegativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.task_log = self.root / "fake-dsh-tasks.jsonl"

        self.dsh = write_fake_script(self.root / "dsh", FAKE_DSH)

        self.env = base_env(self.root, fake_dsh=self.dsh)
        self.env.update({
            # Keep the concurrency probe (a real child process) out of the test.
            "DSH_MAX_CONCURRENT_CHILDREN": "8",
            "FAKE_DSH_LOG": str(self.task_log),
        })

    def _run(self, args, env_extra=None):
        extra = dict(env_extra or {})
        env = dict(self.env)
        env.update(extra)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        with mock.patch.dict(os.environ, env, clear=False):
            return harness.run_harness(args, cfg)

    def _args(self, **overrides):
        args = {
            "prompt": "Rescue a partially finished report",
            "subagents": 0,
            "quality": False,
            "independent_review": False,
            "max_seconds": 20,
            "workspace": str(self.workspace),
        }
        args.update(overrides)
        return args

    def _saved_run(self):
        paths = sorted((self.root / "runs").glob("*.json"))
        self.assertEqual(len(paths), 1, "expected exactly one saved run: %r" % (paths,))
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def _task_entries(self):
        if not self.task_log.is_file():
            return []
        return [json.loads(line) for line in
                self.task_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _finisher_entries(self):
        return [entry for entry in self._task_entries() if "FINISHER" in entry["task"]]

    # -- no files ---------------------------------------------------------

    def test_stall_without_output_files_is_still_error(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._args(), {
                "FAKE_DSH_BUILDER_MODE": "stall_empty",
                "DSH_STALL_SECONDS": "1",
            })

        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertFalse(run["rescued"])
        self.assertTrue(run["stalled"])
        self.assertEqual(run["stop_reason"], "stalled")
        self.assertIn("builder", run["stalled_step"])

        rescue = run["rescue"]
        self.assertFalse(rescue["attempted"])
        self.assertEqual(rescue["candidate_files"], [])
        self.assertEqual(rescue["reason"], "stalled")

        self.assertIn("no usable output files", run["error"])
        self.assertIn("no usable output files", str(caught.exception))

        entries = self._task_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "builder")
        self.assertNotIn("FINISHER", entries[0]["task"])

    # -- empty balance has priority ---------------------------------------

    def test_no_balance_skips_rescue_even_with_candidates(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._args(), {"FAKE_DSH_BUILDER_MODE": "quota_candidate"})

        run = self._saved_run()
        self.assertEqual(run["status"], "no_balance")
        self.assertEqual(run["error"], harness.NO_BALANCE_MESSAGE)
        self.assertIn(harness.NO_BALANCE_MESSAGE, str(caught.exception))
        self.assertFalse(run["rescued"])
        self.assertFalse((run.get("rescue") or {}).get("attempted"))
        self.assertNotIn("finisher", run)

        # The candidate existed, but a no-balance failure must never rescue.
        candidate = self.workspace / "candidates" / "wave1" / "candidate-1.md"
        self.assertTrue(candidate.is_file())

        entries = self._task_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "builder")
        self.assertEqual(self._finisher_entries(), [])

    # -- finisher succeeds but writes nothing ------------------------------

    def test_noop_finisher_is_not_a_rescue(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._args(), {
                "FAKE_DSH_BUILDER_MODE": "stall_candidate",
                "DSH_STALL_SECONDS": "1",
                "FAKE_DSH_FINISHER_MODE": "noop",
            })

        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertFalse(run["rescued"])
        self.assertEqual(run["rescue"]["status"], "no_deliverable")
        self.assertIn("no deliverable", str(caught.exception))
        # The builder's candidate is still kept on disk and in the run record.
        self.assertTrue(
            (self.workspace / "candidates" / "wave1" / "candidate-1.md").is_file())
        self.assertEqual(len(self._finisher_entries()), 1)

    def test_zero_byte_finisher_is_not_a_rescue(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._args(), {
                "FAKE_DSH_BUILDER_MODE": "stall_candidate",
                "DSH_STALL_SECONDS": "1",
                "FAKE_DSH_FINISHER_MODE": "empty",
            })

        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertFalse(run["rescued"])
        self.assertEqual(run["rescue"]["status"], "no_deliverable")
        self.assertEqual(run["finisher"]["status"], "ok")
        self.assertIn("no deliverable", str(caught.exception))
        final = self.workspace / "final.md"
        self.assertTrue(final.is_file())
        self.assertEqual(final.stat().st_size, 0)
        candidate = self.workspace / "candidates" / "wave1" / "candidate-1.md"
        self.assertTrue(candidate.is_file())
        self.assertGreater(candidate.stat().st_size, 0)

    # -- finisher itself fails --------------------------------------------

    def test_finisher_failure_keeps_candidates_and_errors(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._args(), {
                "FAKE_DSH_BUILDER_MODE": "stall_candidate",
                "FAKE_DSH_FINISHER_MODE": "error",
                "DSH_STALL_SECONDS": "1",
            })

        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertFalse(run["rescued"])
        self.assertTrue(run["stalled"])
        self.assertEqual(run["stop_reason"], "stalled")

        rescue = run["rescue"]
        self.assertTrue(rescue["attempted"])
        self.assertEqual(rescue["reason"], "stalled")
        self.assertEqual(rescue["status"], "finisher_error")
        self.assertEqual(rescue["finisher_status"], "error")
        self.assertIn("fake finisher failure", rescue["finisher_error"])
        self.assertIn("finisher failed", run["error"])
        self.assertIn("Run saved", str(caught.exception))

        candidate = "candidates/wave1/candidate-1.md"
        self.assertIn(candidate, run["output_files"])
        self.assertTrue((self.workspace / candidate).is_file())
        self.assertIn("Partial but useful", (self.workspace / candidate).read_text(
            encoding="utf-8"))

        entries = self._task_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["builder", "finisher"])
        self.assertEqual(len(self._finisher_entries()), 1)

    # -- ordinary builder error does not rescue ---------------------------

    def test_ordinary_error_with_files_does_not_rescue(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._args(), {"FAKE_DSH_BUILDER_MODE": "error_candidate"})

        run = self._saved_run()
        self.assertEqual(run["status"], "error")
        self.assertFalse(run["rescued"])
        self.assertFalse(run["stalled"])
        self.assertIsNone(run["stop_reason"])
        self.assertIn("fake ordinary builder failure", run["error"])
        self.assertIn("fake ordinary builder failure", str(caught.exception))

        rescue = run["rescue"]
        self.assertFalse(rescue["attempted"])
        self.assertEqual(rescue["reason"], "not_stopped_early")
        self.assertEqual(rescue["candidate_files"], [])

        # The file survived on disk, but an exit-1 crash is not a stop reason.
        self.assertTrue((self.workspace / "candidates" / "wave1" /
                         "candidate-1.md").is_file())

        entries = self._task_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "builder")
        self.assertEqual(self._finisher_entries(), [])


class RescueLimitTests(unittest.TestCase):
    def test_rescue_limit_capped_and_never_zero(self):
        self.assertEqual(harness._rescue_limit(1500, 1500), 60.0)
        self.assertEqual(harness._rescue_limit(1500, 480), 480.0)
        self.assertEqual(harness._rescue_limit(None, 1), 480.0)
        self.assertEqual(harness._rescue_limit(2, 99), 2.0)

        cases = [
            (1500, 1500), (1500, 480), (1500, 0), (None, 1),
            (2, 99), (2, 0), (300, 290), (300, 0), (600, 0),
            (1, 1), (0.5, 10), (60, 60), (None, 10 ** 6),
        ]
        for limit, elapsed in cases:
            value = harness._rescue_limit(limit, elapsed)
            self.assertGreater(value, 0.0,
                               "rescue limit must never be zero for %r/%r" % (limit, elapsed))
            self.assertLessEqual(value, harness.FINISHER_MAX_SECONDS)
            if limit is None:
                expected = harness.FINISHER_MAX_SECONDS
            else:
                remaining = max(0.0, float(limit) - max(0.0, float(elapsed)))
                floor = min(float(limit), harness.FINISHER_FLOOR_SECONDS)
                expected = round(
                    min(harness.FINISHER_MAX_SECONDS, max(remaining, floor)), 1)
            self.assertEqual(value, expected,
                             "unexpected rescue limit for %r/%r" % (limit, elapsed))


if __name__ == "__main__":
    unittest.main()
