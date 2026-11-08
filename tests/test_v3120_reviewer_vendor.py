"""Focused tests for the 3.12.0 reviewer-vendor slice (R5, DESIGN §8)."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from deepseek_mcp import events, reviewer_backup, reviewer_vendor
from deepseek_mcp.reviewer_vendor import (
    DECLINE_OPTION,
    QUESTION,
    ReviewerVendorConflict,
    assert_reviewer_eligible,
    build_attention,
    builder_vendor_set,
    canonical_vendor,
    legacy_candidates,
    normalize_candidate,
    resolve_choice,
    review_header,
    select_candidate,
)


def _exe(directory, name):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o700)
    return path


class VendorMapTests(unittest.TestCase):
    def test_aliases_are_case_insensitive_and_unknown_is_none(self):
        cases = {
            "anthropic": "Anthropic",
            "Claude Sonnet": "Anthropic",
            "opus-4": "Anthropic",
            "MOONSHOT": "Kimi (Moonshot)",
            "kimi-k3": "Kimi (Moonshot)",
            "OpenAI": "OpenAI",
            "gpt-5": "OpenAI",
            "google": "Google",
            "Gemini-3": "Google",
            "deepseek-flash": "DeepSeek",
            "ac" "me-reviewer": None,
            "": None,
            None: None,
        }
        for value, expected in cases.items():
            self.assertEqual(canonical_vendor(value), expected, value)


class BuilderSetTests(unittest.TestCase):
    def test_includes_slices_candidate_losers_and_backup(self):
        cfg = SimpleNamespace(
            builder_model="deepseek-flash",
            builder_provider="deepseek-official",
            dsh_model="deepseek-flash",
            dsh_provider="deepseek-official",
            slice_models={"api": "anthropic/claude-opus-4-1"},
            model_candidates={
                "api": ["moonshot/kimi-k3", "openai/gpt-5"],
            },
            backup_builder_model="google/gemini-3",
            builder_model_max_slices=8,
        )
        vendors, unknown = builder_vendor_set(cfg)
        self.assertFalse(unknown)
        self.assertEqual(
            vendors,
            ["Anthropic", "DeepSeek", "Google", "Kimi (Moonshot)", "OpenAI"],
        )

    def test_unknown_builder_fails_closed(self):
        cfg = SimpleNamespace(
            builder_model="mystery-1", builder_provider="", dsh_model="",
            dsh_provider="", slice_models={}, model_candidates={},
            backup_builder_model="",
        )
        vendors, unknown = builder_vendor_set(cfg)
        self.assertTrue(unknown)
        self.assertEqual(vendors, [])


class EligibilityTests(unittest.TestCase):
    def test_cross_vendor_passes(self):
        candidate = {"vendor": "Anthropic", "model": "claude-opus-4-1"}
        self.assertTrue(assert_reviewer_eligible(candidate, ["DeepSeek"]))

    def test_same_vendor_requires_consent(self):
        candidate = {"vendor": "Anthropic", "model": "claude-opus-4-1"}
        with self.assertRaises(ReviewerVendorConflict):
            assert_reviewer_eligible(candidate, ["Anthropic"])
        with self.assertRaises(ReviewerVendorConflict):
            assert_reviewer_eligible(
                candidate, ["Anthropic"], chosen_by_user=True,
                same_vendor=False)
        self.assertTrue(assert_reviewer_eligible(
            candidate, ["Anthropic"], chosen_by_user=True, same_vendor=True))

    def test_unknown_vendor_raises(self):
        with self.assertRaises(ReviewerVendorConflict):
            assert_reviewer_eligible({"vendor": "ac" "me"}, ["DeepSeek"])

    def test_headers_are_exact(self):
        candidate = {"vendor": "anthropic", "model": "claude-opus-4-1"}
        self.assertEqual(
            review_header(candidate), "Reviewed by: Anthropic/claude-opus-4-1")
        self.assertEqual(
            review_header(candidate, same_vendor_choice=True),
            "Reviewed by: Anthropic/claude-opus-4-1 \u2014 "
            "same-vendor review (chosen by user)",
        )


class NormalizeTests(unittest.TestCase):
    def test_explicit_vendor_wins_and_paths_expand(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = _exe(tmp, "my-reviewer")
            patch = os.path.join(tmp, "reviewer.yml")
            with open(patch, "w", encoding="utf-8") as handle:
                handle.write("patch: true\n")
            candidate = normalize_candidate({
                "name": "opus",
                "vendor": "OpenAI",
                "provider": "anthropic",
                "model": "claude-opus-4-1",
                "dsh_bin": binary,
                "patch": patch,
            }, None)
            self.assertEqual(candidate["vendor"], "OpenAI")
            self.assertTrue(candidate["legacy_wrapper"])
            self.assertTrue(candidate["enabled"])
            self.assertIsNone(candidate["error"])
            self.assertEqual(candidate["dsh_bin"], binary)
            expanded = normalize_candidate(
                {"vendor": "DeepSeek", "dsh_bin": "~/reviewer"}, None)
            self.assertTrue(os.path.isabs(expanded["dsh_bin"]))
            self.assertFalse(expanded["dsh_bin"].startswith("~"))

    def test_bad_bin_and_patch_become_errors_not_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing-reviewer")
            candidate = normalize_candidate(
                {"vendor": "DeepSeek", "dsh_bin": missing}, None)
            self.assertEqual(candidate["error"], "dsh_bin_missing")
            unreadable = os.path.join(tmp, "no-such-patch.yml")
            candidate = normalize_candidate(
                {"vendor": "DeepSeek", "patch": unreadable}, None)
            self.assertEqual(candidate["error"], "patch_unreadable")
            self.assertEqual(
                set(candidate),
                {"name", "vendor", "provider", "model", "dsh_bin", "patch",
                 "api_key_env", "enabled", "legacy_wrapper", "error"},
            )


class LegacyTests(unittest.TestCase):
    def test_legacy_wrapper_inference_is_unchanged(self):
        self.assertEqual(
            reviewer_backup._infer_vendor("/tmp/reviewer-claude"), "Anthropic")
        self.assertEqual(
            reviewer_backup._infer_vendor("/tmp/kimi-reviewer"),
            "Kimi (Moonshot)")

    def test_legacy_candidates_and_ignored_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _exe(tmp, "claude-reviewer")
            backup = _exe(tmp, "kimi-reviewer")
            cfg = SimpleNamespace(
                reviewers=[], dsh_bin=main, backup_dsh_bin=backup,
                reviewer_vendor="", reviewer_model="",
                backup_reviewer_vendor="", backup_reviewer_model="",
            )
            candidates = legacy_candidates(cfg)
            self.assertFalse(candidates.legacy_ignored)
            self.assertEqual([c["name"] for c in candidates],
                             ["main", "backup"])
            self.assertEqual(candidates[0]["vendor"], "Anthropic")
            self.assertEqual(candidates[1]["vendor"], "Kimi (Moonshot)")

            cfg.reviewers = [{
                "name": "only", "vendor": "OpenAI", "model": "gpt-5",
            }]
            candidates = legacy_candidates(cfg)
            self.assertTrue(candidates.legacy_ignored)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["vendor"], "OpenAI")

    def test_legacy_backup_requires_consent_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = _exe(tmp, "claude-reviewer")
            cfg = SimpleNamespace(
                backup_dsh_bin=backup, backup_reviewer_vendor="",
                backup_reviewer_model="claude-opus-4-1",
                dsh_model="deepseek-flash",
            )
            paid = []

            def legacy_post_failure_branch():
                candidate = reviewer_backup.eligible_backup(
                    cfg, ["Anthropic"])
                if candidate is not None:
                    paid.append(candidate)
                return candidate

            self.assertIsNone(legacy_post_failure_branch())
            self.assertEqual(paid, [])
            consent = reviewer_backup.eligible_backup(
                cfg, ["Anthropic"], chosen_by_user=True)
            self.assertIsNotNone(consent)
            self.assertEqual(consent["vendor"], "Anthropic")
            # Cross-vendor backups keep working without the consent flag.
            self.assertIsNotNone(
                reviewer_backup.eligible_backup(cfg, ["DeepSeek"]))


class AttentionTests(unittest.TestCase):
    def _payload(self, policy="ask"):
        candidates = [
            {"name": "opus", "vendor": "Anthropic",
             "model": "claude-opus-4-1",
             "state": "same_vendor_preflight_ok"},
            {"name": "kimi", "vendor": "Kimi (Moonshot)",
             "model": "kimi-k3.1-pro", "state": "preflight_failed",
             "class": "auth"},
        ]
        return build_attention(candidates, ["Anthropic"], policy=policy)

    def test_one_validated_payload_and_labels(self):
        payload = self._payload()
        self.assertEqual(payload["question"], QUESTION)
        self.assertEqual(payload["options"], [
            "Anthropic/claude-opus-4-1 (same vendor)",
            DECLINE_OPTION,
        ])
        self.assertTrue(2 <= len(payload["options"]) <= 8)
        self.assertIsNotNone(events.build_reviewer_attention(payload))
        self.assertEqual(payload["evidence"]["kind"], "reviewer_vendor")
        self.assertEqual(payload["evidence"]["builders"], ["Anthropic"])
        states = [c["state"] for c in payload["evidence"]["candidates"]]
        self.assertIn("same_vendor_preflight_ok", states)
        self.assertIn("preflight_failed", states)

    def test_resolve_by_option_name_and_label(self):
        payload = self._payload()
        status, candidate = resolve_choice(
            payload, option=payload["options"][0])
        self.assertEqual(status, "same_vendor")
        self.assertEqual(candidate["model"], "claude-opus-4-1")
        self.assertEqual(resolve_choice(payload, answer="opus")[0],
                         "same_vendor")
        self.assertEqual(
            resolve_choice(
                payload, answer="Anthropic/claude-opus-4-1")[0],
            "same_vendor")
        self.assertEqual(
            resolve_choice(payload, answer="Kimi (Moonshot)/kimi-k3.1-pro"),
            ("invalid", None))

    def test_decline_and_invalid(self):
        payload = self._payload()
        self.assertEqual(
            resolve_choice(payload, option=DECLINE_OPTION),
            ("decline", None))
        self.assertEqual(
            resolve_choice(payload, answer="Do not review; return the build"),
            ("decline", None))
        self.assertEqual(
            resolve_choice(payload, option="something else"),
            ("invalid", None))

    def test_policy_never_removes_same_vendor_options(self):
        payload = self._payload(policy="never")
        self.assertNotIn("Anthropic/claude-opus-4-1 (same vendor)",
                         payload["options"])
        self.assertIn(DECLINE_OPTION, payload["options"])
        self.assertEqual(len(payload["options"]), 2)


class PreflightTests(unittest.TestCase):
    def _candidate(self, name, vendor, model):
        return {"name": name, "vendor": vendor, "model": model,
                "provider": "", "dsh_bin": "", "patch": "",
                "api_key_env": "", "enabled": True,
                "legacy_wrapper": False, "error": None}

    def test_order_and_bounded_retry(self):
        calls = []
        candidates = [
            self._candidate("anth", "Anthropic", "claude-opus-4-1"),
            self._candidate("openai", "OpenAI", "gpt-5"),
        ]

        def preflight(candidate):
            calls.append(candidate["name"])
            if candidate["name"] == "anth":
                return {"status": "error", "error": "HTTP 503"}
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(candidates, ["DeepSeek"], preflight)
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["candidate"]["name"], "openai")
        self.assertFalse(result["same_vendor"])
        self.assertEqual(calls, ["anth", "anth", "openai"])
        self.assertEqual(result["candidates"][0]["attempts"], 2)

    def test_auth_moves_on_immediately(self):
        calls = []
        candidates = [
            self._candidate("anth", "Anthropic", "claude-opus-4-1"),
            self._candidate("openai", "OpenAI", "gpt-5"),
        ]

        def preflight(candidate):
            calls.append(candidate["name"])
            if candidate["name"] == "anth":
                return {"status": "error", "error": "401 unauthorized"}
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(candidates, ["DeepSeek"], preflight)
        self.assertEqual(result["status"], "selected")
        self.assertEqual(calls, ["anth", "openai"])

    def test_no_cross_vendor_pauses_on_same_vendor(self):
        candidates = [
            self._candidate("anth", "Anthropic", "claude-opus-4-1"),
            self._candidate("deep", "DeepSeek", "deepseek-r1"),
        ]

        def preflight(candidate):
            if candidate["name"] == "anth":
                return {"status": "error", "error": "401 unauthorized"}
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(candidates, ["DeepSeek"], preflight)
        self.assertEqual(result["status"], "paused")
        self.assertIsNone(result["candidate"])
        self.assertEqual(
            result["attention"]["options"][0],
            "DeepSeek/deepseek-r1 (same vendor)")
        self.assertEqual(
            result["candidates"][1]["state"], "same_vendor_preflight_ok")

    def test_policy_never_offers_no_same_vendor_option(self):
        candidates = [self._candidate("deep", "DeepSeek", "deepseek-r1")]

        def preflight(_candidate):
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(
            candidates, ["DeepSeek"], preflight, policy="never")
        self.assertEqual(result["status"], "paused")
        self.assertFalse(any("(same vendor)" in option
                             for option in result["attention"]["options"]))

    def test_over_cap_entries_are_recorded(self):
        candidates = [
            self._candidate("c%d" % index, "Anthropic", "m%d" % index)
            for index in range(10)
        ]
        calls = []

        def preflight(candidate):
            calls.append(candidate["name"])
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(
            candidates, ["DeepSeek"], preflight, max_preflights=2)
        self.assertEqual(result["status"], "selected")
        self.assertEqual(calls, ["c0"])
        over_cap = [entry for entry in result["candidates"]
                    if entry.get("state") == "over_cap"]
        self.assertEqual(len(over_cap), 8)
        errors = reviewer_vendor.reviewer_config_errors(result["candidates"])
        self.assertEqual(len(errors), 8)
        self.assertTrue(all(item["error"] == "reviewer_cap_exceeded"
                            for item in errors))


class EventsLimitTests(unittest.TestCase):
    def test_reviewer_validator_keeps_existing_limits(self):
        good = {"question": "Q?", "options": ["A", "B"]}
        self.assertIsNotNone(events.build_reviewer_attention(good))
        self.assertIsNone(events.build_reviewer_attention(
            {"question": "Q?", "options": ["A"]}))
        self.assertIsNone(events.build_reviewer_attention(
            {"question": "Q?", "options": ["A", "x" * 201]}))
        self.assertIsNone(events.build_reviewer_attention(
            {"question": "x" * 501, "options": ["A", "B"]}))
        fenced = "```attention\n%s\n```" % json.dumps(good)
        self.assertEqual(events.validate_attention(fenced)["question"], "Q?")


if __name__ == "__main__":
    unittest.main()
