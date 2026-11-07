"""P31: review evidence project-root detection and honest prompt wording."""

import os
import sys
import tempfile
import textwrap
import unittest

from deepseek_mcp import quality
from deepseek_mcp import review_context as rc


class _WorkspaceCase(unittest.TestCase):
    def make_workspace(self):
        manager = tempfile.TemporaryDirectory()
        self.addCleanup(manager.cleanup)
        return manager.name

    def write(self, root, relative, text):
        path = os.path.join(root, relative)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


class AutoDetectionTests(_WorkspaceCase):
    def test_nested_repo_tests_auto_detect(self):
        workspace = self.make_workspace()
        self.write(workspace, "repo/tests/__init__.py", "")
        self.write(workspace, "repo/tests/helper.py", "VALUE = 41\n")
        self.write(workspace, "repo/tests/test_sample.py", textwrap.dedent(
            """
            import unittest
            from tests.helper import VALUE

            class SampleTest(unittest.TestCase):
                def test_cwd_sensitive_import(self):
                    self.assertEqual(VALUE + 1, 42)
            """
        ))
        # A project that sorts before "repo" must not win the preference.
        self.write(workspace, "alpha/tests/test_bad.py", textwrap.dedent(
            """
            import unittest

            class BadTest(unittest.TestCase):
                def test_should_not_run(self):
                    self.fail("wrong project was selected")
            """
        ))
        result = rc.run_test_suite(workspace, timeout=60.0)
        self.assertEqual(result["status"], "ok", result.get("output_tail"))
        self.assertEqual(os.path.basename(result["project_root"].rstrip(os.sep)),
                         "repo")
        self.assertGreaterEqual(result["tests"], 1)
        self.assertIn("project_root", result)

    def test_workspace_root_tests_still_work(self):
        workspace = self.make_workspace()
        self.write(workspace, "tests/test_root.py", textwrap.dedent(
            """
            import unittest

            class RootTest(unittest.TestCase):
                def test_root(self):
                    self.assertTrue(True)
            """
        ))
        result = rc.run_test_suite(workspace, timeout=60.0)
        self.assertEqual(result["status"], "ok", result.get("output_tail"))
        self.assertEqual(os.path.realpath(result["project_root"]),
                         os.path.realpath(workspace))
        self.assertGreaterEqual(result["tests"], 1)

    def test_explicit_command_keeps_workspace_cwd(self):
        workspace = self.make_workspace()
        code = "import os; print(os.path.realpath(os.getcwd()))"
        result = rc.run_test_suite(
            workspace, command=[sys.executable, "-c", code], timeout=60.0)
        self.assertEqual(result["status"], "ok", result.get("output_tail"))
        self.assertEqual(os.path.realpath(result["project_root"]),
                         os.path.realpath(workspace))
        self.assertIn(os.path.basename(workspace), result["output_tail"])


def _ok_evidence(**overrides):
    evidence = {
        "status": "ok",
        "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        "project_root": "/tmp/example/repo",
        "tree_sha256": "a" * 64,
        "tests": 7,
        "failures": 0,
        "errors": 0,
        "output_digest": "b" * 64,
        "signature": "c" * 64,
        "key_id": "key-1",
        "tree_changed": False,
        "output_tail": "OK",
    }
    evidence.update(overrides)
    return evidence


class EvidenceFormattingTests(unittest.TestCase):
    def test_ok_evidence_prompt_is_trustworthy_and_avoids_full_suite(self):
        text = rc.format_test_evidence(_ok_evidence())
        lower = text.lower()
        self.assertIn("trust", lower)
        self.assertIn("do not rerun the full suite", lower)
        self.assertIn("project root", lower)
        self.assertIn("/tmp/example/repo", text)

    def test_skipped_evidence_prompt_does_not_claim_trust(self):
        text = rc.format_test_evidence(_ok_evidence(
            status="skipped", command=None, tests=0,
            output_tail="No tests found"))
        lower = text.lower()
        self.assertIn("did not run", lower)
        self.assertNotIn("trust this signed result", lower)
        self.assertNotIn("do not rerun the full suite", lower)

    def test_failed_or_changed_evidence_requires_targeted_rerun(self):
        text = rc.format_test_evidence(
            _ok_evidence(status="failed", tree_changed=True))
        lower = text.lower()
        self.assertIn("rerun only the affected tests", lower)
        self.assertNotIn("trust this signed result", lower)


