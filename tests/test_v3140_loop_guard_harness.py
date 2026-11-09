"""Slice E2: loop-guard harness integration (no real model calls)."""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from deepseek_mcp import harness, quality, safety, status
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_script


class FakeCfg:
    def __init__(self, enabled=True):
        self.loop_guard_enabled = enabled
        self.loop_guard_confirm = 2
        self.loop_guard_threshold = 3

    def get(self, key, default=None):
        return getattr(self, key, default)


class ActionGuard:
    def __init__(self, actions):
        self._actions = list(actions)
        self.polls = 0
        self.observed = []
        self.signatures = []

    def poll(self):
        if self.polls >= len(self._actions):
            return None
        action = self._actions[self.polls]
        self.polls += 1
        return action

    def observe_event(self, event, parsed=None):
        self.observed.append((event, parsed))

    def note_workspace(self, signature):
        self.signatures.append(signature)

    def snapshot(self):
        return {"polls": self.polls, "actions": list(self._actions)}


class BoomGuard:
    def poll(self):
        raise RuntimeError("guard exploded")


class LoopGuardHarnessTests(unittest.TestCase):
    def test_nudge_writes_file_and_requests_nudged_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ActionGuard([{
                "action": "nudge",
                "signal": "repeat_tool",
                "evidence": {"tool": "bash", "count": 5,
                             "command": "pytest /Users/example-user/x.py"},
            }])
            stops, lines, actions = [], [], []
            entry = harness._apply_loop_guard_action(
                guard, tmp, "builder", stops.append, lines.append, actions)
            self.assertEqual(entry["action"], "nudge")
            self.assertEqual(stops, ["loop_nudge"])
            self.assertTrue(lines)
            path = os.path.join(tmp, "LOOP_GUARD_NUDGE.md")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("repeat_tool", text)
            self.assertIn("pytest", text)
            self.assertNotIn("/Users/example-user", text)
            self.assertIn("~/x", text)
            self.assertNotIn("/Users/example-user", text)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["file"], "LOOP_GUARD_NUDGE.md")
            self.assertIn("restarting", " ".join(lines).lower())

    def test_replan_requests_loop_stop_and_enables_rescue(self):
        guard = ActionGuard([{
            "action": "replan",
            "signal": "same_edit_loop",
            "evidence": [{"file": "index.html", "edits": 6}],
        }])
        stops, lines, actions = [], [], []
        entry = harness._apply_loop_guard_action(
            guard, tempfile.gettempdir(), "builder", stops.append,
            lines.append, actions)
        self.assertEqual(entry["action"], "replan")
        self.assertEqual(stops, ["loop"])
        self.assertIn("loop", harness._RESCUE_STOP_REASONS)
        self.assertFalse(harness._loop_guard_blocks_finisher("loop"))
        self.assertTrue(any("re-plan" in line.lower() for line in lines))

    def test_stop_requests_loop_stop_and_does_not_rescue(self):
        guard = ActionGuard([{
            "action": "stop",
            "signal": "loop_hard_stop",
            "evidence": {"repeats": 9},
        }])
        stops, lines, actions = [], [], []
        harness._apply_loop_guard_action(
            guard, tempfile.gettempdir(), "builder", stops.append,
            lines.append, actions)
        self.assertEqual(stops, ["loop_stop"])
        self.assertTrue(harness._loop_guard_blocks_finisher("loop_stop"))
        self.assertNotIn("loop_stop", harness._RESCUE_STOP_REASONS)

    def test_finisher_replan_task_names_evidence(self):
        task = quality.build_finisher_task(
            "Build the page", ["index.html"], stop_reason="loop",
            loop_evidence=[{"signal": "repeat_tool", "count": 7}])
        self.assertIn("Re-plan", task)
        self.assertIn("repeat_tool", task)
        self.assertIn("loop guard", task.lower())

    def test_reviewer_and_ask_labels_never_get_a_guard(self):
        cfg = FakeCfg(enabled=True)
        self.assertIsNone(harness._make_loop_guard(cfg, "reviewer"))
        self.assertIsNone(harness._make_loop_guard(cfg, "ask-review"))
        self.assertIsNone(harness._make_loop_guard(FakeCfg(enabled=False), "builder"))
        # Wave and revision labels are builder phases too, not only the
        # literal "builder" label.
        self.assertIsNotNone(harness._make_loop_guard(cfg, "wave 2 builder"))
        self.assertIsNotNone(harness._make_loop_guard(
            cfg, "revision round 1 builder"))
        self.assertIsNotNone(harness._make_loop_guard(cfg, "wave 1 builder"))

    def test_builder_guard_uses_config_and_observe_is_guarded(self):
        fake_mod = types.ModuleType("deepseek_mcp.loop_guard")
        captured = {}

        class StubGuard:
            def __init__(self, config=None, label="builder", clock=None):
                captured.update(config=config, label=label, clock=clock)

            def observe_event(self, event, parsed=None):
                captured["event"] = event

            def note_workspace(self, signature):
                captured["signature"] = signature

            def snapshot(self):
                return {}

        fake_mod.LoopGuard = StubGuard
        old = sys.modules.get("deepseek_mcp.loop_guard")
        sys.modules["deepseek_mcp.loop_guard"] = fake_mod
        try:
            cfg = FakeCfg()
            guard = harness._make_loop_guard(cfg, "builder", clock=object())
            self.assertIsInstance(guard, StubGuard)
            self.assertEqual(captured["label"], "builder")
            self.assertIs(captured["config"], cfg)
        finally:
            if old is None:
                sys.modules.pop("deepseek_mcp.loop_guard", None)
            else:
                sys.modules["deepseek_mcp.loop_guard"] = old

    def test_hard_stop_status_is_a_known_failure_status(self):
        self.assertIn("stopped", status.STATUSES)
        self.assertIn("stopped", status.FAILURE_STATUSES)
        self.assertEqual(status.final_status("stopped")[0], "stopped")

    def test_broken_guard_never_raises(self):
        actions = []
        entry = harness._apply_loop_guard_action(
            BoomGuard(), tempfile.gettempdir(), "builder",
            lambda reason: actions.append(reason), None, [])
        self.assertIsNone(entry)
        self.assertEqual(actions, [])

    def test_waste_report_records_loops_and_estimate_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = {"loop_guard": {
                "snapshot": {"loops_caught": 2,
                             "estimated_tokens_saved": 4800},
                "actions": [{"action": "nudge"}, {"action": "replan"}],
            }}
            harness._record_loop_guard_waste(run, tmp)
            path = os.path.join(tmp, "WASTE_REPORT.md")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("Loops caught: 2", text)
            self.assertIn("Estimated tokens saved: 4800", text)
            self.assertIn("nudge=1", text)
            self.assertIn("replan=1", text)
            # Idempotent: a second finalization must not duplicate the block.
            harness._record_loop_guard_waste(run, tmp)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read().count("## Loop guard"), 1)

    def test_finisher_loop_guard_is_merged_into_run_metrics(self):
        run = {"loop_guard": {
            "snapshot": {"loops_caught": 1, "estimated_tokens_saved": 100},
            "actions": [{"action": "nudge"}]}}
        finisher = {"loop_guard": {
            "snapshot": {"loops_caught": 2, "estimated_tokens_saved": 300},
            "actions": [{"action": "stop", "signal": "repeat_tool"}]}}
        harness._merge_loop_guard(run, finisher, "finisher")
        info = run["loop_guard"]
        self.assertEqual(info["snapshot"]["loops_caught"], 3)
        self.assertEqual(info["snapshot"]["estimated_tokens_saved"], 400)
        self.assertEqual([item["action"] for item in info["actions"]],
                         ["nudge", "stop"])
        self.assertEqual(info["phases"][0]["label"], "finisher")
        self.assertEqual(info["actions"][1]["label"], "finisher")

    def test_confirm_uses_one_cheap_call_only_when_enabled(self):
        cfg = FakeCfg()
        cfg.loop_guard_confirm = True
        calls = []

        def fake_call(messages, **kwargs):
            calls.append((messages, kwargs))
            return {"choices": [{"message": {"content": "CONFIRM"}}]}

        action = {"signal": "repeat_tool",
                  "evidence": {"signal": "repeat_tool", "count": 4,
                               "threshold": 3}}
        with mock.patch("deepseek_mcp.api.call_deepseek",
                        side_effect=fake_call):
            self.assertTrue(harness._confirm_loop_signal(cfg, action))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("model"), "deepseek-flash")
        self.assertLessEqual(calls[0][1].get("max_tokens"), 16)
        self.assertIn("CONFIRM", calls[0][0][0]["content"])

        cfg.loop_guard_confirm = False
        with mock.patch("deepseek_mcp.api.call_deepseek",
                        side_effect=fake_call):
            self.assertTrue(harness._confirm_loop_signal(cfg, action))
        self.assertEqual(len(calls), 1, "confirm must be off by default")




