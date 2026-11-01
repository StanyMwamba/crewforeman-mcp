"""v3.7.0 registry, migration, versioning and statistics tests."""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import skills
from deepseek_mcp.skill_registry import (MAX_INDEX_BYTES, RegistryError,
                                         SkillRegistry, safe_skill_path,
                                         split_front_matter, validate_key)
from tests import v370_support as support


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-reg-")
        self.addCleanup(self.tmp.cleanup)
        self.registry = SkillRegistry(self.cfg)
        self.directory = Path(self.cfg.skills_dir)

    def test_migration_registers_the_five_seeds_without_rewriting(self):
        before = (self.directory / "poster.md").read_bytes()
        data = self.registry.load()
        self.assertEqual(sorted(data["skills"]),
                         ["code", "data", "document", "poster", "web"])
        for key, entry in data["skills"].items():
            self.assertEqual(entry["status"], "seed")
            self.assertEqual(entry["source"], "builtin")
            self.assertTrue(entry["approved"])
            self.assertEqual(entry["version"], 1)
        self.assertEqual((self.directory / "poster.md").read_bytes(), before)
        self.assertTrue((self.directory / "index.json").is_file())
        self.assertEqual(len(list((self.directory / ".versions").iterdir())), 5)
        # An idempotent second load must not bump the revision.
        revision = self.registry.load()["revision"]
        self.assertEqual(self.registry.load()["revision"], revision)

    def test_seed_v1_rolls_back_after_many_versions(self):
        self.registry.load()
        original = self.registry.content("poster")
        for number in range(3):
            self.registry.add_version(
                "poster", original + "\n- [ ] Extra check %d.\n" % number,
                "refresh", review={"score": 9.0, "rounds": 1, "blocking": 0})
        result = self.registry.rollback("poster", 1)
        self.assertEqual(result["status"], "seed")
        self.assertEqual(self.registry.content("poster"), original)
        self.assertGreater(result["to_version"], 1)
        self.assertEqual(self.registry.get("poster")["version"],
                         result["to_version"])
        self.assertEqual(self.registry.read_version("poster", 1)[0]["version"], 1)

    def test_rollback_without_a_previous_version_is_actionable(self):
        self.registry.load()
        with self.assertRaises(RegistryError) as caught:
            self.registry.rollback("poster")
        self.assertIn("no previous version", str(caught.exception))

    def test_note_run_dedupes_a_repeated_run_id(self):
        self.registry.load()
        run = {"id": "run-1", "quality_score": 8.8,
               "review_rounds": [{"kind": "review", "blocking_defects": 0}]}
        first = self.registry.note_run("poster", run,
                                       baseline={"median": 8.0, "n": 3,
                                                 "blocking_rate": 0.0})
        second = self.registry.note_run("poster", run,
                                        baseline={"median": 8.0, "n": 3,
                                                  "blocking_rate": 0.0})
        self.assertEqual(first["stats"]["uses"], 1)
        self.assertEqual(second["stats"]["uses"], 1)
        self.assertEqual(second["reason"], "duplicate_run")

    def test_note_lesson_counts_duplicates(self):
        self.registry.load()
        one = self.registry.note_lesson("poster", "Check the QR quiet zone.",
                                        "run-1")
        two = self.registry.note_lesson("poster", "check the qr quiet zone",
                                        "run-2")
        self.assertEqual(one["hash"], two["hash"])
        self.assertEqual(two["n"], 2)

    def test_create_then_merge_into_a_seed_never_creates_a_duplicate(self):
        self.registry.load()
        draft = support.valid_draft(key="poster", cluster_id="c-1")
        draft["_merge_target"] = "poster"
        before = self.registry.get("poster")["version"]
        result = self.registry.create_or_merge(
            draft, {"score": 9.0, "rounds": 1, "blocking": 0},
            {"id": "c-1", "baseline": {"median": 8.0, "mean": 8.0, "n": 3,
                                       "blocking_rate": 0.0, "source": "members"}},
            "auto", "run-1")
        self.assertEqual(result["action"], "merge")
        self.assertFalse((self.directory / "poster-2.md").exists())
        self.assertGreater(self.registry.get("poster")["version"], before)
        self.assertEqual(self.registry.get("poster")["source"], "builtin")

    def test_create_candidate_freezes_the_cluster_baseline(self):
        self.registry.load()
        result = self.registry.create_or_merge(
            support.valid_draft(), {"score": 9.2, "rounds": 1, "blocking": 0},
            {"id": "c-test", "baseline": {"median": 8.0, "mean": 8.0, "n": 3,
                                           "blocking_rate": 0.1,
                                           "source": "members"}},
            "auto", "run-1")
        entry = self.registry.get(result["key"])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(entry["stats"]["baseline_ref"]["median"], 8.0)
        self.assertIn("frozen", entry["stats"]["baseline_ref"])
        self.assertTrue((self.directory / (result["key"] + ".md")).is_file())
        meta, body = split_front_matter(
            (self.directory / (result["key"] + ".md")).read_text(encoding="utf-8"))
        self.assertEqual(meta["key"], result["key"])
        self.assertIn("## Lessons learned", body)

    def test_invalid_keys_and_traversal_are_refused(self):
        for bad in ("../x", "/abs", "a/b", "Index", "has space", "x" * 40):
            with self.assertRaises(RegistryError):
                validate_key(bad)
        with self.assertRaises(RegistryError):
            safe_skill_path(self.directory, "../escape")

    def test_index_rotation_and_recovery_from_bak(self):
        self.registry.load()
        self.registry.save()
        self.assertTrue((self.directory / "index.json.bak").is_file())
        (self.directory / "index.json").write_text("{not json", encoding="utf-8")
        recovered = SkillRegistry(self.cfg).load()
        self.assertEqual(len(recovered["skills"]), 5)
        self.assertTrue(any("recovered" in str(item).lower()
                            for item in recovered.get("warnings") or []))

    def test_save_is_atomic_on_replace_failure(self):
        self.registry.load()
        before = (self.directory / "index.json").read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.registry.save()
        self.assertEqual((self.directory / "index.json").read_bytes(), before)

    def test_entries_and_get_resolve_aliases(self):
        self.registry.load()
        self.assertIsNotNone(self.registry.get("visual"))
        self.assertEqual(self.registry.resolve_key("visual-design"), "poster")
        self.assertEqual(self.registry.resolve_key("poster.md"), "poster")
        self.assertEqual(len(self.registry.entries()), 5)
        self.assertEqual(len(self.registry.entries(status="seed")), 5)

    def test_off_mode_listing_does_not_create_a_registry(self):
        self.env["DSH_AUTO_SKILLS"] = "off"
        from deepseek_mcp.config import load_config
        cfg = load_config(config_path=self.root / "config.json", env=self.env)
        rows = skills.list_skills(cfg)
        self.assertEqual(len(rows), 5)
        self.assertFalse((self.directory / "index.json").exists())
        self.assertFalse((self.directory / ".versions").exists())


if __name__ == "__main__":
    unittest.main()
