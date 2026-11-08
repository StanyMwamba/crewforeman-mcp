"""3.12.0 integration seams: overlay execution, headers, schema, status."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import builder_backup, harness, model_policy, server, status
from deepseek_mcp.config import load_config


def _target(provider="anthropic", model="claude-sonnet-5-5"):
    return model_policy.ModelTarget(
        provider=provider, model=model, vendor="Anthropic",
        base_url="https://api.anthropic.com", api="anthropic-messages",
        api_key_env="ANTHROPIC_API_KEY", source="job")


class OverlayExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = load_config(config_path=self.ws / "config.json", env={
            "DEEPSEEK_MCP_CONFIG": str(self.ws / "config.json"),
            "DSH_STATE_DIR": str(self.ws / "state"),
            "DEEPSEEK_API_KEY": "test-key-not-real",
        })

    def test_override_creates_0600_overlay_and_cleans_it(self):
        seen = {}

        def fake_impl(cfg, workspace, task, **kwargs):
            patch = kwargs.get("patch")
            self.assertTrue(patch)
            path = Path(patch)
            self.assertTrue(path.is_file())
            seen["mode"] = stat.S_IMODE(path.stat().st_mode)
            seen["yaml"] = path.read_text(encoding="utf-8")
            return {"status": "ok", "answer": "done", "models": ["claude-sonnet-5-5"],
                    "estimated_cost_usd": None, "cost_known": False,
                    "session_logs": [], "output_files": []}

        with mock.patch.object(harness, "_execute_once_impl",
                               side_effect=fake_impl):
            result = harness._execute_once(
                self.cfg, str(self.ws), "task", model_spec=_target())
        self.assertEqual(seen["mode"], 0o600)
        self.assertIn("apiKeyEnv", seen["yaml"])
        self.assertIn("ANTHROPIC_API_KEY", seen["yaml"])
        self.assertNotIn("test-key-not-real", seen["yaml"])
        self.assertEqual(result["model_spec"]["model"], "claude-sonnet-5-5")
        self.assertIn("model_overlay", result)
        overlay_dir = Path(result["model_overlay"]["sha256"] and
                           list(self.ws.glob(".dsh-model-overlay-*"))[0]
                           if list(self.ws.glob(".dsh-model-overlay-*"))
                           else self.ws)
        # The overlay file itself is gone; the directory may be removed too.
        self.assertFalse(list(self.ws.glob(".dsh-model-overlay-*/overlay.yml")))

    def test_default_target_generates_no_overlay(self):
        captured = {}

        def fake_impl(cfg, workspace, task, **kwargs):
            captured["patch"] = kwargs.get("patch")
            return {"status": "ok", "answer": "done", "models": None,
                    "estimated_cost_usd": None, "cost_known": False,
                    "session_logs": [], "output_files": []}

        default = model_policy.resolve_target(self.cfg)
        with mock.patch.object(harness, "_execute_once_impl",
                               side_effect=fake_impl):
            harness._execute_once(self.cfg, str(self.ws), "task",
                                  model_spec=default)
        self.assertIsNone(captured["patch"])
        self.assertEqual(list(self.ws.glob(".dsh-model-overlay-*")), [])


class HeaderAndStatusTests(unittest.TestCase):
    def _run(self, **extra):
        run = {"id": "r1", "seconds": 1, "tools_used": {}, "subagents": 0,
               "independent_review": False, "status": "ok"}
        run.update(extra)
        return run

    def test_backup_builder_header(self):
        line = builder_backup.header_line(
            {"vendor": "Anthropic", "model": "claude-sonnet-5-5"},
            "service unavailable (5xx)")
        self.assertEqual(
            line, " | backup builder: Anthropic/claude-sonnet-5-5 "
                  "(main failed: service unavailable (5xx))")
        text = harness._result_text(self._run(backup_builder={
            "used": True, "vendor": "Anthropic", "model": "claude-sonnet-5-5",
            "reason_class": "service unavailable (5xx)", "line": line,
        }), "/tmp/run.md")
        self.assertIn(line, text.splitlines()[0])

    def test_review_header_and_declined_status(self):
        text = harness._result_text(self._run(
            independent_review=True, quality_score=None,
            review_header="Reviewed by: Anthropic/claude-sonnet-5-5 — "
                          "same-vendor review (chosen by user)"),
            "/tmp/run.md")
        self.assertIn("same-vendor review (chosen by user)", text)
        final, detail, passed = status.final_status(
            review_status="declined", review_recorded=False)
        self.assertEqual(final, "ok_unreviewed")
        self.assertIn("declined", detail)
        self.assertFalse(passed)
        self.assertIn("needs_input", status.STATUSES)
        self.assertNotIn("needs_input", status.SUCCESS_STATUSES)
        self.assertNotIn("needs_input", status.FAILURE_STATUSES)

    def test_schema_exposes_new_parameters(self):
        tool = next(item for item in server.TOOLS
                    if item["name"] == "harness_deepseek")
        props = tool["inputSchema"]["properties"]
        for name in ("builder_model", "builder_provider", "slice_models",
                     "model_candidates", "reviewer_model", "reviewer_provider"):
            self.assertIn(name, props)

    def test_checkpoint_spec_keeps_model_args(self):
        cfg = load_config(config_path="/nonexistent/config.json", env={
            "DEEPSEEK_MCP_CONFIG": "/nonexistent/config.json"})
        spec = harness._job_spec_for_checkpoint(
            {"id": "r", "prompt": "p", "subagents": 0}, {
                "builder_model": "m", "builder_provider": "p",
                "slice_models": {"a": "p/m"}, "reviewer_model": "rm",
            }, Path("/tmp/ws"))
        self.assertEqual(spec["builder_model"], "m")
        self.assertEqual(spec["slice_models"], {"a": "p/m"})
        self.assertEqual(spec["reviewer_model"], "rm")


class ReviewerSelectionHarnessTests(unittest.TestCase):
    def setUp(self):
        from tests.fakes import base_env, write_fake_dsh
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.root / "sessions")
        self.env.update({
            "FAKE_DSH_SESSIONS": str(self.root / "sessions"),
            "FAKE_DSH_REVIEW_SCORES": "9.0",
            "FAKE_DSH_REVIEW_BLOCKING": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
        })
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)

    def _run(self, vendors):
        from unittest import mock
        self.cfg._values["reviewers"] = [
            {"name": "rev-%d" % index, "vendor": vendor,
             "dsh_bin": str(self.dsh)}
            for index, vendor in enumerate(vendors)
        ]
        with mock.patch.dict(os.environ, self.env, clear=False):
            return harness.run_harness({
                "prompt": "Write one note.", "subagents": 1,
                "quality": True, "quality_rounds": 1,
                "workspace": str(self.ws)}, self.cfg)

    def test_cross_vendor_candidate_is_selected(self):
        _text, run, _path = self._run(["Kimi (Moonshot)"])
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["review_status"], "accepted")
        self.assertEqual(run["reviewer_choice"]["status"], "selected")
        self.assertEqual(run["reviewer_choice"]["vendor"], "Kimi (Moonshot)")
        self.assertIn("Reviewed by: Kimi (Moonshot)/",
                      run["review_header"])
        self.assertNotIn("same-vendor review", run["review_header"])

    def test_same_vendor_pauses_with_one_question(self):
        _text, run, _path = self._run(["DeepSeek"])
        self.assertEqual(run["status"], "needs_input")
        self.assertEqual(run["review_status"], "paused")
        self.assertEqual(run["reviewer_choice"]["status"], "paused")
        attention = run["reviewer_attention"]
        self.assertEqual(len(attention["options"]), 2)
        self.assertEqual(attention["options"][0],
                         "DeepSeek/unknown (same vendor)")
        self.assertIn("Do not review", attention["options"][1])
        self.assertFalse(run["review_in_flight"])

    def test_same_vendor_resume_runs_the_chosen_reviewer(self):
        _text, run, _path = self._run(["DeepSeek"])
        option = run["reviewer_attention"]["options"][0]
        with mock.patch.dict(os.environ, self.env, clear=False):
            _text, resumed, _path = harness.resume_harness(
                {"resume_run_id": run["id"], "option": option}, self.cfg)
        self.assertEqual(resumed["status"], "ok")
        self.assertEqual(resumed["review_status"], "accepted")
        self.assertEqual(resumed["reviewer_choice"]["same_vendor"], True)
        self.assertEqual(resumed["reviewer_choice"]["chosen_by_user"], True)
        self.assertEqual(
            resumed["review_header"],
            "Reviewed by: DeepSeek/unknown — same-vendor review "
            "(chosen by user)")
        self.assertFalse(resumed.get("review_in_flight"))

    def test_decline_returns_ok_unreviewed_without_inventing_a_score(self):
        _text, run, _path = self._run(["DeepSeek"])
        option = run["reviewer_attention"]["options"][-1]
        with mock.patch.dict(os.environ, self.env, clear=False):
            _text, resumed, _path = harness.resume_harness(
                {"resume_run_id": run["id"], "option": option}, self.cfg)
        self.assertEqual(resumed["status"], "ok_unreviewed")
        self.assertTrue(resumed["review_declined_by_user"])
        self.assertEqual(resumed["review_status"], "declined")
        self.assertIsNone(resumed["quality_score"])



class CompetitionTests(unittest.TestCase):
    def test_missing_candidate_is_not_selected_and_winner_is_copied(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            (ws / "SLICES.md").write_text(
                "# SLICES\n\n## Wave 1\n\n### Slice api\nWrites: src/api.py\n",
                encoding="utf-8")
            (ws / "candidates").mkdir()
            (ws / "candidates" / "api--good.py").write_text(
                "def api():\n    return 'good'\n", encoding="utf-8")
            (ws / "MODEL_AUDIT.json").write_text(
                '{"slices": [{"slice": "api", "winner": "p/good", '
                '"candidates": ['
                '{"tag": "good", "provider": "p", "model": "good", '
                '"file": "candidates/api--good.py", "status": "ok"}, '
                '{"tag": "bad", "provider": "p", "model": "bad", '
                '"file": "candidates/api--missing.py", "status": "ok"}]}]}',
                encoding="utf-8")
            cfg = load_config(config_path=str(ws / "config.json"), env={
                "DEEPSEEK_MCP_CONFIG": str(ws / "config.json")})
            run = {}
            harness._apply_model_competition(
                run, ws, cfg, "build the api",
                {"api": [{"provider": "p", "model": "good"},
                         {"provider": "p", "model": "bad"}]})
            record = run["model_competition"]["api"]
            self.assertTrue(record["applied"])
            self.assertEqual(record["winner"], "good")
            self.assertEqual((ws / "src" / "api.py").read_text(
                encoding="utf-8"),
                "def api():\n    return 'good'\n")
            self.assertTrue(any(item.get("score") is not None
                                for item in record["candidates"]))



if __name__ == "__main__":
    unittest.main()
