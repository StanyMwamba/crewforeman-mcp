"""3.13.0 sandbox policy contract: status, modes, dispatch, blocked semantics.

Mocks only: no test here runs a real sandbox or spawns ``dsh``.  The real
boundary is covered by ``tests/test_sandbox_boundary*.py``.
"""

import os
import tempfile
import unittest
from unittest import mock

from deepseek_mcp import config, platforms, safety


def _status(kind="none", enforcement="none", abi=None, reason=None,
            tested=False, delete="unverified"):
    return {
        "schema": 1, "kind": kind, "enforcement": enforcement, "abi": abi,
        "probe": {"ok": tested, "at": 0, "ttl": 300.0, "reason": reason},
        "canary": {"ok": None, "at": None, "reason": "not_run"},
        "tested": tested, "degraded": enforcement != "enforced",
        "blocked": False, "reason": reason, "temp_roots": [],
        "profile_sha256": None, "requested": "auto", "platform": "Darwin",
        "writes_outside": "denied" if enforcement == "enforced" else "unverified",
        "delete_outside": delete, "network": "unrestricted",
        "sandbox_header": "fixture",
    }


class StatusTests(unittest.TestCase):
    def test_status_schema_and_vocabulary(self):
        with mock.patch.object(safety, "seatbelt_usable", return_value=True):
            status = safety.sandbox_status(force=True, system="Darwin")
        for key in ("schema", "kind", "enforcement", "abi", "probe", "canary",
                    "tested", "degraded", "blocked", "reason", "temp_roots",
                    "profile_sha256", "requested", "platform",
                    "writes_outside", "delete_outside", "network",
                    "sandbox_header"):
            self.assertIn(key, status)
        self.assertEqual(status["schema"], 1)
        self.assertEqual(status["kind"], "seatbelt")
        self.assertEqual(status["enforcement"], "enforced")
        self.assertTrue(status["tested"])
        self.assertEqual(status["network"], "unrestricted")

    def test_linux_abi_matrix(self):
        cases = [
            (8, "enforced", "landlock-8"),
            (3, "enforced", "landlock-3"),
            (2, "degraded", "landlock-2"),
            (1, "degraded", "landlock-1"),
        ]
        for abi, enforcement, label in cases:
            with self.subTest(abi=abi):
                with mock.patch.object(safety, "_STATUS_CACHE", {}), \
                        mock.patch(
                            "deepseek_mcp.sandbox_linux.probe_abi",
                            return_value={"ok": True, "abi": abi,
                                          "reason": None}):
                    status = safety.sandbox_status(
                        force=True, cfg={"linux_sandbox": "landlock",
                                         "linux_min_abi": 3},
                        system="Linux")
                self.assertEqual(status["kind"], "landlock")
                self.assertEqual(status["enforcement"], enforcement)
                self.assertEqual(status["abi"], label)
                self.assertTrue(status["tested"])
                if abi < 3:
                    self.assertIn("abi_lt_3", status["reason"])

    def test_linux_errno_reasons_are_distinct(self):
        for errno_value, reason in ((38, "landlock_disabled"),):
            with self.subTest(errno=errno_value):
                with mock.patch.object(safety, "_STATUS_CACHE", {}), \
                        mock.patch(
                            "deepseek_mcp.sandbox_linux.probe_abi",
                            return_value={"ok": False, "abi": None,
                                          "reason": reason}):
                    status = safety.sandbox_status(
                        force=True, cfg={"linux_sandbox": "landlock"},
                        system="Linux")
                self.assertEqual(status["kind"], "none")
                self.assertEqual(status["reason"], reason)

    def test_bwrap_only_when_probed(self):
        with mock.patch.object(safety, "_STATUS_CACHE", {}), \
                mock.patch(
                    "deepseek_mcp.sandbox_linux.probe_abi",
                    return_value={"ok": False, "reason": "landlock_disabled"}), \
                mock.patch.object(safety, "_bwrap_probe",
                                  return_value=(True, None, "/usr/bin/bwrap")):
            status = safety.sandbox_status(
                force=True, cfg={"linux_sandbox": "auto"}, system="Linux")
        self.assertEqual(status["kind"], "bwrap")
        self.assertEqual(status["enforcement"], "enforced")

    def test_windows_probe(self):
        with mock.patch.object(safety, "_STATUS_CACHE", {}), \
                mock.patch(
                    "deepseek_mcp.sandbox_windows.probe_status",
                    return_value={"ok": True, "admin": False,
                                  "restricting_sid": "logon"}):
            status = safety.sandbox_status(
                force=True, cfg={"windows_sandbox": "restricted"},
                system="Windows")
        self.assertEqual(status["kind"], "windows-write-restricted")
        self.assertEqual(status["enforcement"], "enforced")
        self.assertFalse(status["admin"])
        self.assertEqual(status["delete_outside"], "unverified")

    def test_cache_respects_force_and_ttl(self):
        with mock.patch.object(safety, "seatbelt_usable", return_value=True), \
                mock.patch.object(safety, "_STATUS_CACHE", {}):
            first = safety.sandbox_status(cfg={"sandbox_probe_ttl": 300})
            second = safety.sandbox_status(cfg={"sandbox_probe_ttl": 300})
        self.assertEqual(first["kind"], second["kind"])


