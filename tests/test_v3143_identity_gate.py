"""3.14.3 identity gate: exact refspec coverage and exception scoping.

The connector's own ``push_with_gate`` is the hard gate.  These tests prove
that it derives every range from the refspecs it is about to push (not only
``HEAD``), rejects gate-suppressing arguments, and that a
``git_identity_exceptions`` entry only allows the repository's own configured
``(name, email)`` pair on an exactly normalised remote.

stdlib unittest; git is required.  Every git command runs with a private
global config so a developer's own identity cannot influence a result.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import git_identity

CONNECTOR_ROOT = Path(git_identity.__file__).resolve().parent.parent
CFG = {
    "git_identity": {"name": "crewforeman-mcp", "email": "noreply@example.com"},
    "git_identity_exceptions": [],
}
FORK_PAIR = ("Fork Owner", "fork.owner@example.org")
EXCEPTION_CFG = {
    "git_identity": dict(CFG["git_identity"]),
    "git_identity_exceptions": ["example-org/Cypraea"],
}


def _run(args, cwd, env=None):
    return subprocess.run(
        ["git"] + list(args), cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


@unittest.skipUnless(shutil.which("git"), "git is required")
class GateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.global_cfg = self.root / "global.gitconfig"
        self.global_cfg.write_text(
            "[user]\n\tname = Leaky Global\n"
            "\temail = leaky.global@example.org\n",
            encoding="utf-8")
        self._env = dict(os.environ)
        os.environ["GIT_CONFIG_GLOBAL"] = str(self.global_cfg)
        os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self.addCleanup(self._restore_env)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        init = _run(["init", "-b", "main"], self.repo)
        if init.returncode != 0:
            _run(["init"], self.repo)
            _run(["checkout", "-b", "main"], self.repo)
        self.bare = self.root / "remote.git"
        self.assertEqual(
            _run(["init", "--bare", str(self.bare)], self.root).returncode, 0)
        self.git("remote", "add", "origin", str(self.bare))

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def git(self, *args):
        proc = _run(args, self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def commit_connector(self, message, filename=None, text=None):
        self.write(filename or (message + ".txt"),
                   text if text is not None else message)
        self.git("add", "-A")
        rc, _out, err = git_identity.commit_with_identity(
            CFG, self.repo, message)
        self.assertEqual(rc, 0, err)
        return self.head()

    def commit_as(self, message, name, email, filename=None, text=None,
                  env_extra=None):
        self.write(filename or (message + ".txt"),
                   text if text is not None else message)
        self.git("add", "-A")
        env = dict(os.environ)
        env.update(env_extra or {})
        proc = _run(["-c", "user.name=" + name, "-c", "user.email=" + email,
                     "commit", "-m", message], self.repo, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.head()

    def commit_local(self, message, filename=None, text=None, env_extra=None):
        """Commit with the repo-local identity for author and committer."""
        self.write(filename or (message + ".txt"),
                   text if text is not None else message)
        self.git("add", "-A")
        env = dict(os.environ)
        env.update(env_extra or {})
        proc = _run(["commit", "-m", message], self.repo, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.head()

    def set_local_identity(self, name=FORK_PAIR[0], email=FORK_PAIR[1]):
        self.git("config", "--local", "user.name", name)
        self.git("config", "--local", "user.email", email)

    def track(self, branch, sha):
        self.git("update-ref", "refs/remotes/origin/" + branch, sha)

    def bare_refs(self):
        proc = _run(["--git-dir", str(self.bare), "for-each-ref",
                     "--format=%(refname)"], self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [line for line in proc.stdout.splitlines() if line]

    def bare_has(self, ref):
        proc = _run(["--git-dir", str(self.bare), "rev-parse", "--verify",
                     ref], self.root)
        return proc.returncode == 0

    def make_bare(self, name):
        path = self.root / name
        self.assertEqual(
            _run(["init", "--bare", str(path)], self.root).returncode, 0)
        return path

    def refs_of(self, path):
        proc = _run(["--git-dir", str(path), "for-each-ref",
                     "--format=%(refname)"], self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [line for line in proc.stdout.splitlines() if line]

    def has_ref(self, path, ref):
        proc = _run(["--git-dir", str(path), "rev-parse", "--verify", ref],
                    self.root)
        return proc.returncode == 0

    def hook_env(self):
        env = dict(os.environ)
        env["CF_PYTHON"] = sys.executable
        return env


class PushRangeTests(GateTestBase):
    def test_foreign_commit_on_side_ref_is_blocked(self):
        base = self.commit_connector("base")
        self.git("checkout", "-b", "side")
        self.commit_as("foreign-side", "Outsider", "outsider@example.org")
        self.git("checkout", "main")
        self.assertEqual(self.head(), base, "HEAD itself must stay clean")
        self.track("side", base)
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "side:side")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("allowlist", err)
        self.assertEqual(self.bare_refs(), [],
                         "a blocked side ref must not reach the remote")

    def test_every_refspec_in_a_list_is_gated(self):
        base = self.commit_connector("base")
        self.git("checkout", "-b", "side")
        self.commit_as("foreign-side", "Outsider", "outsider@example.org")
        self.git("checkout", "main")
        self.track("side", base)
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", ["main:main", "side:side"])
        self.assertNotEqual(rc, 0, err)
        self.assertIn("side:side", err)
        self.assertEqual(self.bare_refs(), [])

    def test_passing_gate_pushes_exactly_the_requested_refspec(self):
        base = self.commit_connector("base")
        self.git("checkout", "-b", "side")
        side = self.commit_connector("side-work")
        self.git("checkout", "main")
        self.track("side", base)
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "side:side")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.bare_has("refs/heads/side"))
        self.assertFalse(self.bare_has("refs/heads/main"),
                         "only the requested refspec is pushed")
        proc = _run(["--git-dir", str(self.bare), "rev-parse",
                     "refs/heads/side"], self.root)
        self.assertEqual(proc.stdout.strip(), side)

    def test_no_verify_is_rejected_before_git_push(self):
        self.commit_connector("base")
        with mock.patch.object(git_identity, "_git",
                               wraps=git_identity._git) as spy:
            rc, _out, err = git_identity.push_with_gate(
                CFG, self.repo, "origin", "main:main",
                extra_args=["--no-verify"])
        self.assertNotEqual(rc, 0)
        self.assertIn("no-verify", err.lower())
        push_calls = [
            call for call in spy.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], list)
            and call.args[1] and call.args[1][0] == "push"
        ]
        self.assertEqual(push_calls, [], "git push must never be invoked")
        self.assertEqual(self.bare_refs(), [])

    def test_dry_run_is_not_mistaken_for_no_verify(self):
        # For push, -n is --dry-run, so it must not be rejected as if it
        # suppressed the gate; it also must not skip the gate.
        self.assertEqual(git_identity.validate_push_args(["-n"]), ["-n"])
        self.assertEqual(
            git_identity.validate_push_args(["--dry-run", "--no-gpg-sign"]),
            ["--dry-run", "--no-gpg-sign"])
        self.commit_connector("base")
        for flag in ("-n", "--dry-run"):
            with self.subTest(flag=flag):
                rc, _out, err = git_identity.push_with_gate(
                    CFG, self.repo, "origin", "main:main", extra_args=[flag])
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.bare_refs(), [])

    def test_unknown_refspec_fails_closed_without_push(self):
        self.commit_connector("base")
        bad_refspecs = (
            "does-not-exist:main",
            "refs/heads/does-not-exist",
            "main:",
            "../outside:main",
            "refs/heads/*:refs/heads/*",
        )
        for refspec in bad_refspecs:
            with self.subTest(refspec=refspec):
                rc, _out, _err = git_identity.push_with_gate(
                    CFG, self.repo, "origin", refspec)
                self.assertNotEqual(rc, 0)
        rc, _out, _err = git_identity.push_with_gate(
            CFG, self.repo, "origin", None)
        self.assertNotEqual(rc, 0, "an empty refspec cannot determine a range")
        self.assertEqual(self.bare_refs(), [])

    def test_force_and_ref_expanding_arguments_fail_closed(self):
        self.commit_connector("base")
        cases = (
            ("+main:main", None),
            ("main:main", ["--force"]),
            ("main:main", ["--force-with-lease"]),
            ("main:main", ["--all"]),
            ("main:main", ["--tags"]),
            ("main:main", ["--mirror"]),
            ("main:main", ["--prune"]),
        )
        for refspec, extra in cases:
            with self.subTest(refspec=refspec, extra=extra):
                rc, _out, _err = git_identity.push_with_gate(
                    CFG, self.repo, "origin", refspec, extra_args=extra)
                self.assertNotEqual(rc, 0)
        self.assertEqual(self.bare_refs(), [])

    def test_remote_that_looks_like_an_option_fails_closed(self):
        self.commit_connector("base")
        for remote in ("--no-verify", "--repo=/tmp/elsewhere", ""):
            with self.subTest(remote=remote):
                rc, _out, _err = git_identity.push_with_gate(
                    CFG, self.repo, remote, "main:main")
                self.assertNotEqual(rc, 0)
        self.assertEqual(self.bare_refs(), [])

    def test_missing_or_incomplete_gate_report_fails_closed(self):
        self.commit_connector("base")
        reports = (
            None,
            {},
            {"ok": True},
            {"ok": False, "messages": ["blocked"]},
            {"ok": True, "head": "0" * 40, "metadata": [{"sha": "0" * 40}]},
            {"ok": True, "head": self.head(), "metadata": []},
        )
        for report in reports:
            with self.subTest(report=report):
                with mock.patch.object(git_identity, "check_push",
                                       return_value=report):
                    rc, _out, _err = git_identity.push_with_gate(
                        CFG, self.repo, "origin", "main:main")
                self.assertNotEqual(rc, 0)
        with mock.patch.object(git_identity, "check_push",
                               side_effect=RuntimeError("gate crashed")):
            rc, _out, err = git_identity.push_with_gate(
                CFG, self.repo, "origin", "main:main")
        self.assertNotEqual(rc, 0)
        self.assertIn("gate crashed", err)
        self.assertEqual(self.bare_refs(), [])


    def test_repointed_remote_with_stale_tracking_ref_is_blocked(self):
        base = self.commit_connector("base")
        foreign = self.commit_as("foreign", "Outsider",
                                 "outsider@example.org")
        old = self.make_bare("old.git")
        self.git("remote", "set-url", "origin", str(old))
        proc = _run(["--git-dir", str(old), "fetch", str(self.repo),
                     "refs/heads/main:refs/heads/main"], self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.git("fetch", "origin",
                 "refs/heads/main:refs/remotes/origin/main")
        self.assertEqual(
            self.git("rev-parse", "refs/remotes/origin/main").strip(),
            foreign)
        new = self.make_bare("new.git")
        self.git("remote", "set-url", "origin", str(new))
        # The stale tracking ref says the new remote already has the foreign
        # tip; the destination is actually empty, so the gate must scan all
        # history and block.
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "main:main")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("allowlist", err)
        self.assertEqual(self.refs_of(new), [])
        # The installed hook must make the same decision from the zero
        # remote_sha supplied by git (not from the stale tracking ref).
        proc = _run(["push", "origin", "main:main"], self.repo,
                    env=self.hook_env())
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.refs_of(new), [])

    def test_empty_destination_ignores_stale_tracking_ref(self):
        self.commit_as("foreign-root", "Outsider", "outsider@example.org")
        clean = self.commit_connector("clean-head")
        self.track("main", clean)
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "main:main")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("allowlist", err)
        self.assertEqual(self.bare_refs(), [])

    def test_unreachable_remote_scans_full_history(self):
        self.commit_as("foreign-root", "Outsider", "outsider@example.org")
        self.commit_connector("clean-head")
        self.git("remote", "set-url", "origin",
                 str(self.root / "does-not-exist.git"))
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "main:main")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("allowlist", err)

    def test_push_sends_the_scanned_sha_not_a_moved_ref(self):
        self.commit_connector("base")
        clean = self.commit_connector("clean-head")
        tree = self.git("rev-parse", "HEAD^{tree}").strip()
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": "Outsider",
            "GIT_AUTHOR_EMAIL": "outsider@example.org",
            "GIT_COMMITTER_NAME": "Outsider",
            "GIT_COMMITTER_EMAIL": "outsider@example.org",
        })
        proc = _run(["commit-tree", tree, "-p", clean, "-m", "foreign"],
                    self.repo, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        foreign = proc.stdout.strip()
        self.assertNotEqual(foreign, clean)
        real_check_push = git_identity.check_push

        def racing_check(*args, **kwargs):
            report = real_check_push(*args, **kwargs)
            # Move the mutable ref after the gate, before git push runs.
            self.git("update-ref", "refs/heads/main", foreign)
            return report

        with mock.patch.object(git_identity, "check_push",
                               side_effect=racing_check):
            rc, _out, err = git_identity.push_with_gate(
                CFG, self.repo, "origin", "main:main")
        self.assertEqual(rc, 0, err)
        landed = _run(["--git-dir", str(self.bare), "rev-parse", "--verify",
                       "refs/heads/main"], self.root).stdout.strip()
        self.assertEqual(landed, clean,
                         "the push must send the sha the gate scanned")
        self.assertNotEqual(landed, foreign)

    def test_content_line_starting_with_plus_plus_is_scanned(self):
        base = self.commit_connector("base")
        secret = "sk-" + "a" * 30
        body = ("++api_" + "key = " + chr(34) + secret + chr(34) + "\n"
                "++contact: personal@" "example.invalid\n")
        self.commit_connector("plusplus", "plusplus.txt", body)
        report = git_identity.check_push(
            CFG, repo=str(self.repo), base=base, head="HEAD")
        self.assertFalse(report["ok"], report)
        rules = {v.get("rule") for v in report["violations"]}
        self.assertIn("api_key", rules, report["violations"])
        self.assertIn("email", rules, report["violations"])
        rc, _out, _err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "main:main")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.bare_refs(), [])

    def test_installed_hook_ignores_target_repo_decoy_gate(self):
        self.commit_as("foreign", "Outsider", "outsider@example.org")
        ok, detail = git_identity.ensure_pre_push_hook(self.repo)
        self.assertTrue(ok, detail)
        pkg = self.repo / "deepseek_mcp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "git_identity.py").write_text(
            "import sys\nprint('fake hook gate OK')\nsys.exit(0)\n",
            encoding="utf-8")
        proc = _run(["push", "origin", "main:main"], self.repo,
                    env=self.hook_env())
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("fake hook gate OK", proc.stdout)
        self.assertEqual(self.bare_refs(), [])


    def test_delete_refspecs_are_materialised_as_destination_deletes(self):
        self.commit_connector("base")
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "main:main")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.bare_has("refs/heads/main"))
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", ":main")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.bare_has("refs/heads/main"))
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "main:main")
        self.assertEqual(rc, 0, err)
        rc, _out, err = git_identity.push_with_gate(
            CFG, self.repo, "origin", "refs/heads/main",
            extra_args=["--delete"])
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.bare_has("refs/heads/main"))

    def test_ship_push_uses_connector_module_not_target_decoy(self):
        # Plain commits: no installed hook, so a decoy module that falls back
        # to a bare `git push` would really land the foreign side ref.
        self.commit_as("base", "crewforeman-mcp", "noreply@example.com")
        self.git("checkout", "-b", "side")
        self.commit_as("foreign-side", "Outsider", "outsider@example.org")
        self.git("checkout", "main")
        self.assertFalse((self.repo / ".git" / "hooks" / "pre-push").exists())
        pkg = self.repo / "deepseek_mcp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "git_identity.py").write_text(
            "import subprocess\n"
            "class GitIdentityError(Exception):\n"
            "    pass\n"
            "def validate_push_args(extra_args=None):\n"
            "    return list(extra_args or [])\n"
            "def push_with_gate(cfg, repo, remote, refspec=None, base='', "
            "head='HEAD', extra_args=None):\n"
            "    subprocess.run(['git', 'push', remote, refspec], "
            "cwd=repo, check=False)\n"
            "    return 0, '', ''\n",
            encoding="utf-8")
        cfg_path = self.root / "ship-config.json"
        cfg_path.write_text(json.dumps(CFG), encoding="utf-8")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable,
             str(CONNECTOR_ROOT / "scripts" / "ship_push.py"),
             "--repo", str(self.repo), "--remote", "origin",
             "--refspec", "side:side", "--config", str(cfg_path)],
            cwd=str(self.root), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(proc.returncode, 0,
                            proc.stdout + proc.stderr)
        self.assertIn("allowlist", proc.stderr, proc.stderr)
        self.assertFalse(self.bare_has("refs/heads/side"))


class ExceptionScopeTests(GateTestBase):
    def _exception_remote(self):
        self.git("remote", "add", "upstream",
                 "https://github.com/example-org/Cypraea.git")

    def test_exception_does_not_skip_the_metadata_scan(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.commit_as("foreign", "Outsider", "outsider@example.org")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["exception"], "example-org/Cypraea")
        self.assertTrue(any(v.get("role") == "author"
                            and v.get("email") == "outsider@example.org"
                            for v in report["violations"]), report)

    def test_exception_allows_only_the_repo_local_pair(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        owner = self.commit_local("owner")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertTrue(report["ok"], report["messages"])
        self.assertEqual(report["exception_identity"],
                         {"name": FORK_PAIR[0], "email": FORK_PAIR[1]})

        # Same email, foreign name -> blocked (not two independent sets).
        self.git("reset", "--hard", owner)
        self.commit_as("foreign-name", "Outsider", FORK_PAIR[1])
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("field") == "name" for v in
                            report["violations"]), report)

        # The global default pair is NOT carried over to an exception remote.
        self.git("reset", "--hard", owner)
        self.commit_as("default-email-foreign-name", "Outsider",
                       "noreply@example.com")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("field") == "email" for v in
                            report["violations"]), report)

        # Local name with a foreign email -> blocked.
        self.git("reset", "--hard", owner)
        self.commit_as("foreign-email", FORK_PAIR[0],
                       "foreign.person@" "example.invalid")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("field") == "email" for v in
                            report["violations"]), report)

    def test_exception_scans_the_committer_pair_too(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        owner = self.commit_local("owner")
        self.git("reset", "--hard", owner)
        # Author is the repo-local pair; committer carries a foreign name
        # with the repo-local address, so the pair (not either half) fails.
        self.commit_local(
            "committer-foreign",
            env_extra={"GIT_COMMITTER_NAME": "Committer Outsider",
                       "GIT_COMMITTER_EMAIL": FORK_PAIR[1]})
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("role") == "committer"
                            and v.get("field") == "name"
                            for v in report["violations"]), report)

    def test_exception_blocks_the_default_connector_pair(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        self.commit_as("connector-default", "crewforeman-mcp",
                       "noreply@example.com")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["exception"], "example-org/Cypraea")

    def test_exception_blocks_a_configured_connector_identity(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        owner = self.commit_local("owner")
        cfg = {
            "git_identity": {"name": "connector-bot",
                             "email": "connector.bot@example.org"},
            "git_identity_exceptions": ["example-org/Cypraea"],
        }
        # The configured connector identity is globally allowed, but on the
        # exception remote only the repo-local pair may pass.
        self.commit_as("connector-identity", "connector-bot",
                       "connector.bot@example.org")
        report = git_identity.check_push(
            cfg, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)

        # The repo-local pair is still allowed on that same remote.
        self.git("reset", "--hard", owner)
        report = git_identity.check_push(
            cfg, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertTrue(report["ok"], report["messages"])

    def test_exception_without_repo_local_email_allows_nothing(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.git("config", "--local", "--unset", "user.email")
        self.assertIsNone(git_identity.repo_local_identity(self.repo))
        self.commit_as("foreign", "Outsider", "outsider@example.org")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["exception"], "example-org/Cypraea")
        self.assertEqual(report["exception_identity"], {})

        # The same is true when only the name exists.
        self.git("config", "--local", "user.name", FORK_PAIR[0])
        self.git("config", "--local", "--unset", "user.name")
        self.git("config", "--local", "user.email", FORK_PAIR[1])
        self.assertIsNone(git_identity.repo_local_identity(self.repo))

    def test_exception_message_trailer_allows_only_its_email(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        self.commit_local(
            "trailer-ok\n\nCo-authored-by: Fork Owner <%s>" % FORK_PAIR[1],
            filename="trailer-ok.txt", text="ok\n")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertTrue(report["ok"], report["messages"])

        self.commit_local(
            "trailer-bad\n\nCo-authored-by: Outsider <outsider@" "example.invalid>",
            filename="trailer-bad.txt", text="bad\n")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("field") == "message"
                            and v.get("email") == ("outsider@" "example.invalid")
                            for v in report["violations"]), report)

    def test_exception_message_trailer_blocks_connector_email(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        # The author/committer are the repo-local pair, but the trailer names
        # the globally configured connector address: on an exception remote
        # only the repo-local address is allowed in messages/trailers.
        self.commit_local(
            "trailer-connector\n\nCo-authored-by: Connector "
            "<noreply@example.com>",
            filename="trailer-connector.txt", text="x\n")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("field") == "message"
                            and v.get("email") == "noreply@example.com"
                            for v in report["violations"]), report)

    def test_exception_content_allows_its_email_and_blocks_another(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        self.commit_local("contact", "contact.txt",
                          "contact: %s\n" % FORK_PAIR[1])
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertTrue(report["ok"], report["messages"])

        self.commit_local("other-contact", "other-contact.txt",
                          "contact: another.person@" "example.invalid\n")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("rule") == "email"
                            for v in report["violations"]), report)

    def test_exception_content_blocks_connector_email(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        cfg = {
            "git_identity": {"name": "connector-bot",
                             "email": "connector.bot@example.org"},
            "git_identity_exceptions": ["example-org/Cypraea"],
        }
        # Metadata is the repo-local pair; content names the globally
        # configured connector address.  It must be blocked even though its
        # domain is a placeholder, because content on an exception remote may
        # only carry the repo-local address (other placeholders stay allowed).
        self.commit_local("connector-content", "connector-content.txt",
                          "contact: connector.bot@example.org\n")
        report = git_identity.check_push(
            cfg, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(v.get("rule") == "email"
                            and v.get("kind") == "content"
                            for v in report["violations"]), report)

        self.git("reset", "--hard", base)
        self.set_local_identity()
        self.commit_local("local-content", "local-content.txt",
                          "contact: %s\n" % FORK_PAIR[1])
        report = git_identity.check_push(
            cfg, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertTrue(report["ok"], report["messages"])

    def test_exception_content_still_blocks_a_secret(self):
        base = self.commit_connector("base")
        self._exception_remote()
        self.set_local_identity()
        secret = "sk-" + "a" * 30
        self.commit_local(
            "secret", "notes.txt",
            "api_" + "key = " + chr(34) + secret + chr(34) + "\n")
        report = git_identity.check_push(
            EXCEPTION_CFG, repo=str(self.repo), base=base, head="HEAD",
            remote="upstream")
        self.assertFalse(report["ok"], report)
        self.assertIn("api_key", {v.get("rule")
                                  for v in report["violations"]})


class RemoteMatchingTests(GateTestBase):
    def _add(self, name, url):
        self.git("remote", "add", name, url)

    def test_owner_repo_shorthand_is_github_exact(self):
        self._add("exact", "https://github.com/example-org/Cypraea.git")
        self._add("evil", "https://evil.example/example-org/Cypraea.git")
        self._add("prefix", "https://github.com/example-org/Cypraea-fork.git")
        self._add("suffix",
                  "https://github.com/attacker/example-org/Cypraea.git")
        self._add("case", "https://github.com/Example-Org/Cypraea.git")
        self._add("scp", "git" "@github.com:example-org/Cypraea.git")
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo,
                                              "exact"),
            "example-org/Cypraea")
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo, "scp"),
            "example-org/Cypraea")
        for name in ("evil", "prefix", "suffix", "case"):
            with self.subTest(name=name):
                self.assertEqual(
                    git_identity.exception_for_remote(
                        EXCEPTION_CFG, self.repo, name), "")

    def test_full_url_entry_matches_exact_host_and_path(self):
        self._add("exact", "https://github.com/example-org/Cypraea.git")
        self._add("evil", "https://evil.example/example-org/Cypraea.git")
        for entry in ("https://github.com/example-org/Cypraea",
                      "https://github.com/example-org/Cypraea.git",
                      "https://GitHub.com/example-org/Cypraea.git",
                      "ssh://git" "@github.com/example-org/Cypraea.git",
                      "git" "@github.com:example-org/Cypraea.git"):
            with self.subTest(entry=entry):
                cfg = dict(EXCEPTION_CFG)
                cfg["git_identity_exceptions"] = [entry]
                self.assertEqual(
                    git_identity.exception_for_remote(cfg, self.repo, "exact"),
                    entry)
        cfg = dict(EXCEPTION_CFG)
        cfg["git_identity_exceptions"] = [
            "https://evil.example/example-org/Cypraea.git"]
        self.assertEqual(
            git_identity.exception_for_remote(cfg, self.repo, "exact"), "")

    def test_invalid_exception_entries_match_nothing(self):
        self._add("exact", "https://github.com/example-org/Cypraea.git")
        self._add("origin-github", "https://github.com/example-org/Cypraea")
        for entry in ("origin", "Cypraea", "github.com/example-org/Cypraea",
                      "a/b/c", "personal@example.org", "",
                      "https://github.com/example-org/Cypraea?x=1"):
            with self.subTest(entry=entry):
                cfg = dict(EXCEPTION_CFG)
                cfg["git_identity_exceptions"] = [entry]
                self.assertEqual(
                    git_identity.exception_for_remote(cfg, self.repo, "exact"),
                    "")
                self.assertEqual(
                    git_identity.exception_for_remote(
                        cfg, self.repo, "origin-github"), "")

    def test_bare_remote_name_entry_never_matches(self):
        self._add("origin-github", "https://github.com/example-org/Cypraea")
        cfg = dict(EXCEPTION_CFG)
        cfg["git_identity_exceptions"] = ["origin"]
        self.assertEqual(
            git_identity.exception_for_remote(cfg, self.repo, "origin"), "")
        self.assertEqual(
            git_identity.exception_for_remote(cfg, self.repo,
                                              "origin-github"), "")

    def test_pushurl_cannot_borrow_the_fetch_url_exception(self):
        self._add("split", "https://github.com/example-org/Cypraea.git")
        self.git("remote", "set-url", "--push", "split",
                 "https://evil.example/example-org/Cypraea.git")
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo,
                                              "split"), "")

    def test_direct_url_is_a_candidate_but_only_exact(self):
        exact = "https://github.com/example-org/Cypraea.git"
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo,
                                              exact),
            "example-org/Cypraea")
        self.assertEqual(
            git_identity.exception_for_remote(
                EXCEPTION_CFG, self.repo,
                "https://github.com/Example-Org/Cypraea.git"), "")
        self.assertEqual(
            git_identity.exception_for_remote(
                EXCEPTION_CFG, self.repo,
                "https://evil.example/example-org/Cypraea.git"), "")
        # A query string can change what an HTTP server serves; fail closed
        # rather than normalising it away.
        self.assertEqual(
            git_identity.exception_for_remote(
                EXCEPTION_CFG, self.repo, exact + "?ref=other"), "")
        # A bare local path is not GitHub shorthand and cannot borrow it.
        self.assertEqual(
            git_identity.exception_for_remote(
                EXCEPTION_CFG, self.repo,
                "github.com/example-org/Cypraea"), "")


    def test_multiple_pushurls_must_all_normalize_the_same(self):
        exact = "https://github.com/example-org/Cypraea.git"
        self._add("split", exact)
        self.git("remote", "set-url", "--push", "split", exact)
        self.git("remote", "set-url", "--push", "--add", "split",
                 "https://evil.example/example-org/Cypraea.git")
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo,
                                              "split"), "")
        # Two differently-spelled but identical normalized destinations are
        # one destination and may use the exception.
        self._add("same", exact)
        self.git("remote", "set-url", "--push", "same", exact)
        self.git("remote", "set-url", "--push", "--add", "same",
                 "https://GitHub.com/example-org/Cypraea.git/")
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo,
                                              "same"),
            "example-org/Cypraea")

    def test_insteadof_rewrite_cannot_borrow_exception(self):
        self.commit_connector("base")
        self.set_local_identity()
        self.commit_local("local", "local.txt", "local\n")
        dest = self.root / "dest"
        dest.mkdir()
        evil = dest / "Cypraea.git"
        self.assertEqual(
            _run(["init", "--bare", str(evil)], self.root).returncode, 0)
        self.git("config", "--local",
                 "url.file://%s/.insteadOf" % dest,
                 "https://github.com/example-org/")
        url = "https://github.com/example-org/Cypraea.git"
        # The literal URL matches, but git rewrites it to a different host;
        # both destinations must agree before an exception can be granted.
        self.assertEqual(
            git_identity.exception_for_remote(EXCEPTION_CFG, self.repo, url),
            "")
        rc, _out, err = git_identity.push_with_gate(
            EXCEPTION_CFG, self.repo, url, "main:main")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("allowlist", err)
        self.assertFalse(self.has_ref(evil, "refs/heads/main"))


if __name__ == "__main__":
    unittest.main()
