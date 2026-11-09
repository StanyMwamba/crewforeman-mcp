"""Focused tests for the 3.12.0 model policy (S1: DESIGN 3, 4, 5.1, 5.2)."""

import os
import stat
import tempfile
import unittest
from unittest import mock

from deepseek_mcp.config import Config, DEFAULTS, SERVER_VERSION, _coerce
from deepseek_mcp.model_overlay import create_overlay, overlay_yaml
from deepseek_mcp.model_policy import (
    BuilderModelError,
    build_model_plan_section,
    competition_result,
    is_default_target,
    resolve_spec,
    resolve_target,
    validate_model_audit,
    validate_model_candidates,
    validate_slice_models,
)


def _providers():
    return [
        {"name": "anthropic", "displayName": "Anthropic",
         "base_url": "https://api.anthropic.com", "api": "anthropic-messages",
         "api_key_env": "ANTHROPIC_API_KEY",
         "models": ["claude-sonnet-5-5", "claude-opus-4-1"]},
        {"name": "ac" "me", "base_url": "https://api.example.test",
         "api_key_env": "ACME_API_KEY", "models": ["ac" "me-large"]},
    ]


def _cfg(**extra):
    values = {"providers": _providers()}
    values.update(extra)
    return Config(values)


class ResolutionTests(unittest.TestCase):
    def test_precedence_tool_config_dsh_default(self):
        cfg = _cfg(builder_provider="", builder_model="deepseek-flash")
        target = resolve_target(cfg)
        self.assertEqual((target.provider, target.model), ("deepseek-official", "deepseek-flash"))
        self.assertEqual(target.source, "config")

        target = resolve_target(cfg, source="job")
        self.assertEqual(target.source, "config")

        cfg = _cfg()
        target = resolve_target(cfg)
        self.assertEqual((target.provider, target.model), ("deepseek-official", "deepseek-flash"))
        self.assertEqual(target.source, "default")

        tool = resolve_target(cfg, provider="anthropic", model="claude-sonnet-5-5",
                              source="job", allow_legacy=True)
        self.assertEqual((tool.provider, tool.model, tool.source),
                         ("anthropic", "claude-sonnet-5-5", "job"))

    def test_provider_inference_and_ambiguity(self):
        cfg = _cfg()
        target = resolve_target(cfg, model="ac" "me-large", allow_legacy=True)
        self.assertEqual(target.provider, "ac" "me")

        cfg = Config({"providers": [
            {"name": "one", "base_url": "https://one.test", "models": ["shared"]},
            {"name": "two", "base_url": "https://two.test", "models": ["shared"]},
        ]})
        with self.assertRaises(BuilderModelError) as raised:
            resolve_target(cfg, model="shared")
        self.assertEqual(raised.exception.code, "builder_model_provider_ambiguous")

    def test_injection_rejection(self):
        cfg = _cfg()
        for bad in ("x;rm", "a b", "-bad", "line\nbreak", "a=b"):
            with self.subTest(model=bad):
                with self.assertRaises(BuilderModelError) as raised:
                    resolve_target(cfg, model=bad)
                self.assertEqual(raised.exception.code, "builder_model_invalid")
        with self.assertRaises(BuilderModelError):
            resolve_target(cfg, provider="bad provider", model="deepseek-flash")

    def test_default_detection(self):
        cfg = _cfg(dsh_provider="deepseek-official", dsh_model="deepseek-flash")
        self.assertTrue(is_default_target(resolve_target(cfg), cfg))
        override = resolve_target(cfg, provider="anthropic",
                                  model="claude-sonnet-5-5", allow_legacy=True)
        self.assertFalse(is_default_target(override, cfg))

    def test_missing_key_names_variable_only(self):
        name = "DSH_TEST_" + "MISSING_KEY"
        fake = "sk-" + "not-" + "real-value"
        cfg = Config({"providers": [
            {"name": "ac" "me", "base_url": "https://api.example.test",
             "api_key_env": name, "models": ["ac" "me-large"]},
        ]})
        with mock.patch.dict(os.environ, {name: ""}):
            with self.assertRaises(BuilderModelError) as raised:
                resolve_target(cfg, provider="ac" "me", model="ac" "me-large")
        self.assertEqual(raised.exception.code, "builder_provider_key_missing")
        self.assertIn(name, raised.exception.message)
        self.assertNotIn(fake, raised.exception.message)

    def test_resolve_spec_shapes(self):
        cfg = _cfg()
        self.assertEqual(resolve_spec("anthropic/claude-sonnet-5-5", cfg),
                         ("anthropic", "claude-sonnet-5-5"))
        self.assertEqual(resolve_spec({"provider": "ac" "me", "model": "ac" "me-large"}, cfg),
                         ("ac" "me", "ac" "me-large"))


