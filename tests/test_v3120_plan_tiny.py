"""S4 tiny PLAN.json contract tests."""
import json
import os
import tempfile
import types
import unittest

from deepseek_mcp import plan_contract


def _cfg(**overrides):
    base = {
        "plan_contract": "block",
        "plan_file": "PLAN.json",
        "plan_optional_max_files": 1,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _write_plan(root, data):
    with open(os.path.join(root, "PLAN.json"), "w", encoding="utf-8") as handle:
        if isinstance(data, str):
            handle.write(data)
        else:
            json.dump(data, handle)


def _valid_plan():
    return {
        "version": 1,
        "task_type": "code",
        "files_expected": ["src/b.py"],
        "tests_expected": [],
        "risks": [],
        "write_set": [],
    }


class TinyPlanTests(unittest.TestCase):
    def _check(self, root, cfg, before, after):
        return plan_contract.check_plan_contract(
            root, cfg, {}, before, after, None, "code")

    def test_absent_tiny_is_optional(self):
        with tempfile.TemporaryDirectory() as root:
            record = self._check(
                root, _cfg(), {},
                {"src/a.py": (10, 10)})
            self.assertEqual(record["status"], "optional")
            self.assertEqual(record["reason"], "tiny_job_absent_plan")
            self.assertEqual(record["mismatches"], [])
            self.assertFalse(record["blocking"])
            self.assertEqual(record["note"], "plan: optional (tiny job)")
            self.assertEqual(record["plan_read_status"], "absent")
            self.assertTrue(record["actual"]["tiny"])
            self.assertEqual(record["actual"]["product_changes"], 1)

    def test_invalid_tiny_is_optional(self):
        with tempfile.TemporaryDirectory() as root:
            _write_plan(root, "{")
            record = self._check(
                root, _cfg(), {},
                {"PLAN.json": (10, 10), "src/a.py": (10, 10)})
            self.assertEqual(record["status"], "optional")
            self.assertEqual(record["reason"], "tiny_job_invalid_plan")
            self.assertFalse(record["blocking"])
            self.assertEqual(record["mismatches"], [])

    def test_snapshot_absent_tiny_is_optional(self):
        with tempfile.TemporaryDirectory() as root:
            _write_plan(root, _valid_plan())
            record = self._check(
                root, _cfg(), {},
                {"src/a.py": (10, 10)})
            self.assertEqual(record["status"], "optional")
            self.assertEqual(record["reason"],
                             "tiny_job_plan_not_in_snapshot")
            self.assertEqual(record["plan_read_status"], "ok")
            self.assertFalse(record["blocking"])

    def test_valid_plan_still_compared_and_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            _write_plan(root, _valid_plan())
            record = self._check(
                root, _cfg(), {},
                {"PLAN.json": (10, 10), "src/a.py": (10, 10)})
            self.assertEqual(record["status"], "mismatch")
            self.assertTrue(record["mismatches"])
            self.assertTrue(record["blocking"])
            self.assertIsNotNone(plan_contract.plan_defect(record))

    def test_threshold_zero_disables_optional(self):
        with tempfile.TemporaryDirectory() as root:
            record = self._check(
                root, _cfg(plan_optional_max_files=0), {},
                {"src/a.py": (10, 10)})
            self.assertEqual(record["status"], "absent")
            self.assertTrue(record["blocking"])
            self.assertFalse(record["actual"]["tiny"])

    def test_two_product_changes_are_not_tiny(self):
        with tempfile.TemporaryDirectory() as root:
            record = self._check(
                root, _cfg(), {},
                {"src/a.py": (10, 10), "src/b.py": (10, 10)})
            self.assertEqual(record["status"], "absent")
            self.assertTrue(record["blocking"])

    def test_artifacts_do_not_inflate_tiny_count(self):
        with tempfile.TemporaryDirectory() as root:
            record = self._check(
                root, _cfg(), {},
                {
                    "src/a.py": (10, 10),
                    "README.md": (10, 10),
                    "logs/run.log": (10, 10),
                    "cache/x.bin": (10, 10),
                    "fix.patch": (10, 10),
                })
            self.assertEqual(record["status"], "optional")
            self.assertEqual(record["reason"], "tiny_job_absent_plan")
            self.assertEqual(record["actual"]["product_changes"], 1)
            self.assertTrue(record["actual"]["tiny"])
            self.assertGreaterEqual(
                record["actual"]["ignored_count"], 4)


if __name__ == "__main__":
    unittest.main()