class PolicyModeTests(unittest.TestCase):
    def _policy(self, mode, status):
        cfg = {"os_sandbox": mode, "os_sandbox_mode": mode}
        with mock.patch.object(safety, "sandbox_status", return_value=status):
            return safety.sandbox_policy(cfg)

    def test_off_is_explicit_opt_out(self):
        policy = self._policy("off", _status("seatbelt", "enforced", "seatbelt",
                                             tested=True))
        self.assertFalse(policy["os_sandbox"])
        self.assertEqual(policy["os_sandbox_mode"], "off")
        self.assertEqual(policy["enforcement"], "none")
        self.assertEqual(policy["reason"], "policy_opt_out")

    def test_require_unavailable_blocks(self):
        policy = self._policy("require", _status(reason="landlock_disabled"))
        self.assertTrue(policy["blocked"])
        self.assertTrue(policy["os_sandbox_blocked"])
        self.assertIn("sandbox", policy["reason"].lower())
        self.assertIn("UNSANDBOXED", policy["sandbox_header"])

    def test_require_enforced_does_not_block(self):
        policy = self._policy("require", _status("landlock", "enforced",
                                                 "landlock-8", tested=True))
        self.assertFalse(policy["blocked"])
        self.assertTrue(policy["os_sandbox"])
        self.assertIn("boundary=landlock(landlock-8) enforced",
                      policy["sandbox_header"])
        self.assertNotIn("UNSANDBOXED", policy["sandbox_header"])

    def test_auto_unavailable_is_degraded_not_blocked(self):
        policy = self._policy("auto", _status(reason="landlock_disabled"))
        self.assertFalse(policy["blocked"])
        self.assertTrue(policy["degraded"])
        self.assertIn("UNSANDBOXED", safety.sandbox_header(policy))

    def test_legacy_values_normalize(self):
        for raw in (False, "off", "false", "0"):
            self.assertEqual(safety._sandbox_mode({"os_sandbox": raw}), "off")
        for raw in (True, "auto", "", None):
            self.assertEqual(safety._sandbox_mode({"os_sandbox": raw}), "auto")
        for raw in ("require", "REQUIRE", "strict"):
            self.assertEqual(safety._sandbox_mode({"os_sandbox": raw}),
                             "require")


