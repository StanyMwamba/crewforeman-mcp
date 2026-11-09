"""Contract tests for sandbox policy/header semantics (mocks only).

These freeze the matrix from DESIGN_PLATFORMS.md section 5.  They activate as
soon as Wave 1 lands ``safety.sandbox_policy``; until then they skip rather
than pretend the API exists.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import safety


class Cfg(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


def _cfg(**kw):
    base = {"os_sandbox": "auto", "os_sandbox_mode": "auto",
            "sandbox": "workspace-write", "linux_sandbox": "landlock"}
    base.update(kw)
    return Cfg(base)


def _status(available, enforcement="none", reason="", tested=False):
    return {"available": available, "mechanism": "seatbelt" if available else "none",
            "enforcement": enforcement, "reason": reason, "tested": tested,
            "canary": {"ok": bool(available)}}


@unittest.skipUnless(hasattr(safety, "sandbox_policy"),
                     "safety.sandbox_policy lands in Wave 1")
class PolicyMatrixTest(unittest.TestCase):
    def _policy(self, mode, status, **cfg):
        with mock.patch.object(safety, "sandbox_status", return_value=status):
            return safety.sandbox_policy(_cfg(os_sandbox_mode=mode, **cfg))

    def test_off_is_an_explicit_opt_out(self):
        policy = self._policy("off", _status(True, "enforced", tested=True))
        self.assertFalse(policy["os_sandbox"])
        self.assertEqual(policy["os_sandbox_mode"], "off")
        self.assertEqual(policy.get("enforcement"), "none")
        self.assertIn(policy.get("reason"), ("policy_opt_out", "off"))
        self.assertFalse(policy.get("blocked"))

    def test_auto_unavailable_degrades_but_starts(self):
        policy = self._policy("auto", _status(False, "none", "unavailable"))
        # auto with no enforced tier starts unwrapped: the legacy boolean is
        # false (nothing to wrap), while the status is visibly degraded.
        self.assertFalse(policy["os_sandbox"])
        self.assertFalse(policy.get("blocked"))
        self.assertTrue(policy.get("degraded"))
        self.assertEqual(policy["os_sandbox_mode"], "auto")

    def test_require_unavailable_blocks(self):
        policy = self._policy("require", _status(False, "none", "unavailable"))
        self.assertTrue(policy.get("blocked"))
        self.assertIn("sandbox", str(policy.get("reason")).lower())

    def test_require_enforced_does_not_block(self):
        policy = self._policy("require", _status(True, "enforced", tested=True))
        self.assertFalse(policy.get("blocked"))
        self.assertEqual(policy.get("enforcement"), "enforced")

    def test_mode_normalization(self):
        for raw in ("REQUIRE", " require ", "Auto", "bogus"):
            policy = self._policy(raw, _status(False, "none", "unavailable"))
            self.assertIn(policy["os_sandbox_mode"], ("auto", "off", "require"))
            self.assertIsInstance(policy["os_sandbox"], bool)

    def test_header_reports_unsandboxed_only_when_degraded(self):
        offline = self._policy("auto", _status(False, "none", "unavailable"))
        online = self._policy("require", _status(True, "enforced", tested=True))
        helper = getattr(safety, "sandbox_header", None)
        if helper is None:
            self.skipTest("sandbox_header not implemented yet")
        self.assertIn("UNSANDBOXED", helper(offline))
        self.assertNotIn("UNSANDBOXED", helper(online))


if __name__ == "__main__":
    unittest.main()
