"""Regression tests for 3.12.1 canonical workspace expansion (S4).

A ``~`` in a workspace argument must be expanded exactly once against HOME and
must never be re-joined to the current base directory.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from deepseek_mcp import pipeline, project_map


class CanonicalWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.fake_home = tempfile.mkdtemp(prefix="dsh-fake-home-")
        self.addCleanup(shutil.rmtree, self.fake_home, True)
        env_patch = mock.patch.dict(os.environ, {"HOME": self.fake_home})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.old_cwd = os.getcwd()
        base = os.path.join(self.fake_home, "DeepSeek-MCP")
        os.makedirs(base, exist_ok=True)
        os.chdir(base)
        self.addCleanup(os.chdir, self.old_cwd)
        self.cfg = {"pipeline_enabled": False}
        self.rel = "~/DeepSeek-MCP/harness-workspace/proj"
        self.expected = os.path.join(
            self.fake_home, "DeepSeek-MCP", "harness-workspace", "proj")
        os.makedirs(self.expected, exist_ok=True)

    def assertCanonical(self, value):
        self.assertEqual(value, self.expected)
        self.assertNotIn("/~/", value)
        self.assertNotIn("DeepSeek-MCP/DeepSeek-MCP", value)

    def test_workspace_from_expands_tilde_once(self):
        got = pipeline._workspace_from({"workspace": self.rel}, self.cfg)
        self.assertCanonical(got)

    def test_prepare_args_expands_workspace_once(self):
        info = pipeline.prepare_args(
            self.cfg, {"prompt": "x", "workspace": self.rel})
        self.assertCanonical(info["canonical_workspace"])
        self.assertFalse(
            os.path.isdir(os.path.join(os.getcwd(), "~")),
            "literal tilde directory was created under cwd")

    def test_prepare_args_uses_pipeline_record_workspace(self):
        info = pipeline.prepare_args(self.cfg, {
            "prompt": "x",
            "_pipeline": {
                "canonical_workspace": self.rel,
                "decided": True,
                "eligible": False,
            },
        })
        self.assertCanonical(info["canonical_workspace"])

    def test_project_map_resolves_tilde_under_fake_home(self):
        map_path = os.path.join(self.expected, project_map.NAME)
        with open(map_path, "w", encoding="utf-8") as handle:
            handle.write("fake map body")
        self.assertEqual(project_map._read_map(self.rel), "fake map body")
        fingerprint = project_map._files_fingerprint(self.rel)
        self.assertGreaterEqual(fingerprint["files"], 1)
        self.assertFalse(
            os.path.isdir(os.path.join(os.getcwd(), "~")),
            "project_map created a literal tilde directory under cwd")

    def test_absolute_path_is_unchanged(self):
        self.assertEqual(
            pipeline._workspace_from({"workspace": self.expected}, self.cfg),
            self.expected)
        self.assertEqual(project_map._root(self.expected), self.expected)


if __name__ == "__main__":
    unittest.main()
