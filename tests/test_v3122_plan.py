"""Focused tests for the 3.13.0 plan bookkeeping and root write-set fixes."""

import json
import os
import tempfile
import unittest

from deepseek_mcp import plan_contract as pc


def snap(files):
    """Build a safety.snapshot_workspace-shaped mapping with injected mtimes."""
    return {
        "files": {
            path: {"mtime": mtime, "size": size}
            for path, (mtime, size) in files.items()
        }
    }


def write_plan(workspace, data, mtime=1000.0):
    path = os.path.join(workspace, "PLAN.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.utime(path, (mtime, mtime))


class DeclaredBookkeepingTests(unittest.TestCase):
    def test_declared_plan_json_is_not_missing(self):
        plan = {
            "version": 1,
            "files_expected": ["src/a.py", "PLAN.json"],
            "tests_expected": [],
            "risks": [],
            "write_set": [],
        }
        self.assertEqual(pc.compare_plan(plan, {"created": ["src/a.py"]}), [])

    def test_declared_report_files_are_ignored(self):
        plan = {
            "version": 1,
            "files_expected": [
                "QUALITY_REPORT.md",
                "REVIEW.md",
                "WASTE_REPORT.md",
                "PLAN.json",
            ],
            "tests_expected": [],
            "risks": [],
            "write_set": [],
        }
        self.assertEqual(pc.compare_plan(plan, {"created": []}), [])

    def test_actual_plan_json_is_ignored(self):
        actual = pc.actual_changes(
            "", {}, {"PLAN.json": (1000.0, 10)}, {}
        )
        self.assertEqual(actual["created"], [])
        self.assertEqual(actual["modified"], [])
        self.assertEqual(actual["deleted"], [])
        self.assertEqual(actual["ignored"], ["PLAN.json"])


class RootWriteSetTests(unittest.TestCase):
    def test_root_write_set_validates(self):
        normalised, errors = pc.validate_plan(
            {"version": 1, "write_set": ["."]}, {}
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalised["write_set"], ["."])

        normalised, errors = pc.validate_plan(
            {"version": 1, "write_set": ["./"]}, {}
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalised["write_set"], ["."])

    def test_root_write_set_covers_src_file(self):
        plan = {
            "version": 1,
            "files_expected": [],
            "tests_expected": [],
            "risks": [],
            "write_set": ["."],
        }
        self.assertEqual(pc.compare_plan(plan, {"created": ["src/a.py"]}), [])

    def test_dot_files_expected_still_invalid(self):
        normalised, errors = pc.validate_plan(
            {"version": 1, "files_expected": ["."]}, {}
        )
        self.assertIn("files_expected: invalid path", errors)
        self.assertEqual(normalised["files_expected"], [])


class CheckContractTests(unittest.TestCase):
    def test_push_like_artifact_only_plan_is_ok(self):
        with tempfile.TemporaryDirectory() as workspace:
            plan = {
                "version": 1,
                "task_type": "code",
                "files_expected": [
                    "QUALITY_REPORT.md",
                    "REVIEW.md",
                    "WASTE_REPORT.md",
                    "PLAN.json",
                    "logs/run.log",
                ],
                "tests_expected": [],
                "risks": [],
                "write_set": ["."],
            }
            write_plan(workspace, plan, 1000.0)
            after = snap(
                {
                    "PLAN.json": (1000.0, 10),
                    "src/a.py": (1001.0, 4),
                    "QUALITY_REPORT.md": (1002.0, 40),
                    "REVIEW.md": (1003.0, 40),
                    "WASTE_REPORT.md": (1004.0, 40),
                    "logs/run.log": (1005.0, 40),
                }
            )
            result = pc.check_plan_contract(
                workspace,
                {"plan_contract": "warn", "plan_optional_max_files": 0},
                None,
                {},
                after,
                990.0,
                "code",
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["mismatch_count"], 0)
            self.assertEqual(result["note"], "plan: ok")

    def test_real_missing_product_file_still_reported(self):
        plan = {
            "version": 1,
            "files_expected": ["src/a.py", "src/b.py"],
            "tests_expected": [],
            "risks": [],
            "write_set": ["src"],
        }
        mismatches = pc.compare_plan(plan, {"created": ["src/a.py"]})
        self.assertEqual(
            [(item["type"], item["path"]) for item in mismatches],
            [("missing_file", "src/b.py")],
        )


if __name__ == "__main__":
    unittest.main()
