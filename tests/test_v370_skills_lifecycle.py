"""v3.7.0 lifecycle: injection, promotion, demotion, rollback and refresh."""

import copy
import unittest

from deepseek_mcp import skill_auto, skills
from deepseek_mcp.skill_registry import SkillRegistry
from tests import v370_support as support


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-life-")
        self.addCleanup(self.tmp.cleanup)
        self.registry = SkillRegistry(self.cfg)
        self.registry.load()

    def create_candidate(self, key="podcast-launch", baseline=True):
        cluster = {"id": "c-test",
                   "baseline": ({"median": 8.0, "mean": 8.0, "n": 3,
                                 "blocking_rate": 0.0, "source": "members"}
                                if baseline else None)}
        self.registry.create_or_merge(
            support.valid_draft(key=key), {"score": 9.2, "rounds": 1,
                                           "blocking": 0},
            cluster, "auto", "run-create")
        return self.registry.get(key)

    def note(self, key, run_id, score=8.8, blocking=0):
        run = {"id": run_id, "quality_score": score,
               "review_rounds": [{"kind": "review", "score": score,
                                  "blocking_defects": blocking}]}
        baseline = {"median": 8.0, "mean": 8.0, "n": 3,
                    "blocking_rate": 0.0, "source": "members"}
        self.registry.note_run(key, run, baseline=baseline)
        return self.registry.get(key)

    def test_candidate_is_injected_and_propose_is_held_until_approved(self):
        prompt = "Plan a podcast launch with cover art and show notes"
        entry = self.create_candidate()
        self.assertEqual(skills.detect_skill_match(prompt, self.cfg)["key"],
                         "podcast-launch")
        data = self.registry.load()
        data["skills"]["podcast-launch"]["approved"] = False
        data["skills"]["podcast-launch"]["status"] = "candidate"
        self.registry.save(data)
        self.assertIsNone(skills.detect_skill_match(prompt, self.cfg)["key"])
        self.registry.set_status("podcast-launch", "candidate", "approved",
                                 approved=True)
        self.assertEqual(skills.detect_skill_match(prompt, self.cfg)["key"],
                         "podcast-launch")

    def test_promotion_after_five_non_worse_runs(self):
        self.create_candidate()
        for index in range(5):
            entry = self.note("podcast-launch", "run-%d" % index)
        result = skill_auto.promote_or_demote(entry, self.cfg)
        self.assertEqual(result["action"], "promote")
        self.assertEqual(self.registry.get("podcast-launch")["status"], "active")

    def test_candidate_without_a_baseline_never_promotes(self):
        entry = self.create_candidate(baseline=False)
        for index in range(6):
            entry = self.note("podcast-launch", "run-%d" % index)
        result = skill_auto.promote_or_demote(entry, self.cfg)
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["reason"], "no_baseline")
        self.assertIsNone(
            self.registry.get("podcast-launch")["stats"]["baseline_ref"])

    def test_worse_runs_demote_after_three_uses(self):
        self.create_candidate()
        for index in range(5):
            self.note("podcast-launch", "good-%d" % index, score=8.8)
        self.registry.set_status("podcast-launch", "active", "promote")
        for index in range(3):
            entry = self.note("podcast-launch", "bad-%d" % index, score=6.0)
        result = skill_auto.promote_or_demote(entry, self.cfg)
        self.assertEqual(result["action"], "demote")
        self.assertEqual(self.registry.get("podcast-launch")["status"],
                         "demoted")
        self.assertIsNone(skills.detect_skill_match(
            "Plan a podcast launch with cover art", self.cfg)["key"])

    def test_demote_after_uses_gate_can_be_raised(self):
        self.cfg._values["auto_skills_demote_after"] = 5
        self.create_candidate()
        self.registry.set_status("podcast-launch", "active", "promote")
        for index in range(3):
            entry = self.note("podcast-launch", "bad-%d" % index, score=5.0)
        result = skill_auto.promote_or_demote(entry, self.cfg)
        self.assertEqual(result["action"], "hold")
        self.assertEqual(self.registry.get("podcast-launch")["status"],
                         "active")

    def test_seed_rollback_returns_to_seed_generated_to_candidate(self):
        self.registry.add_version("poster", self.registry.content("poster"),
                                  "refresh")
        seed_result = self.registry.rollback("poster")
        self.assertEqual(seed_result["status"], "seed")
        self.create_candidate()
        self.registry.add_version("podcast-launch",
                                  self.registry.content("podcast-launch"),
                                  "refresh")
        generated = self.registry.rollback("podcast-launch")
        self.assertEqual(generated["status"], "candidate")
        self.assertTrue(self.registry.get("podcast-launch")["approved"])

    def test_rollback_missing_version_lists_available(self):
        self.create_candidate()
        with self.assertRaises(Exception) as caught:
            self.registry.rollback("podcast-launch", 99)
        self.assertIn("version", str(caught.exception).lower())

    def test_merge_into_seed_adds_aliases_and_bumps_version(self):
        draft = support.valid_draft(key="poster")
        draft["aliases"] = ["podcast-poster", "launch-poster"]
        before = self.registry.get("poster")["version"]
        result = self.registry.create_or_merge(
            draft, {"score": 9.0, "rounds": 1, "blocking": 0},
            {"id": "c-test", "baseline": None}, "auto", "run-merge")
        entry = self.registry.get("poster")
        self.assertEqual(result["action"], "merge")
        self.assertGreater(entry["version"], before)
        self.assertIn("podcast-poster", entry["aliases"])
        self.assertEqual(len(entry["aliases"]), len(set(entry["aliases"])))

    def test_refresh_gate_pass_writes_a_version_and_resets_counters(self):
        self.create_candidate()
        data = self.registry.load()
        entry = data["skills"]["podcast-launch"]
        entry["lessons"] = {"hash1": {"n": 3, "first": "2026-01-01",
                                      "last": "2026-01-02",
                                      "sample": "check the feed"}}
        entry["refresh"] = {"last_attempt": None, "attempts": 0,
                            "last_version": None}
        self.registry.save(data)
        refresh = {
            "schema": 1, "action": "refresh", "key": "podcast-launch",
            "checklist_additions": [
                {"section": "Verification",
                 "text": "Verify the RSS feed resolves before announcing."}],
            "lessons": [], "rationale": "Three runs missed the feed check."}
        preview = skill_author_refresh_preview(self.registry, refresh)
        from deepseek_mcp import skill_author
        original_author = skill_author.author_draft
        original_gate = skill_author.gate_refresh

        def fake_author(action, evidence, existing=None, cfg=None, **kwargs):
            return copy.deepcopy(refresh)

        def fake_gate(refresh_obj, evidence, existing_content, cfg,
                      cancel_check=None):
            return {"accepted": True, "decision": "refresh",
                    "preview": preview, "review": {"score": 9.0,
                                                   "blocking_defects": []},
                    "additions": 1}

        skill_author.author_draft = fake_author
        skill_author.gate_refresh = fake_gate
        self.addCleanup(lambda: setattr(skill_author, "author_draft",
                                        original_author))
        self.addCleanup(lambda: setattr(skill_author, "gate_refresh",
                                        original_gate))
        before = self.registry.get("podcast-launch")["version"]
        result = skill_auto.maybe_refresh("podcast-launch", self.cfg)
        self.assertEqual(result["action"], "refreshed")
        self.assertGreater(self.registry.get("podcast-launch")["version"], before)
        self.assertEqual(
            self.registry.get("podcast-launch")["lessons"]["hash1"]["n"], 0)


def skill_author_refresh_preview(registry, refresh):
    from deepseek_mcp import skill_author
    return skill_author.refresh_preview(registry.content("podcast-launch"),
                                        refresh)


if __name__ == "__main__":
    unittest.main()
