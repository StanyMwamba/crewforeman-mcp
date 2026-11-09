"""3.13.0 macOS Seatbelt: profile bytes, credential ordering, wrapper shape.

The profile builder is pure; the real deny is covered by the boundary test.
"""

import os
import unittest
from unittest import mock

from deepseek_mcp import safety


class ProfileTests(unittest.TestCase):
    def test_profile_shape_and_credential_denies_last(self):
        ws = os.path.realpath("/tmp/ws")
        profile = safety.seatbelt_profile(
            "/tmp/ws", mode="workspace-write",
            extra_writes=["/tmp/extra"], dsh_home="/tmp/private-dsh")
        lines = profile.splitlines()
        self.assertEqual(lines[0], "(version 1)")
        self.assertEqual(lines[1], "(allow default)")
        self.assertEqual(lines[2], "(deny file-write*)")
        self.assertIn('(allow file-write* (subpath %s))'
                      % safety._sbpl_quote(ws), profile)
        # The credential deny must come after every allow rule.
        last_allow = max(index for index, line in enumerate(lines)
                         if line.startswith("(allow file-write*"))
        deny_indexes = [index for index, line in enumerate(lines)
                        if "(deny file-write*" in line
                        and ".credentials.yaml" in line]
        self.assertTrue(deny_indexes)
        self.assertTrue(all(index > last_allow for index in deny_indexes))
        self.assertIn(".credentials.yaml", profile)

    def test_read_only_does_not_grant_workspace_root(self):
        ws = os.path.realpath("/tmp/ws")
        profile = safety.seatbelt_profile("/tmp/ws", mode="read-only")
        self.assertNotIn('(allow file-write* (subpath %s))'
                         % safety._sbpl_quote(ws), profile)
        self.assertIn('(literal %s)'
                      % safety._sbpl_quote(os.path.join(ws, "REVIEW.md")),
                      profile)

    def test_private_reviewer_home_does_not_grant_operator_runtime(self):
        home = os.path.realpath(os.path.expanduser("~"))
        private = os.path.join(home, ".dsh-review-home-test")
        profile = safety.seatbelt_profile(
            "/tmp/ws", mode="workspace-write", dsh_home=private)
        self.assertIn('(subpath %s)'
                      % safety._sbpl_quote(os.path.join(private, "sessions")),
                      profile)
        self.assertNotIn(
            '(subpath %s)'
            % safety._sbpl_quote(os.path.join(home, ".dsh", "profiles")),
            profile)

    def test_dangerous_extra_writes_are_rejected(self):
        home = os.path.realpath(os.path.expanduser("~"))
        safe = os.path.realpath("/tmp/safe-extra")
        profile = safety.seatbelt_profile(
            "/tmp/ws", mode="workspace-write",
            extra_writes=["/", home, os.path.join(home, ".dsh"), safe])
        self.assertNotIn('(subpath "/")', profile)
        self.assertNotIn('(subpath %s)' % safety._sbpl_quote(home), profile)
        self.assertNotIn('(subpath %s)'
                         % safety._sbpl_quote(os.path.join(home, ".dsh")),
                         profile)
        self.assertIn('(subpath %s)' % safety._sbpl_quote(safe), profile)

    def test_sbpl_quoting(self):
        profile = safety.seatbelt_profile('/tmp/weird"dir\\x', mode="read-only")
        self.assertIn('\\"', profile)
        self.assertIn("\\\\", profile)


class WrapTests(unittest.TestCase):
    def _policy(self):
        return {
            "os_sandbox": True, "os_sandbox_mode": "auto",
            "sandbox": "workspace-write", "sandbox_canary": "off",
            "os_sandbox_policy": {
                "schema": 1, "kind": "seatbelt", "enforcement": "enforced",
                "abi": "seatbelt", "tested": True, "reason": None,
                "delete_outside": "denied", "sandbox_header": "fixture",
            },
        }

    def test_wrap_uses_sandbox_exec_and_profile(self):
        with mock.patch.object(safety.sys, "platform", "darwin"), \
                mock.patch.object(safety, "seatbelt_usable", return_value=True):
            argv, meta = safety.wrap_command(
                ["dsh", "--json", "task"], self._policy(), "/tmp/ws",
                tmpdir="/tmp/ws/.dsh-tmp-1")
        self.assertTrue(meta["wrapped"])
        self.assertEqual(argv[:2], ["sandbox-exec", "-p"])
        ws = os.path.realpath("/tmp/ws")
        self.assertIn('(subpath %s)' % safety._sbpl_quote(ws), argv[2])
        self.assertIn("(regex ", argv[2])
        self.assertIn(".dsh-tmp-", argv[2])
        self.assertEqual(argv[-3:], ["dsh", "--json", "task"])

    def test_missing_seatbelt_degrades_or_blocks(self):
        policy = self._policy()
        policy["os_sandbox_mode"] = "require"
        with mock.patch.object(safety, "seatbelt_usable", return_value=False), \
                mock.patch.object(safety, "_SEATBELT_PROBE",
                                  {"done": True, "usable": False, "at": 0,
                                   "reason": "sandbox-exec not available"}):
            argv, meta = safety.wrap_command(["dsh"], policy, "/tmp/ws")
        self.assertEqual(argv, ["dsh"])
        self.assertFalse(meta["wrapped"])
        self.assertTrue(meta["blocked"])
        self.assertIn("sandbox-exec", meta["reason"])

    def test_header_for_enforced_macos(self):
        status = safety.sandbox_status(force=True, system="Darwin")
        # Nested hosts report UNSANDBOXED; the header must always be present
        # and never claim enforcement without a probe.
        self.assertIn("network=unrestricted", status["sandbox_header"])
        if status["enforcement"] == "enforced":
            self.assertIn("boundary=seatbelt", status["sandbox_header"])
        else:
            self.assertIn("UNSANDBOXED", status["sandbox_header"])


if __name__ == "__main__":
    unittest.main()
