"""3.11.0 S0 integration seams: prompt ABI, plan/gate/notes call sites.

The gate test drives ``run_harness`` with a fake executor: a builder writes a
leaked AWS key, ``gate_max_revisions=0``, and the run must end ``gate_failed``
without any reviewer process. No network and no real Harness call.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import failure_notes, harness, quality
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_script


class PromptAbiTests(unittest.TestCase):
    def test_plan_block_precedes_work_method_and_note_follows_practices(self):
        task = quality.build_harness_task(
            "Do the brief", 2, "code", quality=False,
            plan_block="PLAN-BLOCK-SENTINEL",
            failure_note="NOTE-BLOCK-SENTINEL")
        self.assertLess(task.index("Do the brief"),
                        task.index("PLAN-BLOCK-SENTINEL"))
        self.assertLess(task.index("PLAN-BLOCK-SENTINEL"),
                        task.index("Work method"))
        self.assertGreater(task.index("NOTE-BLOCK-SENTINEL"),
                           task.index("Work method"))

    def test_revision_abi_accepts_plan_and_note(self):
        task = quality.build_revision_task(
            "brief", ["src/a.py"], ["fix it"], ["broken"], round_no=2,
            plan_block="REV-PLAN", failure_note="REV-NOTE")
        self.assertIn("REV-PLAN", task)
        self.assertIn("REV-NOTE", task)
        self.assertLess(task.index("REV-PLAN"), task.index("REV-NOTE"))

    def test_failure_note_helper_records_injected_kinds(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = base_env(root)
        cfg = load_config(config_path=root / "config.json", env=env)
        failure_notes.record("code", ["failing test or build"], "run-a", 1, cfg)
        failure_notes.record("code", ["failing test or build"], "run-b", 1, cfg)
        note, details = harness._failure_note_for("code", cfg)
        self.assertIn("Common failures for this task type (code)", note)
        self.assertEqual(details["task_type"], "code")
        self.assertTrue(details["injected"])
        empty, _ = harness._failure_note_for("creative", cfg)
        self.assertEqual(empty, "")

    def test_review_round_is_recorded_once_in_failure_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = load_config(config_path=root / "config.json",
                          env=base_env(root))
        run = {"id": "run-f3", "task_type": "code"}
        entry = {"kind": "review", "round": 1, "blocking_defects": 1,
                 "defects": ["missing required fact: the brief date"]}
        harness._record_review_failure_notes(run, cfg, entry)
        harness._record_review_failure_notes(run, cfg, entry)
        second = dict(entry)
        second_run = {"id": "run-f3b", "task_type": "code"}
        harness._record_review_failure_notes(second_run, cfg, second)
        note, details = harness._failure_note_for("code", cfg)
        self.assertIn("missing required fact", note)
        self.assertTrue(details["injected"])
        store = json.loads((Path(cfg.state_dir) / "failure_notes.json")
                           .read_text(encoding="utf-8"))
        bucket = store["task_types"]["code"]["missing required fact"]
        self.assertEqual(bucket["count"], 2,
                         "the same (run_id, round_no) must not double-count; "
                         "a second run must add one")

    def test_gate_baseline_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = load_config(config_path=root / "config.json",
                          env=base_env(root))
        snapshot = {"src/a.py": (123456789, 12), "b.txt": (1, 2)}
        path = harness._save_gate_baseline(cfg, "run-gate", snapshot)
        self.assertTrue(path)
        self.assertEqual(harness._load_gate_baseline(cfg, "run-gate"),
                         snapshot)

    def test_plan_block_merge_adds_one_defect_and_fix(self):
        result = {"path": "PLAN.json", "mode": "block", "status": "mismatch",
                  "blocking_defect": "plan_contract: 1 deviation(s) "
                                      "(unexpected_file: src/x.py)"}
        defects, fixes, blocking = harness._merge_plan_contract_defect(
            result, [], [], 9)
        self.assertEqual(len(defects), 1)
        self.assertEqual(blocking, 9)
        self.assertTrue(fixes)
        warn = {"path": "PLAN.json", "mode": "warn", "status": "mismatch",
                "blocking_defect": None}
        same = harness._merge_plan_contract_defect(warn, ["d"], ["f"], 0)
        self.assertEqual(same, (["d"], ["f"], 0))

    def test_plan_block_and_revision_block_are_bounded(self):
        block = harness._plan_block("PLAN.json")
        self.assertIn("version", block)
        self.assertIn("files_expected", block)
        self.assertIn("before", block.lower())
        self.assertLess(len(block.split()), 160)
        self.assertIn("PLAN.json", harness._revision_plan_block("PLAN.json"))


class GateFailureIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        fake_dsh = write_fake_script(
            self.root / "dsh", "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        env = base_env(self.root)
        env.update({
            "DSH_BIN": str(fake_dsh),
            "DSH_INDEPENDENT_REVIEW": "1",
            "DSH_QUALITY": "1",
            "DSH_QUALITY_ROUNDS": "1",
            "DSH_GATE_ENABLED": "1",
            "DSH_GATE_TESTS": "off",
            "DSH_GATE_LINT": "off",
            "DSH_GATE_SECRET_SCAN": "1",
            "DSH_GATE_MAX_REVISIONS": "0",
        })
        self.cfg = load_config(config_path=self.root / "config.json", env=env)
        self.calls = []

    def _builder(self, cfg, workspace, task, **kwargs):
        label = str(kwargs.get("label") or "")
        self.calls.append(label)
        if label == "builder":
            path = Path(workspace) / "src" / "a.py"
            path.write_text(
                'aws_access_key_id = "AKIA' 'ABCDEFGHIJKLMNOP"\n',
                encoding="utf-8")
            return {
                "status": "ok", "answer": "built", "exit_code": 0,
                "seconds": 1.0, "output_files": ["src/a.py"],
                "session_logs": [], "tools_used": {}, "events": 1,
                "estimated_cost_usd": 0.0, "cost_known": True,
                "models": {"deepseek-flash": {}}, "usage": {},
                "stop_reason": "ok", "stalled": False, "timed_out": False,
                "child_progress_events": 0, "child_sessions_active": 0,
                "tmpdir_isolated": False, "transport": "headless",
            }
        raise AssertionError("unexpected paid phase: %s" % label)

    def test_gate_failure_sets_gate_failed_without_reviewer(self):
        args = {
            "prompt": "build a small module", "subagents": 1,
            "quality": True, "quality_rounds": 1,
            "workspace": str(self.workspace),
            "independent_review": True,
            "live_scaling": False,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=self._builder):
            text, run, _path = harness.run_harness(args, self.cfg)
        self.assertEqual(self.calls, ["builder"],
                         "no reviewer or revision may run after gate failure")
        self.assertEqual(run.get("status"), "gate_failed", run.get("error"))
        self.assertEqual(run.get("review_status"), "skipped")
        self.assertIsNone(run.get("quality_score"))
        self.assertFalse(run.get("review_rounds"))
        gate = run.get("deterministic_gate") or {}
        self.assertEqual(gate.get("verdict"), "failed")
        self.assertTrue(gate.get("blocked_review"))
        self.assertTrue(any(
            finding.get("rule_id") for check in gate.get("checks") or []
            for finding in check.get("findings") or []))
        self.assertIn("gate: failed", text.splitlines()[0])

    def test_gate_revision_runs_without_reviewer_and_cap_is_honoured(self):
        self.cfg._values.update({"gate_max_revisions": 1})
        calls = []

        def fake_execute(cfg, workspace, task, **kwargs):
            label = str(kwargs.get("label") or "")
            calls.append(label)
            path = Path(workspace) / "src" / "a.py"
            if label == "builder":
                path.write_text(
                    'aws_access_key_id = "AKIA' 'ABCDEFGHIJKLMNOP"\n',
                    encoding="utf-8")
            elif label.startswith("gate revision"):
                # The revision fixes one leak but introduces another, so the
                # second gate run fails and the cap ends the run.
                path.write_text(
                    'token = "ghp_' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
                    encoding="utf-8")
            else:
                raise AssertionError("unexpected paid phase: %s" % label)
            return {
                "status": "ok", "answer": "done", "exit_code": 0,
                "seconds": 1.0, "output_files": ["src/a.py"],
                "session_logs": [], "tools_used": {}, "events": 1,
                "estimated_cost_usd": 0.0, "cost_known": True,
                "models": {"deepseek-flash": {}}, "usage": {},
                "stop_reason": "ok", "stalled": False, "timed_out": False,
                "child_progress_events": 0, "child_sessions_active": 0,
                "tmpdir_isolated": False, "transport": "headless",
            }

        args = {
            "prompt": "build a small module", "subagents": 1,
            "quality": True, "quality_rounds": 1,
            "workspace": str(self.workspace),
            "independent_review": True, "live_scaling": False,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=fake_execute):
            _text, run, _path = harness.run_harness(args, self.cfg)
        self.assertEqual(calls, ["builder", "gate revision 1"])
        self.assertEqual(run.get("status"), "gate_failed")
        gate = run.get("deterministic_gate") or {}
        self.assertEqual(gate.get("revision_rounds"), 1)
        self.assertEqual(len(gate.get("revisions") or []), 1)
        self.assertFalse(run.get("review_rounds"))
        self.assertEqual(run.get("review_status"), "skipped")

    def test_plan_contract_is_read_from_a_pathlib_workspace(self):
        import json as _json

        def fake_execute(cfg, workspace, task, **kwargs):
            root = Path(workspace)
            (root / "PLAN.json").write_text(_json.dumps({
                "version": 1, "task_type": "code",
                "files_expected": ["src/a.py"], "tests_expected": [],
                "risks": [], "write_set": ["src/"],
            }), encoding="utf-8")
            (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            return {
                "status": "ok", "answer": "built", "exit_code": 0,
                "seconds": 1.0, "output_files": ["src/a.py"],
                "session_logs": [], "tools_used": {}, "events": 1,
                "estimated_cost_usd": 0.0, "cost_known": True,
                "models": {"deepseek-flash": {}}, "usage": {},
                "stop_reason": "ok", "stalled": False, "timed_out": False,
                "child_progress_events": 0, "child_sessions_active": 0,
                "tmpdir_isolated": False, "transport": "headless",
            }

        args = {
            "prompt": "build", "subagents": 1, "quality": True,
            "quality_rounds": 1, "workspace": str(self.workspace),
            "independent_review": False, "live_scaling": False,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=fake_execute):
            _text, run, _path = harness.run_harness(args, self.cfg)
        plan = run.get("plan") or {}
        self.assertNotEqual(plan.get("status"), "absent", plan)
        self.assertEqual(plan.get("mode"), "warn")
        self.assertEqual(plan.get("path"), "PLAN.json")
        self.assertNotIn("PLAN.json", run.get("deliverable_files") or [])

    def test_pipeline_lost_fails_before_builder(self):
        args = {
            "prompt": "build", "subagents": 1, "workspace": str(self.workspace),
            "quality": False, "independent_review": False,
            "_pipeline_lost": True,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=AssertionError("no paid phase")):
            with self.assertRaises(RuntimeError):
                harness.run_harness(args, self.cfg)

    def test_gate_resume_reuses_baseline_and_revision_cap(self):
        from deepseek_mcp.store import save_run
        self.cfg._values.update({
            "gate_max_revisions": 1, "gate_tests": "off", "gate_lint": "off",
        })
        source = "20261108-120000-src001"
        harness._save_gate_baseline(self.cfg, source, {"old.py": (1, 2)})
        save_run({
            "id": source, "status": "running", "prompt": "x",
            "created": "2026-11-08T12:00:00-05:00",
            "workspace": str(self.workspace),
            "deterministic_gate": {
                "verdict": "failed", "revision_rounds": 1,
                "revisions": [{"round": 1, "label": "gate revision 1"}],
                "tree_sha256": "stale-tree", "checks": [],
            },
        }, self.cfg)
        (self.workspace / "src" / "a.py").write_text(
            'aws_access_key_id = "AKIA' 'ABCDEFGHIJKLMNOP"\n',
            encoding="utf-8")
        calls = []

        def fake_execute(cfg, workspace, task, **kwargs):
            calls.append(str(kwargs.get("label") or ""))
            raise AssertionError("resume reused the builder; no paid phase "
                                 "may start")

        builder = {
            "status": "ok", "answer": "built", "exit_code": 0,
            "seconds": 1.0, "output_files": ["src/a.py"],
            "session_logs": [], "tools_used": {}, "events": 1,
            "estimated_cost_usd": 0.0, "cost_known": True,
            "models": {"deepseek-flash": {}}, "usage": {},
            "stop_reason": "ok", "stalled": False, "timed_out": False,
        }
        args = {
            "prompt": "build a small module", "subagents": 1,
            "quality": False, "quality_rounds": 1,
            "workspace": str(self.workspace),
            "independent_review": True, "live_scaling": False,
            "_prior_builder": builder, "_source_run_id": source,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=fake_execute):
            _text, run, _path = harness.run_harness(args, self.cfg)
        self.assertEqual(calls, [])
        self.assertEqual(run.get("status"), "gate_failed")
        gate = run.get("deterministic_gate") or {}
        self.assertEqual(gate.get("revision_rounds"), 1,
                         "the saved gate revision count must survive resume")
        self.assertEqual(len(gate.get("revisions") or []), 1)
        self.assertTrue(any(
            item.get("rule_id") for check in gate.get("checks") or []
            for item in check.get("findings") or []),
            "the saved pre-builder baseline must be used for the secret scan")

    def test_gate_quality_false_still_gates(self):
        args = {
            "prompt": "build a small module", "subagents": 1,
            "quality": False, "quality_rounds": 1,
            "workspace": str(self.workspace),
            "independent_review": True, "live_scaling": False,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=self._builder):
            _text, run, _path = harness.run_harness(args, self.cfg)
        self.assertEqual(self.calls, ["builder"])
        self.assertEqual(run.get("status"), "gate_failed",
                         "independent_review is authoritative even when "
                         "quality=false")
        self.assertTrue((run.get("deterministic_gate") or {}).get(
            "blocked_review"))

    def test_gate_skipped_when_no_tests_or_lint_and_secret_clean(self):
        (self.workspace / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        args = {
            "prompt": "build", "subagents": 1, "quality": True,
            "quality_rounds": 1, "workspace": str(self.workspace),
            "independent_review": False,
        }
        with mock.patch.object(harness, "_execute_once",
                               side_effect=self._builder):
            _text, run, _path = harness.run_harness(args, self.cfg)
        # No independent review: no gate is started at all.
        self.assertNotIn("reviewer", " ".join(self.calls).lower())
        self.assertIsNone(run.get("deterministic_gate"))


if __name__ == "__main__":
    unittest.main()