class NudgedRestartTests(unittest.TestCase):
    """The nudge must reach a real builder process, not only a file."""

    def test_run_harness_restarts_once_with_the_nudge_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            workspace = os.path.join(root, "workspace")
            os.makedirs(workspace)
            dsh = write_fake_script(
                os.path.join(root, "dsh"), "#!/bin/sh\nexit 0\n")
            cfg = load_config(
                config_path=os.path.join(root, "config.json"),
                env=base_env(root, fake_dsh=dsh))
            nudge = {
                "action": "nudge",
                "signal": "loop_guard_repeat_command",
                "evidence": {"command": "pytest -q", "count": 3,
                             "threshold": 2},
                "file": "LOOP_GUARD_NUDGE.md",
            }
            first = {
                "status": "error",
                "error": "loop guard requested a nudged restart",
                "stop_reason": "loop_nudge", "answer": None, "seconds": 0.5,
                "output_files": [], "session_logs": [], "tools_used": {},
                "events": 1, "stalled": False, "timed_out": False,
                "tokens_in": 1, "tokens_out": 1,
                "estimated_cost_usd": 0.0, "cost_known": True,
                "loop_guard": {
                    "enabled": True, "label": "builder",
                    "actions": [nudge],
                    "snapshot": {"loops_caught": 1,
                                 "estimated_tokens_saved": 1200},
                },
            }
            second = dict(first)
            second.update(status="ok", error=None, stop_reason="ok",
                          answer="done", loop_guard={"enabled": True,
                                                     "label": "builder",
                                                     "actions": [],
                                                     "snapshot": {}})
            calls = []

            def fake_execute(cfg, workspace, task, **kwargs):
                calls.append(task)
                return first if len(calls) == 1 else second

            with mock.patch.object(harness, "_execute_once",
                                   side_effect=fake_execute):
                _text, run, _path = harness.run_harness({
                    "prompt": "Do a small task",
                    "workspace": workspace,
                    "subagents": 0,
                    "quality": False,
                    "independent_review": False,
                }, cfg)
            self.assertEqual(len(calls), 2, calls)
            self.assertIn("## Loop guard nudge", calls[1])
            self.assertIn("loop_guard_repeat_command", calls[1])
            self.assertIn("pytest -q", calls[1])
            self.assertTrue(run["loop_nudge"]["used"])
            self.assertEqual(run["status"], "ok")
            actions = (run.get("loop_guard") or {}).get("actions") or []
            self.assertEqual(actions[0]["action"], "nudge")


