"""Unit tests for the Linux Landlock launcher (no pytest, no root)."""

import errno
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

from deepseek_mcp import sandbox_linux as sl


class FakeKernel:
    """Minimal syscall(444/445/446) fake: probe returns ``abi``."""

    def __init__(self, abi=3, fail_number=None, fail_errno=errno.EINVAL):
        self.abi = abi
        self.fail_number = fail_number
        self.fail_errno = fail_errno
        self.calls = []

    def __call__(self, number, *args):
        self.calls.append(number)
        if number == self.fail_number:
            raise OSError(self.fail_errno, os.strerror(self.fail_errno))
        if number == sl.SYS_LANDLOCK_CREATE_RULESET and args and args[0] is None:
            return self.abi
        if number == sl.SYS_LANDLOCK_CREATE_RULESET:
            return 1000
        if number == sl.SYS_LANDLOCK_ADD_RULE:
            return 0
        if number == sl.SYS_LANDLOCK_RESTRICT_SELF:
            return 0
        raise AssertionError("unexpected syscall %r" % (number,))


def _fake_patches(kernel):
    return (
        mock.patch.object(sl, "_platform", lambda: "linux"),
        mock.patch.object(sl, "_active_threads", lambda: 1),
        mock.patch.object(sl, "_syscall", kernel),
        mock.patch.object(sl, "_prctl", lambda *a: 0),
    )


class AbiMaskTests(unittest.TestCase):
    def test_abi_1_through_9(self):
        for abi in range(1, 10):
            mask = sl.write_access_mask(abi)
            self.assertTrue(mask & sl.LANDLOCK_ACCESS_FS_WRITE_FILE)
            self.assertTrue(mask & sl.LANDLOCK_ACCESS_FS_REMOVE_FILE)
            self.assertTrue(mask & sl.LANDLOCK_ACCESS_FS_MAKE_REG)
            self.assertFalse(mask & sl.LANDLOCK_ACCESS_FS_EXECUTE, abi)
            self.assertFalse(mask & sl.LANDLOCK_ACCESS_FS_READ_FILE, abi)
            self.assertFalse(mask & sl.LANDLOCK_ACCESS_FS_READ_DIR, abi)
            self.assertEqual(
                bool(mask & sl.LANDLOCK_ACCESS_FS_REFER), abi >= 2, abi
            )
            self.assertEqual(
                bool(mask & sl.LANDLOCK_ACCESS_FS_TRUNCATE), abi >= 3, abi
            )
            self.assertEqual(
                sl.file_write_access(abi) & ~sl.FILE_WRITE_ACCESS,
                sl.LANDLOCK_ACCESS_FS_TRUNCATE if abi >= 3 else 0,
            )

    def test_probe_abi_success(self):
        with mock.patch.object(sl, "_syscall", FakeKernel(abi=4)):
            probe = sl.probe_abi()
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["abi"], 4)
        self.assertIsNone(probe["errno"])

    def test_errno_matrix_distinct_reasons(self):
        expected = {
            errno.ENOSYS: "kernel_missing_landlock",
            errno.EOPNOTSUPP: "landlock_lsm_disabled",
            errno.EPERM: "landlock_blocked_by_policy",
            errno.E2BIG: "landlock_attr_size_incompatible",
            errno.EINVAL: "landlock_invalid_argument",
        }
        reasons = set()
        for code, reason in expected.items():
            def boom(number, *args, _code=code):
                raise OSError(_code, os.strerror(_code))

            with mock.patch.object(sl, "_syscall", boom):
                probe = sl.probe_abi()
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["errno"], code)
            self.assertEqual(probe["reason"], reason)
            reasons.add(probe["reason"])
        self.assertEqual(len(reasons), len(expected))


class StructTests(unittest.TestCase):
    def test_struct_sizes(self):
        self.assertEqual(sl.RULESET_ATTR_SIZE, 8)
        self.assertEqual(sl.PATH_BENEATH_ATTR_SIZE, 12)
        self.assertEqual(len(sl.pack_ruleset_attr(0xFFFF)), 8)
        self.assertEqual(struct.calcsize("=Qi"), 12)
        packed = sl.pack_path_beneath_attr(0x1F, 7)
        self.assertEqual(len(packed), 12)
        allowed, parent = struct.unpack("=Qi", packed)
        self.assertEqual(allowed, 0x1F)
        self.assertEqual(parent, 7)


@unittest.skipIf(
    sys.platform == "win32",
    "Landlock is Linux-only; Windows enforces writes with the restricted-token "
    "launcher (deepseek_mcp.sandbox_windows)")
