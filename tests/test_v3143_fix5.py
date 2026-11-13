"""Focused regression tests for FIX 5 (OS sandbox opt-out / danger labels).

Windows-compatible: tempfile/pathlib only, no network, no /tmp assumptions.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import config, jobs, platforms, safety


class Cfg(dict):
    """Dict config with attribute access, matching the real Config object."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _enforced_seatbelt():
    return {
        "schema": 1, "kind": "seatbelt", "enforcement": "enforced",
        "abi": "seatbelt", "tested": True, "degraded": False,
        "blocked": False, "reason": None, "temp_roots": [],
        "probe": {"ok": True, "at": 1.0, "ttl": 300.0, "reason": None},
        "canary": {"ok": True, "at": 1.0, "reason": None},
        "writes_outside": "denied", "delete_outside": "denied",
        "network": "unrestricted",
    }


class UnsetConfigTest(unittest.TestCase):
    def test_unset_os_sandbox_workspace_write_gets_boundary(self):
        with mock.patch.object(safety, "sandbox_status",
                               lambda **kw: _enforced_seatbelt()):
            policy = safety.sandbox_policy(
                {}, args={"sandbox": "workspace-write"})
        self.assertEqual(policy["os_sandbox_mode"], "auto")
        self.assertTrue(policy["os_sandbox"])
        self.assertEqual(policy["enforcement"], "enforced")
        self.assertIn("boundary=seatbelt(seatbelt) enforced",
                      policy["sandbox_header"])
        self.assertNotIn("UNSANDBOXED", policy["sandbox_header"])


class ExplicitOffTest(unittest.TestCase):
    def test_off_is_labelled_opt_out_and_job_status_warns(self):
        cfg = Cfg(os_sandbox="off")
        policy = safety.sandbox_policy(cfg)
        self.assertEqual(policy["os_sandbox_mode"], "off")
        self.assertEqual(policy["reason"], "policy_opt_out")
        self.assertFalse(policy["os_sandbox"])
        self.assertIn("policy_opt_out", policy["sandbox_header"])
        expected = ("WARNING: OS sandbox disabled by config os_sandbox=off; "
                    "file writes are unbounded")
        self.assertEqual(safety.sandbox_config_warning(cfg), expected)

        with tempfile.TemporaryDirectory() as tmp:
            run_id = "20260101-000000-abcdef"
            state = {"status": "running", "progress": "builder",
                     "updated": "2026-01-01T00:00:00Z", "title": "t"}
            Path(tmp, run_id + ".status.json").write_text(
                json.dumps(state), encoding="utf-8")
            cfg["jobs_dir"] = tmp
            text = jobs.tool_status({"run_id": run_id}, cfg)
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith(expected), text[:120])
        self.assertIn("Job", text)

    def test_auto_has_no_warning(self):
        self.assertIsNone(safety.sandbox_config_warning(Cfg(os_sandbox="auto")))

    def test_platform_opt_out_warns_on_its_own_platform(self):
        from deepseek_mcp import platforms
        cfg = Cfg(os_sandbox="auto", linux_sandbox="off")
        with mock.patch.object(platforms, "normalize_system",
                               return_value="Linux"):
            warning = safety.sandbox_config_warning(cfg)
        self.assertIn("linux_sandbox=off", warning)
        # The same key is irrelevant on macOS: no misleading warning.
        with mock.patch.object(platforms, "normalize_system",
                               return_value="Darwin"):
            self.assertIsNone(safety.sandbox_config_warning(cfg))


class DangerFullAccessTest(unittest.TestCase):
    def test_danger_is_explicit_label_and_never_wrapped(self):
        policy = safety.resolve({"sandbox": "danger"}, {})
        self.assertEqual(policy["sandbox"], "danger-full-access")
        self.assertFalse(policy["os_sandbox"])
        self.assertFalse(policy["os_sandbox_blocked"])
        self.assertIn("explicit danger-full-access opt-in",
                      policy["sandbox_header"])
        with tempfile.TemporaryDirectory() as tmp:
            _argv, meta = safety.wrap_command(
                ["dsh", "--version"], policy, tmp)
        self.assertFalse(meta["wrapped"])
        self.assertFalse(meta["blocked"])
        self.assertIn("explicit danger-full-access opt-in",
                      meta["sandbox_header"])

    def test_danger_cannot_be_derived_from_config(self):
        with self.assertRaises(ValueError):
            safety.resolve({}, {"sandbox": "danger-full-access"})


class RealConfigTest(unittest.TestCase):
    """The connector passes config.Config, not a dict subclass."""

    def test_real_config_off_warns_in_text_and_structured_status(self):
        expected = ("WARNING: OS sandbox disabled by config os_sandbox=off; "
                    "file writes are unbounded")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config({"os_sandbox": "off", "jobs_dir": tmp})
            self.assertEqual(safety.sandbox_config_warning(cfg), expected)
            self.assertEqual(safety.sandbox_policy(cfg)["reason"],
                             "policy_opt_out")
            run_id = "20260101-000000-realcfg"
            state = {"status": "running", "progress": "builder",
                     "updated": "2026-01-01T00:00:00Z", "title": "t"}
            Path(tmp, run_id + ".status.json").write_text(
                json.dumps(state), encoding="utf-8")
            plain = jobs.tool_status({"run_id": run_id}, cfg)
            structured = jobs.tool_status(
                {"run_id": run_id, "events_since": 0}, cfg)
        self.assertIsInstance(plain, str)
        self.assertTrue(plain.startswith(expected), plain[:160])
        self.assertIsInstance(structured, dict)
        self.assertEqual(structured.get("sandbox_warning"), expected)

    def test_macos_default_workspace_writer_gets_a_boundary(self):
        if platforms.normalize_system() != "Darwin":
            self.skipTest("macOS-only proof of the default boundary")
        policy = safety.sandbox_policy({}, args={"sandbox": "workspace-write"})
        self.assertEqual(policy["os_sandbox_mode"], "auto")
        self.assertTrue(policy["os_sandbox"])
        self.assertEqual(policy["enforcement"], "enforced")
        self.assertIn("boundary=seatbelt", policy["sandbox_header"])
        self.assertNotIn("UNSANDBOXED", policy["sandbox_header"])


class DangerRecordTest(unittest.TestCase):
    def test_danger_record_marks_the_request_and_is_not_degraded(self):
        policy = safety.resolve({"sandbox": "danger"}, {"os_sandbox": "off"})
        self.assertFalse(policy["os_sandbox"])
        with tempfile.TemporaryDirectory() as tmp:
            _argv, meta = safety.wrap_command(["dsh"], policy, tmp)
        self.assertEqual(meta["requested"], "danger-full-access")
        self.assertEqual(meta["reason"], "explicit_danger_full_access_opt_in")
        self.assertFalse(meta["degraded"])
        record = safety.normalize_os_sandbox_record(meta)
        self.assertEqual(record["requested"], "danger-full-access")
        self.assertFalse(record["degraded"])
        self.assertIn("explicit danger-full-access opt-in",
                      record["sandbox_header"])


if __name__ == "__main__":
    unittest.main()
