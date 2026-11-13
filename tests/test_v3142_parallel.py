"""3.14.3 automatic parallel jobs: inference, admission, stray-diff handling."""

import json
import os
import tempfile
import shutil
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import admission, jobs, pipeline, writesets
from deepseek_mcp.config import load_config


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

    def test_serial_peer_in_another_workspace_is_independent(self):
        other = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other, True))
        new = _record(self.ws, ["src/a.py"], 0.92, run_id="new")
        active = [_record(other, ["src/other.py"], 0.0, eligible=False,
                          run_id="serial-other")]
        decision = writesets.parallel_decision(new, active, self.cfg)
        self.assertTrue(decision["parallel"], decision)
        self.assertEqual(decision["reason"], "disjoint_confident")

    def test_queue_persists_confidence_for_inferred_parallel_job(self):
        store = admission.AdmissionStore(os.path.join(self.ws, "admission"))
        store.enqueue("w1", pipeline={
            "write_set": ["src/a.py"],
            "write_set_source": "inferred",
            "write_set_impl": "auto",
            "write_set_confidence": 0.9,
            "write_set_basis": "plan",
            "pipeline_eligible": True,
            "canonical_workspace": self.ws,
        })
        record = store._load_item("w1")
        self.assertEqual(record.get("write_set_confidence"), 0.9)
        self.assertEqual(record.get("write_set_basis"), "plan")
        decision = writesets.parallel_decision(record, [], self.cfg)
        self.assertTrue(decision["parallel"], decision)
        self.assertEqual(decision["reason"], "disjoint_confident")

    def test_auto_parallel_opt_out(self):
        cfg = {"auto_parallel": False}
        decision = writesets.parallel_decision(
            _record(self.ws, ["src/a.py"], 0.92), [], cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "auto_parallel_disabled")
        self.assertEqual(decision.get("same_workspace_active"), 0)

    def test_record_opt_out_overrides_enabled_config(self):
        record = _record(self.ws, ["src/a.py"], 0.92)
        record["auto_parallel"] = False
        decision = writesets.parallel_decision(record, [], self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "auto_parallel_disabled")

    def test_pipeline_block_opt_out_is_seen_by_admission(self):
        record = _record(self.ws, ["src/a.py"], 0.92)
        record["pipeline"] = {"auto_parallel": False,
                              "auto_parallel_min_confidence": 0.85}
        decision = writesets.parallel_decision(record, [], self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "auto_parallel_disabled")

    def test_record_min_confidence_overrides_config(self):
        record = _record(self.ws, ["src/a.py"], 0.92)
        record["auto_parallel_min_confidence"] = 0.99
        active = [_record(self.ws, ["src/b.py"], 0.92, run_id="old")]
        decision = writesets.parallel_decision(record, active, self.cfg)
        self.assertFalse(decision["parallel"])
        self.assertEqual(decision["reason"], "low_confidence")

    def test_admission_starts_opted_out_job_alone(self):
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
        store = Store([])
        controller = admission.AdmissionController(
            cfg, store=store, detected_limit=4)
        record = _record(self.ws, ["src/a.py"], 0.92, run_id="optout")
        record["auto_parallel"] = False
        # No same-repo peer: the opt-out disables parallelism, not the job.
        self.assertTrue(controller._may_start(record))
        self.assertEqual(record.get("parallel_reason"),
                         "auto_parallel_disabled")
        # A same-repo running peer keeps it serial.
        store._active = [_record(self.ws, ["src/b.py"], 0.92)]
        self.assertFalse(controller._may_start(record))

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

    def test_traversal_glob_cannot_allow_every_file(self):
        self.assertEqual(
            writesets.stray_files(["../*"], ["etc/passwd"]), ["etc/passwd"])
        self.assertEqual(
            writesets.stray_files(["src/../*"], ["etc/passwd"]),
            ["etc/passwd"])

    def test_windows_case_collision_counts_as_overlap(self):
        self.assertTrue(writesets.sets_overlap(
            ["src/A.py"], ["src/a.py"]))

    def test_absolute_path_inside_workspace_is_repo_relative(self):
        with tempfile.TemporaryDirectory() as root:
            absolute = os.path.join(root, "src", "a.py")
            self.assertEqual(
                writesets.normalise_rel(absolute, root), "src/a.py")

    def test_case_containment_follows_the_filesystem(self):
        # A case-only difference is a stray on case-sensitive POSIX, but the
        # same file on Windows.
        self.assertEqual(
            writesets.path_covered("src/A.py", ["src/a.py"]),
            os.name == "nt")
        self.assertEqual(writesets.stray_files(["src/a.py"], ["src/A.py"]),
                         [] if os.name == "nt" else ["src/A.py"])

    def test_empty_write_set_is_never_installed(self):
        decision = writesets.integration_decision(
            {"write_set": []}, ["src/a.py"], True)
        self.assertEqual(decision["action"], "serial_reintegrate")
        self.assertEqual(decision["reason"], "no_write_set")

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

    def test_plan_write_set_becomes_pipeline_eligible(self):
        cfg = {"state_dir": os.path.join(self.tmp.name, "state"),
               "pipeline_enabled": True, "pipeline_isolation": "worktree",
               "auto_parallel": True,
               "auto_parallel_min_confidence": 0.85}
        with open(os.path.join(self.repo, "PLAN.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"write_set": ["src/a.py"]}, handle)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com",
             "-c", "user.name=t", "add", "PLAN.json"],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com",
             "-c", "user.name=t", "commit", "-q", "-m", "plan"],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        args = {"prompt": "build it", "workspace": self.repo}
        stub = {"id": "run-plan", "workspace": self.repo,
                "pipeline": {"slot": "base"}}
        pipe = pipeline.prepare_args(cfg, args, stub)
        self.assertEqual(pipe["write_set_source"], "plan")
        self.assertTrue(pipe["eligible"], pipe.get("unavailable_reason"))
        self.assertEqual(pipe["write_set"], ["src/a.py"])

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


