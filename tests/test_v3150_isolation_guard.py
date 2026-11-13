"""3.15.0 regressions for the test-suite home-state pollution guard.

The suite used to write fake run records (prompts like ``Write one note.``)
into the real ``~/DeepSeek-MCP/runs`` because fixtures built
``load_config(env={...})`` dicts without any state/log path keys.  These
tests lock both halves of the fix: the systemic write guard in
``tests.isolation`` (a store write aimed at the real home state root or the
``<repo>/runs`` fallback raises ``IsolationViolation``) and the fixture env
dicts (``load_config`` with them can never select the home ``log_dir``
default).  Every record here uses the prompt ``guard-canary`` and is written
only inside the case-local temp area — nothing is created under the real
home.
"""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests
from deepseek_mcp import store
from deepseek_mcp.config import load_config as real_load_config
from tests import isolation


def _canary_run(run_id):
    return {
        "id": run_id,
        "kind": "single",
        "created": "2026-01-01T00:00:00+00:00",
        "title": "guard-canary",
        "prompt": "guard-canary",
        "model": "guard-canary",
        "status": "ok",
        "answer": "guard-canary",
        "files": [],
    }


def _state_env(root):
    root = Path(root)
    return {
        "CREWFOREMAN_STATE_DIR": str(root),
        "DSH_STATE_DIR": str(root / "state"),
        "DEEPSEEK_MCP_LOG_DIR": str(root / "runs"),
        "DEEPSEEK_MCP_JOBS_DIR": str(root / "jobs"),
    }


