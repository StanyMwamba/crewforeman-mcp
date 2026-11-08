"""P3.4a regressions for the per-test isolation layer and sandbox guard."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests
from tests import isolation
from tests import sandbox_guard


class PerTestIsolationTests(unittest.TestCase):
    def setUp(self):
        # The stdlib runner's load_tests hook normally isolates every test.
        # A focused ``python -m unittest tests.test_p34a_isolation`` run (or a
        # runner without the hook) must not depend on that, so enter the case
        # explicitly when no case is already active, and clean it up in
        # tearDown via addCleanup.
        self._case_context = None
        if not isolation.is_active():
            self._case_context = isolation.isolated_case()
            self._case_context.__enter__()
            self.addCleanup(self._case_context.__exit__, None, None, None)

    def _case(self):
        return Path(os.environ["CREWFOREMAN_STATE_DIR"]).parent

    def test_every_mutable_root_points_inside_the_case_dir(self):
        case = self._case()
        self.assertTrue(case.name.startswith("case-"), case)
        for name in isolation.ISOLATED_ENV_VARS:
            value = os.environ.get(name)
            self.assertIsNotNone(value, "%s was not redirected" % name)
            path = Path(value)
            self.assertTrue(
                path == case or str(path).startswith(str(case) + os.sep),
                "%s=%s escaped %s" % (name, value, case))
        self.assertTrue(isolation.is_active())
        self.assertEqual(Path(os.environ[isolation.CASE_ENV_VAR]), case)
        self.assertTrue(Path(os.environ["TMPDIR"]).is_dir())
        self.assertEqual(os.environ["DSH_STATE_DIR"],
                         os.environ["DEEPSEEK_MCP_STATE_DIR"])
        self.assertEqual(Path(os.environ["TMPDIR"]).name, "tmp")
        config = Path(os.environ["DEEPSEEK_MCP_CONFIG"])
        self.assertTrue(config.is_file())
        self.assertTrue(str(config).startswith(str(case) + os.sep))

    def test_home_userprofile_and_xdg_are_unchanged(self):
        case = self._case()
        for name, expected in isolation.HOME_AT_IMPORT.items():
            self.assertEqual(os.environ.get(name), expected,
                             "%s must not be redirected per test" % name)
        home = os.environ.get("HOME")
        self.assertIsNotNone(home)
        self.assertFalse(str(Path(home)).startswith(str(case)))
        self.assertEqual(Path.home(), Path(home))

    def test_tempfile_tempdir_is_inside_the_workspace_case(self):
        case = self._case()
        workspace = Path(tests.workspace_temp_root())
        self.assertEqual(tempfile.gettempdir(), os.environ["TMPDIR"])
        self.assertTrue(tempfile.gettempdir().startswith(str(case) + os.sep))
        self.assertTrue(
            tempfile.gettempdir().startswith(str(workspace) + os.sep),
            "%s is not under the workspace root %s"
            % (tempfile.gettempdir(), workspace))
        handle, name = tempfile.mkstemp(prefix="p34a-")
        try:
            self.assertTrue(name.startswith(os.environ["TMPDIR"] + os.sep), name)
        finally:
            os.close(handle)
            os.unlink(name)

    def test_default_config_derives_all_roots_inside_the_case(self):
        from deepseek_mcp.config import load_config

        case = self._case()
        cfg = load_config(config_path=case / "config.json", env=dict(os.environ))
        for key, leaf in (("state_dir", "state"), ("log_dir", "runs"),
                          ("jobs_dir", "jobs"), ("dsh_sessions_dir", "sessions")):
            self.assertEqual(Path(getattr(cfg, key)), case / "root" / leaf, key)
        self.assertEqual(Path(cfg.pid_file), case / "root" / "keep_awake.pid")

    def test_cleanup_deletes_case_and_restores_env(self):
        outer_tmp = tempfile.gettempdir()
        outer_state = os.environ["CREWFOREMAN_STATE_DIR"]
        with isolation.isolated_case() as nested:
            self.assertNotEqual(nested, Path(outer_state).parent)
            (nested / "sentinel.txt").write_text("x", encoding="utf-8")
            self.assertTrue((nested / "sentinel.txt").is_file())
        self.assertFalse(nested.exists(), "case dir was not removed")
        self.assertEqual(tempfile.gettempdir(), outer_tmp)
        self.assertEqual(os.environ["CREWFOREMAN_STATE_DIR"], outer_state)

    def test_inherited_per_key_aliases_are_cleared_and_restored(self):
        from deepseek_mcp.config import load_config

        outer_names = isolation.CLEARED_ENV_VARS + ("DSH_SESSIONS_DIR",)
        outer = {name: str(Path(tempfile.gettempdir()) / "p34a-outer" / name)
                 for name in outer_names}
        with mock.patch.dict(os.environ, outer):
            with isolation.isolated_case() as nested:
                self.assertEqual(
                    Path(os.environ["CREWFOREMAN_STATE_DIR"]).parent, nested)
                # B1: sessions no longer derive from state_root, so the case
                # must set the explicit alias instead of clearing it.
                self.assertEqual(
                    Path(os.environ["DSH_SESSIONS_DIR"]),
                    nested / "root" / "sessions")
                self.assertTrue(
                    str(Path(os.environ["DSH_SESSIONS_DIR"])).startswith(
                        str(nested) + os.sep))
                for name in isolation.CLEARED_ENV_VARS:
                    self.assertNotIn(
                        name, os.environ,
                        "%s escaped the isolated case" % name)
                cfg = load_config(config_path=nested / "config.json",
                                  env=dict(os.environ))
                for key, leaf in (("log_dir", "runs"), ("jobs_dir", "jobs"),
                                  ("pid_file", "keep_awake.pid"),
                                  ("dsh_sessions_dir", "sessions")):
                    self.assertEqual(Path(getattr(cfg, key)),
                                     nested / "root" / leaf, key)
            for name, value in outer.items():
                self.assertEqual(os.environ.get(name), value,
                                 "%s was not restored" % name)

    def test_workspace_temp_root_prefers_workspace_local_dsh_tmp_env(self):
        repo = Path(tests.__file__).resolve().parent.parent
        candidate = Path(tempfile.mkdtemp(
            prefix=".dsh-tmp-regression-", dir=str(repo)))
        self.addCleanup(shutil.rmtree, str(candidate), True)
        with mock.patch.dict(os.environ):
            for name in ("DSH_TEST_TMPDIR", "DSH_TMPDIR", "TMP", "TEMP"):
                os.environ.pop(name, None)
            os.environ["TMPDIR"] = str(candidate)
            selected = Path(tests.workspace_temp_root())
        self.assertEqual(selected, candidate.resolve())
        self.assertTrue(selected.name.startswith(".dsh-tmp-"))

    def test_ambient_dsh_tmp_outside_the_workspace_is_not_a_grant(self):
        # A directory merely named .dsh-tmp-* under the system temp root is
        # not the Harness workspace grant (NB1/S6).  DSH_TEST_TMPDIR remains
        # the explicit escape hatch and is accepted anywhere.
        repo = Path(tests.__file__).resolve().parent.parent
        outside = Path("/tmp") / ".dsh-tmp-outside-grant-test"
        self.assertFalse(tests._workspace_grant(outside, repo))

    def test_workspace_temp_root_failure_is_a_counted_skip(self):
        class DummyPassingTest(unittest.TestCase):
            def test_pass(self):
                pass

        dummy = DummyPassingTest("test_pass")
        result = unittest.TestResult()
        with mock.patch.dict(os.environ):
            os.environ.pop(isolation.CASE_ENV_VAR, None)
            self.assertFalse(isolation.is_active())
            with mock.patch.object(
                    tests, "workspace_temp_root",
                    side_effect=unittest.SkipTest(
                        "environment-only: forced failure")):
                returned = dummy.run(result)
        self.assertIs(returned, result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.failures, result.failures)
        self.assertFalse(result.errors, result.errors)
        self.assertIn("environment-only", result.skipped[0][1])
        self.assertIs(result.skipped[0][0], dummy)


class SandboxGuardTests(unittest.TestCase):
    def tearDown(self):
        sandbox_guard.reset_cache()

    def test_skip_reason_is_environment_only_and_names_nesting(self):
        with mock.patch.object(sandbox_guard.safety, "seatbelt_usable",
                               return_value=False), \
                mock.patch.dict(sandbox_guard.safety._SEATBELT_PROBE,
                                {"reason": "sandbox-exec cannot be nested in "
                                           "this process (probe exit 1)"}):
            sandbox_guard.reset_cache()
            with self.assertRaises(unittest.SkipTest) as caught:
                sandbox_guard.require_real_sandbox()
        reason = str(caught.exception)
        self.assertTrue(reason.strip())
        self.assertIn("environment-only", reason)
        self.assertIn("nested", reason.lower())
        self.assertIn("probe exit 1", reason)

    def test_probe_result_is_cached_in_the_module(self):
        with mock.patch.object(sandbox_guard.safety, "seatbelt_usable",
                               return_value=True) as probe:
            sandbox_guard.reset_cache()
            sandbox_guard.require_real_sandbox()
            sandbox_guard.require_real_sandbox()
            sandbox_guard.nested_sandbox_reason()
        self.assertEqual(probe.call_count, 1)

    def test_usable_sandbox_does_not_skip(self):
        with mock.patch.object(sandbox_guard.safety, "seatbelt_usable",
                               return_value=True):
            sandbox_guard.reset_cache()
            sandbox_guard.require_real_sandbox()  # must not raise
        self.assertEqual(sandbox_guard.nested_sandbox_reason(), "")


if __name__ == "__main__":
    unittest.main()