class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.workspace = os.path.join(self.root, "workspace")
        self.tmpdir = os.path.join(self.root, "tmp", ".dsh-tmp-exact")
        os.makedirs(self.workspace)
        os.makedirs(self.tmpdir)

    def tearDown(self):
        self.temp.cleanup()

    def _apply(self, policy, kernel=None):
        kernel = kernel or FakeKernel()
        patches = _fake_patches(kernel)
        for patcher in patches:
            patcher.start()
        try:
            return sl.apply_landlock(policy)
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def _policy(self, mode="workspace-write", **extra):
        policy = {
            "workspace": self.workspace,
            "mode": mode,
            "tmpdir": self.tmpdir,
            "min_abi": 3,
        }
        policy.update(extra)
        return policy

    def test_exact_tmpdir_no_regex(self):
        sibling = self.tmpdir + "-sibling"
        os.makedirs(sibling)
        result = self._apply(self._policy("read-only"))
        self.assertTrue(result["ok"])
        self.assertIn(self.tmpdir, result["granted"])
        self.assertNotIn(sibling, result["granted"])
        self.assertNotIn(self.workspace, result["granted"])

    def test_non_directory_rule_masks_file_rights(self):
        review = os.path.join(self.workspace, "REVIEW.md")
        with open(review, "w") as handle:
            handle.write("report\n")
        result = self._apply(self._policy("read-only"), FakeKernel(abi=3))
        rules = {rule["path"]: rule for rule in result["rules"]}
        self.assertIn(review, rules)
        self.assertFalse(rules[review]["is_dir"])
        self.assertEqual(
            rules[review]["access"],
            sl.LANDLOCK_ACCESS_FS_WRITE_FILE | sl.LANDLOCK_ACCESS_FS_TRUNCATE,
        )
        # No directory-only or read rights on the literal file rule.
        self.assertFalse(
            rules[review]["access"]
            & (sl.LANDLOCK_ACCESS_FS_REMOVE_FILE
               | sl.LANDLOCK_ACCESS_FS_MAKE_REG
               | sl.LANDLOCK_ACCESS_FS_READ_FILE)
        )

    def test_non_directory_mask_without_truncate_on_abi_1(self):
        review = os.path.join(self.workspace, "REVIEW.md")
        with open(review, "w") as handle:
            handle.write("report\n")
        result = self._apply(self._policy(read_only=True, min_abi=1),
                             FakeKernel(abi=1))
        rules = {rule["path"]: rule for rule in result["rules"]}
        self.assertEqual(rules[review]["access"],
                         sl.LANDLOCK_ACCESS_FS_WRITE_FILE)

    def test_missing_grants_recorded_not_fatal(self):
        missing = os.path.join(self.root, "missing-extra")
        policy = self._policy("read-only", extra_writes=[missing],
                              dsh_home=os.path.join(self.root, "dsh"))
        result = self._apply(policy)
        skipped = {item["path"]: item["reason"]
                   for item in result["skipped"]}
        self.assertIn(missing, skipped)
        self.assertIn("missing", skipped[missing])
        self.assertFalse(result["review_write"])
        self.assertIn("review", [item["source"] for item in result["skipped"]])

    def test_abi_below_minimum_required_is_125(self):
        policy = self._policy(min_abi=3, required=True)
        with self.assertRaises(sl.SandboxUnavailable) as caught:
            self._apply(policy, FakeKernel(abi=2))
        self.assertEqual(caught.exception.exit_code, 125)
        self.assertEqual(caught.exception.abi, 2)

    def test_abi_below_minimum_auto_is_degraded(self):
        result = self._apply(self._policy(min_abi=3, required=False),
                             FakeKernel(abi=2))
        self.assertTrue(result["degraded"])
        self.assertEqual(result["abi"], 2)
        self.assertFalse(result["handled_access_fs"]
                         & sl.LANDLOCK_ACCESS_FS_TRUNCATE)

    def test_add_rule_failure_is_126(self):
        policy = self._policy("read-only")
        with self.assertRaises(sl.SandboxApplyError) as caught:
            self._apply(policy, FakeKernel(abi=3,
                                           fail_number=sl.SYS_LANDLOCK_ADD_RULE,
                                           fail_errno=errno.EINVAL))
        self.assertEqual(caught.exception.exit_code, 126)
        self.assertEqual(caught.exception.errno, errno.EINVAL)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.workspace = os.path.join(self.root, "ws")
        os.makedirs(self.workspace)
        self.review = os.path.join(self.workspace, "REVIEW.md")

    def tearDown(self):
        self.temp.cleanup()

    def test_policy_json_round_trip(self):
        policy = {
            "workspace": self.workspace,
            "mode": "workspace-write",
            "tmpdir": os.path.join(self.root, "tmp"),
            "extra_writes": [self.review],
            "dsh_home": os.path.join(self.root, "dsh"),
            "min_abi": 3,
            "required": True,
            "unknown": "ignored",
        }
        encoded = sl.policy_json(policy)
        decoded = json.loads(encoded)
        self.assertEqual(decoded, sl.parse_policy(encoded))
        self.assertEqual(decoded["mode"], "workspace-write")
        self.assertEqual(decoded["workspace"], self.workspace)
        self.assertEqual(decoded["min_abi"], 3)
        self.assertTrue(decoded["required"])
        self.assertNotIn("unknown", decoded)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            sl.parse_policy({"workspace": self.workspace,
                             "mode": "danger-full-access"})

    def test_cli_failure_json_has_contract_keys(self):
        with mock.patch.object(sl, "parse_policy", side_effect=ValueError):
            with mock.patch.object(sys, "stderr", new=_Capture()):
                code = sl.main(["--policy", "{}", "--", "/bin/true"])
        self.assertEqual(code, 126)

    def test_usage_failure_is_126_without_paths(self):
        capture = _Capture()
        with mock.patch.object(sys, "stderr", capture):
            code = sl.main(["--policy", "{}"])
        self.assertEqual(code, 126)
        payload = json.loads(capture.text.strip())
        self.assertEqual(set(payload), {"code", "abi", "errno"})
        self.assertNotIn(os.path.sep, capture.text)


class _Capture:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()
