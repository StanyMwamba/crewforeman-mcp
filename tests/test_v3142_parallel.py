"""3.14.2 automatic parallel jobs: inference, admission, stray-diff handling."""

import json
import os
import tempfile
import shutil
import subprocess
import types
import unittest

from deepseek_mcp import admission, pipeline, writesets


def _record(workspace, write_set, confidence, eligible=True, run_id="r1"):
    return {
        "run_id": run_id,
        "work_id": run_id,
        "pipeline_eligible": eligible,
        "canonical_workspace": workspace,
        "write_set": list(write_set),
        "write_set_confidence": confidence,
        "priority": 1,
        "depends_on": None,
    }


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_plan_write_set_is_confident(self):
        with open(os.path.join(self.tmp.name, "PLAN.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"write_set": ["src/a.py", "tests/test_a.py"]}, handle)
        result = writesets.infer_write_set("build it", self.tmp.name)
        self.assertEqual(result["write_set"],
                         ["src/a.py", "tests/test_a.py"])
        self.assertGreaterEqual(result["confidence"], 0.9)
        self.assertEqual(result["source"], "plan")

    def test_plan_parts_union(self):
        with open(os.path.join(self.tmp.name, "PLAN.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"parts": [
                {"id": "p1", "write_set": ["src/a.py"]},
                {"id": "p2", "write_set": ["docs/b.md"]}]}, handle)
        result = writesets.infer_write_set("build it", self.tmp.name)
        self.assertEqual(result["write_set"], ["src/a.py", "docs/b.md"])
        self.assertIn("plan_parts", result["sources"])

    def test_prompt_paths_are_inferred(self):
        result = writesets.infer_write_set(
            "Edit `src/a.py`, then update docs/b.md and run the tests.",
            self.tmp.name)
        self.assertEqual(result["write_set"], ["src/a.py", "docs/b.md"])
        self.assertGreaterEqual(result["confidence"], 0.85)

    def test_prose_only_is_low_confidence(self):
        result = writesets.infer_write_set(
            "Improve the code in src/auth/login.py please", self.tmp.name)
        self.assertEqual(result["write_set"], ["src/auth/login.py"])
        self.assertLess(result["confidence"], 0.85)

    def test_whole_repo_is_wide(self):
        result = writesets.infer_write_set(
            "Refactor the entire repo and change `src/a.py`", self.tmp.name)
        self.assertTrue(result["wide"])
        self.assertLess(result["confidence"], 0.85)

    def test_no_paths_is_empty(self):
        result = writesets.infer_write_set("Make it faster", self.tmp.name)
        self.assertEqual(result["write_set"], [])
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["source"], "none")

    def test_dangerous_paths_are_rejected(self):
        result = writesets.infer_write_set(
            "`/etc/passwd` `../outside.py` `.git/config`", self.tmp.name)
        self.assertEqual(result["write_set"], [])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.ws = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.ws, True))
        self.cfg = {"auto_parallel": True,
                    "auto_parallel_min_confidence": 0.85}

    def test_disjoint_and_confident_is_parallel(self):
        new = _record(self.ws, ["src/a.py"], 0.92, run_id="new")
        active = [_record(self.ws, ["src/b.py"], 0.92, run_id="old")]
        decision = writesets.parallel_decision(new, active, self.cfg)
        self.assertTrue(decision["parallel"])
        self.assertEqual(decision["reason"], "disjoint_confident")

    def test_overlap_is_serial(self):
        new = _record(self.ws, ["src/a.py"], 0.92, run_id="new")
        active = [_record(self.ws, ["src/"], 0.92, run_id="old")]
        decision = writesets.parallel_decision(new, active, self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "write_set_overlap")

    def test_low_confidence_is_serial(self):
        new = _record(self.ws, ["src/a.py"], 0.60, run_id="new")
        active = [_record(self.ws, ["src/b.py"], 0.92, run_id="old")]
        decision = writesets.parallel_decision(new, active, self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "low_confidence")

    def test_peer_low_confidence_is_serial(self):
        new = _record(self.ws, ["src/a.py"], 0.92, run_id="new")
        active = [_record(self.ws, ["src/b.py"], 0.60, run_id="old")]
        decision = writesets.parallel_decision(new, active, self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "peer_low_confidence")

    def test_different_workspace_is_independent(self):
        other = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other, True))
        new = _record(self.ws, ["src/a.py"], 0.92, run_id="new")
        active = [_record(other, ["src/a.py"], 0.92, run_id="old")]
        decision = writesets.parallel_decision(new, active, self.cfg)
        self.assertTrue(decision["parallel"])

    def test_auto_parallel_opt_out(self):
        cfg = {"auto_parallel": False}
        decision = writesets.parallel_decision(
            _record(self.ws, ["src/a.py"], 0.92), [], cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "auto_parallel_disabled")

    def test_no_write_set_is_serial(self):
        decision = writesets.parallel_decision(
            _record(self.ws, [], 0.0), [], self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "no_write_set")

    def test_admission_allows_disjoint_and_blocks_overlap(self):
        class Store:
            dependency_probe = None

            def __init__(self, active):
                self._active = list(active)
                self.saved = None

            def active(self):
                return list(self._active)

            def _save_item(self, work_id, record):
                self.saved = record

        cfg = types.SimpleNamespace(
            max_harness_jobs=4, reserve_resume_slot=False,
            pipeline_enabled=True, pipeline_require_disjoint=True,
            auto_parallel=True, auto_parallel_min_confidence=0.85,
            state_dir=self.ws)
        active = [_record(self.ws, ["src/b.py"], 0.92, run_id="old")]
        store = Store(active)
        controller = admission.AdmissionController(
            cfg, store=store, detected_limit=4)
        record = _record(self.ws, ["src/a.py"], 0.92, run_id="new")
        self.assertTrue(controller._may_start(record))
        store._active = [_record(self.ws, ["src/a.py"], 0.92, run_id="old2")]
        overlap = _record(self.ws, ["src/a.py"], 0.92, run_id="new2")
        self.assertFalse(controller._may_start(overlap))
        self.assertEqual(record.get("parallel_reason"), "disjoint_confident")
        self.assertEqual(overlap.get("parallel_reason"), "write_set_overlap")


