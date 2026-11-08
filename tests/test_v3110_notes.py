"""F3 failure-note tests (stdlib unittest, injected clock)."""

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deepseek_mcp import failure_notes as fn
from deepseek_mcp.config import Config

UTC = dt.timezone.utc
NOW = dt.datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def iso(days_ago=0.0):
    return (NOW - dt.timedelta(days=days_ago)).isoformat()


class FailureNotesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def cfg(self, **overrides):
        values = {
            "state_dir": str(self.tmp),
            "failure_notes": "on",
            "failure_notes_max": 3,
            "failure_notes_half_life_days": 30,
            "failure_notes_min_occurrences": 2,
            "failure_notes_max_store": 200,
            "failure_notes_store": "",
        }
        values.update(overrides)
        return Config(values)

    def store(self):
        return self.tmp / "failure_notes.json"

    def test_exact_half_life(self):
        cfg = self.cfg()
        for round_no, days in ((1, 60), (2, 30), (3, 1)):
            fn.record("code", ["crash"], "run-1", round_no, cfg,
                      now=NOW - dt.timedelta(days=days))
        data = fn.load(cfg)
        decayed = fn.decay(data["task_types"]["code"], NOW, 30)
        self.assertAlmostEqual(decayed["crash"]["weight"], 1.727, places=3)

    def test_future_last_seen_clamped(self):
        cfg = self.cfg()
        future = (NOW + dt.timedelta(days=30)).isoformat()
        payload = {
            "version": 1,
            "task_types": {"code": {"crash": {
                "count": 2, "weight": 1.0, "first_seen": future,
                "last_seen": future, "sample_run_ids": []}}},
            "seen": {}, "updated": "",
        }
        self.store().write_text(json.dumps(payload), encoding="utf-8")
        decayed = fn.decay(fn.load(cfg)["task_types"]["code"], NOW, 30)
        self.assertAlmostEqual(decayed["crash"]["weight"], 1.0)
        note = fn.note_for("code", cfg, now=NOW)
        self.assertIn("weight 1.00", note)
        fn.record("code", ["crash"], "run-future", 1, cfg, now=NOW)
        stored = fn.load(cfg)["task_types"]["code"]["crash"]
        self.assertAlmostEqual(stored["weight"], 2.0)
        self.assertLessEqual(stored["last_seen"], NOW.isoformat())

    def test_prune_below_threshold(self):
        entry = {"count": 1, "weight": 0.04, "first_seen": iso(),
                 "last_seen": iso(), "sample_run_ids": []}
        pruned = fn.prune({"crash": entry}, self.cfg(), NOW)
        self.assertNotIn("crash", pruned)

    def test_global_cap_200(self):
        entries = {}
        for index in range(250):
            entries["kind-%03d" % index] = {
                "count": 2, "weight": float(index + 1),
                "first_seen": iso(), "last_seen": iso(),
                "sample_run_ids": [],
            }
        pruned = fn.prune(entries, self.cfg(), NOW)
        self.assertEqual(len(pruned), 200)
        self.assertIn("kind-249", pruned)
        self.assertNotIn("kind-000", pruned)

    def test_dedupe_run_id_and_round(self):
        cfg = self.cfg()
        fn.record("code", ["crash"], "run-9", 1, cfg, now=NOW)
        fn.record("code", ["crash"], "run-9", 1, cfg, now=NOW)
        entry = fn.load(cfg)["task_types"]["code"]["crash"]
        self.assertEqual(entry["count"], 1)
        self.assertAlmostEqual(entry["weight"], 1.0)

    def test_next_round_increments(self):
        cfg = self.cfg()
        fn.record("code", ["crash"], "run-9", 1, cfg, now=NOW)
        fn.record("code", ["crash"], "run-9", 2, cfg, now=NOW)
        entry = fn.load(cfg)["task_types"]["code"]["crash"]
        self.assertEqual(entry["count"], 2)
        self.assertAlmostEqual(entry["weight"], 2.0)

    def test_only_blocking_review_rounds(self):
        cfg = self.cfg()
        run = {
            "run_id": "run-review",
            "task_type": "code",
            "review_rounds": [
                {"kind": "review", "round": 1, "blocking_defects": 2,
                 "defects": ["failing test or build",
                             "missing required fact",
                             "failing test or build"]},
                {"kind": "review", "round": 2, "blocking_defects": 0,
                 "defects": ["crash"]},
                {"kind": "builder", "round": 3, "blocking_defects": 5,
                 "defects": ["crash"]},
                {"kind": "review", "round": 4, "blocking_defects": 1,
                 "defects": ["missing required fact"]},
            ],
        }
        summary = fn.record_review_run(run, cfg, now=NOW)
        self.assertEqual(summary["rounds"], 2)
        kinds = fn.load(cfg)["task_types"]["code"]
        self.assertEqual(kinds["failing test or build"]["count"], 1)
        self.assertEqual(kinds["missing required fact"]["count"], 2)
        self.assertNotIn("crash", kinds)
        before = json.loads(self.store().read_text(encoding="utf-8"))
        fn.terminal_sweep(run, cfg, now=NOW)
        after = json.loads(self.store().read_text(encoding="utf-8"))
        self.assertEqual(before["task_types"], after["task_types"])
        self.assertNotIn("failure_notes", run)

    def test_leaky_labels_rejected(self):
        cfg = self.cfg()
        leaky = [
            "secret@example.com",
            "/etc/passwd",
            "C:\\Users\\alice\\private.txt",
            "bad\\label",
            "https://evil.example/x",
            "see www.evil.example now",
            "alice@example.com",
            "one two three four five six seven",
            "x" * 65,
        ]
        fn.record("code", leaky, "run-leak", 1, cfg, now=NOW)
        self.assertEqual(fn.load(cfg)["task_types"], {})
        self.assertEqual(fn.note_for("code", cfg, now=NOW), "")
        self.assertFalse(self.store().exists())

    def test_hash_fallback_never_rendered(self):
        cfg = self.cfg()
        fn.record("code", ["layout wobble"], "run-hash-1", 1, cfg, now=NOW)
        fn.record("code", ["layout wobble"], "run-hash-2", 2, cfg, now=NOW)
        data = fn.load(cfg)
        keys = list(data["task_types"]["code"])
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("sig:"))
        self.assertEqual(data["task_types"]["code"][keys[0]]["count"], 2)
        note = fn.note_for("code", cfg, now=NOW)
        self.assertEqual(note, "")
        self.assertNotIn("sig:", note)
        self.assertNotIn("layout wobble",
                         self.store().read_text(encoding="utf-8"))

    def test_code_and_creative_are_isolated(self):
        cfg = self.cfg()
        fn.record("code", ["crash"], "run-code-1", 1, cfg, now=NOW)
        fn.record("code", ["crash"], "run-code-2", 2, cfg, now=NOW)
        self.assertIn("crash", fn.note_for("code", cfg, now=NOW))
        self.assertEqual(fn.note_for("creative", cfg, now=NOW), "")
        self.assertEqual(fn.note_for("research", cfg, now=NOW), "")
        self.assertEqual(fn.note_for("unknown", cfg, now=NOW), "")

    def test_min_occurrences_gate(self):
        cfg = self.cfg()
        fn.record("code", ["crash"], "run-min-1", 1, cfg, now=NOW)
        self.assertEqual(fn.note_for("code", cfg, now=NOW), "")
        fn.record("code", ["crash"], "run-min-2", 2, cfg, now=NOW)
        note = fn.note_for("code", cfg, now=NOW)
        self.assertIn("Common failures for this task type (code)", note)
        self.assertIn("- crash (seen 2x, weight 2.00)", note)

    def test_disabled_creates_no_file(self):
        cfg = self.cfg(failure_notes="off")
        fn.record("code", ["crash"], "run-off", 1, cfg, now=NOW)
        self.assertEqual(fn.load(cfg)["task_types"], {})
        self.assertEqual(fn.note_for("code", cfg, now=NOW), "")
        self.assertFalse(self.store().exists())
        self.assertFalse((self.tmp / "failure_notes.lock").exists())
        fn.reset(cfg)
        self.assertFalse(self.store().exists())

    def test_corrupt_store_quarantined(self):
        cfg = self.cfg()
        self.store().write_text("{not json", encoding="utf-8")
        data = fn.load(cfg)
        self.assertEqual(data["task_types"], {})
        corrupt = self.store().with_name("failure_notes.json.corrupt")
        self.assertTrue(corrupt.exists())
        self.assertFalse(self.store().exists())
        fn.record("code", ["crash"], "run-after-corrupt", 1, cfg, now=NOW)
        self.assertIn("crash", fn.load(cfg)["task_types"]["code"])

    def test_two_sequential_writers_survive(self):
        cfg1 = self.cfg()
        cfg2 = self.cfg()
        fn.record("code", ["crash"], "run-w1", 1, cfg1, now=NOW)
        fn.record("code", ["broken artifact"], "run-w2", 2, cfg2, now=NOW)
        kinds = fn.load(cfg1)["task_types"]["code"]
        self.assertEqual(kinds["crash"]["count"], 1)
        self.assertEqual(kinds["broken artifact"]["count"], 1)

    def test_note_bounds_and_format(self):
        cfg = self.cfg(failure_notes_max=3)
        labels = ["crash", "overlap", "qr code", "missing required fact",
                  "broken artifact", "empty deliverable"]
        serial = 0
        for label in labels:
            for _ in range(2):
                serial += 1
                fn.record("code", [label], "run-bound-%d" % serial, serial,
                          cfg, now=NOW)
        note = fn.note_for("code", cfg, now=NOW)
        self.assertLessEqual(len(note), 400)
        lines = note.splitlines()
        self.assertEqual(lines[0], "Common failures for this task type (code)")
        self.assertEqual(
            lines[1],
            "Recent reviewer blocking defects. Pre-flight these before returning:")
        bullets = lines[2:]
        self.assertEqual(len(bullets), 3)
        for bullet in bullets:
            self.assertRegex(
                bullet, r"^- .+ \(seen 2x, weight 2\.00\)$")

    def test_path_override_relative_and_absolute(self):
        cfg = self.cfg(failure_notes_store="alt/notes.json")
        fn.record("code", ["crash"], "run-path", 1, cfg, now=NOW)
        relative = self.tmp / "alt" / "notes.json"
        self.assertTrue(relative.exists())
        self.assertFalse(self.store().exists())
        self.assertEqual(
            json.loads(relative.read_text(encoding="utf-8"))["version"], 1)

        absolute = self.tmp / "abs" / "custom.json"
        cfg2 = self.cfg(failure_notes_store=str(absolute))
        fn.record("creative", ["overlap"], "run-abs", 1, cfg2, now=NOW)
        self.assertTrue(absolute.exists())

    def test_empty_note_and_reset(self):
        cfg = self.cfg()
        self.assertEqual(fn.note_for("code", cfg, now=NOW), "")
        fn.record("unknown", ["crash"], "run-unknown", 1, cfg, now=NOW)
        self.assertEqual(fn.note_for("unknown", cfg, now=NOW), "")
        self.assertEqual(fn.note_for("code", cfg, now=NOW), "")
        self.assertTrue(fn.reset(cfg)["reset"])
        self.assertFalse(self.store().exists())
        self.assertEqual(fn.load(cfg)["task_types"], {})


if __name__ == "__main__":
    unittest.main()
