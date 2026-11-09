"""Focused unit tests for the 3.12.0 backup-builder helpers (R4 / DESIGN §7)."""

import os
import tempfile
import unittest
from types import SimpleNamespace

from deepseek_mcp import builder_backup as bb


def cfg(**overrides):
    values = {
        "backup_builder_model": "",
        "backup_builder_provider": "",
        "backup_builder_max_cost_usd": 1.0,
        "backup_builder_min_attempts": 2,
        "dsh_bin": "/opt/dsh",
        "dsh_model": "deepseek-flash",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def error(text, status="error"):
    return {"status": status, "error": text}


class ConfiguredTests(unittest.TestCase):
    def test_empty_model_disables(self):
        self.assertIsNone(bb.configured(cfg()))
        self.assertIsNone(bb.configured(None))

    def test_defaults_and_same_binary(self):
        result = bb.configured(cfg(backup_builder_model="claude-sonnet-4",
                                   dsh_bin="/opt/dsh"))
        self.assertEqual(result["bin"], "/opt/dsh")
        self.assertEqual(result["max_cost_usd"], 1.0)
        self.assertEqual(result["min_attempts"], 2)
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["vendor"], "anthropic")
        self.assertEqual(result["model"], "claude-sonnet-4")

    def test_explicit_provider_wins_and_unknown_is_documented(self):
        self.assertEqual(
            bb.configured(cfg(backup_builder_model="m1",
                              backup_builder_provider="Example"))["vendor"],
            "Example",
        )
        self.assertEqual(
            bb.configured(cfg(backup_builder_model="mystery-model"))["vendor"],
            "unknown",
        )

    def test_min_attempts_and_zero_cap(self):
        result = bb.configured(cfg(backup_builder_model="m1",
                                   backup_builder_min_attempts=3,
                                   backup_builder_max_cost_usd=0))
        self.assertEqual(result["min_attempts"], 3)
        self.assertEqual(result["max_cost_usd"], 0.0)

    def test_resolve_target_falls_back_without_model_policy(self):
        result = bb.resolve_backup_target(cfg(backup_builder_model="m1"))
        self.assertEqual(result["model"], "m1")
        self.assertEqual(result["provider"], "unknown")


class SwitchTableTests(unittest.TestCase):
    def test_success_stall_timeout_cancel_quality_never_switch(self):
        executions = (
            {"status": "ok", "score": 2.0},
            {"status": "ok", "score": 1.0},
            {"status": "ok", "score": None},
            {"status": "stalled"},
            {"status": "timed_out"},
            {"status": "cancelled"},
            {"status": "error", "error": "whole-job deadline exceeded"},
        )
        for execution in executions:
            with self.subTest(execution=execution):
                decision = bb.switch_decision(execution, cfg(backup_builder_model="m1"))
                self.assertFalse(decision["switch"])
                self.assertIsNone(decision["reason_class"])

    def test_credit_and_auth_switch_immediately(self):
        for text, reason in (
            ("insufficient balance", "credit/budget"),
            ("HTTP 402 Payment Required", "credit/budget"),
            ("HTTP 401 Unauthorized", "auth"),
            ("invalid api key", "auth"),
        ):
            with self.subTest(text=text):
                decision = bb.switch_decision(error(text),
                                              cfg(backup_builder_model="m1"),
                                              attempts=1)
                self.assertTrue(decision["switch"])
                self.assertEqual(decision["reason_class"], reason)

    def test_5xx_and_stream_only_after_min_attempts(self):
        for text, reason in (
            ("503 Service Unavailable", "service unavailable (5xx)"),
            ("read ECONNRESET", "stream dropped"),
        ):
            with self.subTest(text=text):
                one = bb.switch_decision(error(text), cfg(backup_builder_model="m1"),
                                         attempts=1)
                self.assertFalse(one["switch"])
                self.assertIsNone(one["reason_class"])
                two = bb.switch_decision(error(text), cfg(backup_builder_model="m1"),
                                         attempts=2)
                self.assertTrue(two["switch"])
                self.assertEqual(two["reason_class"], reason)

    def test_min_attempts_three_respected(self):
        conf = cfg(backup_builder_model="m1", backup_builder_min_attempts=3)
        self.assertFalse(bb.switch_decision(error("500"), conf, attempts=2)["switch"])
        self.assertTrue(bb.switch_decision(error("500"), conf, attempts=3)["switch"])

    def test_writes_never_switch(self):
        decision = bb.switch_decision(error("insufficient balance"),
                                      cfg(backup_builder_model="m1"),
                                      writes=True)
        self.assertFalse(decision["switch"])
        self.assertEqual(decision["reason_class"], "side_effects")

    def test_zero_cap_disables_and_unknown_cost_refused(self):
        zero = cfg(backup_builder_model="m1", backup_builder_max_cost_usd=0)
        self.assertEqual(bb.switch_decision(error("401"), zero)["reason_class"],
                         "disabled")
        unknown = bb.switch_decision(error("401"), cfg(backup_builder_model="m1"),
                                     cost_known=False)
        self.assertFalse(unknown["switch"])
        self.assertEqual(unknown["reason_class"], "cost_unknown")

    def test_retry_policy_must_allow(self):
        class Blocked:
            def should_retry(self, *args, **kwargs):
                return False, "blocked"

        decision = bb.switch_decision(error("502"),
                                      cfg(backup_builder_model="m1"),
                                      attempts=2, retry_policy=Blocked())
        self.assertFalse(decision["switch"])
        self.assertIsNone(decision["reason_class"])

    def test_retry_policy_deadline_blocks(self):
        decision = bb.switch_decision(error("503"),
                                      cfg(backup_builder_model="m1"),
                                      attempts=2, remaining_seconds=0)
        self.assertFalse(decision["switch"])

    def test_spent_ceiling_refuses(self):
        decision = bb.switch_decision(error("503"),
                                      cfg(backup_builder_model="m1"),
                                      attempts=2, ceiling=1.0, spent=1.0)
        self.assertFalse(decision["switch"])
        self.assertEqual(decision["reason_class"], "budget")

    def test_no_second_switch(self):
        decision = bb.switch_decision(error("insufficient balance"),
                                      cfg(backup_builder_model="m1"),
                                      used=True)
        self.assertFalse(decision["switch"])
        self.assertTrue(bb.uses_backup_once(True))
        self.assertFalse(bb.uses_backup_once(False))

    def test_missing_backup_config_disables(self):
        decision = bb.switch_decision(error("401"), cfg())
        self.assertFalse(decision["switch"])
        self.assertEqual(decision["reason_class"], "disabled")


