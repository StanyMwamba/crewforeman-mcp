"""Role/vendor routing tests for the 3.15.0 MiMo slice (W6).

Pure standard-library unit tests: vendor aliases, model_policy resolution
for the builtin ``mimo`` provider, backup derivation for MiMo targets,
crosscheck crew vendor selection and reviewer cross-vendor eligibility.
No network, no paid calls: preflight paths use stub functions only.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from deepseek_mcp import builder_backup, reviewer_backup
from deepseek_mcp.config import Config
from deepseek_mcp.crosscheck import (
    CrosscheckConfigError,
    CrosscheckCrewVendorConflict,
    select_crew_target,
)
from deepseek_mcp.model_policy import (
    DEFAULT_MIMO_MODEL,
    DEFAULT_MIMO_PROBE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    MIMO_API_KEY_ENV,
    canonical_vendor as policy_canonical_vendor,
    resolve_target,
)
from deepseek_mcp.reviewer_vendor import (
    ReviewerVendorConflict,
    assert_reviewer_eligible,
    canonical_vendor,
    select_candidate,
)

MIMO_VENDOR = "Xiaomi (MiMo)"


class CanonicalVendorTests(unittest.TestCase):
    def test_mimo_aliases_are_case_insensitive_substrings(self):
        cases = {
            "xiaomi/mimo-v2.6-pro": MIMO_VENDOR,
            "mimo-v2.6-pro": MIMO_VENDOR,
            "Xiaomi/MiMo-v2.6-Flash": MIMO_VENDOR,
            "mimo": MIMO_VENDOR,
            "MIMO": MIMO_VENDOR,
            "xiaomi": MIMO_VENDOR,
            "XIAOMI": MIMO_VENDOR,
            "mimo-v2": MIMO_VENDOR,
            "mimo-v2.6": MIMO_VENDOR,
            "mimo-v2.6-flash": MIMO_VENDOR,
        }
        for value, expected in cases.items():
            self.assertEqual(canonical_vendor(value), expected, value)

    def test_existing_aliases_are_unchanged(self):
        cases = {
            "deepseek-flash": "DeepSeek",
            "moonshot/kimi-k3": "Kimi (Moonshot)",
            "claude-opus-4-1": "Anthropic",
            "gpt-5": "OpenAI",
            "gemini-3": "Google",
            "ac" "me-reviewer": None,
            "": None,
        }
        for value, expected in cases.items():
            self.assertEqual(canonical_vendor(value), expected, value)

    def test_model_policy_canonical_vendor_matches(self):
        self.assertEqual(
            policy_canonical_vendor("xiaomi/mimo-v2.6-pro"), MIMO_VENDOR)
        self.assertEqual(policy_canonical_vendor("mimo"), MIMO_VENDOR)

    def test_provider_map_and_registry(self):
        from deepseek_mcp import reviewer_vendor
        self.assertIn(MIMO_VENDOR, reviewer_vendor.VENDORS)
        self.assertEqual(reviewer_vendor._PROVIDERS[MIMO_VENDOR], "mimo")


class ResolveTargetTests(unittest.TestCase):
    def test_provider_mimo_bare_model_without_providers_entry(self):
        cfg = Config({})
        self.assertEqual(cfg.get("providers"), [])
        target = resolve_target(cfg, "mimo", "mimo-v2.6-pro")
        self.assertEqual(target.provider, "mimo")
        self.assertEqual(target.model, "mimo-v2.6-pro")
        self.assertEqual(target.vendor, MIMO_VENDOR)
        self.assertEqual(target.api_key_env, MIMO_API_KEY_ENV)

    def test_provider_mimo_prefixed_model(self):
        target = resolve_target(Config({}), "mimo", "xiaomi/mimo-v2.6-pro")
        self.assertEqual(target.provider, "mimo")
        self.assertEqual(target.model, "xiaomi/mimo-v2.6-pro")
        self.assertEqual(target.vendor, MIMO_VENDOR)

    def test_builder_config_mapping_resolves_cleanly(self):
        cfg = Config({
            "builder_provider": "mimo",
            "builder_model": "xiaomi/mimo-v2.6-pro",
        })
        target = resolve_target(cfg)
        self.assertEqual(target.provider, "mimo")
        self.assertEqual(target.model, "xiaomi/mimo-v2.6-pro")
        self.assertEqual(target.vendor, MIMO_VENDOR)
        self.assertEqual(target.source, "config")

    def test_vendor_label_on_model_target(self):
        target = resolve_target(Config({}), "mimo", "xiaomi/mimo-v2.6-pro")
        self.assertEqual(target.as_dict()["vendor"], MIMO_VENDOR)
        self.assertEqual(target.vendor, MIMO_VENDOR)

    def test_alias_provider_normalises_to_mimo(self):
        target = resolve_target(Config({}), "xiaomi", "mimo-v2.6-pro")
        self.assertEqual(target.provider, "mimo")
        self.assertEqual(target.vendor, MIMO_VENDOR)

    def test_model_only_form_infers_mimo_provider(self):
        target = resolve_target(Config({}), model="xiaomi/mimo-v2.6-pro")
        self.assertEqual(target.provider, "mimo")
        self.assertEqual(target.vendor, MIMO_VENDOR)

    def test_probe_model_resolves_to_mimo(self):
        target = resolve_target(
            Config({}), "mimo", "xiaomi/mimo-v2.6-flash")
        self.assertEqual(target.vendor, MIMO_VENDOR)
        self.assertEqual(target.model, "xiaomi/mimo-v2.6-flash")

    def test_other_vendors_keep_existing_resolution(self):
        target = resolve_target(Config({}))
        self.assertEqual(target.provider, DEFAULT_PROVIDER)
        self.assertEqual(target.model, DEFAULT_MODEL)
        self.assertEqual(target.vendor, "DeepSeek")


def _backup_cfg(**overrides):
    values = {
        "backup_builder_model": "",
        "backup_builder_provider": "",
        "backup_builder_max_cost_usd": 1.0,
        "backup_builder_min_attempts": 2,
        "dsh_bin": "/opt/dsh",
        "dsh_model": "deepseek-flash",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_exe(directory, name):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o700)
    return path


class BackupDerivationTests(unittest.TestCase):
    def test_builder_backup_mimo_explicit_provider(self):
        target = builder_backup.resolve_backup_target(_backup_cfg(
            backup_builder_model="xiaomi/mimo-v2.6-pro",
            backup_builder_provider="mimo"))
        self.assertEqual(target["provider"], "mimo")
        self.assertEqual(target["model"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual(target["vendor"], MIMO_VENDOR)

    def test_builder_backup_mimo_model_only(self):
        for model in ("mimo-v2.6-pro", "xiaomi/mimo-v2.6-pro"):
            with self.subTest(model=model):
                target = builder_backup.resolve_backup_target(
                    _backup_cfg(backup_builder_model=model))
                self.assertEqual(target["model"], model)
                self.assertEqual(target["vendor"], MIMO_VENDOR)

    def test_builder_backup_other_vendors_keep_derivation(self):
        target = builder_backup.resolve_backup_target(_backup_cfg(
            backup_builder_model="claude-sonnet-4"))
        self.assertEqual(target["provider"], "anthropic")
        self.assertEqual(target["vendor"], "anthropic")
        target = builder_backup.resolve_backup_target(_backup_cfg(
            backup_builder_model="m1"))
        self.assertEqual(target["provider"], "unknown")
        self.assertEqual(target["vendor"], "unknown")

    def test_builder_backup_probe_model_uses_mimo_probe_default(self):
        settings = {"provider": "mimo", "model": "xiaomi/mimo-v2.6-pro"}
        self.assertEqual(
            builder_backup.probe_model(settings, None),
            DEFAULT_MIMO_PROBE_MODEL)
        other = {"provider": "anthropic", "model": "claude-sonnet-4"}
        self.assertEqual(
            builder_backup.probe_model(other, None), "claude-sonnet-4")

    def test_builder_backup_probe_model_reads_config_override(self):
        cfg = SimpleNamespace(mimo_probe_model="custom/probe-model")
        self.assertEqual(
            builder_backup.probe_model({"model": "mimo-v2.6-pro"}, cfg),
            "custom/probe-model")

    def test_reviewer_backup_mimo_candidate_is_cross_vendor(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _write_exe(tmp, "mimo-wrapper")
            cfg = {
                "backup_dsh_bin": exe,
                "backup_reviewer_vendor": MIMO_VENDOR,
                "backup_reviewer_model": "xiaomi/mimo-v2.6-pro",
                "dsh_model": "deepseek-flash",
            }
            candidate, error = reviewer_backup.backup_eligibility(
                cfg, ["DeepSeek"])
            self.assertIsNone(error)
            self.assertEqual(candidate["vendor"], MIMO_VENDOR)
            self.assertEqual(
                reviewer_backup.eligible_backup(cfg, ["DeepSeek"])["model"],
                "xiaomi/mimo-v2.6-pro")

    def test_reviewer_backup_model_string_only_identifies_mimo(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _write_exe(tmp, "mimo-wrapper")
            cfg = {
                "backup_dsh_bin": exe,
                "backup_reviewer_vendor": "",
                "backup_reviewer_model": "mimo-v2.6-pro",
                "dsh_model": "deepseek-flash",
            }
            candidate, error = reviewer_backup.backup_eligibility(
                cfg, ["DeepSeek"])
            self.assertIsNone(error)
            self.assertEqual(candidate["vendor"], MIMO_VENDOR)

    def test_reviewer_backup_same_vendor_keeps_consent_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _write_exe(tmp, "mimo-wrapper")
            cfg = {
                "backup_dsh_bin": exe,
                "backup_reviewer_vendor": MIMO_VENDOR,
                "backup_reviewer_model": "xiaomi/mimo-v2.6-pro",
                "dsh_model": "deepseek-flash",
            }
            candidate, error = reviewer_backup.backup_eligibility(
                cfg, [MIMO_VENDOR])
            self.assertEqual(error, "reviewer_vendor_same_as_builder")
            candidate, error = reviewer_backup.backup_eligibility(
                cfg, [MIMO_VENDOR], chosen_by_user=True)
            self.assertIsNone(error)

    def test_reviewer_backup_probe_model(self):
        self.assertEqual(
            reviewer_backup.probe_model({"model": "xiaomi/mimo-v2.6-pro"}),
            DEFAULT_MIMO_PROBE_MODEL)
        self.assertEqual(
            reviewer_backup.probe_model({"model": "claude-sonnet-4"}),
            "claude-sonnet-4")


class CrewTargetTests(unittest.TestCase):
    def test_result_contract_keys(self):
        out = select_crew_target(Config({}), {}, {"vendor": "DeepSeek"})
        self.assertEqual(
            set(out.keys()),
            {"provider", "model", "vendor", "builder_vendor",
             "different_vendor", "source"})

    def test_auto_deepseek_builder_picks_mimo_crew(self):
        out = select_crew_target(Config({}), {}, {"vendor": "DeepSeek"})
        self.assertEqual(out["provider"], "mimo")
        self.assertEqual(out["model"], DEFAULT_MIMO_MODEL)
        self.assertEqual(out["vendor"], MIMO_VENDOR)
        self.assertEqual(out["builder_vendor"], "DeepSeek")
        self.assertTrue(out["different_vendor"])
        self.assertEqual(out["source"], "auto")

    def test_auto_mimo_builder_picks_deepseek_crew(self):
        out = select_crew_target(Config({}), {}, {"vendor": MIMO_VENDOR})
        self.assertEqual(out["provider"], DEFAULT_PROVIDER)
        self.assertEqual(out["model"], DEFAULT_MODEL)
        self.assertEqual(out["vendor"], "DeepSeek")
        self.assertEqual(out["builder_vendor"], MIMO_VENDOR)
        self.assertTrue(out["different_vendor"])
        self.assertEqual(out["source"], "auto")

    def test_auto_reads_builder_vendor_from_provider_model(self):
        out = select_crew_target(
            Config({}), {},
            {"provider": "deepseek-official", "model": "deepseek-flash"})
        self.assertEqual(out["vendor"], MIMO_VENDOR)
        self.assertEqual(out["builder_vendor"], "DeepSeek")
        self.assertEqual(out["source"], "auto")
        out = select_crew_target(
            Config({}), {},
            {"provider": "mimo", "model": "xiaomi/mimo-v2.6-pro"})
        self.assertEqual(out["vendor"], "DeepSeek")
        self.assertEqual(out["builder_vendor"], MIMO_VENDOR)

    def test_auto_mimo_default_model_config_is_honoured(self):
        cfg = Config({"mimo_default_model": "xiaomi/mimo-v2.6-flash"})
        out = select_crew_target(cfg, {}, {"vendor": "DeepSeek"})
        self.assertEqual(out["model"], "xiaomi/mimo-v2.6-flash")
        self.assertEqual(out["vendor"], MIMO_VENDOR)

    def test_explicit_different_vendor_is_honored(self):
        args = {
            "crosscheck_crew_provider": "mimo",
            "crosscheck_crew_model": "xiaomi/mimo-v2.6-pro",
        }
        out = select_crew_target(Config({}), args, {"vendor": "DeepSeek"})
        self.assertEqual(out["source"], "explicit")
        self.assertEqual(out["provider"], "mimo")
        self.assertEqual(out["model"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual(out["vendor"], MIMO_VENDOR)
        self.assertEqual(out["builder_vendor"], "DeepSeek")
        self.assertTrue(out["different_vendor"])

    def test_explicit_same_vendor_raises_conflict(self):
        args = {
            "crosscheck_crew_provider": "deepseek-official",
            "crosscheck_crew_model": "deepseek-flash",
        }
        with self.assertRaises(CrosscheckCrewVendorConflict) as raised:
            select_crew_target(Config({}), args, {"vendor": "DeepSeek"})
        self.assertEqual(raised.exception.reason,
                         "crosscheck_crew_same_vendor")
        self.assertIsInstance(raised.exception, ValueError)
        mimo_args = {
            "crosscheck_crew_provider": "mimo",
            "crosscheck_crew_model": "xiaomi/mimo-v2.6-pro",
        }
        with self.assertRaises(CrosscheckCrewVendorConflict):
            select_crew_target(Config({}), mimo_args, {"vendor": MIMO_VENDOR})
        with self.assertRaises(CrosscheckCrewVendorConflict):
            select_crew_target(
                Config({}), mimo_args, {"model": "mimo-v2.6-pro"})

    def test_explicit_config_keys_win_without_args(self):
        cfg = Config({
            "crosscheck_crew_provider": "mimo",
            "crosscheck_crew_model": "xiaomi/mimo-v2.6-pro",
        })
        out = select_crew_target(cfg, {}, {"vendor": "DeepSeek"})
        self.assertEqual(out["source"], "explicit")
        self.assertEqual(out["provider"], "mimo")
        self.assertEqual(out["vendor"], MIMO_VENDOR)

    def test_explicit_args_override_config_keys(self):
        cfg = Config({
            "crosscheck_crew_provider": "deepseek-official",
            "crosscheck_crew_model": "deepseek-flash",
        })
        args = {
            "crosscheck_crew_provider": "mimo",
            "crosscheck_crew_model": "xiaomi/mimo-v2.6-pro",
        }
        out = select_crew_target(cfg, args, {"vendor": "DeepSeek"})
        self.assertEqual(out["source"], "explicit")
        self.assertEqual(out["vendor"], MIMO_VENDOR)

    def test_explicit_invalid_pair_is_rejected(self):
        args = {
            "crosscheck_crew_provider": "bad provider",
            "crosscheck_crew_model": "m1",
        }
        with self.assertRaises(CrosscheckConfigError):
            select_crew_target(Config({}), args, {"vendor": "DeepSeek"})
        with self.assertRaises(CrosscheckConfigError):
            select_crew_target(
                Config({}), {"crosscheck_crew_model": "x;rm"},
                {"vendor": "DeepSeek"})

    def test_explicit_model_only_fills_provider(self):
        out = select_crew_target(
            Config({}), {"crosscheck_crew_model": "xiaomi/mimo-v2.6-pro"},
            {"vendor": "DeepSeek"})
        self.assertEqual(out["provider"], "mimo")
        self.assertEqual(out["vendor"], MIMO_VENDOR)

    def test_auto_for_unknown_builder_is_different_vendor(self):
        out = select_crew_target(Config({}), {}, None)
        self.assertEqual(out["source"], "auto")
        self.assertTrue(out["different_vendor"])
        self.assertEqual(out["builder_vendor"], "unknown")


class ReviewerEligibilityTests(unittest.TestCase):
    def test_mimo_candidate_is_cross_vendor_vs_deepseek(self):
        candidates = (
            {"vendor": MIMO_VENDOR, "model": "xiaomi/mimo-v2.6-pro"},
            {"model": "xiaomi/mimo-v2.6-pro"},
            {"provider": "mimo", "model": "mimo-v2.6-pro"},
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertTrue(
                    assert_reviewer_eligible(candidate, ["DeepSeek"]))

    def test_mimo_candidate_is_cross_vendor_vs_other_builders(self):
        self.assertTrue(assert_reviewer_eligible(
            {"model": "mimo-v2.6-pro"}, ["Google"]))
        self.assertTrue(assert_reviewer_eligible(
            {"model": "mimo-v2.6-pro"}, ["Kimi (Moonshot)"]))

    def test_same_vendor_xiaomi_keeps_consent_policy(self):
        candidate = {"vendor": MIMO_VENDOR, "model": "xiaomi/mimo-v2.6-pro"}
        with self.assertRaises(ReviewerVendorConflict) as raised:
            assert_reviewer_eligible(candidate, [MIMO_VENDOR])
        self.assertEqual(raised.exception.reason,
                         "reviewer_vendor_same_as_builder")
        self.assertTrue(assert_reviewer_eligible(
            candidate, [MIMO_VENDOR], chosen_by_user=True, same_vendor=True))

    def test_select_candidate_preflights_mimo_as_cross_vendor(self):
        calls = []

        def preflight(candidate):
            calls.append(candidate.get("model"))
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(
            [{"name": "mimo-reviewer", "vendor": MIMO_VENDOR,
              "model": "xiaomi/mimo-v2.6-pro"}],
            ["DeepSeek"], preflight)
        self.assertEqual(result["status"], "selected")
        self.assertFalse(result["same_vendor"])
        self.assertEqual(result["candidate"]["vendor"], MIMO_VENDOR)
        self.assertEqual(calls, ["xiaomi/mimo-v2.6-pro"])

    def test_select_candidate_same_vendor_pauses_for_consent(self):
        def preflight(candidate):
            return {"status": "ok", "answer": "READY"}

        result = select_candidate(
            [{"name": "mimo-reviewer", "vendor": MIMO_VENDOR,
              "model": "xiaomi/mimo-v2.6-pro"}],
            [MIMO_VENDOR], preflight)
        self.assertEqual(result["status"], "paused")
        self.assertIsNotNone(result["attention"])


if __name__ == "__main__":
    unittest.main()
