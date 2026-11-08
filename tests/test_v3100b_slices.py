"""Focused tests for deepseek_mcp.slices (item 6b)."""

import os
import tempfile
import unittest

from deepseek_mcp import slices


HEADING_TEXT = """# Team plan

## Wave 1 (2 builders)

### Builder 1 — alpha

Writes:
- `repo/deepseek_mcp/a.py`
- repo/tests/test_a.py

### Builder 2 — beta

Writes: repo/deepseek_mcp/b.py, repo/tests/test_b.py
"""

TABLE_TEXT = """# Team plan

| Slice | Wave | Writes |
| --- | --- | --- |
| Builder 1 | 1 | `repo/a.py`, repo/b.py |
| Builder 2 | 1 | repo/c.py |
"""

BUILD_SHAPE_TEXT = """# SLICES — wave plan and write-sets (BUILD test)

One wave of six builders.

## Wave 1 (6 builders)

### B1 — Item 2 review scope (module + tests)

Writes:
- `repo/deepseek_mcp/review_scope.py`
- `repo/tests/test_v3100b_review_scope.py`

### B2 — Item 6a project map (module + tests)

Writes:
- `repo/deepseek_mcp/project_map.py`
- `repo/tests/test_v3100b_project_map.py`

### B3 — Item 6b slices overlap (module + tests)

Writes:
- `repo/deepseek_mcp/slices.py`
- `repo/tests/test_v3100b_slices.py`

### B4 — Item 6c waste report (module + tests)

Writes:
- `repo/deepseek_mcp/waste_report.py`
- `repo/tests/test_v3100b_waste_report.py`

### B5 — Item 6d docs

Writes:
- `repo/README.md`
- `repo/docs/TEAMS.md`
- `repo/CHANGELOG.md`
- `repo/tests/test_v3100b_docs.py`

### B6 — Integration (quality/harness/store + tests)

Writes:
- `repo/deepseek_mcp/quality.py`
- `repo/deepseek_mcp/harness.py`
- `repo/deepseek_mcp/store.py`
- `repo/tests/test_v3100b_integration.py`

## Wave 2 (lead only, after the sub-leader summary)

The lead owns everything else.

Writes:
- repo/ fixes in files already touched by B1-B6

## Overlap check

| Wave | Slices | Shared file? | Result |
| ---: | --- | --- | --- |
| 1 | B1, B2, B3, B4, B5, B6 | None | PASS |
| 2 | lead | n/a | PASS |
"""


class ConstantsTests(unittest.TestCase):
    def test_public_constants(self):
        self.assertEqual(slices.NAME, "SLICES.md")
        self.assertEqual(slices.MAX_TEXT, 20000)


class HeadingFormatTests(unittest.TestCase):
    def test_heading_waves_and_write_sets(self):
        result = slices.parse_slices(HEADING_TEXT)
        self.assertEqual(result["waves"], 1)
        self.assertEqual([s["name"] for s in result["slices"]],
                         ["Builder 1 — alpha", "Builder 2 — beta"])
        self.assertEqual([s["wave"] for s in result["slices"]], [1, 1])
        self.assertEqual(result["slices"][0]["files"],
                         ["repo/deepseek_mcp/a.py", "repo/tests/test_a.py"])
        self.assertEqual(result["slices"][1]["files"],
                         ["repo/deepseek_mcp/b.py", "repo/tests/test_b.py"])
        self.assertEqual(result["overlaps"], [])
        self.assertEqual(result["warnings"], 0)

    def test_write_set_subheading_and_inline_variants(self):
        text = """## Wave 1

### Builder 1

Writes (repo/x.py, repo/y.py):

### Builder 2

#### Write-set
- ./repo/z.py
- repo//w.py

### Builder 3

Write set: ./repo/q.py; `repo/r.py`
"""
        result = slices.parse_slices(text)
        self.assertEqual(len(result["slices"]), 3)
        self.assertEqual(result["slices"][0]["files"], ["repo/x.py", "repo/y.py"])
        self.assertEqual(result["slices"][1]["files"], ["repo/z.py", "repo/w.py"])
        self.assertEqual(result["slices"][2]["files"], ["repo/q.py", "repo/r.py"])
        self.assertEqual(result["warnings"], 0)


