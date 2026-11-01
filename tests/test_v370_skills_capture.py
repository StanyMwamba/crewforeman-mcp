"""v3.7.0 unmatched capture, privacy and clustering tests."""

import json
import unittest
from pathlib import Path

from deepseek_mcp import skill_auto
from tests import v370_support as support


def _run(run_id, prompt="Plan a podcast launch with cover art and show notes",
         score=8.0, status="ok_below_gate", blocking=0, task_type="creative"):
    return {
        "id": run_id, "prompt": prompt, "status": status,
        "quality_score": score, "task_type": task_type,
        "size_estimate": "m", "output_files": ["cover.png", "notes.pdf"],
        "skill_match": {"key": None, "unmatched": True, "confidence": 0.3,
                        "scores": {"document": 1}},
        "review_rounds": [{"kind": "review", "score": score,
                           "blocking_defects": blocking,
                           "defects": ["missing cover art"] if blocking else [],
                           "fixes": ["add cover"] if blocking else []}],
    }


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-capture-")
        self.addCleanup(self.tmp.cleanup)

    def test_pii_is_removed_before_tokenisation(self):
        email = "jordan" + "@example.com"
        secret = "sk" + "-abcdef123456"
        aws = "AKIA" + "ABCDEFGHIJKLMNOP"
        bearer = "Bear" + "er abc.def"
        prompt = ("Email %s or use %s and "
                  "%s at https://example.com/x?token=1, "
                  "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc, "
                  "%s and call +1 (555) 123-4567. "
                  "The launch poster needs a cover." % (email, bearer, secret, aws))
        tokens = skill_auto.extract_keywords(prompt)
        joined = " ".join(tokens)
        for needle in ("jordan", "example.com", "Bearer", "sk-abcdef",
                       "eyJhbGci", "AKIA", "555", "123-4567"):
            self.assertNotIn(needle.lower(), joined.lower())
        self.assertIn("poster", tokens)

    def test_probable_proper_nouns_are_dropped(self):
        self.assertNotIn("jordan", skill_auto.extract_keywords(
            "Jordan needs a launch poster"))
        self.assertNotIn("jordan", skill_auto.extract_keywords(
            "Plan a launch with Jordan and Maria in Berlin"))
        self.assertIn("design", skill_auto.extract_keywords(
            "Design a launch poster"))
        self.assertIn("plan", skill_auto.extract_keywords(
            "Plan a launch poster"))

    def test_evidence_sanitiser_redacts_names_at_any_position(self):
        home = "/" + "Users" + "/jordan/a.png"
        text = skill_auto.sanitise_evidence(
            "Jordan said the QR code is missing at https://x.com and " + home)
        self.assertNotIn("jordan", text.lower())
        self.assertNotIn("https://", text)
        self.assertIn("[name]", text)
        self.assertLessEqual(len(text), 200)

    def test_capture_record_never_contains_raw_text_or_paths(self):
        run = _run("run-1")
        block = skill_auto.after_run(run, self.cfg)
        self.assertTrue(block["capture"]["captured"])
        capture_text = (Path(self.cfg.skills_dir) / ".capture" /
                        "unmatched.jsonl").read_text(encoding="utf-8")
        for raw in ("Plan a podcast launch", "cover.png", "notes.pdf",
                    str(self.root)):
            self.assertNotIn(raw, capture_text)
        record = json.loads(capture_text.splitlines()[0])
        for key in ("v", "run_id", "created", "task_type", "keywords",
                    "intent", "deliverable_kinds", "size_bucket", "match",
                    "outcome", "severe"):
            self.assertIn(key, record)
        self.assertNotIn("prompt", record)
        self.assertNotIn("title", record)
        self.assertEqual(record["intent"], " ".join(record["keywords"][:3]))

    def test_samples_are_off_by_default_and_capped_when_enabled(self):
        run = _run("run-1")
        skill_auto.after_run(run, self.cfg)
        base = (Path(self.cfg.skills_dir) / ".capture" /
                "unmatched.jsonl").read_text(encoding="utf-8")
        self.assertIn('"defect_samples":[]', base)
        self.assertIn('"fix_samples":[]', base)
        self.env["DSH_AUTO_SKILLS"] = "auto"
        self.cfg._values["auto_skills_store_evidence_text"] = True
        run2 = _run("run-2")
        skill_auto.after_run(run2, self.cfg)
        lines = (Path(self.cfg.skills_dir) / ".capture" /
                 "unmatched.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[-1])
        self.assertLessEqual(len(record["outcome"]["defect_samples"]), 3)
        self.assertTrue(all(len(item) <= 200
                            for item in record["outcome"]["defect_samples"]))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-capture2-")
        self.addCleanup(self.tmp.cleanup)

    def test_simple_question_is_skipped_without_a_capture(self):
        run = _run("run-simple", prompt="What is 2+2?", task_type="simple",
                   score=None)
        run["review_rounds"] = []
        block = skill_auto.after_run(run, self.cfg)
        self.assertEqual(block["capture"]["reason"], "skipped_simple")
        self.assertFalse(block["capture"]["captured"])
        self.assertFalse((Path(self.cfg.skills_dir) / ".capture" /
                          "unmatched.jsonl").exists())

    def test_repeated_run_id_is_never_captured_twice(self):
        run = _run("run-1")
        skill_auto.after_run(run, self.cfg)
        again = _run("run-1")
        block = skill_auto.after_run(again, self.cfg)
        # after_run returns {} for an already observed run only when the run
        # carries the latch; capture_seen is the independent guard.
        self.assertTrue(skill_auto.capture_seen("run-1", self.cfg))
        lines = (Path(self.cfg.skills_dir) / ".capture" /
                 "unmatched.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_unrelated_runs_stay_in_separate_clusters(self):
        skill_auto.after_run(_run("a", prompt="Plan a podcast launch with cover art and show notes"), self.cfg)
        skill_auto.after_run(_run("b", prompt="Plan a podcast launch with cover art and show notes"), self.cfg)
        skill_auto.after_run(_run("c", prompt="Analyze a CSV dataset and chart the metrics",
                                  task_type="research"), self.cfg)
        data = json.loads((Path(self.cfg.skills_dir) / ".capture" /
                           "clusters.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["clusters"]), 2)

    def test_three_similar_runs_make_a_ready_cluster(self):
        for index in range(3):
            block = skill_auto.after_run(
                _run("run-%d" % index), self.cfg)
        self.assertEqual(block["capture"]["count"], 3)
        self.assertTrue(block["capture"]["ready"])
        data = json.loads((Path(self.cfg.skills_dir) / ".capture" /
                           "clusters.json").read_text(encoding="utf-8"))
        cluster = list(data["clusters"].values())[0]
        self.assertEqual(cluster["status"], "ready")
        self.assertEqual(len(cluster["members"]), 3)
        self.assertEqual(data["schema"], 1)

    def test_severe_runs_lower_the_threshold_to_two(self):
        first = _run("s1", status="error", score=5.0, blocking=2)
        first["review_rounds"][0]["defects"] = ["data loss in export"]
        skill_auto.after_run(first, self.cfg)
        second = _run("s2", status="ok_below_gate", score=8.0, blocking=1)
        skill_auto.after_run(second, self.cfg)
        data = json.loads((Path(self.cfg.skills_dir) / ".capture" /
                           "clusters.json").read_text(encoding="utf-8"))
        cluster = list(data["clusters"].values())[0]
        self.assertEqual(cluster["severe_count"], 1)
        self.assertEqual(cluster["status"], "ready")

    def test_single_severe_run_is_held_by_default(self):
        run = _run("one", status="error", score=5.0, blocking=2)
        block = skill_auto.after_run(run, self.cfg)
        self.assertFalse(block["capture"]["ready"])


if __name__ == "__main__":
    unittest.main()
