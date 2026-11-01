"""v3.7.0 configuration defaults, modes, validation and off-mode isolation."""

import unittest
from pathlib import Path

from deepseek_mcp import quality, skills
from deepseek_mcp.config import DEFAULTS, load_config
from tests import v370_support as support


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-config-")
        self.addCleanup(self.tmp.cleanup)

    def test_defaults_are_shipped(self):
        for key, value in {
            "auto_skills": "auto",
            "auto_skills_min_confidence": 0.55,
            "auto_skills_min_score": 2.0,
            "auto_skills_cluster_min": 3,
            "auto_skills_cluster_min_severe": 2,
            "auto_skills_quality_gate": 8.5,
            "auto_skills_review_rounds": 2,
            "auto_skills_promote_after": 5,
            "auto_skills_demote_after": 3,
            "auto_skills_max_versions": 10,
            "auto_skills_store_evidence_text": False,
        }.items():
            self.assertEqual(DEFAULTS[key], value, key)
            self.assertEqual(getattr(self.cfg, key), value, key)

    def test_env_aliases_and_precedence(self):
        env = dict(self.env)
        env.pop("DSH_AUTO_SKILLS", None)
        env["DEEPSEEK_AUTO_SKILLS"] = "propose"
        cfg = load_config(config_path=self.root / "config.json", env=env)
        self.assertEqual(cfg.auto_skills, "propose")
        env["DSH_AUTO_SKILLS"] = "off"
        cfg = load_config(config_path=self.root / "config.json", env=env)
        self.assertEqual(cfg.auto_skills, "off")

    def test_invalid_enum_raises_with_the_three_values(self):
        self.env["DSH_AUTO_SKILLS"] = "sometimes"
        with self.assertRaises(ValueError) as caught:
            load_config(config_path=self.root / "config.json", env=self.env)
        self.assertIn("auto, propose, off", str(caught.exception))

    def test_invalid_gate_raises(self):
        self.env["DSH_AUTO_SKILLS_GATE"] = "11"
        with self.assertRaises(ValueError):
            load_config(config_path=self.root / "config.json", env=self.env)

    def test_confidence_and_size_are_clamped(self):
        self.env["DSH_AUTO_SKILLS_MIN_CONFIDENCE"] = "4"
        cfg = load_config(config_path=self.root / "config.json", env=self.env)
        self.assertEqual(cfg.auto_skills_min_confidence, 1.0)
        cfg._values["auto_skills_max_skill_chars"] = 100
        # The config loader clamps the shipped/parsed value; a direct mutation
        # is clamped by the use site.
        self.assertEqual(
            __import__("deepseek_mcp.skill_registry", fromlist=["x"])._max_chars(cfg),
            4000)

    def test_off_mode_is_seed_only_and_writes_nothing(self):
        self.env["DSH_AUTO_SKILLS"] = "off"
        cfg = load_config(config_path=self.root / "config.json", env=self.env)
        directory = Path(cfg.skills_dir)
        before = sorted(str(path.relative_to(directory))
                        for path in directory.rglob("*"))
        rows = skills.list_skills(cfg)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row.get("status") == "seed" for row in rows))
        after = sorted(str(path.relative_to(directory))
                       for path in directory.rglob("*"))
        self.assertEqual(before, after)
        self.assertFalse((directory / "index.json").exists())

    def test_classification_is_unchanged_in_off_mode(self):
        result = quality.classify_task("Write a report about the data")
        self.assertEqual(result["task_type"], "creative")
        self.assertEqual(result["skill"], "document")

    def test_off_mode_never_adopts_a_custom_file(self):
        custom = Path(self.cfg.skills_dir) / "custom.md"
        custom.write_text("# Custom\n\n## Brief\n- [ ] Do the thing.\n",
                          encoding="utf-8")
        self.env["DSH_AUTO_SKILLS"] = "off"
        cfg = load_config(config_path=self.root / "config.json", env=self.env)
        keys = [row["key"] for row in skills.list_skills(cfg)]
        self.assertNotIn("custom", keys)
        with self.assertRaises(RuntimeError):
            skills.read_skill("custom", cfg)
        self.assertIsNone(skills.load_skill("custom", cfg))

    def test_propose_saves_but_does_not_inject(self):
        self.cfg._values["auto_skills"] = "propose"
        cfg = self.cfg
        from deepseek_mcp.skill_registry import SkillRegistry
        registry = SkillRegistry(cfg)
        registry.load()
        registry.create_or_merge(
            support.valid_draft(), {"score": 9.2, "rounds": 1, "blocking": 0},
            {"id": "c-test", "baseline": {"median": 8.0, "n": 3,
                                          "blocking_rate": 0.0}},
            "propose", "run-propose")
        entry = registry.get("podcast-launch")
        self.assertEqual(entry["status"], "candidate")
        self.assertFalse(entry["approved"])
        self.assertIsNone(skills.detect_skill_match(
            "Plan a podcast launch with cover art", cfg)["key"])


if __name__ == "__main__":
    unittest.main()