class TableFormatTests(unittest.TestCase):
    def test_table_slices_and_writes(self):
        result = slices.parse_slices(TABLE_TEXT)
        self.assertEqual(result["waves"], 1)
        self.assertEqual([s["name"] for s in result["slices"]], ["Builder 1", "Builder 2"])
        self.assertEqual([s["wave"] for s in result["slices"]], [1, 1])
        self.assertEqual(result["slices"][0]["files"], ["repo/a.py", "repo/b.py"])
        self.assertEqual(result["slices"][1]["files"], ["repo/c.py"])
        self.assertEqual(result["warnings"], 0)

    def test_table_reordered_columns(self):
        text = """| Writes | Slice | Wave |
| --- | --- | --- |
| `repo/a.py` | B1 | 2 |
| repo/b.py | B2 | 2 |
"""
        result = slices.parse_slices(text)
        self.assertEqual([s["name"] for s in result["slices"]], ["B1", "B2"])
        self.assertEqual([s["wave"] for s in result["slices"]], [2, 2])
        self.assertEqual(result["slices"][0]["files"], ["repo/a.py"])


class OverlapTests(unittest.TestCase):
    def test_same_wave_overlap_is_one_warning(self):
        text = """## Wave 1

### Builder 1
Writes: ./repo/a.py, repo/b.py

### Builder 2
#### Write-set
- repo//a.py
- repo/c.py
"""
        result = slices.parse_slices(text)
        self.assertEqual(result["warnings"], 1)
        self.assertEqual(len(result["overlaps"]), 1)
        overlap = result["overlaps"][0]
        self.assertEqual(overlap["wave"], 1)
        self.assertEqual(overlap["file"], "repo/a.py")
        self.assertEqual(overlap["slices"], ["Builder 1", "Builder 2"])

    def test_table_same_wave_overlap(self):
        text = """| Slice | Wave | Writes |
| --- | --- | --- |
| Builder 1 | 1 | repo/a.py |
| Builder 2 | 1 | ./repo//a.py |
"""
        result = slices.parse_slices(text)
        self.assertEqual(result["warnings"], 1)
        self.assertEqual(result["overlaps"][0]["file"], "repo/a.py")

    def test_same_wave_duplicate_names_are_distinct_claimants(self):
        text = """## Wave 1

### Builder 1
Writes: repo/a.py

### Builder 1
Writes: ./repo//a.py
"""
        result = slices.parse_slices(text)
        self.assertEqual(result["warnings"], 1)
        self.assertEqual(len(result["overlaps"]), 1)
        self.assertEqual(result["overlaps"][0]["file"], "repo/a.py")
        self.assertEqual(result["overlaps"][0]["slices"],
                         ["Builder 1", "Builder 1"])

    def test_different_wave_reuse_is_allowed(self):
        text = """## Wave 1

### Builder 1
Writes: repo/shared.py

## Wave 2

### Builder 2
Writes: ./repo//shared.py
"""
        result = slices.parse_slices(text)
        self.assertEqual(result["warnings"], 0)
        self.assertEqual(result["overlaps"], [])
        self.assertEqual([s["wave"] for s in result["slices"]], [1, 2])
        self.assertEqual(result["slices"][0]["files"], result["slices"][1]["files"])


class EmptyWriteSetTests(unittest.TestCase):
    def test_none_write_sets_do_not_warn(self):
        text = """## Wave 1

### Builder 1
Writes: (none)

### Builder 2
Writes:
- none
- -
"""
        result = slices.parse_slices(text)
        self.assertEqual(len(result["slices"]), 2)
        self.assertEqual(result["slices"][0]["files"], [])
        self.assertEqual(result["slices"][1]["files"], [])
        self.assertEqual(result["warnings"], 0)


class InspectTests(unittest.TestCase):
    def test_missing_file_is_not_present(self):
        with tempfile.TemporaryDirectory() as workspace:
            result = slices.inspect(workspace)
        self.assertEqual(result, {
            "present": False,
            "waves": 0,
            "slices": 0,
            "overlaps": [],
            "warnings": 0,
        })

    def test_present_file_is_parsed(self):
        with tempfile.TemporaryDirectory() as workspace:
            with open(os.path.join(workspace, slices.NAME), "w", encoding="utf-8") as fh:
                fh.write(TABLE_TEXT)
            result = slices.inspect(workspace)
        self.assertTrue(result["present"])
        self.assertEqual(len(result["slices"]), 2)


class BuildShapeTests(unittest.TestCase):
    def test_own_slices_shape_parses_with_no_overlap(self):
        result = slices.parse_slices(BUILD_SHAPE_TEXT)
        self.assertEqual(len(result["slices"]), 6)
        self.assertEqual([s["name"].split(" ")[0] for s in result["slices"]],
                         ["B1", "B2", "B3", "B4", "B5", "B6"])
        self.assertEqual([s["wave"] for s in result["slices"]], [1] * 6)
        self.assertEqual(result["warnings"], 0)
        self.assertEqual(result["overlaps"], [])


if __name__ == "__main__":
    unittest.main()
