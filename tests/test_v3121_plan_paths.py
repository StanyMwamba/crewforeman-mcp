"""S3 3.12.1: PLAN paths declared relative to a product subfolder.

The workspace may hold the product in a ``repo/`` subfolder, so the observed
diff paths are ``repo/src/a.py`` while ``PLAN.json`` declares ``src/a.py``.
``compare_plan()`` must normalise the common wrapper before comparing, while
keeping the real observed path in mismatch records and never weakening the
comparison when there is no product prefix.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from deepseek_mcp import pipeline, plan_contract


def _actual(created=None, modified=None, deleted=None, root=None):
    actual = {
        "created": list(created or []),
        "modified": list(modified or []),
        "deleted": list(deleted or []),
    }
    if root is not None:
        actual["_root"] = root
    return actual


def _plan(files=None, tests=None, write_set=None):
    return {
        "files_expected": list(files or []),
        "tests_expected": list(tests or []),
        "write_set": list(write_set or []),
    }


class ProductSubfolderTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plan-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        # The workspace wraps a checkout named repo/; the workspace itself is
        # not a checkout.  This is the layout that produced the 3.12.0
        # "plan: 40 mismatch (missing_file, missing_test)" note.
        os.makedirs(os.path.join(self.root, "repo", ".git"))
        os.makedirs(os.path.join(self.root, "repo", "src"))
        os.makedirs(os.path.join(self.root, "repo", "tests"))

    def test_wrapped_paths_match_product_relative_plan(self):
        actual = _actual(
            created=["repo/src/a.py", "repo/tests/test_a.py"],
            root=self.root,
        )
        plan = _plan(
            files=["src/a.py"],
            tests=["tests/test_a.py"],
            write_set=["src/"],
        )
        self.assertEqual(plan_contract.compare_plan(plan, actual), [])

    def test_direct_paths_still_match(self):
        actual = _actual(created=["src/a.py", "tests/test_a.py"],
                         root=self.root)
        plan = _plan(
            files=["src/a.py"],
            tests=["tests/test_a.py"],
            write_set=["src/"],
        )
        self.assertEqual(plan_contract.compare_plan(plan, actual), [])

    def test_missing_file_still_reported_with_prefix(self):
        actual = _actual(
            created=["repo/src/a.py", "repo/tests/test_a.py"],
            root=self.root,
        )
        plan = _plan(
            files=["src/a.py", "src/missing.py"],
            tests=["tests/test_a.py"],
            write_set=["src/"],
        )
        mismatches = plan_contract.compare_plan(plan, actual)
        self.assertEqual(
            [m for m in mismatches if m["type"] == "missing_file"],
            [{
                "type": "missing_file",
                "path": "src/missing.py",
                "detail": "declared file was not changed",
            }],
        )

    def test_outside_write_set_reported_with_real_observed_path(self):
        actual = _actual(
            created=["repo/src/a.py", "repo/tests/test_a.py",
                     "repo/other/b.py"],
            root=self.root,
        )
        plan = _plan(
            files=["src/a.py"],
            tests=["tests/test_a.py"],
            write_set=["src/"],
        )
        mismatches = plan_contract.compare_plan(plan, actual)
        self.assertEqual(
            [m for m in mismatches if m["type"] == "unexpected_file"],
            [{
                "type": "unexpected_file",
                "path": "repo/other/b.py",
                "detail": "not declared",
            }],
        )

    def test_root_level_file_disables_normalisation(self):
        actual = _actual(created=["repo/src/a.py", "setup.py"],
                         root=self.root)
        plan = _plan(
            files=["src/a.py"],
            tests=[],
            write_set=["src/"],
        )
        mismatches = plan_contract.compare_plan(plan, actual)
        types = [m["type"] for m in mismatches]
        self.assertIn("missing_file", types)
        unexpected = [m for m in mismatches if m["type"] == "unexpected_file"]
        self.assertEqual(
            sorted(m["path"] for m in unexpected),
            ["repo/src/a.py", "setup.py"],
        )
        # The prefix was not stripped, so the declared file is reported.
        missing = [m for m in mismatches if m["type"] == "missing_file"]
        self.assertEqual([m["path"] for m in missing], ["src/a.py"])

    def test_a_lone_package_directory_is_not_a_product_root(self):
        # Workspace root IS the checkout, so a plan that drops the package
        # directory must not be normalised into a match.
        os.makedirs(os.path.join(self.root, ".git"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "deepseek_mcp"), exist_ok=True)
        actual = _actual(created=["deepseek_mcp/gate.py"], root=self.root)
        plan = _plan(files=["gate.py"], write_set=["gate.py"])
        mismatches = plan_contract.compare_plan(plan, actual)
        self.assertEqual([m["type"] for m in mismatches],
                         ["missing_file", "unexpected_file"])
        self.assertEqual([m["path"] for m in mismatches],
                         ["gate.py", "deepseek_mcp/gate.py"])

    def test_no_root_evidence_means_no_normalisation(self):
        # Without a known workspace root there is no evidence that repo/ is a
        # product wrapper, so the exact path must be declared.
        actual = _actual(created=["repo/src/a.py"])
        plan = _plan(files=["src/a.py"], write_set=["src/"])
        mismatches = plan_contract.compare_plan(plan, actual)
        self.assertIn("missing_file", [m["type"] for m in mismatches])


class NotePlanWriteSetTest(unittest.TestCase):
    """The pipeline's PLAN -> write-set gate uses the same normalisation."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plan-note-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.plan_path = os.path.join(self.root, "PLAN.json")

    def _run(self, write_set):
        return {"pipeline": {
            "write_set": list(write_set),
            "write_set_source": "arg",
            "write_set_impl": "builtin",
        }}

    def _write_plan(self, files):
        with open(self.plan_path, "w", encoding="utf-8") as handle:
            json.dump({"files_expected": files}, handle)

    def test_workspace_write_set_matches_product_relative_plan(self):
        self._write_plan(["src/a.py"])
        pipe = pipeline.note_plan(self._run(["repo/src/"]), self.plan_path)
        self.assertEqual(pipe["plan_files_outside_write_set"], [])

    def test_unrelated_plan_file_stays_outside(self):
        self._write_plan(["other/a.py"])
        pipe = pipeline.note_plan(self._run(["repo/src/"]), self.plan_path)
        self.assertEqual(pipe["plan_files_outside_write_set"], ["other/a.py"])

    def test_plan_already_workspace_relative_is_not_stripped(self):
        self._write_plan(["repo/src/a.py"])
        pipe = pipeline.note_plan(self._run(["repo/src/"]), self.plan_path)
        self.assertEqual(pipe["plan_files_outside_write_set"], [])


if __name__ == "__main__":
    unittest.main()