class CeilingTests(unittest.TestCase):
    def test_own_cap_and_min_whole_remaining(self):
        self.assertEqual(bb.effective_ceiling(1.0, None), 1.0)
        self.assertEqual(bb.effective_ceiling(1.0, 0.4), 0.4)
        self.assertEqual(bb.effective_ceiling(1.0, 5.0), 1.0)
        self.assertEqual(bb.effective_ceiling(0, 5.0), 0.0)
        self.assertIsNone(bb.effective_ceiling(1.0, 5.0, cost_known=False))


class SideEffectGuardTests(unittest.TestCase):
    def test_clean_and_mtime_change(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = os.path.join(workspace, "work.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("one\n")
            before = bb._snapshot(workspace)
            self.assertEqual(
                bb.side_effect_guard_result(before, workspace),
                {"clean": True, "reason": None},
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("two\n")
            # The guard is documented as mtime+size best-effort.  Windows
            # temp volumes can expose coarse timestamps, so make the change
            # observable instead of assuming POSIX mtime granularity.
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns,
                               stat.st_mtime_ns + 2_000_000_000))
            self.assertEqual(
                bb.side_effect_guard_result(before, workspace),
                {"clean": False, "reason": "side_effects"},
            )

    def test_tools_used_is_dirty_even_when_mtimes_are_coarse(self):
        with tempfile.TemporaryDirectory() as workspace:
            with open(os.path.join(workspace, "work.py"), "w", encoding="utf-8") as f:
                f.write("one\n")
            before = bb._snapshot(workspace)
            for tools in (["write"], [{"tool": "edit", "path": "work.py"}],
                          ["apply_patch"]):
                with self.subTest(tools=tools):
                    self.assertEqual(
                        bb.side_effect_guard_result(before, workspace, tools_used=tools),
                        {"clean": False, "reason": "side_effects"},
                    )

    def test_new_file_is_dirty(self):
        with tempfile.TemporaryDirectory() as workspace:
            before = bb._snapshot(workspace)
            with open(os.path.join(workspace, "new.py"), "w", encoding="utf-8") as f:
                f.write("x\n")
            self.assertFalse(bb.side_effect_guard_result(before, workspace)["clean"])


class HeaderAndRecordTests(unittest.TestCase):
    def test_header_exact_fragment(self):
        target = {"vendor": "Anthropic", "model": "claude-sonnet-4"}
        self.assertEqual(
            bb.header_line(target, "service unavailable (5xx)"),
            " | backup builder: Anthropic/claude-sonnet-4 "
            "(main failed: service unavailable (5xx))",
        )

    def test_record_shape_settlement_and_no_bin(self):
        target = {"bin": "/opt/dsh", "vendor": "Anthropic",
                  "model": "claude-sonnet-4"}
        result = bb.record(target, "credit/budget", status="ok", attempts=2,
                           ceiling_usd=0.4, cost_usd=0.2, cost_known=True)
        self.assertEqual(
            set(result),
            {"used", "status", "vendor", "model", "reason_class", "main_error",
             "attempts", "ceiling_usd", "settlement", "line"},
        )
        self.assertTrue(result["used"])
        self.assertNotIn("bin", result)
        self.assertEqual(result["settlement"], {"cost_usd": 0.2, "cost_known": True})
        self.assertEqual(
            result["line"],
            "Built by: backup (Anthropic/claude-sonnet-4) \u2014 "
            "main failed: credit/budget",
        )

    def test_record_unknown_cost_and_skipped(self):
        result = bb.record({"vendor": "v", "model": "m"}, "cost_unknown",
                           status="skipped", cost_known=False, cost_usd=3.0)
        self.assertFalse(result["used"])
        self.assertEqual(result["settlement"], {"cost_usd": None,
                                                "cost_known": False})

    def test_record_main_error_bounded_and_redacted(self):
        fake_key = "sk-" + "FAKE000000000000000000000000"
        raw = "%s at /opt/secret/project/file.py " % fake_key + ("x" * 900)
        result = bb.record({"vendor": "v", "model": "m"}, "auth",
                           status="failed", main_error=raw)
        self.assertLessEqual(len(result["main_error"]), 500)
        self.assertNotIn(fake_key, result["main_error"])
        self.assertNotIn("/opt/secret", result["main_error"])

    def test_error_message_attributes_builder_vendor(self):
        conf = cfg(backup_builder_model="kimi-k2",
                   backup_builder_provider="Kimi (Moonshot)")
        message = bb.error_message(error("insufficient balance"), conf)
        self.assertIn("Kimi (Moonshot)/kimi-k2", message)
        self.assertIn("builder", message)
        self.assertIn("quota", message)


if __name__ == "__main__":
    unittest.main()
