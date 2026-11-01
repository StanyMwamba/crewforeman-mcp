import json
import shutil
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import skills
from deepseek_mcp.config import load_config
from deepseek_mcp.store import new_run, save_run
from tests.fakes import base_env

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(str(REPO_SKILLS), str(self.root / "skills"))
        env = base_env(self.root)
        env["DSH_SKILLS_DIR"] = str(self.root / "skills")
        self.cfg = load_config(config_path=self.root / "config.json", env=env)

    def test_detect_all_skill_types(self):
        cases = {
            "Design a print poster with a QR code": "poster",
            "Write a cover letter for a job": "document",
            "Build a responsive HTML web page for a cafe": "web",
            "Write a Python CLI with tests and a build step": "code",
            "Analyze this CSV dataset and chart the metrics": "data",
        }
        for prompt, expected in cases.items():
            self.assertEqual(skills.detect_skill(prompt), expected, prompt)
        self.assertIsNone(skills.detect_skill("What is 2+2?"))

    def test_list_and_read_skill(self):
        listing = skills.format_skill_list(self.cfg)
        for key in ("poster", "document", "web", "code", "data"):
            self.assertIn(key, listing)
        content = skills.read_skill("poster", self.cfg)
        self.assertIn("Poster / visual design", content)
        self.assertIn("QR codes", content)
        content_alias = skills.read_skill("visual", self.cfg)
        self.assertEqual(content, content_alias)
        with self.assertRaises(RuntimeError):
            skills.read_skill("no-such-skill", self.cfg)

    def test_append_lesson_sanitises_personal_data(self):
        email = "person" + "@" + "example.com"
        home = "/" + "Users" + "/" + "someone/file"
        secret = "sk" + "-" + "abcdef123456"
        path = skills.append_lesson(
            "poster",
            "Email me at %s or %s; token %s" % (email, home, secret),
            self.cfg, run_id="run-1", rating="down")
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("## Lessons learned", text)
        self.assertIn("rating=down", text)
        self.assertNotIn(email, text)
        self.assertNotIn(home, text)
        self.assertNotIn(secret, text)
        self.assertIn("[email]", text)
        self.assertIn("[redacted]", text)

    def test_rate_run_updates_run_and_skill(self):
        run = new_run("harness", {"_run_id": "rate-test"}, "Design a print poster", self.cfg)
        run.update(status="ok", quality_score=6.0, skill="poster")
        save_run(run, self.cfg)
        message = skills.rate_run("rate-test", "down",
                                  "Heading overlapped the footer on the rendered poster", self.cfg)
        self.assertIn("Saved rating", message)
        updated = json.loads((self.root / "runs" / "rate-test.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["rating"], "down")
        self.assertIn("Heading overlapped", updated["rating_note"])
        skill_text = (self.root / "skills" / "poster.md").read_text(encoding="utf-8")
        self.assertIn("User rated the result down", skill_text)
        self.assertIn("Heading overlapped", skill_text)

    def test_rate_run_rejects_bad_rating(self):
        run = new_run("harness", {"_run_id": "rate-bad"}, "Design a poster", self.cfg)
        run.update(status="ok")
        save_run(run, self.cfg)
        with self.assertRaises(RuntimeError):
            skills.rate_run("rate-bad", "meh", "", self.cfg)

    def test_near_duplicate_lessons_are_not_appended_twice(self):
        path = skills.append_lesson(
            "poster", "Headline overlaps the footer on the rendered poster", self.cfg,
            run_id="dup-1")
        skills.append_lesson(
            "poster", "Headline overlaps the footer on the rendered poster!", self.cfg,
            run_id="dup-2")
        text = Path(path).read_text(encoding="utf-8")
        self.assertEqual(text.count("Headline overlaps the footer"), 1)

    def test_lesson_cap_condenses_into_at_most_ten_bullets(self):
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
                 "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
                 "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
                 "victor", "whiskey", "xray", "yankee", "zulu", "amber", "bronze",
                 "copper", "denim", "emerald", "flint", "garnet", "hazel", "ivory",
                 "jade", "kelp", "lemon", "mango", "nectar", "olive", "pearl",
                 "quartz", "ruby", "slate"]
        path = None
        for index, word in enumerate(words):
            path = skills.append_lesson(
                "poster", "Always check %s spacing before export and rerender" % word,
                self.cfg, run_id="cap-%d" % index)
        prefix, active, consolidated = skills._parse_lessons(
            Path(path).read_text(encoding="utf-8"))
        self.assertLessEqual(len(active), 30)
        self.assertLessEqual(len(consolidated), 10)
        self.assertEqual(len(active), 30)

    def test_only_eight_most_relevant_lessons_are_injected(self):
        for index, topic in enumerate(("alpha", "bravo", "charlie", "delta", "echo",
                                       "foxtrot", "golf", "hotel", "india", "juliet")):
            skills.append_lesson("poster", "Always check %s spacing before export" % topic,
                                 self.cfg, run_id="inject-%d" % index)
        text = (self.root / "skills" / "poster.md").read_text(encoding="utf-8")
        injected = skills.with_selected_lessons(text, "poster layout for hotel", 8)
        bullets = [line for line in injected.splitlines()
                   if line.startswith("- ") and not line.startswith("- [")]
        self.assertEqual(len(bullets), 8)
        self.assertIn("hotel", injected)

    def test_lesson_signal_only_for_real_events(self):
        clean = {"status": "ok", "review_rounds": [
            {"kind": "review", "blocking_defects": 0}]}
        self.assertFalse(skills.lesson_signal(clean))
        self.assertIsNone(skills.distill_lesson(clean))
        failed = {"status": "error", "error": "boom", "review_rounds": []}
        self.assertTrue(skills.lesson_signal(failed))
        self.assertIn("run failed", skills.distill_lesson(failed))
        fixed = {"status": "ok", "review_rounds": [
            {"kind": "review", "blocking_defects": 1,
             "defects": ["Headline overlaps footer"]},
            {"kind": "revision", "fixes": ["Move the footer"], "status": "ok"}]}
        self.assertTrue(skills.lesson_signal(fixed))
        lesson = skills.distill_lesson(fixed)
        self.assertIn("independent review found", lesson)
        self.assertIn("fixed", lesson)


    def test_rescued_review_failure_lesson_is_not_run_failed_unknown(self):
        run = {
            "id": "rescued-review-failed",
            "status": "ok_unreviewed",
            "rescued": True,
            "stalled": True,
            "review_error": "fake reviewer failure",
            "review_rounds": [],
        }
        self.assertTrue(skills.lesson_signal(run))
        lesson = skills.distill_lesson(run)
        self.assertIn("independent review failed: fake reviewer failure", lesson)
        self.assertNotIn("run failed: unknown", lesson)


if __name__ == "__main__":
    unittest.main()
