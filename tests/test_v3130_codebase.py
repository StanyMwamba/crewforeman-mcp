"""v3.13.0 codebase guards: no Windows-fatal liveness probe, quiet corrupt runs.

The first class is an AST scan, not a grep: comments and string literals that
mention ``os.kill(pid, 0)`` must not trip it.  On Windows, signal 0 is not a
harmless liveness probe: CPython routes ``os.kill`` through
``GenerateConsoleCtrlEvent``/``TerminateProcess``, so a "probe" can be delivered
as CTRL_C_EVENT (or kill the process).  The only legal occurrence is the POSIX
branch of :func:`deepseek_mcp.platforms.pid_alive`; every other module must call
that helper instead.

The second class pins ``store.load_runs`` behaviour for a truncated/corrupt
``runs/*.json``: skip it, keep loading valid records, and warn at most once per
path (module-level memory), rather than on every call or test run.

The third class scans every object in the git object database, not just the
working tree or reachable commits.  A local clone hard-links the source object
database, so dangling blobs left by old stashes can carry credentials that no
current file contains.  The scan reports the rule and object id only, never the
matched text.

The fourth class runs the product's own secret rule set
(:func:`deepseek_mcp.gate.scan_tree`) over the repository.  It is the same
scanner the deterministic gate uses, so a credential-shaped fixture literal
fails in the suite instead of in review.  Fixtures that exist to test the
allowlist must carry an inline allowlist marker.
"""

import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import gate, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env


REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_os_kill_signal_zero(node):
    """True for a call shaped like ``os.kill(<pid>, 0)`` (or ``sig=0``)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute)
            and func.attr == "kill"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"):
        return False
    candidates = []
    if len(node.args) >= 2:
        candidates.append(node.args[1])
    for keyword in node.keywords:
        if keyword.arg in ("sig", "signal"):
            candidates.append(keyword.value)
    return any(isinstance(item, ast.Constant) and item.value == 0
               for item in candidates)


def _signal_zero_kill_lines(tree):
    """Line numbers of real ``os.kill(..., 0)`` calls in one AST."""
    return {node.lineno for node in ast.walk(tree)
            if _is_os_kill_signal_zero(node)}


def _allowed_lines(tree):
    """Lines inside ``platforms.pid_alive``; its POSIX branch is the one home."""
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "pid_alive":
            allowed.update(_signal_zero_kill_lines(node))
    return allowed


def _scan_repository(root):
    """Return ``path:line`` for every forbidden signal-0 kill under root."""
    violations = []
    for subdir in ("deepseek_mcp", "tests", "tools"):
        base = root / subdir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            lines = _signal_zero_kill_lines(tree)
            if not lines:
                continue
            allowed = set()
            if relative == "deepseek_mcp/platforms.py":
                allowed = _allowed_lines(tree)
            for line in sorted(lines - allowed):
                violations.append("%s:%d" % (relative, line))
    return violations


class PidLivenessScanTests(unittest.TestCase):
    def test_scanner_ignores_comments_and_strings(self):
        source = (
            '"""os.kill(123, 0) in a docstring is only text."""\n'
            "# os.kill(456, 0) in a comment is only text.\n"
            'MESSAGE = "os.kill(789, 0)"\n'
        )
        self.assertEqual(_signal_zero_kill_lines(ast.parse(source)), set())

    def test_scanner_flags_a_real_call(self):
        source = "import os\nos.kill(123, 0)\nos.kill(pid=4, sig=0)\n"
        self.assertEqual(_signal_zero_kill_lines(ast.parse(source)), {2, 3})

    def test_no_signal_zero_kill_outside_platforms_posix_branch(self):
        violations = _scan_repository(REPO_ROOT)
        self.assertEqual(
            violations, [],
            "os.kill(pid, 0) is not a liveness probe on Windows; call "
            "platforms.pid_alive instead: %s" % ", ".join(violations))

    def test_platforms_keeps_exactly_the_posix_probe(self):
        path = REPO_ROOT / "deepseek_mcp" / "platforms.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.assertEqual(len(_allowed_lines(tree)), 1)


class CorruptRunLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=base_env(self.root))
        self.log_dir = Path(self.cfg.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _save_valid(self, run_id="valid-run"):
        return store.save_run({
            "id": run_id,
            "kind": "single",
            "created": "2026-01-01T00:00:00+00:00",
            "title": "valid",
            "status": "ok",
            "model": "deepseek-flash",
            "prompt": "hello",
            "answer": "world",
            "files": [],
        }, self.cfg)

    def test_corrupt_run_is_skipped_and_warns_once(self):
        self._save_valid()
        bad = self.log_dir / "truncated.json"
        bad.write_text('{"id": "truncated"', encoding="utf-8")

        with self.assertLogs(level="WARNING") as captured:
            first = store.load_runs(self.cfg)
            second = store.load_runs(self.cfg)

        self.assertIn("valid-run", [run.get("id") for run in first])
        self.assertNotIn("truncated", [run.get("id") for run in first])
        self.assertEqual([run.get("id") for run in first],
                         [run.get("id") for run in second])

        messages = [record.getMessage() for record in captured.records
                    if "truncated.json" in record.getMessage()]
        self.assertEqual(len(messages), 1,
                         "one warning per corrupt path, not per call: %r"
                         % (messages,))
        self.assertIn(str(bad), messages[0])
        self.assertIn("JSONDecodeError", messages[0])


SECRET_PATTERNS = (
    ("deepseek-openai-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("github-token",
     re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key-header",
     re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
)


def _iter_git_objects(raw):
    """Yield ``(sha, type, body)`` from ``git cat-file --batch`` output."""
    pos = 0
    while pos < len(raw):
        newline = raw.find(b"\n", pos)
        if newline < 0:
            raise ValueError("truncated git cat-file header")
        header = raw[pos:newline].split()
        if len(header) != 3 or not header[2].isdigit():
            raise ValueError("bad git cat-file header: %r" % raw[pos:newline][:80])
        sha, kind, size = header[0].decode("ascii"), header[1].decode("ascii"), int(header[2])
        start = newline + 1
        end = start + size
        if end > len(raw):
            raise ValueError("truncated git cat-file body for %s" % sha)
        yield sha, kind, raw[start:end]
        pos = end + 1


class SecretObjectScanTests(unittest.TestCase):
    """Every git object, including dangling ones, must be credential-free."""

    def test_git_object_database_has_no_credential_literals(self):
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        try:
            proc = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "cat-file",
                 "--batch-all-objects", "--batch"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except FileNotFoundError:
            self.skipTest("git is not installed")
        except subprocess.TimeoutExpired:
            self.fail("git object scan timed out after 120 s")
        if proc.returncode != 0:
            self.fail("git cat-file failed: %s"
                      % proc.stderr.decode("utf-8", "replace")[-300:])

        findings = []
        try:
            objects = _iter_git_objects(proc.stdout)
            for sha, kind, body in objects:
                text = body.decode("utf-8", "replace")
                for name, pattern in SECRET_PATTERNS:
                    if pattern.search(text):
                        findings.append("%s in %s %s" % (name, kind, sha[:12]))
        except ValueError as exc:
            self.fail(str(exc))
        self.assertEqual(
            findings, [],
            "credential-shaped literal(s) survive in the git object database; "
            "delete the dangling object(s) and keep fixtures synthetic: %s"
            % ", ".join(findings))


class SourceSecretScanTests(unittest.TestCase):
    """The product gate rules must find no unallowlisted source literal."""

    def test_repository_has_no_unallowlisted_secret_findings(self):
        findings = gate.scan_tree({}, REPO_ROOT, skip=[".git"])
        active = [item for item in findings if not item.get("allowlisted")]
        self.assertEqual(
            active, [],
            "credential-shaped literal(s) in the working tree; compose "
            "synthetic fixtures from fragments or add an allowlist marker: %s"
            % ", ".join("%s %s:%s" % (
                item["rule_id"], item["path"], item["line"])
                for item in active))


if __name__ == "__main__":
    unittest.main()
