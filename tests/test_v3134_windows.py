"""3.13.6 regression tests for Windows-only defects, runnable on any host.

The Windows CI failure set was dominated by host path semantics rather than
by Windows-only APIs, so these tests force the Windows comparison mode on a
macOS/Linux host (``_CASE_INSENSITIVE_FS``) or mock the Win32 surface.  The
path/casefold/ACL/CRLF/sdist tests would have failed before the 3.13.6 fixes;
a few structural checks (lock file, process flags) guard the fix without
failing on POSIX beforehand.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, plan_contract as pc
from deepseek_mcp import platforms, project_map
from deepseek_mcp import sandbox_windows as sw
from deepseek_mcp import work
import build_backend


class PlanPathPortabilityTests(unittest.TestCase):
    def test_ntpath_normcase_is_not_a_posix_normaliser(self):
        # Document the primitive that caused the bug: ntpath.normcase both
        # lowercases and rewrites "/" as "\\".
        import ntpath
        self.assertEqual(ntpath.normcase("A/B.py"), "a\\b.py")

    def test_windows_mode_keeps_posix_spelling_and_folds_only_for_match(self):
        with mock.patch.object(pc, "_CASE_INSENSITIVE_FS", True):
            self.assertEqual(pc.normalize_path("A\\B.py"), "A/B.py")
            self.assertEqual(pc.normalize_path("PLAN.json"), "PLAN.json")
            self.assertTrue(pc.same_path("plan.json", "PLAN.json"))
            self.assertTrue(pc.same_path("SRC\\A.py", "src/a.py"))
            self.assertTrue(pc._prefix_matches("SRC", "src/a.py"))
            self.assertTrue(pc._entry_matches("SRC/*.py", "src/a.py"))

    def test_posix_mode_stays_case_sensitive(self):
        with mock.patch.object(pc, "_CASE_INSENSITIVE_FS", False):
            self.assertFalse(pc.same_path("plan.json", "PLAN.json"))
            self.assertFalse(pc.same_path("src/A.py", "src/a.py"))
            self.assertEqual(pc.normalize_path("A\\B.py"), "A/B.py")

    def test_invalid_path_errors_name_the_field_not_a_previous_path(self):
        # Dedupe must not rebind the validation field name (a 3.13.6 review
        # caught the old ``key = match_key(path)`` shadowing bug).
        _normalised, errors = pc.validate_plan(
            {"version": 1,
             "files_expected": ["ok.py", "/abs.py", "../up.py"]}, {})
        self.assertEqual(errors, ["files_expected: invalid path"] * 2)

    def test_plan_json_case_is_preserved_in_the_actual_diff(self):
        with mock.patch.object(pc, "_CASE_INSENSITIVE_FS", True):
            actual = pc.actual_changes(
                "", {"a.py": (1000.0, 1)},
                {"a.py": (1000.0, 1), "PLAN.json": (1.0, 1)}, {})
        self.assertEqual(actual["ignored"], ["PLAN.json"])
        self.assertEqual(actual["created"], [])


class CanonicalPathCaseTests(unittest.TestCase):
    def test_windows_canonical_path_does_not_normcase(self):
        # Force the Windows branch and make normcase observable.  The 3.13.6
        # code lowercased every workspace root on Windows.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CaseWorkspace")
            os.makedirs(path)
            with mock.patch.object(
                    platforms, "normalize_system",
                    return_value=platforms.WINDOWS), \
                    mock.patch.object(
                        platforms.os.path, "normcase",
                        side_effect=lambda value: str(value).lower()):
                result = platforms.canonical_path(path, system="Windows")
        self.assertIn("CaseWorkspace", result)


class WindowsPrivateAclTests(unittest.TestCase):
    SID = "S-1-5-21-111-222-333-1001"
    PATH = r"C:\work dir\f.txt"

    def _run(self, output, trustees=None, whoami=None):
        completed = mock.Mock(returncode=0, stdout=output, stderr=b"")
        with mock.patch.object(platforms, "_windows_user_sid",
                               return_value=self.SID), \
                mock.patch.object(
                    platforms, "_windows_trustees",
                    return_value=trustees or {r"domain\runneradmin"}), \
                mock.patch.object(
                    platforms, "_windows_whoami_trustees",
                    return_value=whoami or set()), \
                mock.patch.object(platforms, "_icacls_binary",
                                  return_value=sys.executable), \
                mock.patch.object(platforms.subprocess, "run",
                                  side_effect=[mock.Mock(returncode=0),
                                               completed]):
            return platforms._windows_private_acl(self.PATH, is_dir=False)

    def _ace(self, trustee, flags="(F)"):
        return ("%s %s:%s\n\nSuccessfully processed 1 files;"
                % (self.PATH, trustee, flags)).encode()

    def test_icacls_account_name_rendering_is_accepted(self):
        self.assertTrue(self._run(self._ace(r"DOMAIN\runneradmin")))

    def test_path_with_spaces_is_stripped_before_trustee_compare(self):
        self.assertTrue(self._run(self._ace(r"DOMAIN\runneradmin")))

    def test_sid_rendering_is_accepted(self):
        self.assertTrue(self._run(
            self._ace(self.SID),
            trustees={self.SID.casefold(), r"domain\runneradmin"}))

    def test_foreign_trustee_is_rejected(self):
        self.assertFalse(self._run(self._ace(r"DOMAIN\someone-else")))

    def test_token_account_name_is_checked_when_environment_differs(self):
        self.assertTrue(self._run(
            self._ace(r"RENAMED-PC\runneradmin"),
            trustees={r"domain\runneradmin"},
            whoami={r"renamed-pc\runneradmin"}))

    def test_inherited_full_control_is_rejected(self):
        self.assertFalse(self._run(
            self._ace(r"DOMAIN\runneradmin", "(I)(F)")))

    def test_owner_plus_system_aces_are_accepted(self):
        output = (self._ace(r"DOMAIN\runneradmin")
                  + b"\n                NT AUTHORITY\\SYSTEM:(F)"
                  + b"\n                BUILTIN\\Administrators:(F)")
        self.assertTrue(self._run(output))

    def test_extra_foreign_ace_is_rejected(self):
        output = (self._ace(r"DOMAIN\runneradmin")
                  + b"\n                Everyone:(F)")
        self.assertFalse(self._run(output))


class ProjectMapPortabilityTests(unittest.TestCase):
    def test_crlf_map_hashes_like_lf(self):
        lf = "# Map\n\n- line one\n- line two\n"
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "PROJECT_MAP.md").write_bytes(
                lf.replace("\n", "\r\n").encode("utf-8"))
            self.assertEqual(project_map._read_map(tmp), lf)
            self.assertEqual(project_map.map_digest(tmp),
                             project_map._sha256(lf))

    def test_store_survives_missing_os_fchmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            # os.fchmod does not exist at all on Windows Python 3.9, so the
            # patch must create the attribute before making it fail.
            with mock.patch.object(project_map.os, "fchmod",
                                   side_effect=AttributeError("no fchmod"),
                                   create=True):
                ok = project_map._store(
                    tmp, os.path.join(tmp, "state"),
                    {"sha256": "x"}, "# Map\n")
            self.assertTrue(ok)


class SdistModeTests(unittest.TestCase):
    def test_shebang_is_executable_without_a_posix_x_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "clean_install.sh")
            path.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            os.chmod(path, 0o644)
            self.assertEqual(
                build_backend._sdist_mode(path, path.read_bytes()), 0o755)
            plain = Path(tmp, "notes.txt")
            plain.write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                build_backend._sdist_mode(plain, plain.read_bytes()), 0o644)


class WorkLockTests(unittest.TestCase):
    def test_existing_lock_file_does_not_block_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = work.WorkStore(tmp, lock_timeout=1.0)
            admission = store.admit("fp-3134", idempotency_key="k")
            work_dir = Path(admission["work_id"] and store.path(
                admission["work_id"]))
            lock = work_dir / ".lock"
            self.assertTrue(lock.is_file())
            # A pre-existing file is normal for the advisory lock; it must
            # not be mistaken for an owned existence lock.
            self.assertEqual(
                store.admit("fp-3134", idempotency_key="k")["reused"], True)


class SandboxWindowsUnitTests(unittest.TestCase):
    def test_create_process_does_not_use_create_no_window(self):
        flags = sw._process_flags()
        self.assertFalse(flags & sw.CREATE_NO_WINDOW)
        self.assertTrue(flags & sw.DETACHED_PROCESS)
        self.assertTrue(flags & sw.CREATE_SUSPENDED)
        self.assertTrue(flags & sw.EXTENDED_STARTUPINFO_PRESENT)

    def test_startupinfo_has_a_desktop_field_and_hidden_console(self):
        self.assertEqual(sw.STARTF_USESHOWWINDOW, 0x00000001)
        self.assertEqual(sw.SW_HIDE, 0)
        self.assertTrue(hasattr(sw._STARTUPINFO, "lpDesktop"))

    def test_canonical_root_grants_a_real_directory(self):
        api = mock.Mock()
        api.get_long_path_name.side_effect = lambda value: value
        api.get_drive_type.return_value = 3
        with tempfile.TemporaryDirectory() as tmp:
            root = sw.canonical_root(tmp, api)
            self.assertTrue(os.path.isdir(root))
            # Case is preserved on return; only the containment comparison
            # is normcased.
            self.assertEqual(os.path.normcase(root),
                             os.path.normcase(os.path.realpath(tmp)))


class StdoutPumpTests(unittest.TestCase):
    def test_reads_chunks_then_eof(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'{"type":"final","text":"ok"}\n')
        os.close(write_fd)
        stream = os.fdopen(read_fd, "rb", buffering=0)
        pump = harness._StdoutPump(stream)
        try:
            chunks = []
            while True:
                chunk = pump.read(2.0)
                if chunk is None:
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            pump.close()
            stream.close()
        self.assertEqual(b"".join(chunks), b'{"type":"final","text":"ok"}\n')

    def test_idle_pipe_returns_none(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        pump = harness._StdoutPump(stream)
        try:
            self.assertIsNone(pump.read(0.05))
        finally:
            os.close(write_fd)
            pump.close()
            stream.close()


if __name__ == "__main__":
    unittest.main()
