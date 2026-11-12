"""3.14.2 slice B: resume applies a pending reviewer-vendor answer."""

import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import checkpoints, harness, reviewer_vendor
from deepseek_mcp.config import load_config
from tests.fakes import write_fake_script


SAME_OPTION = "Anthropic/claude-opus-5-5 (same vendor)"


def _attention(vendor, model, option):
    return {
        "question": ("The builder uses the same vendor as the reviewer. "
                     "Choose a reviewer or decline."),
        "options": [option, reviewer_vendor.DECLINE_OPTION],
        "evidence": {
            "kind": "reviewer_vendor",
            "builders": [vendor],
            "round": 1,
            "candidates": [{
                "vendor": vendor,
                "model": model,
                "name": "main",
                "state": "preflight_ok",
            }],
        },
    }


class ResumeAttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.state = self.root / "state"
        self.dsh = Path(write_fake_script(
            self.root / "fake-dsh", "import sys\nsys.exit(0)\n"))
        env = {
            "DEEPSEEK_MCP_CONFIG": str(self.root / "config.json"),
            "DSH_STATE_DIR": str(self.state),
            "DSH_BIN": str(self.dsh),
            "DSH_SKILLS_DIR": str(self.root / "skills"),
            "DSH_AUTO_SKILLS": "off",
            "DEEPSEEK_API_KEY": "test-key-not-real",
        }
        self.cfg = load_config(config_path=self.root / "config.json", env=env)
        # load_config prefers the ambient environment; pin the temp state so
        # the test never reads the operator's real run history.
        self.cfg._values["state_dir"] = str(self.state)
        self.cfg._values["runs_dir"] = str(self.root / "runs")
        self.cfg._values["reviewers"] = [{
            "name": "main", "vendor": "DeepSeek",
            "dsh_bin": str(self.dsh),
        }]

    def _spec(self):
        return {
            "prompt": "Write one note.",
            "workspace": str(self.ws),
            "title": "reviewer attention resume",
            "subagents": 0,
            "quality_used": True,
            "independent_review": True,
            "quality_rounds": 1,
        }

    def test_checkpoint_only_attention_reenters_review(self):
        """No run JSON, all phases done, attention only in metadata."""
        run_id = "resume-attention-checkpoint-only"
        spec = self._spec()
        attention = _attention("Anthropic", "claude-opus-5-5", SAME_OPTION)
        checkpoint = checkpoints.Checkpoint(self.state / "checkpoints", run_id)
        checkpoint.create(
            ["builder", "review-1"],
            metadata={"spec": spec,
                      "reviewer_attention": attention,
                      "reviewer_attention_reason": "same_vendor_reviewer"})
        checkpoint.finish("builder", status="done", answer="done")
        checkpoint.finish("review-1", status="done")
        captured = {}

        def fake_run(args, cfg, progress_cb=None, cancel_check=None):
            captured["args"] = dict(args)
            return ("resumed", {"id": run_id, "status": "ok"}, "/tmp/run.json")

        with mock.patch.object(harness, "run_harness", side_effect=fake_run):
            harness.resume_harness(
                {"resume_run_id": run_id, "option": SAME_OPTION}, self.cfg)

        self.assertIn("args", captured, "resume replayed instead of re-running")
        self.assertEqual(captured["args"].get("attention_option"), SAME_OPTION)
        self.assertEqual(
            captured["args"].get("_saved_reviewer_attention"), attention)

    def _run_with_option(self, option):
        attention = _attention("DeepSeek", "unknown", option)
        calls = []

        def fake_execute(cfg, workspace, task, **kwargs):
            label = kwargs.get("label") or ""
            calls.append(label)
            if "preflight" in label:
                return {"status": "ok", "answer": "READY",
                        "session_logs": [], "seconds": 0.1,
                        "estimated_cost_usd": 0.0}
            return {"status": "ok", "answer": "Reviewed.",
                    "stop_reason": "budget", "session_logs": [],
                    "seconds": 0.1, "estimated_cost_usd": 0.0}

        args = {
            "prompt": "Write one note.",
            "workspace": str(self.ws),
            "subagents": 0,
            "quality": True,
            "independent_review": True,
            "quality_rounds": 1,
            "_prior_builder": {
                "status": "ok", "answer": "done", "seconds": 0.1,
                "output_files": [], "session_logs": [],
                "estimated_cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1,
                "transport": "headless", "sandbox": "read-only",
            },
            "_saved_reviewer_attention": attention,
            "attention_option": option,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=fake_execute):
            _text, run, _path = harness.run_harness(args, self.cfg)
        return run, calls

    def test_run_harness_selects_chosen_candidate_and_labels_it(self):
        option = "DeepSeek/unknown (same vendor)"
        run, calls = self._run_with_option(option)
        self.assertEqual(run["reviewer_choice"]["status"], "selected")
        self.assertEqual(run["reviewer_choice"]["vendor"], "DeepSeek")
        self.assertTrue(run["reviewer_choice"]["same_vendor"])
        self.assertTrue(run["reviewer_choice"]["chosen_by_user"])
        self.assertIn(reviewer_vendor.SAME_VENDOR_MARKER,
                      run.get("review_header") or "")
        self.assertTrue(any("reviewer round" in label for label in calls),
                        "the chosen reviewer was not executed")

    def test_checkpoint_spec_restores_per_job_reviewer_target(self):
        """The exact 3.13.1 case: the option names a per-job model, not cfg."""
        run_id = "resume-attention-job-model"
        option = "Anthropic/claude-opus-5-5 (same vendor)"
        attention = _attention("Anthropic", "claude-opus-5-5", option)
        spec = self._spec()
        spec["reviewer_model"] = "claude-opus-5-5"
        spec["reviewer_provider"] = "anthropic"
        checkpoint = checkpoints.Checkpoint(self.state / "checkpoints", run_id)
        checkpoint.create(
            ["builder", "review-1"],
            metadata={"spec": spec, "reviewer_attention": attention,
                      "reviewer_attention_reason": "same_vendor_reviewer"})
        checkpoint.finish("builder", status="done", answer="done")
        checkpoint.finish("review-1", status="done")
        captured = {}

        def fake_run(args, cfg, progress_cb=None, cancel_check=None):
            captured["args"] = dict(args)
            return ("resumed", {"id": run_id, "status": "ok"}, "/tmp/run.json")

        with mock.patch.object(harness, "run_harness", side_effect=fake_run):
            harness.resume_harness(
                {"resume_run_id": run_id, "option": option}, self.cfg)

        self.assertEqual(captured["args"].get("reviewer_model"),
                         "claude-opus-5-5")
        self.assertEqual(captured["args"].get("reviewer_provider"), "anthropic")
        self.assertEqual(captured["args"].get("attention_option"), option)

    def test_chosen_per_job_model_is_executed_with_its_spec(self):
        option = "Anthropic/claude-opus-5-5 (same vendor)"
        attention = _attention("Anthropic", "claude-opus-5-5", option)
        target = types.SimpleNamespace(
            provider="anthropic", model="claude-opus-5-5",
            vendor="Anthropic")
        seen = []

        def fake_execute(cfg, workspace, task, **kwargs):
            label = kwargs.get("label") or ""
            seen.append((label, kwargs.get("model_spec")))
            if "preflight" in label:
                return {"status": "ok", "answer": "READY",
                        "session_logs": [], "seconds": 0.1,
                        "estimated_cost_usd": 0.0}
            return {"status": "ok", "answer": "Reviewed.",
                    "stop_reason": "budget", "session_logs": [],
                    "seconds": 0.1, "estimated_cost_usd": 0.0}

        args = {
            "prompt": "Write one note.",
            "workspace": str(self.ws),
            "subagents": 0, "quality": True, "independent_review": True,
            "quality_rounds": 1,
            "reviewer_model": "claude-opus-5-5",
            "reviewer_provider": "anthropic",
            "reviewer_vendor": "Anthropic",
            "_prior_builder": {
                "status": "ok", "answer": "done", "seconds": 0.1,
                "output_files": [], "session_logs": [],
                "estimated_cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1,
                "transport": "headless", "sandbox": "read-only",
            },
            "_saved_reviewer_attention": attention,
            "attention_option": option,
        }
        with mock.patch.object(
                harness.model_policy, "resolve_target",
                return_value=target) as resolve, \
                mock.patch.object(harness, "_execute_once",
                                  side_effect=fake_execute):
            _text, run, _path = harness.run_harness(args, self.cfg)

        self.assertEqual(run["reviewer_choice"]["status"], "selected")
        self.assertEqual(run["reviewer_choice"]["vendor"], "Anthropic")
        self.assertEqual(run["reviewer_choice"]["model"], "claude-opus-5-5")
        self.assertIn(reviewer_vendor.SAME_VENDOR_MARKER,
                      run.get("review_header") or "")
        self.assertTrue(resolve.called)
        review_specs = [spec for label, spec in seen
                        if "reviewer round" in label]
        self.assertTrue(review_specs, seen)
        self.assertTrue(all(spec is target for spec in review_specs), seen)

    def test_decline_option_resolves_to_declined(self):
        run, _calls = self._run_with_option(reviewer_vendor.DECLINE_OPTION)
        self.assertTrue(run.get("review_declined_by_user"))
        self.assertEqual(run["review_status"], "declined")


if __name__ == "__main__":
    unittest.main()