class IsolationGuardTests(unittest.TestCase):
    def setUp(self):
        # The stdlib runner's hook already isolates every test; a focused run
        # under a runner without the hook must not silently skip the guard.
        self._case_context = None
        if not isolation.is_active():
            self._case_context = isolation.isolated_case()
            self._case_context.__enter__()
            self.addCleanup(self._case_context.__exit__, None, None, None)

    def test_save_run_targeting_the_real_home_state_root_is_refused(self):
        home_runs = isolation.real_home_state_root() / "runs"
        cfg = real_load_config(env={"DEEPSEEK_MCP_LOG_DIR": str(home_runs)},
                               include_file=False)
        self.assertEqual(str(cfg.log_dir), str(home_runs))
        run = _canary_run("guard-canary-home")

        with self.assertRaises(isolation.IsolationViolation) as caught:
            store.save_run(run, cfg)

        message = str(caught.exception)
        self.assertIn(str(home_runs), message)
        self.assertIn("config", message)
        self.assertIn("log_dir", message)
        # The guard must raise before any probe or write touches the home
        # state root.
        self.assertFalse((home_runs / "guard-canary-home.json").exists())
        self.assertFalse((home_runs / "guard-canary-home.md").exists())

    def test_save_run_inside_the_case_dir_works(self):
        root = Path(tempfile.mkdtemp(prefix="guard-canary-"))
        self.assertTrue(
            str(root).startswith(os.environ[isolation.CASE_ENV_VAR] + os.sep),
            root)
        cfg = real_load_config(config_path=root / "config.json",
                               env=dict(_state_env(root), **{
                                   "DEEPSEEK_MCP_CONFIG": str(root / "config.json"),
                               }))
        run = _canary_run("guard-canary-local")

        md_path = Path(store.save_run(run, cfg))

        self.assertTrue(md_path.is_file())
        self.assertTrue(md_path.with_suffix(".json").is_file())
        self.assertTrue(str(md_path).startswith(str(root) + os.sep), md_path)
        self.assertIsNone(isolation.forbidden_target_reason(md_path))
        self.assertIn('"prompt": "guard-canary"',
                      md_path.with_suffix(".json").read_text(encoding="utf-8"))

    def test_fixed_fixture_env_dicts_never_select_home_log_dir(self):
        # Run the cheap config-building units of the fixtures that used to
        # hand-build load_config(env=...) dicts with no state/log keys, and
        # assert every resulting config keeps its write roots out of the home
        # state root and inside the test scratch area.
        recorded = []

        def recorder(*args, **kwargs):
            cfg = real_load_config(*args, **kwargs)
            recorded.append(cfg)
            return cfg

        from tests import (test_v3120_integration, test_v3143_fix2,
                           test_v3140_resume_attention, test_v320_stall)
        modules = (test_v3120_integration, test_v320_stall,
                   test_v3143_fix2, test_v3140_resume_attention)
        with contextlib.ExitStack() as stack:
            for module in modules:
                stack.enter_context(
                    mock.patch.object(module, "load_config", recorder))
            self._exercise_fixture_config_units(
                test_v3120_integration, test_v320_stall,
                test_v3143_fix2, test_v3140_resume_attention)

        self.assertTrue(recorded, "fixtures never called load_config")
        scratch = [os.environ[isolation.CASE_ENV_VAR],
                   str(tests.workspace_temp_root()), tempfile.gettempdir()]
        scratch = [os.path.realpath(item) for item in scratch if item]
        for cfg in recorded:
            for key in ("log_dir", "state_dir", "jobs_dir"):
                value = cfg.get(key)
                reason = isolation.forbidden_target_reason(value)
                self.assertIsNone(
                    reason,
                    "%s=%r would write into %s" % (key, value, reason))
                norm = os.path.realpath(os.path.abspath(
                    os.path.expanduser(str(value))))
                self.assertTrue(
                    any(norm == root or norm.startswith(root + os.sep)
                        for root in scratch),
                    "%s=%r is outside the test scratch area %s"
                    % (key, value, scratch))

    def _exercise_fixture_config_units(self, v3120, v320, v3143, v3140):
        def set_up(instance):
            try:
                instance.setUp()
            finally:
                instance.doCleanups()

        set_up(v3120.OverlayExecutionTests(
            "test_default_target_generates_no_overlay"))
        set_up(v3120.ReviewerSelectionHarnessTests(
            "test_cross_vendor_candidate_is_selected"))
        instance = v3120.HeaderAndStatusTests(
            "test_checkpoint_spec_keeps_model_args")
        try:
            instance.test_checkpoint_spec_keeps_model_args()
        finally:
            instance.doCleanups()
        v3120.CompetitionTests(
            "test_missing_candidate_is_not_selected_and_winner_is_copied"
        ).test_missing_candidate_is_not_selected_and_winner_is_copied()
        v320.StallSecondsTests(
            "test_default_is_480_and_invalid_values_disable"
        ).test_default_is_480_and_invalid_values_disable()
        instance = v320.HarnessStallIntegrationTests(
            "test_stall_stops_builder_and_saves_stalled_run")
        try:
            instance.setUp()
            instance._cfg(str(instance.root / "fake-dsh"))
        finally:
            instance.doCleanups()
        v3143.DependsOnPolicyTests(
            "test_config_rejects_unknown_policy"
        ).test_config_rejects_unknown_policy()
        v3143.DependsOnPolicyTests(
            "test_config_default_and_env_mapping"
        ).test_config_default_and_env_mapping()
        set_up(v3140.ResumeAttentionTests(
            "test_decline_option_resolves_to_declined"))

    def test_store_fallback_to_repo_runs_is_refused(self):
        repo_runs = isolation.repo_runs_dir()
        self.assertEqual(_norm(store.FALLBACK_LOG_DIR), _norm(repo_runs))
        blocker = Path(tempfile.mkdtemp(prefix="guard-canary-")) / "blocker"
        blocker.write_text("a plain file where a directory is needed",
                           encoding="utf-8")
        cfg = real_load_config(config_path=blocker.parent / "missing.json",
                               env=dict(_state_env(blocker.parent), **{
                                   "DEEPSEEK_MCP_LOG_DIR": str(blocker / "runs"),
                               }))
        existed = repo_runs.exists()

        # A write whose configured log_dir cannot be resolved must not fall
        # back to <repo>/runs: the fallback probe itself is refused.
        with self.assertRaises(isolation.IsolationViolation) as caught:
            store.save_run(_canary_run("guard-canary-fallback"), cfg)
        self.assertIn(str(repo_runs), str(caught.exception))

        with self.assertRaises(isolation.IsolationViolation):
            store.resolve_log_dir(cfg)

        # An explicit target equal to the fallback is refused as well.
        cfg._values["log_dir"] = str(repo_runs)
        with self.assertRaises(isolation.IsolationViolation) as caught:
            store.save_run(_canary_run("guard-canary-fallback-direct"), cfg)
        self.assertIn(str(repo_runs), str(caught.exception))

        self.assertEqual(repo_runs.exists(), existed,
                         "the refused fallback created %s" % (repo_runs,))
        if repo_runs.exists():
            self.assertFalse(list(repo_runs.glob("guard-canary*")))
        self.assertFalse((blocker.parent / "runs").exists())


def _norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


if __name__ == "__main__":
    unittest.main()
