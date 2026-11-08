"""Tests for deepseek_mcp.review_scope and its quality.parse_review wiring.

Part B item 2 (3.10.0): pre-existing findings are non-blocking follow-ups,
except security/data-loss ones, which stay blocking and are tagged.  The
classification rule is deterministic:

* ``[security]`` at the start of the cleaned line -> blocking ``security``;
* ``[data-loss]`` at the start -> blocking ``data-loss``;
* ``[minor]`` at the start -> non-blocking, kind ``None``;
* no tag -> fail safe: any security/data-loss keyword makes it blocking
  (``security`` wins if both appear), even when the sentence is negated.

Tags are only tags at the start of the item; a mid-line ``[minor]`` does not
downgrade anything.  The stored ``text`` has the leading tag removed.
"""

import unittest

from deepseek_mcp import quality, review_scope


FOUR_PHRASES = (
    ("Legacy migration cannot avoid data loss", "data-loss"),
    ("Legacy migration does not avoid data loss", "data-loss"),
    ("Old migration is not free of data loss", "data-loss"),
    ("Legacy endpoint cannot avoid exposing user credentials", "security"),
)


class ReviewScopeUnitTests(unittest.TestCase):
    def test_follow_up_headings(self):
        for line in (
            "# Follow-ups",
            "## Follow-up",
            "### Followups",
            "## Follow-ups (pre-existing, non-blocking)",
            "## FOLLOW-UPS (pre-existing)",
            "Follow-up",
            "Followups",
        ):
            self.assertTrue(review_scope.is_follow_up_heading(line), line)

    def test_non_follow_up_headings(self):
        for line in (
            "## Blocking defects",
            "## Non-blocking defects",
            "## Score",
            "",
            "- Follow-ups are listed below",
            "The follow-ups are listed below.",
        ):
            self.assertFalse(review_scope.is_follow_up_heading(line), line)

    def test_follow_up_items_parse_and_skip(self):
        text = "\n".join([
            "# Review",
            "## Follow-ups (pre-existing, non-blocking)",
            "- Legacy cache files are left behind",
            "- None",
            "* N/A",
            "+ nil",
            "1. (none)",
            "",
            "## Score",
            "Final quality score: 9/10",
        ])
        items = review_scope.follow_up_items(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "Legacy cache files are left behind")
        self.assertFalse(items[0]["blocking"])
        self.assertIsNone(items[0]["kind"])

    def test_at_most_max_items(self):
        bullets = "\n".join("- old issue %d" % i
                            for i in range(review_scope.MAX_ITEMS + 7))
        items = review_scope.follow_up_items(
            "## Follow-ups\n\n" + bullets + "\n")
        self.assertEqual(len(items), review_scope.MAX_ITEMS)

    def test_strip_follow_ups_keeps_other_text(self):
        text = ("# Review\n\n"
                "## Follow-ups (pre-existing, non-blocking)\n"
                "- old issue in untouched code\n"
                "- [security] old cookie flag\n\n"
                "## Score\n\nFinal quality score: 8.5/10\n")
        stripped = review_scope.strip_follow_ups(text)
        self.assertNotIn("old issue", stripped)
        self.assertNotIn("old cookie", stripped)
        self.assertNotIn("Follow-ups", stripped)
        self.assertIn("## Score", stripped)
        self.assertIn("Final quality score: 8.5/10", stripped)

    def test_strip_without_follow_up_is_unchanged(self):
        text = "## Blocking defects\n- build fails\n"
        self.assertEqual(review_scope.strip_follow_ups(text), text)

    def test_format_follow_up(self):
        self.assertEqual(
            review_scope.format_follow_up({"text": "old typo",
                                           "blocking": False, "kind": None}),
            "old typo")
        self.assertEqual(
            review_scope.format_follow_up({"text": "old cookie",
                                           "blocking": True,
                                           "kind": "security"}),
            "[security] old cookie")
        self.assertEqual(
            review_scope.format_follow_up({"text": "old prune",
                                           "blocking": True,
                                           "kind": "data-loss"}),
            "[data-loss] old prune")

    def test_prompt_block_contents(self):
        block = review_scope.prompt_block()
        lowered = block.lower()
        self.assertIn(review_scope.FOLLOW_UP_HEADING, block)
        self.assertIn("non-blocking", lowered)
        # Every follow-up line must carry exactly one of the three tags.
        for tag in ("[security]", "[data-loss]", "[minor]"):
            self.assertIn(tag, lowered)
        self.assertIn("do not repeat follow-ups under blocking", lowered)


