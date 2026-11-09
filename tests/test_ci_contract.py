"""Contract tests for the thin macOS + Linux CI setup.

The workflow is read as text on purpose: PyYAML is not a test dependency and
the checks here are about exact commands, runner labels and pinned SHAs rather
than about YAML semantics. The parser only understands the shape this
repository's workflow uses (one ``jobs:`` mapping, two-space job keys, block
or flow sequences).

``NEWEST_STABLE_PYTHON`` is deliberately pinned in the test as well as in the
workflow: 3.15 is the current stable CPython line (3.15.2, released 2026-10-25) and
actions/setup-python resolves the newest 3.15.x from the live
actions/python-versions manifest. Moving CI to 3.16 must update both places,
which is the point of the cross-check.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CI_SCRIPT = ROOT / "scripts" / "ci.sh"

FLOOR_PYTHON = "3.9"
NEWEST_STABLE_PYTHON = "3.15"
NEWEST_STABLE_PATCH = "3.15.2"
RUNNERS = {"macos-latest", "ubuntu-latest"}
CI_RUN_STEP = "bash scripts/ci.sh"
GATE_COMMAND = "-m unittest discover -s tests -q"

SHA_PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$"
)
WINDOWS_RUNNER = re.compile(r"(?i)\bwindows-(?:latest|20\d\d)\b")


def _top_block(text, key):
    """Body of a top-level ``key:`` block, up to the next top-level key."""
    match = re.search(r"(?m)^%s:\s*$" % re.escape(key), text)
    if match is None:
        return None
    rest = text[match.end():]
    end = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_-]*:\s*", rest)
    return rest[:end.start()] if end else rest


def _job_blocks(text):
    """Return ``{job name: block}`` for direct children of ``jobs:``."""
    body = _top_block(text, "jobs")
    if body is None:
        return {}
    jobs = {}
    current = None
    lines = []
    for line in body.splitlines():
        match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if match:
            if current is not None:
                jobs[current] = "\n".join(lines)
            current = match.group(1)
            lines = [line]
        elif current is not None:
            lines.append(line)
    if current is not None:
        jobs[current] = "\n".join(lines)
    return jobs


def _flow_list(text, key):
    """Values of a one-line flow sequence such as ``os: [a, b]``."""
    match = re.search(
        r"(?m)^\s+%s:\s*\[([^\]]*)\]\s*(?:#.*)?$" % re.escape(key), text
    )
    if match is None:
        return None
    return [
        item.strip().strip("'\"")
        for item in match.group(1).split(",")
        if item.strip()
    ]


def _run_values(block):
    return [
        value.strip()
        for value in re.findall(r"(?m)^\s+run:\s*(.+?)\s*$", block)
    ]


class CiWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _job_blocks(cls.text)

    def test_single_matrix_job_and_no_windows_runner(self):
        self.assertEqual(
            set(self.jobs), {"test"},
            "expected exactly one 'test' job, found %r" % sorted(self.jobs),
        )
        self.assertIsNone(
            WINDOWS_RUNNER.search(self.text),
            "workflow still requests a Windows runner; CI is macOS + Linux only",
        )
        self.assertNotIn("windows-2025", self.text)
        self.assertNotIn("windows-latest", self.text)

    def test_matrix_is_exactly_macos_and_linux(self):
        oses = _flow_list(self.text, "os")
        self.assertIsNotNone(oses, "workflow has no matrix os flow list")
        self.assertEqual(set(oses), RUNNERS)
        self.assertEqual(len(oses), 2, "matrix.os must have exactly two entries")

        runs_on = re.search(r"(?m)^\s+runs-on:\s*(.+?)\s*$", self.text)
        self.assertIsNotNone(runs_on, "job has no runs-on")
        self.assertEqual(runs_on.group(1).strip(), "${{ matrix.os }}")

    def test_matrix_python_is_floor_plus_newest_stable(self):
        versions = _flow_list(self.text, "python")
        self.assertIsNotNone(versions, "workflow has no matrix python flow list")
        self.assertEqual(
            set(versions), {FLOOR_PYTHON, NEWEST_STABLE_PYTHON},
            "matrix.python must be exactly %s and %s, got %r"
            % (FLOOR_PYTHON, NEWEST_STABLE_PYTHON, versions),
        )
        # State the exact current patch, not just the minor line.
        self.assertIn(
            NEWEST_STABLE_PATCH, self.text,
            "workflow must state the newest stable patch (%s)" % NEWEST_STABLE_PATCH,
        )

    def test_each_job_calls_only_scripts_ci_sh(self):
        self.assertTrue(self.jobs, "workflow has no jobs")
        for name, block in self.jobs.items():
            runs = _run_values(block)
            self.assertEqual(
                runs, [CI_RUN_STEP],
                "job %r must have exactly one run step %r; got %r"
                % (name, CI_RUN_STEP, runs),
            )
            uses = re.findall(r"(?m)^\s*-\s*uses:\s*(\S+)", block)
            self.assertEqual(
                [value.split("@", 1)[0] for value in uses],
                ["actions/checkout", "actions/setup-python"],
                "job %r must only use checkout then setup-python; got %r"
                % (name, uses),
            )
            steps = re.findall(r"(?m)^\s{6}-\s+(?:name|uses|run):", block)
            self.assertEqual(
                len(steps), 3,
                "job %r must have exactly 3 steps (checkout, setup, ci.sh); "
                "got %d" % (name, len(steps)),
            )
        self.assertEqual(
            len(re.findall(r"(?m)^\s+run:\s*", self.text)), 1,
            "the workflow must contain exactly one run: step",
        )

    def test_actions_are_pinned_to_full_sha(self):
        uses = re.findall(r"(?m)^\s*-\s*uses:\s*(\S+)", self.text)
        self.assertEqual(len(uses), 2, "expected two pinned actions; got %r" % uses)
        for value in uses:
            self.assertRegex(
                value, SHA_PINNED_ACTION,
                "action %r is not pinned to a full 40-hex commit SHA" % value,
            )

    def test_no_secrets_and_no_pull_request_target(self):
        lowered = self.text.lower()
        self.assertNotIn("secrets.", lowered)
        self.assertNotIn("secrets[", lowered)
        self.assertNotIn("pull_request_target", lowered)

    def test_triggers_are_push_main_and_pull_request_only(self):
        on_block = _top_block(self.text, "on")
        self.assertIsNotNone(on_block, "workflow has no on: block")
        self.assertIn("push:", on_block)
        self.assertIn("pull_request:", on_block)
        self.assertNotIn("workflow_dispatch", self.text)
        push = on_block.split("push:", 1)[1].split("pull_request:", 1)[0]
        self.assertRegex(push, r"branches:\s*\[\s*main\s*\]")

    def test_permissions_are_contents_read(self):
        self.assertRegex(
            self.text,
            r"(?m)^permissions:\s*\n\s+contents:\s*read\s*$",
        )

    def test_workflow_stays_thin(self):
        lowered = self.text.lower()
        for forbidden in ("upload-artifact", "pull_request_target", "hygiene:"):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("probe:", lowered)
        self.assertNotIn("boundary:", lowered)
        self.assertGreaterEqual(self.text.count("timeout-minutes:"), 1)


class CiScriptContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = CI_SCRIPT.read_text(encoding="utf-8")

    def test_script_exists_executable_and_nonempty(self):
        self.assertTrue(CI_SCRIPT.is_file(), "missing %s" % CI_SCRIPT)
        self.assertGreater(CI_SCRIPT.stat().st_size, 0)
        self.assertTrue(
            os.access(str(CI_SCRIPT), os.X_OK),
            "%s must be executable" % CI_SCRIPT,
        )

    def test_script_runs_the_exact_gate_command(self):
        self.assertIn(GATE_COMMAND, self.script)
        self.assertIn("DSH_OS_SANDBOX=0", self.script)
        self.assertIn("DSH_REVIEW_PREFLIGHT=0", self.script)
        self.assertIn('PYTHON="${PYTHON:-python3}"', self.script)

    def test_script_requires_the_real_boundary(self):
        self.assertIn("CF_REQUIRE_REAL_SANDBOX=1", self.script)
        self.assertNotIn("CF_REQUIRE_REAL_SANDBOX=0", self.script)
        self.assertIn("tests.test_sandbox_boundary", self.script)

    def test_script_builds_fake_dsh_and_probes(self):
        self.assertIn("tests/build_fake_dsh.py", self.script)
        self.assertIn("tests/sandbox_probe.py --require", self.script)

    def test_script_has_no_secrets_and_passes_bash_syntax(self):
        self.assertNotIn("secrets.", self.script)
        self.assertNotIn("secrets[", self.script)
        completed = subprocess.run(
            ["bash", "-n", str(CI_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode, 0,
            "bash -n failed for %s\nstdout:\n%s\nstderr:\n%s"
            % (CI_SCRIPT, completed.stdout, completed.stderr),
        )


if __name__ == "__main__":
    unittest.main()