class OverlayTests(unittest.TestCase):
    def test_overlay_shape_mode_and_cleanup(self):
        name = "ANTHROPIC_API_KEY"
        fake = "sk-" + "not-real-" + "overlay-value"
        cfg = _cfg()
        with mock.patch.dict(os.environ, {name: fake}):
            target = resolve_target(cfg, provider="anthropic",
                                    model="claude-sonnet-5-5", source="job")
            text = overlay_yaml(target, cfg)
            with tempfile.TemporaryDirectory() as workspace:
                overlay = create_overlay(workspace, target, cfg)
                try:
                    self.assertTrue(os.path.isfile(overlay.path))
                    if os.name != "nt":
                        # Windows privacy is an ACL, not a POSIX mode.
                        self.assertEqual(
                            stat.S_IMODE(os.stat(overlay.path).st_mode), 0o600)
                    self.assertEqual(overlay.dir, os.path.dirname(overlay.path))
                    with open(overlay.path, "rb") as handle:
                        digest = __import__("hashlib").sha256(handle.read()).hexdigest()
                    self.assertEqual(overlay.sha256, digest)
                finally:
                    overlay.cleanup()
                self.assertFalse(os.path.exists(overlay.path))
                self.assertFalse(os.path.exists(overlay.dir))
                overlay.cleanup()
        self.assertIn("- id: llm-pi-ai", text)
        self.assertIn("- id: agent-default-model", text)
        self.assertIn('        apiKeyEnv: "ANTHROPIC_API_KEY"', text)
        self.assertIn('    provider: "anthropic"', text)
        self.assertIn('    model: "claude-sonnet-5-5"', text)
        self.assertNotIn(fake, text)