class StrayTests(unittest.TestCase):
    def test_inside_and_outside(self):
        write_set = ["src/", "docs/PARALLEL.md"]
        self.assertEqual(
            writesets.stray_files(write_set, ["src/a.py", "docs/PARALLEL.md"]),
            [])
        self.assertEqual(
            writesets.stray_files(write_set, ["src/a.py", "setup.py"]),
            ["setup.py"])

    def test_glob_entry(self):
        self.assertEqual(
            writesets.stray_files(["tests/test_*.py"], ["tests/test_a.py"]), [])
        self.assertEqual(
            writesets.stray_files(["tests/test_*.py"], ["tests/helper.py"]),
            ["tests/helper.py"])

    def test_integration_decision(self):
        pipe = {"write_set": ["src/"]}
        clean = writesets.integration_decision(pipe, ["src/a.py"], True)
        self.assertEqual(clean["action"], "install")
        stray = writesets.integration_decision(pipe, ["setup.py"], True)
        self.assertEqual(stray["action"], "serial_reintegrate")
        self.assertEqual(stray["reason"], "write_set_stray")
        failed = writesets.integration_decision(pipe, ["src/a.py"], False)
        self.assertEqual(failed["action"], "serial_reintegrate")
        self.assertEqual(failed["reason"], "apply_check_failed")


class PipelinePrepareTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        with open(os.path.join(self.repo, "src", "a.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("x = 1\n")

        def git(*args):
            subprocess.run(
                ["git", "-c", "user.email=t@example.com",
                 "-c", "user.name=t"] + list(args),
                cwd=self.repo, check=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
        git("init", "-q")
        git("add", "src/a.py")
        git("commit", "-q", "-m", "init")

    def test_inferred_job_becomes_pipeline_eligible(self):
        cfg = {"state_dir": os.path.join(self.tmp.name, "state"),
               "pipeline_enabled": True, "pipeline_isolation": "worktree",
               "auto_parallel": True,
               "auto_parallel_min_confidence": 0.85}
        args = {"prompt": "Edit `src/a.py`", "workspace": self.repo}
        stub = {"id": "run-test", "workspace": self.repo,
                "pipeline": {"slot": "base"}}
        pipe = pipeline.prepare_args(cfg, args, stub)
        self.assertTrue(pipe["eligible"], pipe.get("unavailable_reason"))
        self.assertEqual(pipe["write_set_source"], "inferred")
        self.assertIn("src/a.py", pipe["write_set"])
        self.assertGreaterEqual(pipe["write_set_confidence"], 0.85)


class PipelineInferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_declared_write_set_falls_back_to_inference(self):
        args = {"prompt": "Edit `src/a.py` and `tests/test_a.py`",
                "workspace": self.tmp.name}
        declared = pipeline.declared_write_set(args, {})
        self.assertEqual(declared["source"], "inferred")
        self.assertEqual(declared["impl"], "auto")
        self.assertGreaterEqual(declared["confidence"], 0.85)
        self.assertIn("src/a.py", declared["write_set"])

    def test_explicit_write_set_still_wins(self):
        args = {"prompt": "Edit `src/a.py`",
                "workspace": self.tmp.name,
                "write_set": ["docs/"]}
        declared = pipeline.declared_write_set(args, {})
        self.assertEqual(declared["source"], "arg")
        self.assertEqual(declared["write_set"], ["docs"])

    def test_auto_parallel_false_keeps_serial_none(self):
        args = {"prompt": "Edit `src/a.py`", "workspace": self.tmp.name}
        declared = pipeline.declared_write_set(args, {"auto_parallel": False})
        self.assertEqual(declared["source"], "none")
        self.assertEqual(declared["write_set"], [])


if __name__ == "__main__":
    unittest.main()
