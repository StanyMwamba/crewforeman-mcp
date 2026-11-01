"""v3.6.0 live-scaling end-to-end tests with the real harness and fake dsh.

The scaling decision itself is never mocked: the actual harness scores the
fake builder's weak deliverable and plans the extra wave.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh

PROMPT = ("Build a status page. Required facts: purple header, "
          "striped table, amber footer.")


class ScalingHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.counter = self.root / "dsh-calls.txt"
        self.fake = write_fake_dsh(self.root / "fake-dsh")
        self.wrapper = self._wrap(self.fake, self.counter)
        self.env = base_env(self.root, fake_dsh=self.wrapper, sessions=self.sessions)
        self.env.update({
            "FAKE_DSH_COUNTER": str(self.counter),
            "FAKE_DSH_SESSIONS": str(self.sessions),
            "FAKE_DSH_REVIEW_SCORE": "8.7",
        })

    def _wrap(self, fake, counter):
        """Count builder/reviewer/revision invocations, then exec the fake."""
        path = self.root / "dsh"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "task = args[-1] if args else ''\n"
            "if 'concurrency probe' in task:\n"
            "    kind = 'probe'\n"
            "elif 'INDEPENDENT REVIEWER' in task:\n"
            "    kind = 'review'\n"
            "elif 'REVISION agent' in task:\n"
            "    kind = 'revision'\n"
            "else:\n"
            "    kind = 'builder'\n"
            "with open(%r, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(kind + '\\n')\n"
            "os.execv(sys.executable, [sys.executable, %r] + args)\n"
            % (str(counter), str(fake)), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def _cfg(self):
        return load_config(config_path=self.root / "config.json", env=self.env)

    def _calls(self):
        if not self.counter.exists():
            return []
        return self.counter.read_text(encoding="utf-8").split()

    def _run(self, env=None, **extra):
        args = {
            "prompt": PROMPT,
            "workspace": str(self.workspace),
            "subagents": 2,
            "quality": True,
            "quality_rounds": 1,
            "live_scaling": True,
            "live_scaling_threshold": 7.0,
            "live_scaling_max_extra_waves": 1,
        }
        args.update(extra)
        with mock.patch.dict(os.environ, env or self.env, clear=False):
            return harness.run_harness(args, self._cfg())

    def test_weak_first_wave_adds_one_extra_wave(self):
        _text, run, _path = self._run()
        scaling = run["live_scaling"]
        self.assertEqual(scaling["extra_waves_used"], 1)
        decisions = scaling["decisions"]
        self.assertGreaterEqual(len(decisions), 2)
        self.assertTrue(decisions[0]["added"])
        self.assertTrue(decisions[0]["weak"])
        self.assertFalse(decisions[1]["added"])
        self.assertIn("extra wave limit", decisions[1]["reason"])
        self.assertEqual(self._calls().count("builder"), 2)

    def test_cost_budget_blocks_extra_wave(self):
        _text, run, _path = self._run(job_max_cost_usd=1e-6)
        scaling = run["live_scaling"]
        self.assertEqual(scaling["extra_waves_used"], 0)
        decisions = scaling["decisions"]
        self.assertTrue(decisions)
        self.assertFalse(decisions[0]["added"])
        self.assertTrue(decisions[0]["weak"])
        self.assertIn("budget", decisions[0]["reason"].lower())
        self.assertEqual(self._calls().count("builder"), 1)

    def test_unknown_cost_blocks_extra_wave(self):
        """No priceable session log + a ceiling: the wave cannot be proven."""
        env = dict(self.env)
        env.pop("FAKE_DSH_SESSIONS", None)
        _text, run, _path = self._run(env=env, job_max_cost_usd=1.0)
        scaling = run["live_scaling"]
        self.assertEqual(scaling["extra_waves_used"], 0)
        decisions = scaling["decisions"]
        self.assertTrue(decisions)
        self.assertFalse(decisions[0]["added"])
        self.assertIn("unknown", decisions[0]["reason"].lower())
        self.assertEqual(self._calls().count("builder"), 1)
        budget = run.get("job_budget") or {}
        self.assertFalse(budget.get("cost_known"))
        self.assertTrue(budget.get("cost_exceeded"))
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")

    def test_cost_budget_blocks_the_reviewer_before_it_starts(self):
        """The whole-job ceiling spans the reviewer, not just extra waves."""
        _text, run, _path = self._run(job_max_cost_usd=1e-6)
        calls = self._calls()
        self.assertEqual(calls.count("builder"), 1)
        self.assertNotIn("review", calls,
                         "reviewer ran despite an exceeded whole-job budget: %r" % (calls,))
        self.assertEqual(run.get("status"), "budget_exceeded")
        self.assertEqual(run.get("stop_reason"), "budget")
        self.assertTrue(run.get("status_detail"))
        self.assertEqual(run.get("review_status"), "stopped")
        self.assertIn("reviewer was not started", run.get("review_error") or "")
        budget = run.get("job_budget") or {}
        self.assertTrue(budget.get("cost_exceeded"))


if __name__ == "__main__":
    unittest.main()
