"""3.9.5 slice B: builder-practices injection into builder/revision prompts."""

import unittest

from deepseek_mcp import builder_practices, quality

CODE_PHRASES = (
    "## Builder practices",
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
)

LIGHT_PHRASES = (
    "## Builder practices",
    "acceptance criteria",
    "numbered checklist",
    "minimal",
    "examples",
    "deliverables",
    "evidence",
)


class PromptWiringTests(unittest.TestCase):
    def builder(self, task_type, enabled=True):
        return quality.build_harness_task(
            "Build the widget from the brief.", 4, "classified as code",
            task_type=task_type, builder_practices=enabled)

    def revision(self, task_type, enabled=True):
        return quality.build_revision_task(
            "Build the widget from the brief.", ["out.txt"],
            ["Fix the broken output"], ["output.txt is empty"],
            task_type=task_type, builder_practices=enabled)

    def assert_injected_once(self, enabled, disabled, block):
        self.assertTrue(block)
        self.assertIn(block, enabled)
        self.assertEqual(enabled.replace(block, "", 1), disabled)

    def test_code_prompt_contains_every_required_phrase(self):
        text = self.builder("code")
        for phrase in CODE_PHRASES:
            self.assertIn(phrase, text)
        self.assertLessEqual(len(quality.builder_practice_block("code").split()), 350)

    def test_disabled_code_prompt_has_no_practices(self):
        enabled = self.builder("code", True)
        disabled = self.builder("code", False)
        block = quality.builder_practice_block("code")
        self.assertNotIn("Builder practices", disabled)
        self.assert_injected_once(enabled, disabled, block)
        self.assertGreater(len(enabled.split()) - len(disabled.split()), 0)

    def test_research_and_creative_get_shorter_light_block(self):
        code_block = quality.builder_practice_block("code")
        light_block = quality.builder_practice_block("research")
        self.assertLess(len(light_block.split()), len(code_block.split()))
        self.assertNotIn("tests first", light_block)
        self.assertLessEqual(len(light_block.split()), 350)
        for task_type in ("research", "creative", "docs"):
            text = self.builder(task_type)
            disabled = self.builder(task_type, False)
            self.assertIn("Builder practices", text)
            for phrase in LIGHT_PHRASES:
                self.assertIn(phrase, text)
            self.assertNotIn("tests first", text)
            self.assert_injected_once(text, disabled, light_block)

    def test_simple_task_gets_no_practices(self):
        text = self.builder("simple")
        self.assertNotIn("Builder practices", text)
        self.assertEqual(quality.builder_practice_block("simple"), "")
        self.assertEqual(text, self.builder("simple", False))

    def test_revision_code_injected_and_disabled_absent(self):
        enabled = self.revision("code", True)
        disabled = self.revision("code", False)
        block = quality.builder_practice_block("code")
        for phrase in CODE_PHRASES:
            self.assertIn(phrase, enabled)
        self.assertNotIn("Builder practices", disabled)
        self.assertNotIn("## Builder practices", disabled)
        self.assert_injected_once(enabled, disabled, block)
        # Immediately after the original brief, before the defect sections.
        self.assertLess(enabled.index(block), enabled.index("## Blocking defects"))
        self.assertGreater(enabled.index(block), enabled.index("## Original brief"))
        self.assertLessEqual(len(block.split()), 350)

    def test_injected_delta_within_word_budget(self):
        for task_type in ("code", "research", "creative"):
            block = quality.builder_practice_block(task_type)
            self.assertLessEqual(len(block.split()), 350)
            for enabled, disabled in (
                    (self.builder(task_type), self.builder(task_type, False)),
                    (self.revision(task_type), self.revision(task_type, False))):
                delta = len(enabled.split()) - len(disabled.split())
                self.assertEqual(delta, len(block.split()))
                self.assertLessEqual(delta, 350)

    def test_defaults_and_positional_compatibility(self):
        # Old positional prefixes still work and default injection stays on.
        text = quality.build_harness_task("Task", 0, "manual", None, False)
        self.assertIn("Builder practices", text)
        rev = quality.build_revision_task("Task", [], [], [])
        self.assertIn("Builder practices", rev)
        self.assertIs(quality.builder_practice_block,
                      builder_practices.block)
        self.assertIs(quality.builder_practice_record,
                      builder_practices.record)
        record = quality.builder_practice_record("code")
        self.assertEqual(record["set"], "builder-practices")
        self.assertEqual(record["version"], "1.0")


if __name__ == "__main__":
    unittest.main()
