"""S4 / F4 pipeline core tests (real temp git repos, stdlib unittest)."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import pipeline


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=60)


@unittest.skipUnless(shutil.which("git"), "git is required for pipeline tests")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.ws / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "base.txt")
        self._git("commit", "-q", "-m", "base")
        self.cfg = {
            "state_dir": str(self.root / "state"),
            "pipeline_enabled": True,
            "pipeline_isolation": "worktree",
            "pipeline_install": "auto",
            "pipeline_require_disjoint": True,
            "pipeline_patch_retention_days": 0,
        }

    def _git(self, *args):
        proc = _run(["git"] + list(args), self.ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _write(self, rel, text):
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _run_record(self, run_id):
        return {"run_id": run_id, "pipeline": {
            "enabled": True, "eligible": True,
            "canonical_workspace": str(self.ws),
            "write_set": ["src/"],
        }}

    def _patch(self, run_id, rel, text):
        created = pipeline.create_worktree(self.cfg, str(self.ws), run_id)
        self.assertTrue(created["ok"], created)
        target = Path(created["path"]) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        run = self._run_record(run_id)
        run["pipeline"].update({
            "base_commit": created["base_commit"],
            "base_ref": created["base_ref"],
            "worktree": created["path"],
        })
        captured = pipeline.capture_patch(run, self.cfg, created["path"])
        self.assertTrue(captured["ok"], captured)
        return run, captured, created

    # -- write sets ---------------------------------------------------------

    def test_write_set_normalise_overlap_and_boundary(self):
        valid = pipeline.normalise_write_set([
            "src\\api\\", "./a//b.py", "/abs", "C:\\x", "../up",
            "bad\nname", "src/*.py",
        ])
        self.assertEqual(valid, ["src/api", "a/b.py", "src/*.py"])
        self.assertTrue(pipeline.overlaps(["src/api"], ["src/api/x.py"]))
        self.assertFalse(pipeline.overlaps(["src/api"], ["src/apis/x.py"]))
        self.assertTrue(pipeline.overlaps(["src/*.py"], ["src/a.py"]))
        self.assertTrue(pipeline.overlaps([], ["anything"]))
        self.assertFalse(pipeline.write_set_contains(["src/api"], "src/apis/x.py"))
        self.assertTrue(pipeline.write_set_contains(["src/api"], "src/api/x.py"))
        self.assertTrue(pipeline.write_set_contains([], "src/api/x.py"))

        declared = pipeline.declared_write_set({
            "write_set": ["src\\api\\", "/abs", "C:\\x", "../up", "bad\nname"],
        }, self.cfg)
        self.assertEqual(declared["source"], "arg")
        self.assertEqual(declared["write_set"], ["src/api"])
        self.assertIn("/abs", declared["invalid"])
        self.assertIn("../up", declared["invalid"])
        self.assertIn("bad\nname", declared["invalid"])

        prompt = {
            "prompt": "```SLICES\n## Slice one\nWrites:\n- lib/a.py\n```",
            "workspace": str(self.ws),
        }
        self.assertEqual(pipeline.declared_write_set(prompt, self.cfg)["source"], "prompt")
        self.assertEqual(
            pipeline.declared_write_set(prompt, self.cfg)["write_set"], ["lib/a.py"]
        )

    def test_plan_narrowing_only(self):
        run = {"pipeline": {
            "write_set": ["src/", "docs/"],
            "write_set_source": "arg",
            "write_set_invalid": [],
        }}
        plan = self.ws / "PLAN.json"
        plan.write_text(json.dumps({"write_set": ["src/api/"]}), encoding="utf-8")
        narrowed = pipeline.note_plan(run, plan)
        self.assertEqual(narrowed["write_set"], ["src/api"])
        self.assertFalse(narrowed["write_set_widened"])

        plan.write_text(json.dumps({
            "write_set": ["src/api/", "tests/"],
            "files_expected": ["src/api/x.py", "outside.py"],
        }), encoding="utf-8")
        widened = pipeline.note_plan(run, plan)
        self.assertTrue(widened["write_set_widened"])
        self.assertTrue(widened["defer_next_start"])
        self.assertIn("src/api", widened["write_set"])
        self.assertIn("tests", widened["write_set"])
        self.assertIn("outside.py", widened["plan_files_outside_write_set"])

    # -- worktree / patch ---------------------------------------------------

    def test_create_worktree_and_patch_includes_untracked(self):
        prepared = pipeline.prepare_args(
            self.cfg, {"workspace": str(self.ws), "write_set": ["src/"]},
            {"run_id": "r-prep"},
        )
        self.assertTrue(prepared["eligible"], prepared)
        self.assertEqual(prepared["write_set"], ["src"])

        run, captured, created = self._patch("run1", "src/new.py", "print('new')\n")
        self.assertTrue(Path(created["path"]).is_dir())
        self.assertTrue(Path(captured["path"]).is_file())
        self.assertIn("src/new.py", captured["files"])
        sidecar = json.loads(Path(captured["sidecar"]).read_text(encoding="utf-8"))
        digest = hashlib.sha256(Path(captured["path"]).read_bytes()).hexdigest()
        self.assertEqual(sidecar["sha256"], digest)
        self.assertEqual(sidecar["bytes"], Path(captured["path"]).stat().st_size)
        self.assertEqual(sidecar["base_commit"], created["base_commit"])
        self.assertTrue(sidecar["complete"])

    # -- install ------------------------------------------------------------

    def test_install_applies_combined(self):
        _r1, _c1, _w1 = self._patch("a1", "src/one.py", "one = 1\n")
        _r2, _c2, _w2 = self._patch("b2", "src/two.py", "two = 2\n")
        before_head = self._git("rev-parse", "HEAD")
        result = pipeline.install_pending(self.cfg, str(self.ws), "b2")
        self.assertEqual(result["status"], "installed", result)
        self.assertEqual(sorted(result["applied"]), ["a1", "b2"])
        self.assertTrue((self.ws / "src/one.py").is_file())
        self.assertTrue((self.ws / "src/two.py").is_file())
        self.assertEqual(self._git("rev-parse", "HEAD"), before_head)
        self.assertEqual(self._git("status", "--porcelain").count("src/"), 1)

    def test_apply_check_conflict_reports_and_applies_nothing(self):
        self._patch("c1", "base.txt", "one\n")
        _run2, _cap2, _wt2 = self._patch("c2", "base.txt", "two\n")
        result = pipeline.install_pending(self.cfg, str(self.ws), "c2")
        self.assertEqual(result["status"], "conflict", result)
        self.assertEqual(result["conflict"]["conflicting_patch"], "c2")
        self.assertEqual(result["applied"], [])
        self.assertEqual((self.ws / "base.txt").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_install_manual_keeps_patch(self):
        cfg = dict(self.cfg, pipeline_install="manual")
        _run1, captured, _wt = self._patch("m1", "src/manual.py", "manual = True\n")
        result = pipeline.install_pending(cfg, str(self.ws), "m1")
        self.assertEqual(result["status"], "manual", result)
        self.assertTrue(Path(captured["path"]).is_file())
        self.assertFalse((self.ws / "src/manual.py").exists())
        sidecar = json.loads(Path(captured["sidecar"]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["status"], "manual")

    def test_pipeline_off_skips(self):
        cfg = dict(self.cfg, pipeline_install="off")
        _run1, captured, _wt = self._patch("o1", "src/off.py", "off = True\n")
        result = pipeline.install_pending(cfg, str(self.ws), "o1")
        self.assertEqual(result["status"], "skipped", result)
        self.assertTrue(Path(captured["path"]).is_file())
        self.assertFalse((self.ws / "src/off.py").exists())

    # -- eligibility --------------------------------------------------------

    def test_non_git_not_eligible(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertFalse(pipeline.is_pipeline_eligible(self.cfg, str(plain)))
        prepared = pipeline.prepare_args(
            self.cfg, {"workspace": str(plain), "write_set": ["src/"]},
            {"run_id": "n1"},
        )
        self.assertFalse(prepared["eligible"])
        self.assertEqual(prepared["unavailable_reason"], "not_git_repo")

    def test_dirty_tree_not_eligible(self):
        (self.ws / "base.txt").write_text("dirty\n", encoding="utf-8")
        self.assertFalse(pipeline.is_pipeline_eligible(
            self.cfg, str(self.ws), ["src/"]
        ))
        prepared = pipeline.prepare_args(
            self.cfg, {"workspace": str(self.ws), "write_set": ["src/"]},
            {"run_id": "d1"},
        )
        self.assertFalse(prepared["eligible"])
        self.assertEqual(prepared["unavailable_reason"], "dirty_tree")

    # -- cleanup ------------------------------------------------------------

    def test_reclaim_removes_retained_worktree(self):
        created = pipeline.create_worktree(self.cfg, str(self.ws), "ret1")
        self.assertTrue(created["ok"], created)
        run = self._run_record("ret1")
        run["pipeline"].update({"worktree": created["path"],
                                "install": {"status": "manual"}})
        finalized = pipeline.finalize_run(run, self.cfg, str(self.ws))
        self.assertTrue(finalized["retained"])
        self.assertTrue(Path(created["path"]).is_dir())
        reclaimed = pipeline.reclaim_worktrees(self.cfg)
        self.assertIn("ret1", reclaimed["reclaimed"], reclaimed)
        self.assertFalse(Path(created["path"]).exists())

    def test_queue_ref_is_private_and_stable(self):
        ref = pipeline.queue_base_ref(str(self.ws))
        self.assertTrue(ref.startswith("refs/crewforeman/queue/"))
        self.assertEqual(ref, pipeline.queue_base_ref(str(self.ws)))


if __name__ == "__main__":
    unittest.main()