class LiveLoopGuardPathTests(unittest.TestCase):
    """Exercise the real subprocess path, not only the extracted helpers."""

    def test_execute_once_stops_a_repeating_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            workspace = os.path.join(root, "workspace")
            os.makedirs(workspace)
            body = (
                "#!/usr/bin/env python3\n"
                "import json, sys, time\n"
                "for index in range(8):\n"
                "    print(json.dumps({'type': 'tool_call', 'name': 'bash',"
                " 'input': {'command': 'pytest -q'}}), flush=True)\n"
                "    print(json.dumps({'type': 'tool_result', 'name': 'bash',"
                " 'is_error': True, 'output': 'FAILED tests/x.py - AssertionError: boom'}),"
                " flush=True)\n"
                "    time.sleep(0.2)\n"
                "print(json.dumps({'type': 'final', 'text': 'done'}), flush=True)\n")
            dsh = write_fake_script(os.path.join(root, "loop-dsh"), body)
            cfg = load_config(
                config_path=os.path.join(root, "config.json"),
                env=base_env(root, fake_dsh=dsh))
            for key, value in (("loop_guard_enabled", True),
                               ("loop_guard_repeat_command", 2),
                               ("loop_guard_poll_seconds", 0.0),
                               ("loop_guard_tokens_per_call", 100),
                               ("loop_guard_confirm", False)):
                cfg._values[key] = value
            policy = safety.resolve({"sandbox": "workspace"}, cfg)
            result = harness._execute_once(
                cfg, workspace, "Do a looping task", limit=30,
                label="builder", policy=policy)
            self.assertEqual(result.get("stop_reason"), "loop_nudge", result)
            info = result.get("loop_guard") or {}
            actions = [item.get("action") for item in info.get("actions") or []]
            self.assertIn("nudge", actions, actions)
            self.assertNotIn("replan", actions, actions)
            self.assertTrue(os.path.exists(
                os.path.join(workspace, "LOOP_GUARD_NUDGE.md")))
            self.assertGreaterEqual(
                (info.get("snapshot") or {}).get("loops_caught", 0), 1)


if __name__ == "__main__":
    unittest.main()