class PendingSerialTests(unittest.TestCase):
    def test_stray_serial_record_reaches_install_pending(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = os.path.join(tmp.name, "ws")
        os.makedirs(ws)
        cfg = {"state_dir": os.path.join(tmp.name, "state")}
        directory = pipeline.patch_dir(cfg)
        os.makedirs(directory)
        data = b"diff --git a/a.py b/a.py\n"
        with open(os.path.join(directory, "r1.patch"), "wb") as handle:
            handle.write(data)
        import hashlib
        pipeline._save_json(os.path.join(directory, "r1.json"), {
            "run_id": "r1", "patch": "r1.patch",
            "sha256": hashlib.sha256(data).hexdigest(),
            "queue_id": pipeline._queue_id(ws),
            "status": "serial_reintegration", "files": ["a.py"],
            "stray_files": ["a.py"], "created": "2026-01-01T00:00:00Z",
            "integration_reason": "write_set_stray",
        })
        pending = pipeline._pending_records(cfg, ws)
        self.assertEqual([row.get("run_id") for row in pending], ["r1"])
        self.assertEqual(pending[0].get("status"), "serial_reintegration")


class PipelinePrepareFrozenTests(unittest.TestCase):
    def test_frozen_opt_out_survives_prepare_without_raw_arg(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = {"state_dir": os.path.join(tmp.name, "state"),
               "pipeline_enabled": True, "auto_parallel": True,
               "auto_parallel_min_confidence": 0.85}
        args = {
            "prompt": "Edit `src/a.py` and `tests/test_a.py`",
            "workspace": tmp.name,
            "_canonical_workspace": tmp.name,
            "_pipeline": {
                "decided": True, "eligible": False,
                "unavailable_reason": "auto_parallel_disabled",
                "write_set": [], "write_set_source": "none",
                "write_set_impl": "builtin", "write_set_confidence": 0.0,
                "auto_parallel": False,
                "auto_parallel_min_confidence": 0.85,
                "base_commit": "deadbeef", "slot": "base",
            },
        }
        pipe = pipeline.prepare_args(cfg, args, {"id": "run-1"})
        self.assertFalse(pipe["auto_parallel"])
        self.assertFalse(pipe["eligible"])
        self.assertEqual(pipe["write_set_source"], "none")
        self.assertEqual(pipe["write_set"], [])
        self.assertEqual(pipe["unavailable_reason"],
                         "auto_parallel_disabled")


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

    def test_explicit_write_set_is_fully_confident(self):
        args = {"prompt": "Edit docs", "workspace": self.tmp.name,
                "write_set": ["docs/"]}
        declared = pipeline.declared_write_set(args, {})
        self.assertEqual(declared["source"], "arg")
        self.assertEqual(declared["confidence"], 1.0)
        decision = writesets.parallel_decision(
            _record(self.tmp.name, declared["write_set"], 1.0, run_id="new"),
            [], {"auto_parallel": True,
                 "auto_parallel_min_confidence": 0.85})
        self.assertTrue(decision["parallel"], decision)

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

    def test_arg_auto_parallel_false_keeps_serial_none(self):
        args = {"prompt": "Edit `src/a.py` and `tests/test_a.py`",
                "workspace": self.tmp.name,
                "auto_parallel": False}
        declared = pipeline.declared_write_set(args, {})
        self.assertNotEqual(declared["source"], "inferred")
        self.assertEqual(declared["source"], "none")
        self.assertEqual(declared["write_set"], [])

    def test_arg_auto_parallel_true_overrides_cfg_false(self):
        args = {"prompt": "Edit `src/a.py` and `tests/test_a.py`",
                "workspace": self.tmp.name,
                "auto_parallel": True}
        declared = pipeline.declared_write_set(
            args, {"auto_parallel": False})
        self.assertEqual(declared["source"], "inferred")
        self.assertIn("src/a.py", declared["write_set"])


class StartJobOptOutTests(unittest.TestCase):
    """The harness tool argument must beat the connector config per job."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.ws)

    def _cfg(self, **overrides):
        cfg = load_config(include_file=False, env={})
        values = {
            "state_dir": os.path.join(self.tmp.name, "state"),
            "jobs_dir": os.path.join(self.tmp.name, "jobs"),
            "harness_workspace": self.ws,
            "max_harness_jobs": 2,
            "reserve_resume_slot": False,
            "pipeline_enabled": True,
            "auto_parallel": True,
        }
        values.update(overrides)
        cfg._values.update(values)
        return cfg

    def _start(self, cfg, args):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen",
                                  return_value=proc) as popen:
            jobs.start_job("harness_deepseek", dict(args), cfg=cfg, wait=0)
        self.assertEqual(popen.call_count, 1)
        specs = list(Path(cfg.jobs_dir).glob("*.job.json"))
        self.assertEqual(len(specs), 1)
        return json.loads(specs[0].read_text(encoding="utf-8"))

    def test_args_opt_out_beats_enabled_config(self):
        cfg = self._cfg(auto_parallel=True)
        spec = self._start(cfg, {
            "prompt": "Edit `src/a.py` and `tests/test_a.py`",
            "workspace": self.ws,
            "auto_parallel": False,
        })
        pipe = spec.get("pipeline") or {}
        self.assertFalse(pipe.get("eligible"))
        self.assertNotEqual(pipe.get("write_set_source"), "inferred")
        self.assertEqual(pipe.get("unavailable_reason"),
                         "auto_parallel_disabled")
        self.assertIs(pipe.get("auto_parallel"), False)

    def test_args_min_confidence_beats_enabled_config(self):
        cfg = self._cfg(auto_parallel=True,
                        auto_parallel_min_confidence=0.85)
        spec = self._start(cfg, {
            "prompt": "Edit `src/a.py` and `tests/test_a.py`",
            "workspace": self.ws,
            "auto_parallel_min_confidence": 0.99,
        })
        pipe = spec.get("pipeline") or {}
        self.assertFalse(pipe.get("eligible"))
        self.assertEqual(pipe.get("unavailable_reason"),
                         "inferred_low_confidence")
        self.assertEqual(pipe.get("auto_parallel_min_confidence"), 0.99)


if __name__ == "__main__":
    unittest.main()
