"""Wave-2 tests for v3.2.0 R2: rescue instead of discard (success paths).

Each test writes a self-contained fake ``dsh`` executable inside its temporary
directory. The fake branches on the task text:

* normal builder task -> write ``candidates/wave1/candidate-1.md``, emit tool
  events, then sleep until the connector stops it;
* ``FINISHER`` -> write ``final.md`` and ``QUALITY_REPORT.md`` and exit 0;
* ``INDEPENDENT REVIEWER`` -> write ``REVIEW.md`` with 9.2/10 and exit 0, or
  emit an error and exit 1 when ``FAKE_DSH_FAIL_REVIEW=1``.

No real Harness process, network or DeepSeek API is involved.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness
from deepseek_mcp.config import load_config
from tests.fakes import base_env


FAKE_DSH_SOURCE = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

task = sys.argv[-1] if len(sys.argv) else ""
workspace = Path.cwd()
log_path = os.environ.get("FAKE_DSH_LOG")


def log(label):
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(label + "\n")


def emit(payload):
    print(json.dumps(payload), flush=True)


def write_quality(score):
    (workspace / "QUALITY_REPORT.md").write_text(
        "# Quality report\n\n- checklist: fake finisher checklist\n"
        "- Final quality score: %.1f/10\n" % score,
        encoding="utf-8",
    )


if "FINISHER" in task:
    log("finisher")
    (workspace / "final.md").write_text(
        "# Final deliverable\n\nFinished from the existing candidate.\n",
        encoding="utf-8",
    )
    write_quality(9.2)
    emit({"type": "final",
          "text": "Rescue complete. Final quality score: 9.2/10. Files: final.md"})
    sys.exit(0)

if "INDEPENDENT REVIEWER" in task:
    log("reviewer")
    if os.environ.get("FAKE_DSH_FAIL_REVIEW") == "1":
        emit({"type": "error",
              "message": "fake reviewer failure: no review available"})
        sys.exit(1)
    (workspace / "REVIEW.md").write_text(
        "# Independent review\n\n"
        "## Score\nFinal quality score: 9.2/10\n\n"
        "## Blocking defects\nNone\n\n"
        "## Non-blocking defects\nNone\n\n"
        "## Fix list\nNone\n",
        encoding="utf-8",
    )
    emit({"type": "final",
          "text": "REVIEW.md written. Final quality score: 9.2/10. "
                  "Blocking defects: 0"})
    sys.exit(0)

# Normal builder: leave a usable candidate behind, then hang (no progress
# events) until the stall watchdog or the max_seconds timer stops us.
log("builder")
candidate = workspace / "candidates" / "wave1" / "candidate-1.md"
candidate.parent.mkdir(parents=True, exist_ok=True)
candidate.write_text("# Candidate 1\n\nPartial draft from the builder.\n",
                     encoding="utf-8")
emit({"type": "tool_call", "name": "workflow"})
emit({"type": "tool_call", "name": "write"})
time.sleep(5)
sys.exit(0)
'''


class RescueSuccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = self.root / "fake-dsh"
        self.dsh.write_text(FAKE_DSH_SOURCE, encoding="utf-8")
        self.dsh.chmod(0o755)
        self.task_log = self.root / "task-log.txt"
        self.env = base_env(self.root, fake_dsh=self.dsh)
        self.env["DSH_MAX_CONCURRENT_CHILDREN"] = "8"
        self.env["FAKE_DSH_LOG"] = str(self.task_log)

    def _config(self, **extra):
        env = dict(self.env)
        for key, value in extra.items():
            env[key] = str(value)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        return env, cfg

    def _run_harness(self, args, cfg, env):
        # run_harness starts the fake through subprocess.Popen, which inherits
        # os.environ rather than the explicit env passed to load_config.
        with mock.patch.dict(os.environ, env, clear=False):
            return harness.run_harness(args, cfg)

    def _task_lines(self):
        if not self.task_log.is_file():
            return []
        return [line for line in self.task_log.read_text(encoding="utf-8").splitlines()
                if line]

    def _saved_run(self):
        paths = sorted((self.root / "runs").glob("*.json"))
        self.assertEqual(len(paths), 1, "exactly one run record should be saved")
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_stall_rescue_reaches_ok_rescued(self):
        env, cfg = self._config(DSH_STALL_SECONDS="1")
        text, run, path = self._run_harness({
            "prompt": "Write a short report about river otters",
            "subagents": 2,
            "quality": True,
            "quality_rounds": 1,
            "independent_review": True,
            "max_seconds": 20,
            "workspace": str(self.workspace),
        }, cfg, env)

        self.assertEqual(run["status"], "ok_rescued")
        self.assertTrue(run["rescued"])
        self.assertTrue(run["stalled"])
        self.assertEqual(run["stop_reason"], "stalled")
        self.assertEqual(run["rescue"]["reason"], "stalled")
        self.assertEqual(run["rescue"]["finisher_status"], "ok")
        self.assertIn("candidates/wave1/candidate-1.md",
                      run["rescue"]["candidate_files"])
        self.assertIn("candidates/wave1/candidate-1.md", run["output_files"])
        self.assertIn("final.md", run["output_files"])
        self.assertAlmostEqual(run["quality_score"], 9.2, places=2)
        self.assertEqual(run["review_status"], "accepted")

        rescue_warnings = [line for line in text.splitlines()
                           if line.startswith("[WARNING]") and "rescued" in line.lower()]
        self.assertTrue(rescue_warnings, "result text must carry a rescue warning")
        self.assertIn("finisher", rescue_warnings[0].lower())

        finisher_rows = [row for row in run["executions"]
                         if row.get("label") == "finisher"]
        self.assertEqual(len(finisher_rows), 1, "one finisher execution row")
        self.assertEqual(finisher_rows[0]["status"], "ok")

        saved = self._saved_run()
        self.assertEqual(saved["status"], "ok_rescued")
        self.assertTrue(saved["rescued"])
        self.assertIn("Rescued:", Path(path).read_text(encoding="utf-8"))

        lines = self._task_lines()
        self.assertEqual(lines.count("finisher"), 1,
                         "exactly one finisher invocation")
        self.assertEqual(lines[0], "builder")
        self.assertEqual(lines[-1], "reviewer")

    def test_time_limit_rescue_reaches_ok_rescued(self):
        env, cfg = self._config(DSH_STALL_SECONDS="480")
        _text, run, _path = self._run_harness({
            "prompt": "Write a short report about river otters",
            "subagents": 0,
            "quality": False,
            "independent_review": False,
            "max_seconds": 2,
            "workspace": str(self.workspace),
        }, cfg, env)

        self.assertEqual(run["status"], "ok_rescued")
        self.assertTrue(run["rescued"])
        self.assertEqual(run["stop_reason"], "time_limit")
        self.assertFalse(run["stalled"])
        self.assertEqual(run["rescue"]["reason"], "time_limit")
        self.assertEqual(run["rescue"]["finisher_status"], "ok")
        self.assertGreater(run["rescue"]["limit_seconds"], 0)
        self.assertLessEqual(run["rescue"]["limit_seconds"], 600)
        self.assertIn("final.md", run["output_files"])
        lines = self._task_lines()
        self.assertEqual(lines.count("finisher"), 1,
                         "exactly one finisher invocation")
        self.assertEqual(lines, ["builder", "finisher"])
        # R1/R2 contract: the final run record keeps the builder's timeout flag.
        self.assertTrue(run["timed_out"])

    def test_review_failure_after_rescue_is_ok_unreviewed(self):
        env, cfg = self._config(DSH_STALL_SECONDS="1", FAKE_DSH_FAIL_REVIEW="1")
        text, run, _path = self._run_harness({
            "prompt": "Write a short report about river otters",
            "subagents": 2,
            "quality": True,
            "quality_rounds": 1,
            "independent_review": True,
            "max_seconds": 20,
            "workspace": str(self.workspace),
        }, cfg, env)

        self.assertEqual(run["status"], "ok_unreviewed")
        self.assertTrue(run["rescued"])
        self.assertTrue(run["stalled"])
        self.assertEqual(run["review_status"], "error")
        self.assertIsNone(run["quality_score"])
        self.assertIn("fake reviewer failure", run["review_error"])

        warnings = [line for line in text.splitlines()
                    if line.startswith("[WARNING]")]
        self.assertTrue(any("rescued" in line.lower() and "finisher" in line.lower()
                            for line in warnings),
                        "rescue warning missing from result text")
        self.assertTrue(any("Independent review failed" in line
                            for line in warnings),
                        "review warning missing from result text")
        self.assertEqual(self._task_lines().count("finisher"), 1,
                         "exactly one finisher invocation")


if __name__ == "__main__":
    unittest.main()