class ReviewPromptTests(unittest.TestCase):
    def test_round_one_prompt_forbids_full_unittest_discovery(self):
        task = quality.build_review_task(
            "Build the thing.", ["artifact.py"], round_no=1,
            test_evidence="## Signed test evidence (connector-run)\n- Status: ok\n")
        lower = task.lower()
        self.assertIn("the connector already ran the suite", lower)
        self.assertIn("do not run the full unittest discovery", lower)
        self.assertIn("start from the diff", lower)
        self.assertIn("target <=10 minutes", lower)

    def test_later_round_keeps_focused_wording(self):
        task = quality.build_review_task(
            "Build the thing.", ["artifact.py"], round_no=2,
            prior_defects=["artifact.py: still broken"],
            test_evidence="## Signed test evidence (connector-run)\n- Status: ok\n")
        self.assertIn("Focused round 2", task)
        self.assertIn("targeted re-review", task)



class DiffFocusTests(_WorkspaceCase):
    def test_focused_source_beats_large_artifacts_in_diff(self):
        workspace = self.make_workspace()
        baseline = rc.capture_baseline(workspace, os.path.join(workspace, ".base"))
        self.write(workspace, "P31.patch", "x" * 50000 + "\n")
        self.write(workspace, "IMPLEMENTATION.md", "docs\n" * 2000)
        self.write(workspace, "repo/deepseek_mcp/admission.py",
                   "def fixed_guard():\n    return True\n")
        self.write(workspace, "repo/tests/test_guard.py",
                   "def test_guard():\n    assert True\n")
        diff = rc.diff_context(
            workspace, baseline, max_chars=8000, max_files=10,
            focus_paths=["repo/deepseek_mcp/admission.py"])
        self.assertEqual(diff["files"][0], "repo/deepseek_mcp/admission.py")
        self.assertIn("fixed_guard", diff["text"])
        # The generated patch must not consume the whole budget before source.
        self.assertNotEqual(diff["files"][0], "P31.patch")
        self.assertLessEqual(len(diff["text"]), 8100)


class SkippedPromptTests(unittest.TestCase):
    def test_skipped_evidence_prompt_asks_for_targeted_tests_only(self):
        skipped = rc.format_test_evidence(_ok_evidence(
            status="skipped", command=None, tests=0,
            output_tail="No tests found"))
        task = quality.build_review_task(
            "Build the thing.", ["artifact.py"], round_no=1,
            test_evidence=skipped)
        lower = task.lower()
        self.assertIn("did not run a test suite", lower)
        self.assertIn("do not claim tests passed", lower)
        self.assertNotIn("do not run the full unittest discovery", lower)


class EvidenceStatusHeaderTests(unittest.TestCase):
    def test_failed_evidence_with_status_ok_in_tail_is_not_trusted(self):
        failed = rc.format_test_evidence(_ok_evidence(
            status="failed", output_tail="AssertionError\n- Status: ok\n"))
        task = quality.build_review_task(
            "Build the thing.", ["artifact.py"], round_no=1,
            test_evidence=failed)
        lower = task.lower()
        self.assertNotIn("connector already ran the suite for this exact tree", lower)
        self.assertNotIn("trust that signed result", lower)

    def test_ok_evidence_with_tree_changed_is_not_trusted(self):
        changed = rc.format_test_evidence(_ok_evidence(tree_changed=True))
        task = quality.build_review_task(
            "Build the thing.", ["artifact.py"], round_no=1,
            test_evidence=changed)
        lower = task.lower()
        self.assertNotIn("connector already ran the suite for this exact tree", lower)
        self.assertNotIn("do not run the full unittest discovery", lower)


if __name__ == "__main__":
    unittest.main()