class TagRuleUnitTests(unittest.TestCase):
    """The deterministic tag rule, independent of quality.parse_review."""

    def items(self, *phrases):
        bullets = "\n".join("- " + phrase for phrase in phrases)
        return review_scope.follow_up_items(
            review_scope.FOLLOW_UP_HEADING + "\n\n" + bullets + "\n")

    def test_untagged_flaw_phrases_block_with_expected_kinds(self):
        items = self.items(*[phrase for phrase, _ in FOUR_PHRASES])
        self.assertEqual(len(items), 4)
        self.assertEqual(
            [item["kind"] for item in items],
            [kind for _, kind in FOUR_PHRASES])
        self.assertTrue(all(item["blocking"] for item in items))
        for item, (phrase, _) in zip(items, FOUR_PHRASES):
            self.assertEqual(item["text"], phrase)

    def test_minor_tagged_flaw_phrases_do_not_block(self):
        tagged = ["[minor] " + phrase for phrase, _ in FOUR_PHRASES]
        items = self.items(*tagged)
        self.assertEqual(len(items), 4)
        for item, (phrase, _) in zip(items, FOUR_PHRASES):
            self.assertFalse(item["blocking"], item["text"])
            self.assertIsNone(item["kind"], item["text"])
            self.assertEqual(item["text"], phrase)

    def test_minor_old_typo_is_non_blocking(self):
        items = self.items(
            "[minor] Old typo in authentication documentation")
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["blocking"])
        self.assertIsNone(items[0]["kind"])
        self.assertEqual(items[0]["text"],
                         "Old typo in authentication documentation")

    def test_security_tag_blocks_and_is_stripped_from_text(self):
        items = self.items("[security] Legacy cookie lacks HttpOnly")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["blocking"])
        self.assertEqual(items[0]["kind"], "security")
        self.assertEqual(items[0]["text"], "Legacy cookie lacks HttpOnly")

    def test_data_loss_tag_blocks_and_is_stripped_from_text(self):
        items = self.items("[data-loss] Old prune job may delete user data")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["blocking"])
        self.assertEqual(items[0]["kind"], "data-loss")
        self.assertEqual(items[0]["text"],
                         "Old prune job may delete user data")

    def test_tag_spelling_is_case_insensitive_and_tolerant(self):
        items = self.items(
            "[SECURITY] old cookie flag",
            "[Data_Loss] old prune job",
            "[ data loss ] old purge job")
        self.assertEqual([item["kind"] for item in items],
                         ["security", "data-loss", "data-loss"])
        self.assertTrue(all(item["blocking"] for item in items))

    def test_untagged_benign_text_without_keywords_does_not_block(self):
        for phrase in (
            "Old README has a typo",
            "Legacy helper variable name is unclear",
            "Stale comment in untouched module",
        ):
            items = self.items(phrase)
            self.assertEqual(len(items), 1, phrase)
            self.assertFalse(items[0]["blocking"], phrase)
            self.assertIsNone(items[0]["kind"], phrase)

    def test_mid_line_tag_is_not_a_tag(self):
        # The trailing [minor] does not downgrade an untagged data-loss line.
        items = self.items(
            "Legacy migration cannot avoid data loss [minor]")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["blocking"])
        self.assertEqual(items[0]["kind"], "data-loss")
        self.assertIn("[minor]", items[0]["text"])

        # A tag mentioned in prose is likewise not a line-start tag.
        items = self.items(
            "Old importer still has SQL injection; see the [minor] tag note")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["blocking"])
        self.assertEqual(items[0]["kind"], "security")

    def test_minor_wins_over_security_and_data_loss_wording(self):
        items = self.items(
            "[minor] Old note about SQL injection and data loss "
            "in security documentation",
            "[minor] Legacy endpoint cannot avoid exposing user credentials",
        )
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertFalse(item["blocking"], item["text"])
            self.assertIsNone(item["kind"], item["text"])

    def test_minor_continuation_with_keyword_stays_non_blocking(self):
        # The explicit [minor] tag must survive an indented continuation that
        # mentions a security keyword; re-classifying the stripped body alone
        # would flip this to blocking.
        items = review_scope.follow_up_items(
            review_scope.FOLLOW_UP_HEADING + "\n\n"
            "- [minor] Old typo\n"
            "  in authentication documentation\n")
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["blocking"])
        self.assertIsNone(items[0]["kind"])
        self.assertIn("authentication", items[0]["text"])

    def test_bare_tag_without_wording_is_skipped(self):
        items = review_scope.follow_up_items(
            review_scope.FOLLOW_UP_HEADING + "\n\n"
            "- [minor]\n- [security]\n- [data-loss]\n- None\n")
        self.assertEqual(items, [])

    def test_format_follow_up_does_not_double_prefix(self):
        self.assertEqual(
            review_scope.format_follow_up({"text": "[security] old cookie",
                                           "blocking": True,
                                           "kind": "security"}),
            "[security] old cookie")

    def test_security_wins_when_both_keyword_families_appear(self):
        items = self.items(
            "Old job can corrupt archived rows and leaks credentials")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["blocking"])
        self.assertEqual(items[0]["kind"], "security")

    def test_untagged_explicit_tags_still_block(self):
        items = self.items(
            "Legacy [security] cookie lacks HttpOnly",
            "Legacy [data-loss] prune job drops rows")
        self.assertEqual([item["kind"] for item in items],
                         ["security", "data-loss"])
        self.assertTrue(all(item["blocking"] for item in items))


