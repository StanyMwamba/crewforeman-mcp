"""P3.2 regressions for tempdir selection in root/read-only sandboxes."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests


class TempDirPortabilityTests(unittest.TestCase):
    def _base(self):
        tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _blocker(self, base):
        blocker = base / "regular-file"
        blocker.write_text("not a directory\n", encoding="utf-8")
        return blocker / "tmp"

    def test_usable_refuses_path_under_a_regular_file(self):
        base = self._base()
        blocked = self._blocker(base)
        self.assertFalse(tests._usable(blocked))
        self.assertFalse(blocked.exists(),
                         "a path under a regular file must not be created")
        sibling = base / "real-dir"
        sibling.mkdir()
        self.assertTrue(tests._usable(sibling))

    def test_nonexistent_explicit_tmpdir_is_ignored_not_created(self):
        base = self._base()
        missing = base / "missing-tmpdir"
        with mock.patch.dict(os.environ, {
                "TMPDIR": str(missing), "TMP": "", "TEMP": "",
                "DSH_TEST_TMPDIR": "", "DSH_TMPDIR": ""}, clear=False):
            selected = tests._select_tempdir()
        self.assertFalse(missing.exists(),
                         "an explicit nonexistent TMPDIR was created")
        self.assertIsNotNone(selected)
        self.assertNotEqual(str(selected), str(missing.resolve()))

    def test_helper_honours_tmpdir_and_make_tempdir_is_usable(self):
        base = self._base()
        with mock.patch.dict(os.environ, {
                "TMPDIR": str(base), "TMP": "", "TEMP": "",
                "DSH_TEST_TMPDIR": "", "DSH_TMPDIR": ""}, clear=False):
            self.assertEqual(Path(tests.tempdir()), base.resolve())
            fresh = Path(tests.make_tempdir(prefix="p32-"))
            self.addCleanup(lambda p=fresh: p.exists() and p.rmdir())
            self.assertTrue(fresh.is_dir())
            self.assertEqual(fresh.parent, base.resolve())

    def test_unusable_tmpdir_falls_back_with_blocker_file_even_as_root(self):
        base = self._base()
        blocked = self._blocker(base)
        uid = getattr(os, "geteuid", lambda: None)()
        with mock.patch.dict(os.environ, {
                "TMPDIR": str(blocked),
                "TMP": str(blocked / "tmp"),
                "TEMP": str(blocked / "temp"),
                "DSH_TEST_TMPDIR": "", "DSH_TMPDIR": ""}, clear=False):
            selected = tests._select_tempdir()
        self.assertIsNotNone(
            selected, "no fallback in a writable copy (uid=%r)" % (uid,))
        self.assertTrue(tests._usable(selected))
        self.assertNotEqual(str(selected), str(blocked))
        self.assertFalse(blocked.exists())
        # The connector's read-only reviewer grants writes only below
        # ``<workspace>/.dsh-tmp-*``.  Prefer that granted path over
        # ``repo/.tmp``, which the same Seatbelt profile denies; this is what
        # keeps the focused P3.2 run from skipping when its TMPDIR is /tmp.
        repo = Path(tests.__file__).resolve().parent.parent
        self.assertEqual(Path(selected), (repo.parent / ".dsh-tmp-tests").resolve())
        # The blocker-file premise must be uid-independent: creating the
        # candidate under a regular file raises ENOTDIR even for uid 0.  This
        # replaces an earlier ``euid == 0 or True`` assertion that could never
        # fail.
        with self.assertRaises(OSError):
            os.makedirs(str(blocked))
        self.assertFalse(blocked.is_dir())

    def test_suite_env_disables_os_sandbox_for_fake_dsh(self):
        """A host where Seatbelt works must not wrap the fake test child.

        The connector's signed suite runs outside DSH's file sandbox, so
        ``seatbelt_usable()`` is True there and the old default ``auto``
        wrapped the fake DSH.  The fake writes session/marker files beside its
        workspace, which the real profile correctly denies, producing 41
        spurious errors in the signed run.  The unit suite must resolve the OS
        wrapper off; the dedicated sandbox tests opt in explicitly.
        """
        from deepseek_mcp import safety
        from deepseek_mcp.config import load_config
        from tests.fakes import base_env

        root = self._base()
        env = base_env(root)
        self.assertEqual(env.get("DSH_OS_SANDBOX"), "0")
        cfg = load_config(config_path=root / "config.json", env=dict(env))
        with mock.patch.object(safety.sys, "platform", "darwin"), \
                mock.patch.object(safety, "seatbelt_usable", return_value=True):
            policy = safety.resolve({}, cfg=cfg)
        self.assertIs(policy["os_sandbox"], False)
