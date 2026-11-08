"""Focused tests for the 3.11.0 F2 written-predictions contract."""

import json
import os
import tempfile
import unittest

from deepseek_mcp import plan_contract as pc


def snap(files):
    """Build a safety.snapshot_workspace-shaped mapping with injected mtimes."""
    return {"files": {path: {"mtime": mtime, "size": size} for path, (mtime, size) in files.items()}}


def write_plan(workspace, data, mtime=1000.0):
    path = os.path.join(workspace, "PLAN.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.utime(path, (mtime, mtime))


class ValidateTests(unittest.TestCase):
    def test_risk_normalisation(self):
        normalised, errors = pc.validate_plan(
            {
                "version": 1,
                "risks": ["flaky fixture", {"risk": "race", "mitigation": "lock"}],
            },
            {},
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            normalised["risks"],
            [
                {"risk": "flaky fixture", "mitigation": ""},
                {"risk": "race", "mitigation": "lock"},
            ],
        )

    def test_rejects_absolute_parent_and_drive(self):
        normalised, errors = pc.validate_plan(
            {
                "version": 1,
                "files_expected": ["/etc/passwd", "../up.py", "C:\\temp\\x.py", ".\\a\\b.py"],
            },
            {},
        )
        self.assertEqual(normalised["files_expected"], ["a/b.py"])
        self.assertEqual(len(errors), 3)

    def test_normalises_dot_backslash_paths(self):
        normalised, errors = pc.validate_plan(
            {"version": 1, "files_expected": [".\\a\\b.py", "./c//d.py"]}, {}
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalised["files_expected"], ["a/b.py", "c/d.py"])

    def test_caps_and_truncated(self):
        normalised, errors = pc.validate_plan(
            {
                "version": 1,
                "files_expected": ["a.py", "b.py", "c.py"],
                "tests_expected": ["tests/test_a.py"],
                "risks": ["one risk", "two risk"],
            },
            {"plan_max_files": 2, "plan_max_risks": 1},
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalised["files_expected"], ["a.py", "b.py"])
        self.assertEqual(normalised["tests_expected"], [])
        self.assertEqual(len(normalised["risks"]), 1)
        self.assertTrue(normalised["truncated"])


class CompareTests(unittest.TestCase):
    def test_glob_single_vs_double_star(self):
        plan = {
            "version": 1,
            "files_expected": ["src/*.py", "lib/**.py"],
            "tests_expected": [],
            "risks": [],
            "write_set": [],
        }
        actual = {"created": ["src/a.py", "src/sub/b.py", "lib/x/y.py"]}
        mismatches = pc.compare_plan(plan, actual)
        self.assertEqual(
            mismatches,
            [
                {
                    "type": "unexpected_file",
                    "path": "src/sub/b.py",
                    "detail": "not declared",
                }
            ],
        )

    def test_unexpected_and_missing(self):
        plan = {
            "version": 1,
            "files_expected": ["src/a.py", "src/b.py"],
            "tests_expected": [],
            "risks": [],
            "write_set": [],
        }
        actual = {"created": ["src/a.py", "src/x.py"]}
        kinds = {(m["type"], m.get("path")) for m in pc.compare_plan(plan, actual)}
        self.assertIn(("missing_file", "src/b.py"), kinds)
        self.assertIn(("unexpected_file", "src/x.py"), kinds)

    def test_missing_and_unplanned_tests(self):
        plan = {
            "version": 1,
            "files_expected": ["src/a.py"],
            "tests_expected": ["tests/test_a.py"],
            "risks": [],
            "write_set": [],
        }
        actual = {"created": ["src/a.py", "tests/test_b.py"]}
        kinds = {(m["type"], m["path"]) for m in pc.compare_plan(plan, actual)}
        self.assertIn(("missing_test", "tests/test_a.py"), kinds)
        self.assertIn(("unplanned_test", "tests/test_b.py"), kinds)

    def test_write_set_segment_boundary(self):
        plan = {
            "version": 1,
            "files_expected": [],
            "tests_expected": [],
            "risks": [],
            "write_set": ["src"],
        }
        actual = {"created": ["src/a.py", "src2/b.py"]}
        self.assertEqual(
            [m["path"] for m in pc.compare_plan(plan, actual)], ["src2/b.py"]
        )
        plan["write_set"] = ["src/"]
        self.assertEqual(
            [m["path"] for m in pc.compare_plan(plan, actual)], ["src2/b.py"]
        )

    def test_risk_hit_path_and_text(self):
        with tempfile.TemporaryDirectory() as workspace:
            auth = os.path.join(workspace, "src")
            os.makedirs(auth)
            with open(os.path.join(auth, "auth.py"), "w", encoding="utf-8") as handle:
                handle.write("print('hello')\n")
            with open(os.path.join(auth, "body.py"), "w", encoding="utf-8") as handle:
                handle.write("a flaky fixture lives here\n")
            after = snap(
                {
                    "PLAN.json": (1000.0, 10),
                    "src/auth.py": (1001.0, 14),
                    "src/body.py": (1002.0, 25),
                }
            )
            actual = pc.actual_changes(workspace, {}, after, {})
            plan = {
                "version": 1,
                "files_expected": ["src/auth.py", "src/body.py"],
                "tests_expected": [],
                "risks": ["auth", "flaky"],
                "write_set": [],
            }
            hits = [m for m in pc.compare_plan(plan, actual) if m["type"] == "risk_hit"]
            self.assertEqual(len(hits), 2)
            details = {m["path"]: m["detail"] for m in hits}
            self.assertIn("risk keyword in path", details.values())
            self.assertIn("risk keyword in text", details.values())


class ActualChangesTests(unittest.TestCase):
    def test_plan_excluded_and_flat_snapshot(self):
        before = {"a.py": (1000.0, 1), "gone.py": (900.0, 2)}
        after = {"a.py": (1000.0, 5), "PLAN.json": (1100.0, 9), "new.py": (1001.0, 3)}
        actual = pc.actual_changes("", before, after, {})
        self.assertEqual(actual["created"], ["new.py"])
        self.assertEqual(actual["modified"], ["a.py"])
        self.assertEqual(actual["deleted"], ["gone.py"])
        self.assertEqual(actual["ignored"], ["PLAN.json"])
        self.assertAlmostEqual(actual["first_change_mtime"], 900.0)

    def test_nanosecond_snapshot_tuples(self):
        before = {"a.py": (1_700_000_000_000_000_000, 1)}
        after = {"a.py": (1_700_000_000_500_000_000, 1)}
        actual = pc.actual_changes("", before, after, {})
        self.assertEqual(actual["modified"], ["a.py"])
        self.assertAlmostEqual(actual["first_change_mtime"], 1_700_000_000.5)


class CheckTests(unittest.TestCase):
    def _result(self, workspace, after, mode="warn", started=990.0, plan_mtime=1000.0,
                plan=None):
        plan = plan or {
            "version": 1,
            "task_type": "code",
            "files_expected": ["src/a.py"],
            "tests_expected": [],
            "risks": [],
            "write_set": [],
        }
        write_plan(workspace, plan, plan_mtime)
        return pc.check_plan_contract(
            workspace,
            {"plan_contract": mode},
            None,
            {},
            after,
            started,
            "code",
        )

    def test_plan_before_first_edit_passes(self):
        with tempfile.TemporaryDirectory() as workspace:
            after = snap({"PLAN.json": (1000.0, 10), "src/a.py": (1100.0, 4)})
            result = self._result(workspace, after)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["mismatch_count"], 0)
            self.assertEqual(result["note"], "plan: ok")
            self.assertFalse(result["blocking"])

    def test_stale_plan_by_mtime(self):
        with tempfile.TemporaryDirectory() as workspace:
            after = snap({"PLAN.json": (900.0, 10), "src/a.py": (1100.0, 4)})
            result = self._result(workspace, after, plan_mtime=900.0)
            self.assertEqual(result["status"], "stale")
            self.assertIn("stale_plan", [m["type"] for m in result["mismatches"]])
            self.assertFalse(result["blocking"])

    def test_late_plan(self):
        with tempfile.TemporaryDirectory() as workspace:
            after = snap({"PLAN.json": (2000.0, 10), "src/a.py": (1000.0, 4)})
            result = self._result(workspace, after, plan_mtime=2000.0)
            self.assertEqual(result["status"], "mismatch")
            self.assertIn("late_plan", [m["type"] for m in result["mismatches"]])

    def test_warn_never_blocks(self):
        with tempfile.TemporaryDirectory() as workspace:
            after = snap(
                {"PLAN.json": (1000.0, 10), "src/a.py": (1001.0, 4), "src/x.py": (1002.0, 4)}
            )
            result = self._result(workspace, after, mode="warn")
            self.assertEqual(result["status"], "mismatch")
            self.assertFalse(result["blocking"])
            self.assertIsNone(result["blocking_defect"])
            self.assertIsNone(pc.plan_defect(result))
            self.assertIn("unexpected_file", result["note"])

    def test_block_returns_one_defect(self):
        with tempfile.TemporaryDirectory() as workspace:
            after = snap(
                {"PLAN.json": (1000.0, 10), "src/a.py": (1001.0, 4), "src/x.py": (1002.0, 4)}
            )
            result = self._result(workspace, after, mode="block")
            self.assertTrue(result["blocking"])
            defect = pc.plan_defect(result)
            self.assertEqual(defect, result["blocking_defect"])
            self.assertIsInstance(defect, str)
            self.assertTrue(defect.startswith("plan_contract:"))
            self.assertIn("unexpected_file", defect)

    def test_absent_in_both_modes(self):
        with tempfile.TemporaryDirectory() as workspace:
            for mode in ("warn", "block"):
                result = pc.check_plan_contract(
                    workspace, {"plan_contract": mode}, None, {}, {}, 990.0, "code"
                )
                self.assertEqual(result["status"], "absent")
                self.assertEqual(result["mismatches"][0]["type"], "absent_plan")
                self.assertEqual(result["blocking"], mode == "block")
                if mode == "block":
                    self.assertTrue(result["blocking_defect"].startswith("plan_contract:"))
                else:
                    self.assertIsNone(result["blocking_defect"])

    def test_invalid_in_both_modes(self):
        with tempfile.TemporaryDirectory() as workspace:
            write_plan(workspace, {"version": 2, "files_expected": ["src/a.py"]})
            after = snap({"PLAN.json": (1000.0, 10), "src/a.py": (1001.0, 4)})
            for mode in ("warn", "block"):
                result = pc.check_plan_contract(
                    workspace, {"plan_contract": mode}, None, {}, after, 990.0, "code"
                )
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["mismatches"][0]["type"], "invalid_plan")
                self.assertEqual(result["blocking"], mode == "block")

    def test_absent_from_snapshot_but_present_is_invalid(self):
        with tempfile.TemporaryDirectory() as workspace:
            after = snap({"src/a.py": (1001.0, 4)})
            result = self._result(workspace, after)
            self.assertEqual(result["status"], "invalid")
            self.assertIn("invalid_plan", [m["type"] for m in result["mismatches"]])

    def test_record_bounded_and_content_free(self):
        with tempfile.TemporaryDirectory() as workspace:
            os.makedirs(os.path.join(workspace, "src"))
            with open(os.path.join(workspace, "src", "a.py"), "w", encoding="utf-8") as handle:
                handle.write("TOPSECRETBODY should never be persisted\n")
            plan = {
                "version": 1,
                "files_expected": ["src/%s.py" % ("a" * 180) for _ in range(80)],
                "tests_expected": [],
                "risks": [{"risk": "r" * 280, "mitigation": "m" * 280} for _ in range(20)],
                "write_set": [],
            }
            after = snap({"PLAN.json": (1000.0, 10), "src/a.py": (1001.0, 40)})
            write_plan(workspace, plan, 1000.0)
            result = pc.check_plan_contract(
                workspace, {"plan_contract": "warn"}, None, {}, after, 990.0, "code"
            )
            encoded = json.dumps(result, ensure_ascii=False)
            self.assertLessEqual(len(encoded.encode("utf-8")), pc.MAX_RECORD_BYTES)
            self.assertNotIn("TOPSECRETBODY", encoded)
            self.assertNotIn(workspace, encoded)
            self.assertLessEqual(result["mismatch_count"], pc.MAX_MISMATCHES)
            self.assertLessEqual(len(result["declared"]["files_sample"]), pc.MAX_PATHS)

    def test_mode_off_returns_none(self):
        with tempfile.TemporaryDirectory() as workspace:
            result = pc.check_plan_contract(
                workspace, {"plan_contract": "off"}, None, {}, {}, 990.0, "code"
            )
            self.assertIsNone(result)
            self.assertEqual(pc.format_plan_note(result), "")


class PlanExclusionTests(unittest.TestCase):
    def test_plan_is_ignored_by_review_context(self):
        from deepseek_mcp import review_context
        self.assertTrue(review_context._ignored("PLAN.json"))
        self.assertTrue(review_context._ignored("plan.json"))
        self.assertFalse(review_context._ignored("src/plan.json.txt"))


if __name__ == "__main__":
    unittest.main()
