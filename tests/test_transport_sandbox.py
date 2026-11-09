"""3.13.0 harness/transport sandbox integration.

The blocked path must never spawn a child: ``os_sandbox=require`` with no
enforced boundary returns ``stop_reason=sandbox_unavailable`` instead of
falling through to an unwrapped ``Popen``.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import config, harness, safety
from tests.fakes import write_fake_dsh


def _blocked_policy():
    return {
        "preset": "workspace-write",
        "sandbox": "workspace-write",
        "approval": "ask",
        "os_sandbox": True,
        "os_sandbox_mode": "require",
        "os_sandbox_blocked": True,
        "sandbox_canary": "auto",
        "sandbox_extra_writes": [],
        "linux_min_abi": 3,
        "os_sandbox_policy": {
            "schema": 1, "kind": "none", "enforcement": "none",
            "abi": None, "probe": {}, "canary": {}, "tested": False,
            "degraded": True, "blocked": True,
            "reason": "sandbox_unavailable: landlock_disabled",
            "temp_roots": [], "profile_sha256": None, "requested": "require",
            "platform": "Linux", "writes_outside": "unverified",
            "delete_outside": "unverified", "network": "unrestricted",
            "sandbox_header": "UNSANDBOXED blocked",
        },
        "sandbox_header": "UNSANDBOXED blocked",
    }


class ConfigForTests:
    def __init__(self, root):
        self.values = {
            "dsh_bin": str(root / "fake-dsh"),
            "timeout": 5.0,
            "stall_seconds": 5.0,
            "stall_max_seconds": 5.0,
            "stall_cap_hard_max_seconds": 5.0,
            "stall_cap_grace_seconds": 0.0,
            "stall_cap_extension_seconds": 0.0,
            "dsh_transport": "headless",
            "dsh_provider": "deepseek-official",
            "dsh_model": "deepseek-flash",
            "reasoning": "max",
            "max_seconds": 5.0,
            "workspace_progress": False,
            "reviewer_sandbox": "read-only",
            "sandbox": "workspace-write",
        }

    def get(self, name, default=None):
        return self.values.get(name, default)

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name)


class BlockedSpawnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "fake-dsh")
        self.cfg = ConfigForTests(self.root)

    def test_headless_blocked_never_spawns(self):
        with mock.patch.object(harness.subprocess, "Popen",
                               side_effect=AssertionError("must not spawn")):
            result = harness._execute_once_impl(
                self.cfg, str(self.workspace), "task", limit=5,
                policy=_blocked_policy())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stop_reason"], "sandbox_unavailable")
        self.assertFalse(result["os_sandbox_wrapped"])
        self.assertIn("enforced file-write boundary", result["error"])
        self.assertEqual(result["os_sandbox"]["blocked"], True)

    def test_sdk_blocked_never_constructs_client(self):
        import time
        started = time.time()
        from deepseek_mcp import transport
        with mock.patch.object(transport, "SdkClient",
                               side_effect=AssertionError("must not spawn")):
            result = harness._sdk_execution(
                self.cfg, str(self.workspace), "task", 5, None, "builder",
                _blocked_policy(), None, started, 2.0,
                dsh_bin=str(self.dsh))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stop_reason"], "sandbox_unavailable")
        self.assertFalse(result["os_sandbox_wrapped"])

    def test_run_record_normalizes_and_has_header(self):
        record = safety.normalize_os_sandbox_record({"wrapped": True})
        self.assertEqual(record["schema"], 0)
        self.assertEqual(record["kind"], "seatbelt")
        self.assertEqual(record["enforcement"], "none")
        self.assertIn("sandbox_header", record)
        modern = _blocked_policy()["os_sandbox_policy"]
        normalized = safety.normalize_os_sandbox_record(modern)
        self.assertEqual(normalized["schema"], 1)
        self.assertTrue(normalized["blocked"])

    def test_resolved_policy_carries_header_and_mode(self):
        cfg = config.load_config(env={
            "DEEPSEEK_MCP_CONFIG": str(self.root / "missing.json"),
            "DSH_OS_SANDBOX": "require",
        })
        policy = safety.resolve({}, cfg)
        self.assertEqual(policy["os_sandbox_mode"], "require")
        self.assertIn("sandbox_header", policy)
        self.assertIn("os_sandbox_policy", policy)
        self.assertEqual(policy["os_sandbox_policy"]["requested"], "require")


if __name__ == "__main__":
    unittest.main()