class PlanTests(unittest.TestCase):
    def test_slice_cap(self):
        cfg = _cfg(builder_model_max_slices=2)
        raw = {"a": "deepseek-official/deepseek-flash",
               "b": "deepseek-official/deepseek-flash",
               "c": "deepseek-official/deepseek-flash"}
        normalized, errors = validate_slice_models(cfg, raw)
        self.assertEqual(list(normalized), ["a", "b"])
        self.assertEqual([error["code"] for error in errors],
                         ["builder_model_max_slices"])

    def test_candidate_cap(self):
        cfg = _cfg(model_candidates_max=3)
        raw = {"api": ["deepseek-official/deepseek-flash"] * 5}
        normalized, errors = validate_model_candidates(cfg, raw)
        self.assertEqual(len(normalized["api"]), 4)
        self.assertEqual(errors[0]["code"], "model_candidates_max")

    def test_plan_section_exact(self):
        cfg = _cfg()
        parent = resolve_target(cfg)
        text = build_model_plan_section(
            parent, {"api": "deepseek-official/deepseek-flash"}, {})
        expected = (
            "## Model plan (connector)\n"
            "Parent builder uses deepseek-official/deepseek-flash.\n"
            "- slice api: deepseek-official/deepseek-flash\n"
            "Use the workflow agent() options {provider, model} for every agent that writes\n"
            "that slice's files. Do not change a slice's model between waves. If the installed\n"
            "Harness cannot override a child model, write MODEL_PLAN_UNSUPPORTED to\n"
            "wave-1-results.md and continue on the parent model. Never claim a model ran when\n"
            "it did not.\n"
        )
        self.assertEqual(text, expected)

    def test_audit_statuses(self):
        cfg = _cfg()
        requested = {"api": {"provider": "deepseek-official", "model": "deepseek-flash"}}
        verified = validate_model_audit(
            {"slices": [{"slice": "api", "winner": "deepseek-official/deepseek-flash",
                         "candidates": [{"tag": "one", "provider": "deepseek-official",
                                         "model": "deepseek-flash", "file": "src/a.py",
                                         "status": "ok"}]}]},
            cfg, requested, ["src/a.py"])
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["slices"][0]["status"], "verified")

        missing = validate_model_audit(None, cfg, requested, ["src/a.py"])
        self.assertEqual(missing["status"], "unverified")
        self.assertNotEqual(missing["status"], "verified")

        ignored = validate_model_audit({"slices": []}, cfg, requested, ["src/a.py"])
        self.assertEqual(ignored["status"], "ignored")
        self.assertTrue(ignored["unmatched"])

        mismatch = validate_model_audit(
            {"slices": [{"slice": "api", "winner": "anthropic/claude-sonnet-5-5",
                         "candidates": []}]},
            cfg, requested, ["src/a.py"])
        self.assertEqual(mismatch["status"], "mismatch")

    def test_competition_tie_breaks(self):
        candidates = [
            {"tag": "a", "provider": "p", "model": "m1", "estimated_cost_usd": 0.5},
            {"tag": "b", "provider": "p", "model": "m2", "estimated_cost_usd": 0.1},
        ]
        result = competition_result(candidates, {"a": 8.0, "b": 8.0}, ["a", "b"])
        self.assertEqual(result["winner"]["tag"], "b")

        unknown = competition_result(
            [{"tag": "c", "provider": "p", "model": "m3"},
             {"tag": "d", "provider": "p", "model": "m4", "estimated_cost_usd": 1.0}],
            {"c": 9.0, "d": 9.0}, ["c", "d"])
        self.assertEqual(unknown["winner"]["tag"], "d")

        ordered = competition_result(
            [{"tag": "x", "provider": "p", "model": "m5", "estimated_cost_usd": 0.2},
             {"tag": "y", "provider": "p", "model": "m6", "estimated_cost_usd": 0.2}],
            {"x": 7.0, "y": 7.0}, ["y", "x"])
        self.assertEqual(ordered["winner"]["tag"], "y")


class RegistryTests(unittest.TestCase):
    def test_new_defaults_and_coercion(self):
        self.assertEqual(SERVER_VERSION, "3.13.2")
        self.assertEqual(DEFAULTS["builder_model"], "")
        self.assertIs(DEFAULTS["builder_allow_unlisted_models"], False)
        self.assertEqual(DEFAULTS["builder_model_max_slices"], 8)
        self.assertEqual(DEFAULTS["backup_builder_max_cost_usd"], 1.0)
        self.assertEqual(DEFAULTS["model_plan_enforcement"], "warn")
        self.assertEqual(DEFAULTS["gate_no_product_mode"], "skip")
        self.assertIsInstance(DEFAULTS["gate_noproduct_globs"], list)
        self.assertEqual(_coerce("gate_no_product_mode", "bogus"), "skip")
        self.assertEqual(_coerce("model_plan_enforcement", "bogus"), "warn")
        self.assertEqual(_coerce("slice_models", '{"api": "p/m"}'), {"api": "p/m"})
        self.assertEqual(_coerce("reviewers", '[{"name": "r"}]'), [{"name": "r"}])


if __name__ == "__main__":
    unittest.main()
