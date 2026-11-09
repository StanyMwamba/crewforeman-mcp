"""Focused tests for the 3.9.5 builder-practices core module and config."""

import json
import os
import unittest

from deepseek_mcp import builder_practices as bp
from deepseek_mcp.config import SERVER_VERSION, load_config

REQUIRED_CODE = [
    "acceptance criteria",
    "numbered checklist",
    "tests first",
    "minimal and in scope",
    "hard-code",
    "examples",
    "full suite",
    "exact counts",
    "re-read",
    "deliverables",
    "never claim",
    "evidence",
]

REQUIRED_LIGHT = [
    "acceptance criteria",
    "numbered checklist",
    "minimal",
    "examples",
    "deliverables",
    "evidence",
]

RECORD_KEYS = {"set", "version", "variant", "enabled", "words", "chars",
               "tokens_est", "budget_words"}


class PracticeTextTests(unittest.TestCase):
    def test_code_block_heading_and_budget(self):
        self.assertTrue(bp.CODE_BLOCK.startswith("## Builder practices"))
        self.assertLessEqual(bp.word_count(bp.CODE_BLOCK), bp.WORD_BUDGET)

    def test_light_block_budget(self):
        self.assertLessEqual(bp.word_count(bp.LIGHT_BLOCK), bp.WORD_BUDGET)

    def test_required_substrings(self):
        for needle in REQUIRED_CODE:
            self.assertIn(needle, bp.CODE_BLOCK)
        for needle in REQUIRED_LIGHT:
            self.assertIn(needle, bp.LIGHT_BLOCK)

    def test_light_block_is_shorter(self):
        self.assertLess(bp.word_count(bp.LIGHT_BLOCK),
                        bp.word_count(bp.CODE_BLOCK))

    def test_code_block_is_self_contained_markdown(self):
        self.assertNotIn("{{", bp.CODE_BLOCK)
        self.assertNotIn("}}", bp.CODE_BLOCK)


class VariantTests(unittest.TestCase):
    def test_variant_for(self):
        self.assertEqual(bp.variant_for("code"), "code")
        self.assertEqual(bp.variant_for(None), "code")
        self.assertEqual(bp.variant_for("research"), "light")
        self.assertEqual(bp.variant_for("creative"), "light")
        for docs_type in ("docs", "doc", "document", "documentation"):
            self.assertEqual(bp.variant_for(docs_type), "light")
        self.assertEqual(bp.variant_for("simple"), "none")

    def test_block_wraps_constant(self):
        self.assertEqual(bp.block("code"), "\n\n" + bp.CODE_BLOCK + "\n")
        self.assertEqual(bp.block("research"),
                         "\n\n" + bp.LIGHT_BLOCK + "\n")

    def test_block_disabled_and_simple_are_empty(self):
        self.assertEqual(bp.block("code", enabled=False), "")
        self.assertEqual(bp.block("simple"), "")


class RecordTests(unittest.TestCase):
    def test_record_keys_and_code_values(self):
        rec = bp.record("code")
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["set"], "builder-practices")
        self.assertEqual(rec["version"], "1.0")
        self.assertEqual(rec["variant"], "code")
        self.assertIs(rec["enabled"], True)
        self.assertEqual(rec["words"], bp.word_count(bp.CODE_BLOCK))
        self.assertEqual(rec["chars"], len(bp.CODE_BLOCK))
        self.assertEqual(rec["tokens_est"], (len(bp.CODE_BLOCK) + 3) // 4)
        self.assertEqual(rec["budget_words"], 350)

    def test_record_light_and_simple(self):
        light = bp.record("research")
        self.assertEqual(light["variant"], "light")
        self.assertEqual(light["words"], bp.word_count(bp.LIGHT_BLOCK))
        simple = bp.record("simple")
        self.assertEqual(simple["variant"], "none")
        self.assertEqual(simple["words"], 0)

    def test_record_disabled(self):
        rec = bp.record("code", enabled=False)
        self.assertEqual(rec["variant"], "none")
        self.assertIs(rec["enabled"], False)
        self.assertEqual(rec["words"], 0)
        self.assertEqual(rec["chars"], 0)
        self.assertEqual(rec["tokens_est"], 0)
        json.dumps(rec)

    def test_word_count(self):
        self.assertEqual(bp.word_count(""), 0)
        self.assertEqual(bp.word_count("one two  three"), 3)


class ConfigTests(unittest.TestCase):
    def test_server_version(self):
        self.assertEqual(SERVER_VERSION, "3.13.7")

    def test_builder_practices_default_true(self):
        cfg = load_config(env={}, include_file=False)
        self.assertIs(cfg.builder_practices, True)

    def test_builder_practices_env_off_on(self):
        off = load_config(env={"DSH_BUILDER_PRACTICES": "off"},
                          include_file=False)
        self.assertIs(off.builder_practices, False)
        on = load_config(env={"DSH_BUILDER_PRACTICES": "on"},
                         include_file=False)
        self.assertIs(on.builder_practices, True)

    def test_config_example_has_key(self):
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "config.example.json")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIs(data["builder_practices"], True)


if __name__ == "__main__":
    unittest.main()