class WrapDispatchTests(unittest.TestCase):
    def test_seatbelt_dispatch(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "auto",
                  "sandbox": "workspace-write",
                  "os_sandbox_policy": _status("seatbelt", "enforced",
                                               "seatbelt", tested=True)}
        with mock.patch.object(safety.sys, "platform", "darwin"), \
                mock.patch.object(safety, "seatbelt_usable", return_value=True):
            argv, meta = safety.wrap_command(
                ["dsh", "--json", "task"], policy, "/tmp/ws")
        self.assertTrue(meta["wrapped"])
        self.assertEqual(argv[0], "sandbox-exec")
        self.assertIn("(deny file-write*)", argv[2])
        self.assertEqual(argv[3:], ["dsh", "--json", "task"])
        self.assertTrue(meta["profile_sha256"])

    def test_landlock_dispatch(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "auto",
                  "sandbox": "workspace-write",
                  "os_sandbox_policy": _status("landlock", "enforced",
                                               "landlock-8", tested=True)}
        argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws",
                                         tmpdir="/tmp/ws/.dsh-tmp-1")
        self.assertTrue(meta["wrapped"])
        self.assertIn("deepseek_mcp.sandbox_linux", argv)
        self.assertIn("--policy", argv)
        payload = argv[argv.index("--policy") + 1]
        self.assertIn('"mode":"workspace-write"', payload)

    def test_windows_dispatch(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "auto",
                  "sandbox": "workspace-write",
                  "os_sandbox_policy": _status(
                      "windows-write-restricted", "enforced",
                      "write-restricted", tested=True)}
        argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws")
        self.assertTrue(meta["wrapped"])
        self.assertIn("deepseek_mcp.sandbox_windows", argv)

    def test_require_missing_boundary_blocks(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "require",
                  "sandbox": "workspace-write",
                  "os_sandbox_policy": _status(reason="landlock_disabled")}
        argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws")
        self.assertEqual(argv, ["dsh"])
        self.assertFalse(meta["wrapped"])
        self.assertTrue(meta["blocked"])
        self.assertIn("sandbox", meta["reason"].lower())

    def test_auto_missing_boundary_degrades(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "auto",
                  "sandbox": "workspace-write",
                  "os_sandbox_policy": _status(reason="landlock_disabled")}
        argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws")
        self.assertEqual(argv, ["dsh"])
        self.assertFalse(meta["wrapped"])
        self.assertFalse(meta["blocked"])
        self.assertTrue(meta["degraded"])

    def test_canary_failure_blocks_required(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "require",
                  "sandbox": "workspace-write", "sandbox_canary": "auto",
                  "os_sandbox_policy": _status("landlock", "enforced",
                                               "landlock-8", tested=True)}
        with mock.patch.object(
                safety, "_run_canary",
                return_value={"ok": False, "at": 0,
                              "reason": "canary_write_allowed"}):
            argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws")
        self.assertEqual(argv, ["dsh"])
        self.assertFalse(meta["wrapped"])
        self.assertTrue(meta["blocked"])
        self.assertIn("canary", meta["reason"])

    def test_canary_failure_degrades_auto(self):
        policy = {"os_sandbox": True, "os_sandbox_mode": "auto",
                  "sandbox": "workspace-write", "sandbox_canary": "auto",
                  "os_sandbox_policy": _status("landlock", "enforced",
                                               "landlock-8", tested=True)}
        with mock.patch.object(
                safety, "_run_canary",
                return_value={"ok": False, "at": 0,
                              "reason": "canary_write_allowed"}):
            argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws")
        self.assertEqual(argv, ["dsh"])
        self.assertFalse(meta["blocked"])
        self.assertIn("UNSANDBOXED", meta["sandbox_header"])


class RecordAndExtrasTests(unittest.TestCase):
    def test_legacy_record_normalizes_without_raising(self):
        for raw in (None, {}, {"wrapped": True}, {"wrapped": False,
                                                 "reason": "x"}):
            record = safety.normalize_os_sandbox_record(raw)
            self.assertEqual(record["schema"], 0)
            self.assertIn("sandbox_header", record)
        wrapped = safety.normalize_os_sandbox_record({"wrapped": True})
        self.assertEqual(wrapped["kind"], "seatbelt")
        self.assertFalse(wrapped["tested"])

    def test_extra_writes_reject_root_home_and_credentials(self):
        home = os.path.realpath(os.path.expanduser("~"))
        safe = safety.validate_extra_writes(
            ["/", home, os.path.join(home, ".dsh"), "/tmp/ok"], "/tmp/ws")
        self.assertNotIn("/", safe)
        self.assertNotIn(home, safe)
        self.assertNotIn(os.path.realpath(os.path.join(home, ".dsh")), safe)
        self.assertIn(os.path.realpath("/tmp/ok"), safe)

    def test_config_keys_and_env_aliases(self):
        env = {
            "DEEPSEEK_MCP_CONFIG": os.path.join(tempfile.gettempdir(),
                                                "cf-missing-config.json"),
            "DEEPSEEK_MCP_LINUX_SANDBOX": "landlock",
            "DEEPSEEK_MCP_LINUX_MIN_ABI": "3",
            "DEEPSEEK_MCP_WINDOWS_SANDBOX": "restricted",
            "DEEPSEEK_MCP_SANDBOX_EXTRA_WRITES": '["/tmp/a"]',
            "DEEPSEEK_MCP_SANDBOX_PROBE_TTL": "12.5",
            "DEEPSEEK_MCP_SANDBOX_CANARY": "off",
        }
        cfg = config.load_config(env=env)
        self.assertEqual(cfg.linux_sandbox, "landlock")
        self.assertEqual(cfg.linux_min_abi, 3)
        self.assertEqual(cfg.windows_sandbox, "restricted")
        self.assertEqual(cfg.sandbox_extra_writes, ["/tmp/a"])
        self.assertEqual(cfg.sandbox_probe_ttl, 12.5)
        self.assertEqual(cfg.sandbox_canary, "off")

    def test_sandbox_support_shim(self):
        with mock.patch.object(safety, "sandbox_status",
                               return_value=_status("landlock", "enforced",
                                                    "landlock-8", tested=True)):
            support = platforms.sandbox_support(system="Linux")
        self.assertEqual(support["kind"], "landlock")
        self.assertTrue(support["available"])


if __name__ == "__main__":
    unittest.main()
