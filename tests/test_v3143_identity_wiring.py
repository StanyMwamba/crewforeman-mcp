"""3.14.3: the connector's commit path uses the configured identity.

Windows-compatible: ``git`` is required only for these tests and they skip
cleanly when it is absent.  Every command runs with a private HOME, a private
global config and ``-c commit.gpgsign=false`` so a developer's own setup
cannot influence the result.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import git_identity

GIT = shutil.which("git")
FOREIGN = "someone-else@example.net"


def _run(argv, cwd, env=None):
    return subprocess.run(argv, cwd=str(cwd), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)


@unittest.skipUnless(GIT, "git is required")
class ConnectorCommitIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-id-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        home = self.tmp / "home"
        home.mkdir()
        # A hostile global identity: every test proves it cannot leak in.
        (home / ".gitconfig").write_text(
            "[user]\n\tname = Global Person\n\temail = %s\n" % FOREIGN,
            encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
            "TZ": "UTC",
            "LC_ALL": "C",
        })
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.assertEqual(_run([GIT, "init", "-q", "-b", "main", str(self.repo)],
                              self.tmp, self.env).returncode, 0)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        _run([GIT, "add", "-A"], self.repo, self.env)

    def _cfg(self):
        return {"git_identity": {"name": "crewforeman-mcp",
                                 "email": "noreply@example.com"}}

    def test_commit_overrides_global_identity(self):
        rc, _out, err = git_identity.commit_with_identity(
            self._cfg(), self.repo, "first")
        self.assertEqual(rc, 0, err)
        out = _run([GIT, "log", "-1", "--format=%an|%ae|%cn|%ce"], self.repo,
                   self.env).stdout.strip()
        self.assertEqual(out, "crewforeman-mcp|noreply@example.com|"
                              "crewforeman-mcp|noreply@example.com")

    def test_rebase_does_not_pick_up_global_identity(self):
        self.assertEqual(git_identity.commit_with_identity(
            self._cfg(), self.repo, "first")[0], 0)
        _run([GIT, "checkout", "-q", "-b", "side"], self.repo, self.env)
        (self.repo / "b.txt").write_text("two\n", encoding="utf-8")
        _run([GIT, "add", "-A"], self.repo, self.env)
        self.assertEqual(git_identity.commit_with_identity(
            self._cfg(), self.repo, "second")[0], 0)
        # A commit made by a foreign actor, then rebased by the connector:
        _run([GIT, "checkout", "-q", "main"], self.repo, self.env)
        (self.repo / "c.txt").write_text("three\n", encoding="utf-8")
        _run([GIT, "add", "-A"], self.repo, self.env)
        _run([GIT, "-c", "user.name=Global Person", "-c",
              "user.email=" + FOREIGN, "commit", "-q", "-m", "foreign"],
             self.repo, self.env)
        before = _run([GIT, "rev-parse", "HEAD"], self.repo,
                      self.env).stdout.strip()
        _run([GIT, "checkout", "-q", "side"], self.repo, self.env)
        rc, _out, err = git_identity.rebase_with_identity(
            self._cfg(), self.repo, "main")
        self.assertEqual(rc, 0, err)
        after = _run([GIT, "rev-parse", "HEAD"], self.repo,
                     self.env).stdout.strip()
        # The replay really happened (a new commit id), and the committer of
        # the commit the rebase created is ours, not the global identity.
        self.assertNotEqual(before, after)
        out = _run([GIT, "log", "-1", "--format=%cn|%ce"], self.repo,
                   self.env).stdout.strip()
        self.assertEqual(out, "crewforeman-mcp|noreply@example.com")
        # Authors are preserved by design; the connector never rewrites them.
        author = _run([GIT, "log", "-1", "--format=%ae"], self.repo,
                      self.env).stdout.strip()
        self.assertEqual(author, "noreply@example.com")

    def test_metadata_gate_blocks_foreign_author(self):
        self.assertEqual(git_identity.commit_with_identity(
            self._cfg(), self.repo, "first")[0], 0)
        base = _run([GIT, "rev-parse", "HEAD"], self.repo, self.env).stdout.strip()
        (self.repo / "d.txt").write_text("four\n", encoding="utf-8")
        _run([GIT, "add", "-A"], self.repo, self.env)
        _run([GIT, "-c", "user.name=Global Person", "-c",
              "user.email=" + FOREIGN, "commit", "-q", "-m", "leak"],
             self.repo, self.env)
        violations = git_identity.scan_metadata(
            self.repo, base, "HEAD", self._cfg())
        self.assertTrue(violations, "foreign author must be a violation")
        self.assertIn(FOREIGN, json.dumps(violations))

    def test_pipeline_no_longer_hardcodes_an_identity(self):
        source = (Path(__file__).resolve().parent.parent / "deepseek_mcp"
                  / "pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("crewforeman@localhost", source)
        self.assertIn("commit_with_identity", source)


if __name__ == "__main__":
    unittest.main()