class ParseReviewIntegrationTests(unittest.TestCase):
    """The two-input path used by the connector: parse_review(report, report)."""

    FOLLOW_UP_HEADING = review_scope.FOLLOW_UP_HEADING

    def make_report(self, follow_ups, score="9.0"):
        lines = [
            "# Independent review",
            "",
            "## Score",
            "",
            "Final quality score: %s/10" % score,
            "",
            "## Blocking defects",
            "",
            "None",
            "",
            self.FOLLOW_UP_HEADING,
            "",
        ]
        lines.extend("- " + phrase for phrase in follow_ups)
        return "\n".join(lines) + "\n"

    def test_untagged_four_phrases_block_via_two_input_path(self):
        report = self.make_report([phrase for phrase, _ in FOUR_PHRASES])
        parsed = quality.parse_review(report, report)

        self.assertEqual(parsed["score"], 9.0)
        self.assertEqual(parsed["blocking_defects"], 4)
        self.assertEqual(len(parsed["follow_ups"]), 4)
        self.assertEqual(len(parsed["blocking_follow_ups"]), 4)
        self.assertEqual(parsed["non_blocking_follow_ups"], [])
        self.assertEqual(
            [item["kind"] for item in parsed["blocking_follow_ups"]],
            [kind for _, kind in FOUR_PHRASES])
        self.assertTrue(all(item["blocking"]
                            for item in parsed["blocking_follow_ups"]))
        self.assertEqual(
            [item["text"] for item in parsed["blocking_follow_ups"]],
            [phrase for phrase, _ in FOUR_PHRASES])

        # Blocking follow-ups are surfaced with the exact marker, never as a
        # plain non-blocking bullet.
        defect_texts = [str(item) for item in parsed["defects"]]
        fix_texts = [str(item) for item in parsed["fixes"]]
        self.assertTrue(any("[blocking: security]" in text
                            for text in defect_texts), defect_texts)
        self.assertTrue(any("[blocking: data-loss]" in text
                            for text in defect_texts), defect_texts)
        self.assertTrue(any("[blocking: security]" in text
                            for text in fix_texts), fix_texts)
        self.assertTrue(any("[blocking: data-loss]" in text
                            for text in fix_texts), fix_texts)
        for phrase, _ in FOUR_PHRASES:
            self.assertTrue(any(phrase in text for text in defect_texts),
                            phrase)
        self.assertEqual(
            quality.quality_verdict(parsed["score"],
                                    parsed["blocking_defects"]),
            "revise")

    def test_minor_tagged_four_phrases_are_accepted(self):
        report = self.make_report(
            ["[minor] " + phrase for phrase, _ in FOUR_PHRASES])
        parsed = quality.parse_review(report, report)

        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertEqual(len(parsed["follow_ups"]), 4)
        self.assertEqual(parsed["blocking_follow_ups"], [])
        self.assertEqual(len(parsed["non_blocking_follow_ups"]), 4)
        self.assertTrue(all(item["kind"] is None
                            for item in parsed["non_blocking_follow_ups"]))
        self.assertFalse(any("[blocking:" in str(item)
                             for item in parsed["defects"]))
        self.assertEqual(
            quality.quality_verdict(parsed["score"],
                                    parsed["blocking_defects"]),
            "accept")

    def test_minor_old_typo_is_accepted(self):
        report = self.make_report(
            ["[minor] Old typo in authentication documentation"])
        parsed = quality.parse_review(report, report)

        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertEqual(len(parsed["follow_ups"]), 1)
        self.assertEqual(parsed["blocking_follow_ups"], [])
        self.assertEqual(parsed["non_blocking_follow_ups"][0]["kind"], None)
        self.assertEqual(parsed["non_blocking_follow_ups"][0]["text"],
                         "Old typo in authentication documentation")
        self.assertEqual(
            quality.quality_verdict(parsed["score"],
                                    parsed["blocking_defects"]),
            "accept")

    def test_security_and_data_loss_tags_block_and_mark(self):
        report = self.make_report([
            "[security] Legacy cookie is not HttpOnly",
            "[data-loss] Old prune job drops archived rows",
        ])
        parsed = quality.parse_review(report, report)

        self.assertEqual(parsed["blocking_defects"], 2)
        self.assertEqual(len(parsed["blocking_follow_ups"]), 2)
        self.assertEqual(
            [item["kind"] for item in parsed["blocking_follow_ups"]],
            ["security", "data-loss"])
        self.assertEqual(
            [item["text"] for item in parsed["blocking_follow_ups"]],
            ["Legacy cookie is not HttpOnly",
             "Old prune job drops archived rows"])
        defect_texts = [str(item) for item in parsed["defects"]]
        self.assertTrue(any("[blocking: security]" in text
                            for text in defect_texts))
        self.assertTrue(any("[blocking: data-loss]" in text
                            for text in defect_texts))

    def test_old_format_review_unchanged(self):
        report = (
            "# Review\n\n"
            "Final quality score: 8.0/10\n\n"
            "## Blocking defects\n\n- Build fails on Python 3.9\n\n"
            "## Fix list\n\n- Fix the syntax error\n")
        parsed = quality.parse_review(report)

        self.assertEqual(parsed["score"], 8.0)
        self.assertEqual(parsed["blocking_defects"], 1)
        self.assertEqual(parsed["defects"], ["Build fails on Python 3.9"])
        self.assertEqual(parsed["fixes"], ["Fix the syntax error"])
        self.assertEqual(parsed.get("follow_ups", []), [])
        self.assertEqual(parsed.get("blocking_follow_ups", []), [])

    def test_follow_up_only_report_with_minor_item_has_no_score(self):
        report = (
            "# Review\n\n"
            + self.FOLLOW_UP_HEADING + "\n\n"
            "- [minor] Old lint warnings in untouched module\n")
        parsed = quality.parse_review(report, report)

        self.assertIsNone(parsed["score"])
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertEqual(len(parsed["follow_ups"]), 1)
        self.assertEqual(parsed["blocking_follow_ups"], [])
        self.assertEqual(parsed["non_blocking_follow_ups"][0]["kind"], None)

    def test_follow_up_only_report_with_blocking_tag_has_no_score(self):
        report = (
            "# Review\n\n"
            + self.FOLLOW_UP_HEADING + "\n\n"
            "- [security] Legacy cookie lacks HttpOnly\n")
        parsed = quality.parse_review(report, report)

        self.assertIsNone(parsed["score"])
        self.assertEqual(parsed["blocking_defects"], 1)
        self.assertEqual(len(parsed["blocking_follow_ups"]), 1)
        self.assertEqual(parsed["blocking_follow_ups"][0]["kind"], "security")
        self.assertTrue(any("[blocking: security]" in str(item)
                            for item in parsed["defects"]))
        self.assertEqual(
            quality.quality_verdict(parsed["score"],
                                    parsed["blocking_defects"]),
            "revise")

    def test_same_report_as_both_inputs_is_counted_once(self):
        report = self.make_report([
            "Legacy migration cannot avoid data loss",
            "[minor] Old typo in authentication documentation",
        ])
        parsed = quality.parse_review(report, report)

        self.assertEqual(len(parsed["follow_ups"]), 2)
        self.assertEqual(parsed["blocking_defects"], 1)
        self.assertEqual(len(parsed["blocking_follow_ups"]), 1)
        self.assertEqual(len(parsed["non_blocking_follow_ups"]), 1)
        self.assertEqual(parsed["blocking_follow_ups"][0]["kind"],
                         "data-loss")
        self.assertEqual(
            len([item for item in parsed["defects"]
                 if "Legacy migration cannot avoid data loss" in str(item)]),
            1)

    def test_numeric_blocking_phrase_inside_follow_up_is_ignored(self):
        parsed = quality.parse_review(self.make_report(
            ["[minor] Old notes mention 2 blocking defects in legacy code"]))
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertEqual(parsed["defects"], [])

    def test_review_task_has_scope_rule_and_tags(self):
        task = quality.build_review_task("Build the widget.", ["widget.py"])
        lowered = task.lower()
        self.assertIn(review_scope.FOLLOW_UP_HEADING, task)
        for tag in ("[security]", "[data-loss]", "[minor]"):
            self.assertIn(tag, lowered)
        self.assertIn("non-blocking", lowered)


if __name__ == "__main__":
    unittest.main()
