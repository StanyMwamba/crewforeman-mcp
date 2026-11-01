"""v3.7.0 end-to-end: unmatched runs -> candidate -> active, with a fake DSH."""

import json
import os
import re
from pathlib import Path
import unittest
from unittest import mock

from deepseek_mcp import harness, skill_auto
from deepseek_mcp.config import load_config
from tests import v370_support as support

PROMPT = "Plan a podcast launch with cover art and show notes"


def _author_answer(task):
    digest = re.search(r'"signature_digest":\s*"([^"]+)"', task)
    cluster = re.search(r'"cluster_id":\s*"([^"]+)"', task)
    runs = re.search(r'"runs":\s*(\d+)', task)
    draft = support.valid_draft(
        cluster_id=cluster.group(1) if cluster else "c-test",
        runs=int(runs.group(1)) if runs else 3,
        digest=digest.group(1) if digest else "sha256:test")
    return draft


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, self.fake = support.make_root(
            "v370-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.review = support.review(9.2)
        self.calls = []

    def fake_runner(self, cfg, task, workspace, label=None, **kwargs):
        self.calls.append(label)
        if label == "skill-author":
            answer = _author_answer(task)
        else:
            answer = self.review
        return {"status": "ok", "answer": json.dumps(answer), "seconds": 0.01}

    def run_once(self, prompt=PROMPT):
        return harness.run_harness(
            {"prompt": prompt, "workspace": str(self.root / "work")},
            self.cfg)

    def test_unmatched_to_active_with_trace_and_observability(self):
        os.environ["FAKE_DSH_SCORE"] = "8.0"
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch.object(harness, "run_agent_once", self.fake_runner):
            for index in range(3):
                _text, run, _path = self.run_once()
                self.assertTrue(run["skill_match"]["unmatched"])
                self.assertTrue(run["skill_auto"]["capture"]["captured"])
                self.assertFalse((run.get("skill") or ""))
            data = json.loads(
                (self.root / "skills" / "index.json").read_text(encoding="utf-8"))
            entry = data["skills"]["podcast-launch"]
            self.assertEqual(entry["status"], "candidate")
            self.assertTrue(entry["approved"])
            self.assertEqual(entry["version"], 1)
            self.assertEqual(entry["review"]["score"], 9.2)
            self.assertEqual(entry["stats"]["baseline_ref"]["median"], 8.0)
            self.assertEqual(run["skill_auto"]["draft"]["action"], "created")
            self.assertTrue(run["skill_auto"]["changes"])
            self.assertEqual(
                run["observability"]["skills"]["saved"], 1)
            trace_path = Path(run["observability"]["trace"]["path"]).expanduser()
            text = trace_path.read_text(encoding="utf-8")
            self.assertIn('"phase": "skill"', text)
            for event in ("match", "capture", "draft", "review", "saved"):
                self.assertIn('"event": "%s"' % event, text, event)
            os.environ["FAKE_DSH_SCORE"] = "8.8"
            for index in range(5):
                _text, run, _path = self.run_once()
                self.assertEqual(run["skill"], "podcast-launch")
                self.assertTrue(run["skill_auto"]["observed"])
            entry = json.loads(
                (self.root / "skills" / "index.json").read_text(
                    encoding="utf-8"))["skills"]["podcast-launch"]
            self.assertEqual(entry["status"], "active")
            self.assertEqual(entry["stats"]["uses"], 5)
            self.assertEqual(entry["stats"]["wins"], 5)
            decisions = [
                json.loads(line) for line in
                (self.root / "skills" / ".capture" /
                 "decisions.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
            self.assertIn("promote", [item.get("action")
                                      for item in decisions])
            self.assertIn("promote", [item.get("action") for item in
                                      run["skill_auto"]["changes"]])

    def test_off_mode_writes_no_skill_fields_or_registry(self):
        env = dict(self.env)
        env["DSH_AUTO_SKILLS"] = "off"
        cfg = load_config(config_path=self.root / "config.json", env=env)
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(harness, "run_agent_once", self.fake_runner):
            _text, run, _path = harness.run_harness(
                {"prompt": PROMPT, "workspace": str(self.root / "work")},
                cfg)
        self.assertNotIn("skill_match", run)
        self.assertNotIn("skill_auto", run)
        self.assertFalse((self.root / "skills" / "index.json").exists())
        self.assertFalse((self.root / "skills" / ".capture").exists())

    def test_no_raw_prompt_in_registry_or_capture(self):
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch.object(harness, "run_agent_once", self.fake_runner):
            for index in range(3):
                self.run_once()
        for path in (self.root / "skills").rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".md"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(PROMPT, text, str(path))
                self.assertNotIn(str(self.root), text, str(path))


if __name__ == "__main__":
    unittest.main()
