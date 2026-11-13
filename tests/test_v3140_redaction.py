"""Regression tests for v3.14.3 bug A: regexing serialised JSON corrupted run records."""

import json
import tempfile
import unittest
from pathlib import Path
import shutil

from deepseek_mcp import events, jobs, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env


class RedactionRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=base_env(self.root))

    def _run(self, run_id):
        return {
            "id": run_id,
            "kind": "single",
            "created": "2026-01-01T00:00:00+00:00",
            "title": "Redaction regression",
            "status": "ok",
            "prompt": "hello",
            "answer": "world",
            "files": [],
        }

    def test_escaped_quote_fragment_survives_save_run(self):
        # The value ends with a quote directly after the home-path segment, so
        # the serialised form has the home path immediately before `\"`.  The
        # old code regexed the serialised JSON and consumed that escape.
        fragment = '{"path": "/Users/example-user"'
        run = self._run("json-quote")
        run["prompt"] = "quoted fragment: " + fragment
        run["answer"] = "trace " + fragment

        path = Path(store.save_run(run, self.cfg))
        json_path = path.with_suffix(".json")
        raw = json_path.read_text(encoding="utf-8")

        # json.loads succeeding is the core assertion: the document is valid.
        data = json.loads(raw)
        self.assertIn('"path": "~"', data["prompt"])
        self.assertTrue(data["prompt"].endswith('"'))
        self.assertEqual(data["prompt"], 'quoted fragment: {"path": "~"')
        self.assertEqual(data["answer"], 'trace {"path": "~"')
        self.assertIn('\\"', raw)

    def test_home_path_in_a_mapping_key_is_redacted(self):
        run = self._run("key-redact")
        run["labels"] = {"/Users/example-user/project": "kept"}
        path = Path(store.save_run(run, self.cfg))
        data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(data["labels"], {"~/project": "kept"})

    def test_home_path_is_still_redacted_in_reloaded_record(self):
        run = self._run("home-redact")
        run["answer"] = "wrote /Users/example-user/project/report.txt and /home/example-user/notes.txt"
        run["prompt"] = "read /Users/example-user/input.json"

        path = Path(store.save_run(run, self.cfg))
        json_path = path.with_suffix(".json")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        raw = json_path.read_text(encoding="utf-8")

        self.assertNotIn("/Users/example-user", raw)
        self.assertNotIn("/home/example-user", raw)
        self.assertIn("~", data["answer"])
        self.assertIn("~", data["prompt"])
        self.assertNotIn("/Users/example-user", data["answer"])
        self.assertNotIn("/home/example-user", data["answer"])

    def test_load_runs_skips_one_corrupt_record_and_keeps_good_one(self):
        good = self._run("good-run")
        store.save_run(good, self.cfg)
        runs_dir = Path(self.cfg.log_dir)
        (runs_dir / "corrupt.json").write_text(
            '{"id": "corrupt", "title": ', encoding="utf-8")

        loaded = store.load_runs(self.cfg)
        ids = [item.get("id") for item in loaded]
        self.assertIn("good-run", ids)
        self.assertNotIn("corrupt", ids)

        with self.assertRaises(RuntimeError) as ctx:
            store.find_run("corrupt", self.cfg)
        self.assertIn("corrupt", str(ctx.exception).lower())

    def test_event_log_redacts_the_object_and_keeps_valid_json(self):
        fragment = '{"path": "/Users/example-user"'
        log = events.EventLog(self.root / "event-log")
        log.append("phase", {"note": "event " + fragment})
        line = (self.root / "event-log" / "events.jsonl").read_text(
            encoding="utf-8").strip()
        data = json.loads(line)
        note = data["payload"]["note"]
        self.assertEqual(note, 'event {"path": "~"')
        self.assertTrue(note.endswith('"'))
        self.assertIn('\\"', line)

    def test_tool_status_tolerates_corrupt_status_record(self):
        jobs_dir = Path(self.cfg.jobs_dir)
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / "corrupt-job.status.json").write_text(
            '{"status": "running", ', encoding="utf-8")

        # Low-level reader already returns an empty mapping.
        self.assertEqual(jobs._read_status(str(jobs_dir), "corrupt-job"), {})

        # The CLI status reader must not raise a bare JSONDecodeError.
        out = jobs.tool_status({"run_id": "corrupt-job"}, self.cfg)
        self.assertIsInstance(out, str)
        self.assertIn("corrupt", out.lower())




    def test_write_status_and_event_log_survive_escaped_home_quote(self):
        fragment = '{"path": "/Users/example-user"'
        status_path = self.root / "jobs" / "job-1.status.json"
        jobs.write_status(status_path, result="trace " + fragment)
        data = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(data["result"], 'trace {"path": "~"')
        self.assertTrue(data["result"].endswith('"'))

        log = events.EventLog(self.root / "work" / "w1")
        log.append("phase", {"note": "trace " + fragment})
        stored = log.read_since(0)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["payload"]["note"], 'trace {"path": "~"')


if __name__ == "__main__":
    unittest.main()
