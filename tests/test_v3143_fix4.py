"""Tests for deepseek_mcp.git_identity (FIX 4 commit-identity gate).

stdlib unittest; Windows-compatible (tempfile/pathlib, no shell, no network).
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

import deepseek_mcp
from deepseek_mcp import config as config_mod
from deepseek_mcp import git_identity

REPO_ROOT = Path(deepseek_mcp.__file__).resolve().parent.parent

CFG = {
    "git_identity": {"name": "crewforeman-mcp", "email": "noreply@example.com"},
    "git_identity_exceptions": [],
}


def _run(args, cwd, env=None):
    return subprocess.run(
        ["git"] + list(args), cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A deliberately different "personal" global identity: nothing the
        # connector commits may pick it up.
        self.global_cfg = self.root / "global.gitconfig"
        self.global_cfg.write_text(
            "[user]\n\tname = Leaky Global\n\temail = leaky.global@example.org\n",
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

    def commit(self, message, name="a.txt", text=None):
        self.write(name, text if text is not None else message)
        self.git("add", "-A")
        rc, _out, err = git_identity.commit_with_identity(CFG, self.repo, message)
        self.assertEqual(rc, 0, err)
        return self.git("rev-parse", "HEAD").strip()

    def foreign_commit(self, message="foreign", email="outsider@example.org"):
        self.write(message + ".txt", message)
        self.git("add", "-A")
        self.git("-c", "user.name=Outsider", "-c", "user.email=" + email,
                 "commit", "-m", message)

    def ident(self, rev="HEAD"):
        out = self.git("log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", rev)
        return out.rstrip("\n").split("\x00")

    def test_config_defaults_are_the_documented_identity(self):
        self.assertEqual(config_mod.DEFAULTS["git_identity"],
                         {"name": "crewforeman-mcp",
                          "email": "noreply@example.com"})
        self.assertEqual(config_mod.DEFAULTS["git_identity_exceptions"], [])

    def test_commit_uses_repo_local_identity_not_global(self):
        self.commit("first")
        author, aemail, committer, cemail = self.ident()
        self.assertEqual((author, aemail), ("crewforeman-mcp", "noreply@example.com"))
        self.assertEqual((committer, cemail), ("crewforeman-mcp", "noreply@example.com"))
        self.assertEqual(self.git("config", "--local", "--get", "user.email").strip(),
                         "noreply@example.com")
        self.assertNotIn("leaky", self.git("log", "-1", "--format=%ae%x00%ce").lower())

    def test_commit_ignores_identity_environment_override(self):
        """GIT_AUTHOR_*/GIT_COMMITTER_* must not beat the configured identity.

        Git ranks those variables above ``-c user.name``/``user.email``, so a
        parent process that exports them would otherwise author the connector
        commit with a personal identity even though the repo config is right.
        """
        self.write("env.txt", "x")
        self.git("add", "-A")
        leak = {
            "GIT_AUTHOR_NAME": "Personal Author",
            "GIT_AUTHOR_EMAIL": "personal.author@example.org",
            "GIT_COMMITTER_NAME": "Personal Committer",
            "GIT_COMMITTER_EMAIL": "personal.committer@example.org",
        }
        with mock.patch.dict(os.environ, leak, clear=False):
            rc, _out, err = git_identity.commit_with_identity(
                CFG, self.repo, "env override")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ident(),
                         ["crewforeman-mcp", "noreply@example.com",
                          "crewforeman-mcp", "noreply@example.com"])

    def test_rebase_ignores_identity_environment_override(self):
        self.commit("base", "base.txt")
        self.git("checkout", "-b", "topic")
        self.commit("topic", "topic.txt")
        self.git("checkout", "main")
        self.commit("main", "main.txt")
        self.git("checkout", "topic")
        leak = {
            "GIT_AUTHOR_NAME": "Personal Author",
            "GIT_AUTHOR_EMAIL": "personal.author@example.org",
            "GIT_COMMITTER_NAME": "Personal Committer",
            "GIT_COMMITTER_EMAIL": "personal.committer@example.org",
        }
        with mock.patch.dict(os.environ, leak, clear=False):
            rc, _out, err = git_identity.rebase_with_identity(
                CFG, self.repo, "main")
        self.assertEqual(rc, 0, err)
        for line in self.git(
                "log", "--format=%an%x00%ae%x00%cn%x00%ce").splitlines():
            if line:
                self.assertEqual(line.split("\x00"),
                                 ["crewforeman-mcp", "noreply@example.com",
                                  "crewforeman-mcp", "noreply@example.com"])

    def test_rebase_keeps_configured_identity(self):
        self.commit("base", "base.txt")
        self.git("checkout", "-b", "topic")
        self.commit("topic", "topic.txt")
        self.git("checkout", "main")
        self.commit("main", "main.txt")
        self.git("checkout", "topic")
        rc, _out, err = git_identity.rebase_with_identity(CFG, self.repo, "main")
        self.assertEqual(rc, 0, err)
        lines = [line for line in
                 self.git("log", "--format=%an%x00%ae%x00%cn%x00%ce").splitlines()
                 if line]
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(line.split("\x00"),
                             ["crewforeman-mcp", "noreply@example.com",
                              "crewforeman-mcp", "noreply@example.com"])

    def test_linked_worktree_commit_uses_identity_and_gate(self):
        self.commit("base", "base.txt")
        worktree = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "topic", str(worktree))
        (worktree / "work.txt").write_text("work\n", encoding="utf-8")
        self.assertEqual(_run(["add", "-A"], worktree).returncode, 0)
        rc, _out, err = git_identity.commit_with_identity(
            CFG, worktree, "worktree commit")
        self.assertEqual(rc, 0, err)
        ident = _run(["log", "-1", "--format=%an|%ae|%cn|%ce"], worktree)
        self.assertEqual(ident.stdout.strip(),
                         "crewforeman-mcp|noreply@example.com|"
                         "crewforeman-mcp|noreply@example.com")
        hooks = _run(["rev-parse", "--git-path", "hooks"], worktree).stdout.strip()
        hook_path = Path(hooks)
        if not hook_path.is_absolute():
            hook_path = worktree / hook_path
        self.assertTrue((hook_path / "pre-push").is_file())

    def test_scan_cap_is_configurable_and_fails_closed(self):
        base = self.commit("base", "base.txt")
        self.commit("second", "second.txt")
        self.commit("third", "third.txt")
        cfg = dict(CFG)
        cfg["git_identity_max_scan_commits"] = 1
        report = git_identity.check_push(
            cfg, repo=str(self.repo), base=base, head="HEAD")
        self.assertFalse(report["ok"], report)
        self.assertTrue(any("too_many_commits" in str(v.get("detail"))
                            for v in report["violations"]), report)

    def test_missing_or_unresolvable_head_fails_closed(self):
        base = self.commit("base", "base.txt")
        for head in ("", "refs/heads/does-not-exist"):
            with self.subTest(head=head):
                report = git_identity.check_push(
                    CFG, repo=str(self.repo), base=base, head=head)
                self.assertFalse(report["ok"], report)
                self.assertTrue(any(v.get("kind") == "error"
                                    for v in report["violations"]), report)

    def test_metadata_gate_blocks_foreign_author_email(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.get("email") == "outsider@example.org"
                            for v in report["violations"]))
        self.assertTrue(any("outsider@example.org" in m
                            for m in report["messages"]))

    def test_metadata_gate_checks_committer_too(self):
        base = self.commit("base", "base.txt")
        self.write("committer.txt", "x")
        self.git("add", "-A")
        env = dict(os.environ)
        env["GIT_COMMITTER_NAME"] = "Committer Outsider"
        env["GIT_COMMITTER_EMAIL"] = "committer@example.org"
        proc = _run(["-c", "user.name=crewforeman-mcp",
                     "-c", "user.email=noreply@example.com",
                     "commit", "-m", "committer"], self.repo, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.get("role") == "committer"
                            and v.get("email") == "committer@example.org"
                            for v in report["violations"]))

    def test_exception_remote_allows_foreign_identity(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        self.git("remote", "add", "upstream",
                 "https://github.com/example-org/Cypraea.git")
        cfg = {"git_identity": dict(CFG["git_identity"]),
               "git_identity_exceptions": ["example-org/Cypraea"]}
        blocked = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                          head="HEAD", remote="upstream")
        self.assertFalse(blocked["ok"])
        allowed = git_identity.check_push(cfg, repo=str(self.repo), base=base,
                                          head="HEAD", remote="upstream")
        self.assertTrue(allowed["ok"], allowed["messages"])
        self.assertEqual(allowed["exception"], "example-org/Cypraea")

    def test_exception_is_exact_not_a_substring(self):
        """An entry must not exempt evil.example/fork-of-owner/repo."""
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        self.git("remote", "add", "evil",
                 "https://evil.example/fork-of-example-org/Cypraea.git")
        cfg = {"git_identity": dict(CFG["git_identity"]),
               "git_identity_exceptions": ["example-org/Cypraea"]}
        report = git_identity.check_push(cfg, repo=str(self.repo), base=base,
                                         head="HEAD", remote="evil")
        self.assertFalse(report["ok"], report)

    def test_exception_matches_full_url_and_owner_repo(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        self.git("remote", "add", "upstream",
                 "https://github.com/example-org/Cypraea.git")
        for entry in ("https://github.com/example-org/Cypraea",
                      "https://github.com/example-org/Cypraea.git",
                      "example-org/Cypraea"):
            cfg = {"git_identity": dict(CFG["git_identity"]),
                   "git_identity_exceptions": [entry]}
            report = git_identity.check_push(
                cfg, repo=str(self.repo), base=base, head="HEAD",
                remote="upstream")
            self.assertTrue(report["ok"], (entry, report["messages"]))

    def test_direct_url_push_cannot_borrow_a_configured_exception(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        cfg = {"git_identity": dict(CFG["git_identity"]),
               "git_identity_exceptions": ["example-org/Cypraea"]}
        report = git_identity.check_push(
            cfg, repo=str(self.repo), base=base, head="HEAD",
            remote="https://github.com/example-org/Cypraea.git")
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["exception"], "")

    def test_bare_remote_name_is_not_an_exception(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        self.git("remote", "add", "origin",
                 "https://evil.example/attacker/repo.git")
        cfg = {"git_identity": dict(CFG["git_identity"]),
               "git_identity_exceptions": ["origin"]}
        report = git_identity.check_push(cfg, repo=str(self.repo), base=base,
                                         head="HEAD", remote="origin")
        self.assertFalse(report["ok"], "a local label must not exempt a URL")

    def test_connector_installs_the_pre_push_hook(self):
        self.commit("first")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.is_file())
        text = hook.read_text(encoding="utf-8")
        self.assertIn("CrewForeman pre-push gate (auto-installed)", text)
        self.assertIn("deepseek_mcp.git_identity", text)
        # The installed hook must be able to import the package even when the
        # clone is not the connector checkout and the package is not installed.
        self.assertIn(str(REPO_ROOT), text)

    def test_existing_operator_hook_is_chained_not_discarded(self):
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        operator = "#!/bin/sh\necho operator-hook\n"
        hook.write_text(operator, encoding="utf-8")
        self.commit("first")
        self.assertEqual((hook.parent / "pre-push.local").read_text(
            encoding="utf-8"), operator)
        text = hook.read_text(encoding="utf-8")
        self.assertIn("CrewForeman pre-push gate (auto-installed)", text)
        self.assertIn("pre-push.local", text)

    def test_core_hooks_path_is_honoured(self):
        self.git("config", "core.hooksPath", ".githooks")
        self.commit("first")
        hook = self.repo / ".githooks" / "pre-push"
        self.assertTrue(hook.is_file())
        self.assertFalse((self.repo / ".git" / "hooks" / "pre-push").exists())

    def test_push_with_gate_blocks_foreign_then_allows_connector_identity(self):
        bare = self.root / "remote.git"
        _run(["init", "--bare", str(bare)], self.root)
        base = self.commit("base", "base.txt")
        self.git("remote", "add", "origin", str(bare))
        self.git("update-ref", "refs/remotes/origin/main", base)
        self.foreign_commit("foreign")
        old_pythonpath = os.environ.get("PYTHONPATH")
        old_cf_python = os.environ.get("CF_PYTHON")
        os.environ["PYTHONPATH"] = str(REPO_ROOT)
        os.environ["CF_PYTHON"] = sys.executable
        try:
            rc, _out, err = git_identity.push_with_gate(
                CFG, self.repo, "origin", "main:refs/heads/main", base=base)
            self.assertNotEqual(rc, 0)
            self.assertIn("allowlist", err)
            remote_heads = _run(["rev-parse", "--verify", "main"], bare)
            self.assertNotEqual(remote_heads.returncode, 0,
                                "a blocked push must not reach the remote")
            # Fixing the history (not stacking on the foreign commit) makes
            # the same range pass the gate.
            self.git("reset", "--hard", base)
            self.commit("mine", "mine.txt")
            rc, _out, err = git_identity.push_with_gate(
                CFG, self.repo, "origin", "main:refs/heads/main", base=base)
            self.assertEqual(rc, 0, err)
            remote_heads = _run(["rev-parse", "--verify", "main"], bare)
            self.assertEqual(remote_heads.returncode, 0)
        finally:
            if old_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = old_pythonpath
            if old_cf_python is None:
                os.environ.pop("CF_PYTHON", None)
            else:
                os.environ["CF_PYTHON"] = old_cf_python

    def test_email_shaped_exception_does_not_widen_the_allowlist(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign", email="personal@example.org")
        cfg = {"git_identity": dict(CFG["git_identity"]),
               "git_identity_exceptions": ["personal@example.org"]}
        report = git_identity.check_push(cfg, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"], report)
        self.assertNotIn("personal@example.org",
                         git_identity.allowed_emails(cfg))

    def test_installed_hook_blocks_a_plain_git_push(self):
        bare = self.root / "hook-remote.git"
        _run(["init", "--bare", str(bare)], self.root)
        base = self.commit("base", "base.txt")
        self.git("remote", "add", "origin", str(bare))
        self.git("update-ref", "refs/remotes/origin/main", base)
        self.foreign_commit("foreign")
        old_pythonpath = os.environ.get("PYTHONPATH")
        old_cf_python = os.environ.get("CF_PYTHON")
        os.environ["PYTHONPATH"] = str(REPO_ROOT)
        os.environ["CF_PYTHON"] = sys.executable
        try:
            proc = _run(["push", "origin", "main:refs/heads/main"], self.repo)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("pre-push", proc.stderr + proc.stdout)
            self.assertNotEqual(
                _run(["rev-parse", "--verify", "main"], bare).returncode, 0)
            self.git("reset", "--hard", base)
            self.commit("mine", "mine.txt")
            proc = _run(["push", "origin", "main:refs/heads/main"], self.repo)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            if old_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = old_pythonpath
            if old_cf_python is None:
                os.environ.pop("CF_PYTHON", None)
            else:
                os.environ["CF_PYTHON"] = old_cf_python

    def test_ship_push_wrapper_calls_the_gate(self):
        import importlib.util
        script = (REPO_ROOT / "scripts" / "ship_push.py")
        spec = importlib.util.spec_from_file_location("cf_ship_push", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        base = self.commit("base", "base.txt")
        self.git("remote", "add", "origin", str(self.root / "unused.git"))
        with mock.patch.object(git_identity, "push_with_gate",
                               return_value=(0, "", "")) as push:
            rc = module.main(["--repo", str(self.repo), "--remote", "origin",
                              "--refspec", "main:main", "--base", base])
        self.assertEqual(rc, 0)
        self.assertTrue(push.called)

    def test_content_scan_still_catches_secret(self):
        base = self.commit("base", "base.txt")
        self.commit("secret", "notes.txt",
                    "api_" + "key = " + chr(34) + "sk-" + "abcdefghijklmnopqrstuvwxyz012345" + chr(34) + "\n")
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"])
        self.assertIn("api_key", {v.get("rule") for v in report["violations"]})

    def test_content_scan_allows_placeholder_email(self):
        base = self.commit("base", "base.txt")
        self.commit("placeholder", "doc.txt", "contact: example@example.com\n")
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertTrue(report["ok"], report["messages"])

    def test_config_object_exception_is_honoured(self):
        """The connector passes a Config hybrid, not a plain dict."""
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        self.git("remote", "add", "upstream",
                 "https://github.com/example-org/Cypraea.git")
        cfg = config_mod.Config({
            "git_identity": dict(CFG["git_identity"]),
            "git_identity_exceptions": ["example-org/Cypraea"],
        })
        report = git_identity.check_push(cfg, repo=str(self.repo), base=base,
                                         head="HEAD", remote="upstream")
        self.assertTrue(report["ok"], report["messages"])
        self.assertEqual(report["exception"], "example-org/Cypraea")

    def test_exception_remote_still_scans_content(self):
        base = self.commit("base", "base.txt")
        self.git("remote", "add", "upstream",
                 "https://github.com/example-org/Cypraea.git")
        self.commit("secret", "notes.txt",
                    "api_" + "key = " + chr(34) + "sk-"
                    + "abcdefghijklmnopqrstuvwxyz012345" + chr(34) + "\n")
        cfg = {"git_identity": dict(CFG["git_identity"]),
               "git_identity_exceptions": ["upstream"]}
        report = git_identity.check_push(cfg, repo=str(self.repo), base=base,
                                         head="HEAD", remote="upstream")
        self.assertFalse(report["ok"], "an exception must not skip content")
        self.assertIn("api_key", {v.get("rule")
                                  for v in report["violations"]})

    def test_git_config_count_cannot_inject_an_identity(self):
        self.write("env.txt", "x")
        self.git("add", "-A")
        leak = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.email",
            "GIT_CONFIG_VALUE_0": "injected@example.org",
        }
        with mock.patch.dict(os.environ, leak, clear=False):
            rc, _out, err = git_identity.commit_with_identity(
                CFG, self.repo, "config injection")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ident(),
                         ["crewforeman-mcp", "noreply@example.com",
                          "crewforeman-mcp", "noreply@example.com"])

    def test_git_config_parameters_cannot_inject_an_identity(self):
        self.write("params.txt", "x")
        self.git("add", "-A")
        leak = {"GIT_CONFIG_PARAMETERS": "'user.email'='injected@example.org'"}
        with mock.patch.dict(os.environ, leak, clear=False):
            rc, _out, err = git_identity.commit_with_identity(
                CFG, self.repo, "config parameters injection")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ident(),
                         ["crewforeman-mcp", "noreply@example.com",
                          "crewforeman-mcp", "noreply@example.com"])

    def test_metadata_gate_blocks_foreign_author_name(self):
        base = self.commit("base", "base.txt")
        self.write("name.txt", "x")
        self.git("add", "-A")
        self.git("-c", "user.name=Outsider",
                 "-c", "user.email=noreply@example.com",
                 "commit", "-m", "foreign name")
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.get("field") == "name"
                            and v.get("name") == "Outsider"
                            for v in report["violations"]), report)


    def test_metadata_gate_blocks_foreign_committer_name(self):
        base = self.commit("base", "base.txt")
        self.write("cname.txt", "x")
        self.git("add", "-A")
        env = dict(os.environ)
        env["GIT_COMMITTER_NAME"] = "Committer Outsider"
        proc = _run(["-c", "user.name=crewforeman-mcp",
                     "-c", "user.email=noreply@example.com",
                     "commit", "-m", "committer name"], self.repo, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.get("role") == "committer"
                            and v.get("field") == "name"
                            and v.get("name") == "Committer Outsider"
                            for v in report["violations"]), report)

    def test_cli_reads_config_object_and_allows_exception_remote(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        self.git("remote", "add", "upstream",
                 "https://github.com/example-org/Cypraea.git")
        cfg_path = self.root / "config.json"
        cfg_path.write_text(json.dumps({
            "git_identity": dict(CFG["git_identity"]),
            "git_identity_exceptions": ["example-org/Cypraea"],
        }), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "deepseek_mcp.git_identity",
             "--repo", str(self.repo), "--base", base, "--head", "HEAD",
             "--remote", "upstream", "--config", str(cfg_path)],
            cwd=str(self.repo), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # load_config returns a Config hybrid; the CLI must not discard it.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK", proc.stdout)

    def test_content_scan_checks_each_commit_not_only_net_diff(self):
        """A secret added then removed still lives in the pushed history."""
        base = self.commit("base", "base.txt")
        secret = "sk-" + "a" * 30
        self.commit("add-secret", "leak.txt",
                    "api_" + "key = " + chr(34) + secret + chr(34) + "\n")
        self.git("rm", "leak.txt")
        rc, _out, err = git_identity.commit_with_identity(
            CFG, self.repo, "remove-secret")
        self.assertEqual(rc, 0, err)
        report = git_identity.check_push(CFG, repo=str(self.repo), base=base,
                                         head="HEAD")
        self.assertFalse(report["ok"], report)
        hits = [v for v in report["violations"] if v.get("rule") == "api_key"]
        self.assertTrue(hits, report["violations"])
        self.assertTrue(hits[0].get("commit"), "violation names the commit")


    def test_cli_exits_non_zero_on_violation(self):
        base = self.commit("base", "base.txt")
        self.foreign_commit("foreign")
        cfg_path = self.root / "config.json"
        cfg_path.write_text(json.dumps(CFG), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "deepseek_mcp.git_identity",
             "--repo", str(self.repo), "--base", base, "--head", "HEAD",
             "--config", str(cfg_path)],
            cwd=str(self.repo), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BLOCKED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
