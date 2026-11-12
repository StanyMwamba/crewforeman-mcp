"""v3.14.2 slice D: secret-scan vendor skips, git listing and time budget."""
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from deepseek_mcp import gate

# Synthetic material is assembled at runtime so this test file never contains
# a contiguous credential-shaped literal (the repository-wide scan checks it).
_SK_PREFIX = "sk-"
_SK_BODY = "abcdefgh" + "12345678"
_FAKE_SK = _SK_PREFIX + _SK_BODY
_FAKE_PASSWORD = "hunter2" + "hunter2"
_FAKE_ASSIGNMENT = "password = '" + _FAKE_PASSWORD + "'\n"


def _cfg(**overrides):
    base = {
        "gate_secret_scan": True,
        "gate_timeout_seconds": 30,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _write(root, rel, text):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    return full


class VendorSkipTests(unittest.TestCase):
    def test_vendor_directories_are_skipped_at_any_depth(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            for rel in (
                ".uv-python/lib/site.py",
                ".venv/lib/python/site.py",
                "venv/lib/site.py",
                "node_modules/pkg/index.js",
                ".tox/lib/site.py",
                "lib/python3.12/site-packages/pkg/mod.py",
                ".eggs/pkg/mod.py",
                ".nox/pkg/mod.py",
                ".pixi/env/mod.py",
                "nested/.venv/lib/mod.py",
                "nested/node_modules/pkg/index.js",
            ):
                _write(root, rel, _FAKE_ASSIGNMENT)
            # Force the os.walk fallback so this covers directory pruning,
            # not just filtering of a flat git path list.
            with mock.patch.object(gate, "_git_enumeration",
                                   return_value=(None, False)):
                report = gate.scan_tree_report(_cfg(), root)
            self.assertEqual(report["findings"], [], report["findings"])
            self.assertFalse(report["timed_out"])
            self.assertEqual(report["source"], "walk")

    def test_explicit_scan_files_still_scans_named_vendor_file(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            _write(root, ".venv/lib/site.py", _FAKE_ASSIGNMENT)
            findings = gate.scan_files(
                _cfg(), root, [".venv/lib/site.py"])
            self.assertEqual(len(findings), 1, findings)
            self.assertEqual(findings[0]["path"], ".venv/lib/site.py")

    def test_explicit_scan_files_scans_named_product_file(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", _FAKE_ASSIGNMENT)
            findings = gate.scan_files(_cfg(), root, ["app.py"])
            self.assertEqual(len(findings), 1, findings)
            self.assertEqual(findings[0]["path"], "app.py")


class GitEnumerationTests(unittest.TestCase):
    def test_git_repo_skips_ignored_file_and_scans_tracked_plus_untracked(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as root:
            _write(root, "tracked.py", _FAKE_ASSIGNMENT)
            _write(root, "untracked.py", "y = 2\n")
            _write(root, "ignored.txt", _FAKE_ASSIGNMENT)
            _write(root, ".gitignore", "ignored.txt\n")
            subprocess.run([git, "init", "-q"], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20)
            subprocess.run(
                [git, "-C", root, "add", ".gitignore", "tracked.py",
                 "untracked.py"],
                check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=20)
            report = gate.scan_tree_report(_cfg(), root)
            self.assertEqual(report["source"], "git")
            paths = [item["path"] for item in report["findings"]]
            self.assertIn("tracked.py", paths)
            self.assertNotIn("ignored.txt", paths)
            self.assertNotIn("untracked.py", paths)
            # Tracked but clean, and untracked-not-ignored, are both listed.
            self.assertEqual(report["files_total"], 3, report)


    def test_root_inside_an_outer_repo_falls_back_to_walk(self):
        # A disposable test root is often a subdirectory of an outer git
        # work tree and ignored by it.  ls-files at that root returns an empty
        # list with rc=0, so it must not be trusted as a clean tree scan.
        git = shutil.which("git")
        if not git:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as outer:
            inner = os.path.join(outer, "ignored", "workspace")
            os.makedirs(inner)
            _write(inner, "app.py", _FAKE_ASSIGNMENT)
            _write(outer, ".gitignore", "ignored/\n")
            subprocess.run([git, "init", "-q"], cwd=outer, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20)
            subprocess.run([git, "-C", outer, "add", ".gitignore"],
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
            report = gate.scan_tree_report(_cfg(), inner)
            self.assertEqual(report["source"], "walk", report)
            self.assertEqual(len(report["findings"]), 1, report)


class BudgetTests(unittest.TestCase):
    def test_tiny_budget_on_many_files_reports_timeout_without_raising(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(200):
                _write(root, "mod_%03d.py" % index, "x = 1\n")
            report = gate.scan_tree_report(
                _cfg(gate_secret_scan_budget_seconds=0.0), root)
            self.assertTrue(report["timed_out"], report)
            self.assertTrue(report["truncated"], report)
            self.assertEqual(report["findings"], [])

    def test_fresh_gate_scan_skips_vendor_tree_without_a_baseline(self):
        # The gate's no-baseline path scans the whole tree; it must use the
        # same vendor pruning as scan_tree_report.
        with tempfile.TemporaryDirectory() as root:
            _write(root, ".venv/lib/site.py", _FAKE_ASSIGNMENT)
            _write(root, "node_modules/pkg/index.js", _FAKE_ASSIGNMENT)
            _write(root, "app.py", "x = 1\n")
            result = gate._secret_check(_cfg(), root, None, None, None)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["findings"], [])
            self.assertGreater(result["files_total"], 0, result)

    def test_secret_check_uses_default_budget_and_reports_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            paths = []
            for index in range(50):
                rel = "mod_%03d.py" % index
                _write(root, rel, "x = 1\n")
                paths.append(rel)
            result = gate._secret_check(
                _cfg(gate_secret_scan_budget_seconds=0.0), root,
                None, None, None, only_paths=paths)
            self.assertEqual(result["status"], "timed_out", result)
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["truncated"])
            self.assertLessEqual(result["files_scanned"], len(paths))



    def test_secret_check_without_diff_uses_budgeted_tree_scan(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            _write(root, ".venv/lib/site.py", _FAKE_ASSIGNMENT)
            result = gate._secret_check(_cfg(), root, None, None, None)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["scan_source"], "walk", result)
            self.assertGreaterEqual(result["files_scanned"], 1)
            self.assertEqual(result["findings"], [])

    def test_secret_check_tree_scan_reports_expired_budget(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(100):
                _write(root, "mod_%03d.py" % index, "x = 1\n")
            result = gate._secret_check(
                _cfg(gate_secret_scan_budget_seconds=0.0),
                root, None, None, None)
            self.assertEqual(result["status"], "timed_out", result)
            self.assertTrue(result["truncated"])



    def test_existing_baseline_with_no_changes_does_not_rescan_old_files(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "old.py", _FAKE_ASSIGNMENT)
            # A baseline exists (even an empty one) and nothing changed: an
            # old untouched finding must not be attributed to this builder.
            result = gate._secret_check(_cfg(), root, {}, {}, None)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["files_total"], 0)
            self.assertEqual(result["files_scanned"], 0)
            self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
