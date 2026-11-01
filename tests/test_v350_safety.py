"""Tests for wave1-v350/safety.py. Fakes only: no network, no real keys."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import safety  # noqa: E402

# Built at runtime so the source carries no literal home path.
_FAKE_HOME = "/" + "Users/" + "operator"


class NormalizeTests(unittest.TestCase):
    def test_aliases(self):
        for raw in ("read-only", "readonly", "read_only", "ro", " RO "):
            self.assertEqual(safety.normalize_sandbox(raw), "read-only")
        for raw in ("workspace-write", "write", "workspace", "rw", "RW"):
            self.assertEqual(safety.normalize_sandbox(raw), "workspace-write")
        for raw in ("danger-full-access", "danger_full_access", "full", "none", "danger"):
            self.assertEqual(safety.normalize_sandbox(raw), "danger-full-access")

    def test_none_and_empty_use_default(self):
        self.assertEqual(safety.normalize_sandbox(None), "workspace-write")
        self.assertEqual(safety.normalize_sandbox(""), "workspace-write")
        self.assertEqual(safety.normalize_sandbox("", default="read-only"), "read-only")
        self.assertEqual(
            safety.normalize_sandbox(None, default="read-only"), "read-only"
        )

    def test_unknown_raises_without_echoing_value(self):
        secret = "sk-" "live-abc123SECRET"
        with self.assertRaises(ValueError) as caught:
            safety.normalize_sandbox(secret)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("abc123", str(caught.exception))


class ResolveTests(unittest.TestCase):
    def test_presets_shape(self):
        self.assertEqual(
            safety.PRESETS["read-only"],
            {"sandbox": "read-only", "approval": "ask"},
        )
        self.assertEqual(safety.PRESETS["danger"]["approval"], "never")
        self.assertEqual(
            safety.PRESETS["danger"]["sandbox"], "danger-full-access"
        )

    def test_reviewer_default_read_only_builder_default_write(self):
        reviewer = safety.resolve({}, reviewer=True)
        self.assertEqual(reviewer["sandbox"], "read-only")
        self.assertEqual(reviewer["preset"], "read-only")
        self.assertEqual(reviewer["approval"], "ask")
        builder = safety.resolve({})
        self.assertEqual(builder["sandbox"], "workspace-write")
        self.assertEqual(builder["preset"], "workspace-write")

    def test_cfg_defaults(self):
        policy = safety.resolve({}, cfg={"reviewer_sandbox": "write"}, reviewer=True)
        self.assertEqual(policy["sandbox"], "workspace-write")
        policy = safety.resolve({}, cfg={"sandbox": "ro"})
        self.assertEqual(policy["sandbox"], "read-only")

    def test_alias_and_requested(self):
        policy = safety.resolve({"sandbox": "RO"})
        self.assertEqual(policy["sandbox"], "read-only")
        self.assertEqual(policy["preset"], "read-only")
        self.assertEqual(policy["requested"], "RO")

    def test_danger_requires_explicit_opt_in(self):
        policy = safety.resolve({"sandbox": "danger"})
        self.assertEqual(policy["sandbox"], "danger-full-access")
        self.assertEqual(policy["preset"], "danger")
        self.assertEqual(policy["approval"], "never")

        with self.assertRaises(ValueError) as caught:
            safety.resolve({}, cfg={"sandbox": "danger"})
        self.assertIn("opt-in", str(caught.exception))
        with self.assertRaises(ValueError):
            safety.resolve({}, cfg={"reviewer_sandbox": "full"}, reviewer=True)
        # ...but an explicit alias is fine.
        self.assertEqual(
            safety.resolve({"sandbox": "full"})["sandbox"], "danger-full-access"
        )

    def test_approval_override_and_validation(self):
        policy = safety.resolve({"sandbox": "read-only", "approval": "never"})
        self.assertEqual(policy["approval"], "never")
        policy = safety.resolve({"sandbox": "workspace-write", "approval": "ask"})
        self.assertEqual(policy["approval"], "ask")
        with self.assertRaises(ValueError):
            safety.resolve({"approval": "sometimes"})

    def test_os_sandbox_flag(self):
        cfg = {}
        with mock.patch.object(safety.sys, "platform", "darwin"), mock.patch.object(
            safety, "seatbelt_usable", return_value=True
        ):
            self.assertIs(safety.resolve({}, cfg=cfg)["os_sandbox"], True)
            self.assertIs(
                safety.resolve({}, cfg={"os_sandbox": "off"})["os_sandbox"], False
            )
            self.assertIs(
                safety.resolve({}, cfg={"os_sandbox": False})["os_sandbox"], False
            )
        with mock.patch.object(safety.sys, "platform", "linux"), mock.patch.object(
            safety, "seatbelt_usable", return_value=True
        ):
            self.assertIs(safety.resolve({}, cfg=cfg)["os_sandbox"], False)
        with mock.patch.object(safety.sys, "platform", "darwin"), mock.patch.object(
            safety, "seatbelt_usable", return_value=False
        ):
            self.assertIs(safety.resolve({}, cfg=cfg)["os_sandbox"], False)


class FlagsTests(unittest.TestCase):
    def test_dsh_env_replaces_unsupported_flags(self):
        policy = safety.resolve({"sandbox": "read-only", "approval": "never"})
        # DSH 0.2.1-rc.5 headless accepts only --json/--session-id. The
        # documented deployment override carries the mode instead.
        self.assertEqual(safety.dsh_flags(policy), [])
        self.assertEqual(safety.dsh_env(policy),
                         {"DSH_PERMISSION_MODE": "read-only"})
        self.assertEqual(
            safety.dsh_env({"sandbox": "danger-full-access"}),
            {"DSH_PERMISSION_MODE": "danger-full-access"},
        )

    def test_add_dir_flags_never_emit_unsupported_option(self):
        with tempfile.TemporaryDirectory() as root:
            child = os.path.join(root, "child")
            os.mkdir(child)
            # The headless profile has no --add-dir; the shim must stay empty
            # rather than abort the run at argument parsing.
            self.assertEqual(safety.add_dir_flags([child]), [])
            self.assertEqual(safety.add_dir_flags([]), [])


class EnvTests(unittest.TestCase):
    def test_scrub_keeps_deepseek_key_and_removes_others(self):
        env = {
            "PATH": "/usr/bin",
            "HOME": "/tmp/home",
            "DEEPSEEK_API_KEY": "sk-" "deepseek-secret",
            "OPENAI_API_KEY": "sk-" "openai-fake",
            "GITHUB_TOKEN": "tok-fake",
            "MY_PASSWORD": "pw-fake",
            "CLIENT_SECRET": "sec-fake",
            "CLOUD_CREDENTIAL": "cred-fake",
            "AUTH_HEADER": "auth-fake",
            "DEEPSEEK_MCP_CONFIG": "/tmp/cfg.json",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.example",
            "OPENAI_BASE_URL": "https://openai.example",
            "SAFE_VAR": "keep-me",
        }
        snapshot = dict(env)
        out = safety.scrubbed_env(env)
        self.assertEqual(out["DEEPSEEK_API_KEY"], "sk-" "deepseek-secret")
        self.assertEqual(out["PATH"], "/usr/bin")
        self.assertEqual(out["SAFE_VAR"], "keep-me")
        for name in (
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "MY_PASSWORD",
            "CLIENT_SECRET",
            "CLOUD_CREDENTIAL",
            "AUTH_HEADER",
            "DEEPSEEK_MCP_CONFIG",
            "DEEPSEEK_BASE_URL",
            "OPENAI_BASE_URL",
        ):
            self.assertNotIn(name, out)
        self.assertEqual(env, snapshot, "input env must not be mutated")

    def test_extra_keep(self):
        env = {"OTHER_TOKEN": "fake", "DEEPSEEK_API_KEY": "fake"}
        out = safety.scrubbed_env(env, extra_keep=("OTHER_TOKEN",))
        self.assertEqual(out["OTHER_TOKEN"], "fake")
        self.assertEqual(out["DEEPSEEK_API_KEY"], "fake")


class SeatbeltTests(unittest.TestCase):
    def test_profile_denies_writes_and_read_only_scope(self):
        ws = os.path.join(tempfile.gettempdir(), "dsh-ws")
        profile = safety.seatbelt_profile(ws, mode="read-only")
        self.assertIn("(version 1)", profile)
        self.assertIn("(allow default)", profile)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn("REVIEW.md", profile)
        self.assertIn(".browser-evidence", profile)
        # TMPDIR may be inside the workspace (literal subpath) or a system
        # temp root (escaped SBPL regex); accept either representation.
        self.assertTrue(".dsh-tmp-" in profile or "dsh\\\\-tmp\\\\-" in profile,
                        profile)
        self.assertIn("/dev/null", profile)
        # no broad workspace write allowance in read-only mode
        self.assertNotIn('(subpath "%s")' % os.path.realpath(ws), profile)

    def test_profile_workspace_write_and_extra(self):
        ws = os.path.realpath("/tmp/dsh-ws")
        extra = os.path.realpath("/tmp/extra")
        profile = safety.seatbelt_profile(ws, mode="workspace-write", extra_writes=[extra])
        self.assertIn('(subpath "%s")' % ws, profile)
        self.assertIn('(subpath "%s")' % extra, profile)

    def test_profile_escapes_quotes_and_backslashes(self):
        profile = safety.seatbelt_profile('/tmp/weird"dir\\x', mode="read-only")
        self.assertIn('\\"', profile)
        self.assertIn("\\\\", profile)


class WrapTests(unittest.TestCase):
    def test_no_os_sandbox_falls_back(self):
        argv, meta = safety.wrap_command(
            ["dsh", "--profile", "sdk"], {"os_sandbox": False}, "/tmp/ws"
        )
        self.assertEqual(argv, ["dsh", "--profile", "sdk"])
        self.assertFalse(meta["wrapped"])
        self.assertIn("reason", meta)

    def test_missing_binary_falls_back(self):
        with mock.patch.object(safety, "seatbelt_usable", return_value=False), \
                mock.patch.object(safety, "_SEATBELT_PROBE",
                                  {"done": True, "usable": False,
                                   "reason": "sandbox-exec not available"}):
            argv, meta = safety.wrap_command(
                ["dsh"], {"os_sandbox": True, "sandbox": "read-only"}, "/tmp/ws"
            )
        self.assertEqual(argv, ["dsh"])
        self.assertFalse(meta["wrapped"])
        self.assertIn("not available", meta["reason"])

    def test_wraps_when_available(self):
        with mock.patch.object(safety, "seatbelt_usable", return_value=True):
            argv, meta = safety.wrap_command(
                ["dsh", "--profile", "sdk"],
                {"os_sandbox": True, "sandbox": "read-only"},
                "/tmp/ws",
            )
        self.assertTrue(meta["wrapped"])
        self.assertEqual(argv[0], "sandbox-exec")
        self.assertEqual(argv[1], "-p")
        self.assertIn("(deny file-write*)", argv[2])
        self.assertEqual(argv[3:], ["dsh", "--profile", "sdk"])

    def test_never_raises_on_bad_input(self):
        with mock.patch.object(safety, "seatbelt_usable", return_value=True):
            argv, meta = safety.wrap_command(None, {"os_sandbox": True}, None)
        self.assertIsInstance(argv, list)
        self.assertIn("wrapped", meta)


class SnapshotDiffTests(unittest.TestCase):
    def test_snapshot_and_diff(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(safety.snapshot_workspace(root), {})
            with open(os.path.join(root, "a.txt"), "w") as handle:
                handle.write("one")
            with open(os.path.join(root, "REVIEW.md"), "w") as handle:
                handle.write("report")
            os.mkdir(os.path.join(root, "sub"))
            with open(os.path.join(root, "sub", "b.txt"), "w") as handle:
                handle.write("bee")
            os.makedirs(os.path.join(root, "node_modules", "pkg"))
            with open(os.path.join(root, "node_modules", "pkg", "x.js"), "w") as handle:
                handle.write("x")

            before = safety.snapshot_workspace(root)
            self.assertIn("a.txt", before)
            self.assertIn("sub/b.txt", before)
            self.assertNotIn("node_modules/pkg/x.js", before)
            self.assertIsInstance(before["a.txt"], tuple)
            self.assertEqual(len(before["a.txt"]), 2)

            with open(os.path.join(root, "a.txt"), "w") as handle:
                handle.write("one plus more text")
            with open(os.path.join(root, "c.txt"), "w") as handle:
                handle.write("cee")
            os.remove(os.path.join(root, "sub", "b.txt"))
            with open(os.path.join(root, "REVIEW.md"), "w") as handle:
                handle.write("report v2")

            after = safety.snapshot_workspace(root)
            changes = safety.diff_workspace(before, after)
            self.assertEqual(
                [(c["path"], c["change"]) for c in changes],
                [("a.txt", "modified"), ("c.txt", "created"), ("sub/b.txt", "deleted")],
            )
            self.assertEqual(safety.diff_workspace({}, {}), [])

    def test_allowed_filtering(self):
        before = {"REVIEW.md": (1, 1), "reports/a.md": (1, 1), "src/x.py": (1, 1)}
        after = {"REVIEW.md": (2, 2), "reports/a.md": (2, 2), "src/x.py": (2, 2)}
        changes = safety.diff_workspace(
            before, after, allowed=("REVIEW.md", "reports/")
        )
        self.assertEqual(changes, [{"path": "src/x.py", "change": "modified"}])
        self.assertEqual(safety.snapshot_workspace("/no/such/workspace/xyz"), {})

    def test_dot_deliverables_are_checked_not_ignored(self):
        before = {"src/x.py": (1, 1)}
        after = {"src/x.py": (1, 1), ".env": (1, 1),
                 ".github/workflows/x.yml": (1, 1)}
        changes = safety.diff_workspace(before, after)
        self.assertEqual(
            [(item["path"], item["change"]) for item in changes],
            [(".env", "created"), (".github/workflows/x.yml", "created")],
        )
        # Known runtime scratch remains ignored.
        scratch = {"src/x.py": (1, 1),
                   ".dsh-tmp-abc/file": (1, 1),
                   ".chrome-profile/x": (1, 1),
                   ".browser-evidence/evidence.json": (1, 1),
                   ".dsh-review-home/profiles/headless/cordis.yml": (1, 1)}
        self.assertEqual(safety.diff_workspace({}, scratch), [
            {"path": "src/x.py", "change": "created"},
        ])

    def test_seatbelt_read_only_does_not_grant_all_of_dsh_home(self):
        ws = os.path.join(tempfile.gettempdir(), "dsh-ws")
        reviewer_home = os.path.join(tempfile.gettempdir(), "private-review-home")
        profile = safety.seatbelt_profile(
            ws, mode="read-only", extra_writes=[reviewer_home],
            dsh_home=reviewer_home)
        dsh_home = os.path.realpath(os.path.expanduser("~/.dsh"))
        self.assertNotIn('(subpath "%s")' % dsh_home, profile)
        self.assertIn(".credentials.yaml", profile)
        self.assertNotIn(os.path.join(dsh_home, "sessions"), profile)
        # The read-only run gets only its private DSH home, never the
        # operator's session/profile store.
        self.assertIn(os.path.realpath(reviewer_home), profile)
        # Even without an explicit private home a read-only profile must never
        # grant the operator's writable DSH directories.
        bare = safety.seatbelt_profile(ws, mode="read-only")
        self.assertNotIn(os.path.join(dsh_home, "sessions"), bare)


class ReviewRedactionTests(unittest.TestCase):
    def test_materialize_review_redacts_key_and_home_path(self):
        from deepseek_mcp import harness

        with tempfile.TemporaryDirectory() as ws:
            review = {"answer": (
                "# Independent review\n\n## Score\nFinal quality score: 9/10\n\n"
                "## Blocking defects\nNone\n\n"
                "Leaked " "sk-" "live-abc123456789 and " + _FAKE_HOME + "/private/notes.txt\n")}
            path = harness._materialize_review(ws, review)
            self.assertIsNotNone(path)
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("sk-" "live-abc123456789", text)
            self.assertNotIn(_FAKE_HOME, text)
            self.assertIn("[redacted-api-key]", text)
            self.assertEqual(review["report_written_by"], "connector")
            self.assertIn("Final quality score: 9/10", text)


if __name__ == "__main__":
    unittest.main()
