"""Focused tests for the E1 deterministic loop-guard detector."""

import unittest

from deepseek_mcp.loop_guard import (
    SIGNAL_NOCHANGE,
    SIGNAL_PINGPONG,
    SIGNAL_REPEAT,
    SIGNAL_TEST,
    LoopGuard,
)


def call(name, **tool_input):
    return {"type": "tool_call", "name": name, "input": tool_input}


def result(output, failed=False, name="Bash"):
    event = {"type": "tool_result", "name": name, "output": output}
    if failed:
        event["is_error"] = True
    return event


class LoopGuardTests(unittest.TestCase):
    def test_repeat_command_signal_and_threshold(self):
        guard = LoopGuard({"loop_guard_repeat_command": 2,
                           "loop_guard_same_test_failure": 99})
        for _ in range(2):
            guard.observe_event(call("Bash", command="pytest -q"))
            guard.observe_event(result("boom", failed=True))
        snap = guard.snapshot()
        self.assertEqual(snap["signals"][SIGNAL_REPEAT]["count"], 2)
        self.assertEqual(snap["signals"][SIGNAL_REPEAT]["threshold"], 2)
        self.assertEqual(snap["loops_caught"], 1)
        action = guard.poll()
        self.assertEqual(action["action"], "nudge")
        self.assertEqual(action["signal"], SIGNAL_REPEAT)
        self.assertEqual(action["evidence"]["count"], 2)

    def test_repeat_threshold_is_configurable(self):
        guard = LoopGuard({"loop_guard_repeat_command": 3,
                           "loop_guard_same_test_failure": 99})
        for _ in range(2):
            guard.observe_event(call("Bash", command="make test"))
            guard.observe_event(result("Exit code 2", failed=True))
        self.assertIsNone(guard.poll())
        guard.observe_event(call("Bash", command="make test"))
        guard.observe_event(result("Exit code 2", failed=True))
        action = guard.poll()
        self.assertEqual(action["signal"], SIGNAL_REPEAT)
        self.assertEqual(action["evidence"]["threshold"], 3)

    def test_edit_pingpong_signal(self):
        guard = LoopGuard({"loop_guard_edit_pingpong": 3,
                           "loop_guard_repeat_command": 99})
        path = "/home/example-user/work/sample.py"
        for content in ("A", "B", "A", "B"):
            guard.observe_event(call("Edit", path=path, content=content))
        snap = guard.snapshot()
        self.assertEqual(snap["signals"][SIGNAL_PINGPONG]["count"], 3)
        action = guard.poll()
        self.assertEqual(action["action"], "nudge")
        self.assertEqual(action["signal"], SIGNAL_PINGPONG)
        self.assertEqual(action["evidence"]["threshold"], 3)

    def test_nochange_calls_signal_and_workspace_reset(self):
        guard = LoopGuard({"loop_guard_nochange_calls": 4,
                           "loop_guard_repeat_command": 99})
        guard.note_workspace("digest-1")
        for _ in range(3):
            guard.observe_event(call("Bash", command="make build"))
        guard.note_workspace("digest-2")
        self.assertEqual(guard.snapshot()["signals"][SIGNAL_NOCHANGE]["count"], 0)
        self.assertIsNone(guard.poll())
        for _ in range(4):
            guard.observe_event(call("Bash", command="make build"))
        action = guard.poll()
        self.assertEqual(action["signal"], SIGNAL_NOCHANGE)
        self.assertEqual(action["evidence"]["count"], 4)

    def test_same_test_failure_signal(self):
        guard = LoopGuard({"loop_guard_same_test_failure": 2})
        failure = ("FAILED tests/test_sample.py::test_alpha - "
                   "AssertionError: assert 1 == 2")
        for _ in range(2):
            guard.observe_event(call("Bash", command="pytest -q"))
            guard.observe_event(result(failure))
        snap = guard.snapshot()
        self.assertEqual(snap["signals"][SIGNAL_TEST]["count"], 2)
        self.assertEqual(snap["signals"][SIGNAL_TEST]["threshold"], 2)
        action = guard.poll()
        self.assertEqual(action["signal"], SIGNAL_TEST)
        self.assertIn("test_alpha", action["evidence"]["key"])

    def test_real_dsh_callid_shape_and_out_of_order_results(self):
        # Captured from a real `dsh --json` run: tool/tool callId/input, and
        # tool_result callId/status/result.
        guard = LoopGuard({"loop_guard_repeat_command": 2,
                           "loop_guard_same_test_failure": 99})
        guard.observe_event({
            "type": "tool_call", "callId": "c1", "tool": "bash",
            "input": {"command": "make test"}})
        guard.observe_event({
            "type": "tool_call", "callId": "c2", "tool": "bash",
            "input": {"command": "pytest -q"}})
        # Results arrive in the opposite order; callId must match each one.
        guard.observe_event({
            "type": "tool_result", "callId": "c2", "status": "error",
            "result": "boom-two"})
        guard.observe_event({
            "type": "tool_result", "callId": "c1", "status": "error",
            "result": "boom-one"})
        details = guard.snapshot()["signals"][SIGNAL_REPEAT]["details"]
        self.assertEqual(len(details), 2, details)
        commands = " ".join(item["command"] for item in details.values())
        self.assertIn("make test", commands)
        self.assertIn("pytest -q", commands)

    def test_assistant_content_blocks_are_observed_once(self):
        guard = LoopGuard({"loop_guard_repeat_command": 2,
                           "loop_guard_nochange_calls": 2,
                           "loop_guard_same_test_failure": 99})
        for index in (1, 2):
            guard.observe_event({
                "type": "assistant/message",
                "data": {"message": {"content": [{
                    "type": "tool_use", "id": "call-%d" % index,
                    "name": "Bash", "input": {"command": "false"}}]}}})
            # The same call also arriving as a tool_call event must not count
            # twice (no-change would otherwise reach 2 on one real call).
            guard.observe_event({
                "type": "tool_call", "id": "call-%d" % index,
                "name": "Bash", "input": {"command": "false"}})
            guard.observe_event({
                "type": "user/message",
                "data": {"message": {"content": [{
                    "type": "tool_result", "tool_use_id": "call-%d" % index,
                    "is_error": True, "content": "boom"}]}}})
        snap = guard.snapshot()
        self.assertEqual(snap["signals"][SIGNAL_REPEAT]["count"], 2)
        self.assertEqual(snap["signals"][SIGNAL_NOCHANGE]["count"], 2)
        action = guard.poll()
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "nudge")

    def test_ladder_nudge_replan_stop_and_single_nudge(self):
        guard = LoopGuard({"loop_guard_repeat_command": 1,
                           "loop_guard_same_test_failure": 99,
                           "loop_guard_max_actions": 3})
        guard.observe_event(call("Bash", command="false"))
        guard.observe_event(result("boom", failed=True))
        actions = [guard.poll()]
        # Escalation needs one more threshold of evidence, not just a poll.
        for _ in range(2):
            guard.observe_event(call("Bash", command="false"))
            guard.observe_event(result("boom", failed=True))
            actions.append(guard.poll())
        self.assertEqual([item["action"] for item in actions],
                         ["nudge", "replan", "stop"])
        self.assertEqual([item["signal"] for item in actions],
                         [SIGNAL_REPEAT] * 3)
        self.assertIsNone(guard.poll())
        snap = guard.snapshot()
        self.assertEqual(snap["actions"], 3)
        self.assertEqual(snap["loops_caught"], 1)
        self.assertEqual(sum(1 for item in snap["action_log"]
                             if item["action"] == "nudge"), 1)

    def test_confirm_is_recorded_without_model_call(self):
        guard = LoopGuard({"loop_guard_repeat_command": 1,
                           "loop_guard_confirm": True,
                           "loop_guard_same_test_failure": 99})
        guard.observe_event(call("Bash", command="false"))
        guard.observe_event(result("boom", failed=True))
        action = guard.poll()
        self.assertTrue(action["evidence"]["needs_confirm"])
        self.assertTrue(action["needs_confirm"])


    def test_read_only_exploration_never_triggers_nochange(self):
        guard = LoopGuard({"loop_guard_nochange_calls": 4,
                           "loop_guard_repeat_command": 99,
                           "loop_guard_same_test_failure": 99})
        guard.note_workspace("digest-1")
        read_only = (
            ("Read", ""),
            ("Bash", "git status --short"),
            ("Bash", "pytest -q"),
            ("Bash", "python -m pytest -q"),
            ("Bash", "python3 -m pytest tests -q"),
            ("Bash", "uv run pytest"),
            ("Bash", "cd repo && git status"),
            ("Bash", "git -C . status"),
            ("Bash", "sed -n 1,20p f"),
            ("run_command", "cat x"),
            ("TodoWrite", ""),
        )
        for _ in range(20):
            for name, command in read_only:
                guard.observe_event(call(name, command=command))
        self.assertIsNone(guard.poll())
        self.assertEqual(
            guard.snapshot()["signals"][SIGNAL_NOCHANGE]["count"], 0)

    def test_healthy_work_has_no_action(self):
        guard = LoopGuard()
        guard.note_workspace("initial")
        for index in range(15):
            guard.observe_event(call("Read", path="/home/example-user/app.py"))
            guard.observe_event(result("ok"))
            guard.note_workspace("digest-%d" % index)
        self.assertIsNone(guard.poll())
        snap = guard.snapshot()
        self.assertEqual(snap["loops_caught"], 0)
        self.assertEqual(snap["actions"], 0)
        self.assertEqual(snap["estimated_tokens_saved"], 0)
        for signal in (SIGNAL_REPEAT, SIGNAL_PINGPONG, SIGNAL_NOCHANGE,
                       SIGNAL_TEST):
            self.assertEqual(snap["signals"][signal]["count"], 0)

    def test_snapshot_counts_and_estimate(self):
        guard = LoopGuard({"loop_guard_repeat_command": 1,
                           "loop_guard_tokens_per_call": 50,
                           "loop_guard_same_test_failure": 99})
        for _ in range(3):
            guard.observe_event(call("Bash", command="false"))
            guard.observe_event(result("boom", failed=True))
        guard.poll()
        guard.observe_event(call("Bash", command="false"))
        guard.observe_event(result("boom", failed=True))
        guard.poll()
        snap = guard.snapshot()
        self.assertEqual(snap["signals"][SIGNAL_REPEAT]["count"], 4)
        self.assertEqual(snap["loops_caught"], 1)
        self.assertEqual(snap["actions"], 2)
        # 4 repeats - threshold 1 = 3 wasted calls * 50 tokens.
        self.assertEqual(snap["estimated_tokens_saved"], 150)
        self.assertIn("approximate", snap["estimated_tokens_saved_basis"])

    def test_disabled_guard_produces_no_action(self):
        guard = LoopGuard({"loop_guard_enabled": False,
                           "loop_guard_nochange_calls": 2})
        for _ in range(10):
            guard.observe_event(call("Read", path="/home/example-user/a.txt"))
        self.assertIsNone(guard.poll())
        snap = guard.snapshot()
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["actions"], 0)


if __name__ == "__main__":
    unittest.main()
