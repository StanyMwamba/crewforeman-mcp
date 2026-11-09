"""Contract tests for the 3-OS / two-job CI setup.

The workflow is read as text on purpose: PyYAML is not a test dependency and
the checks here are about exact commands, runner labels and pinned SHAs rather
than about YAML semantics. The parser only understands the shape this
repository's workflow uses (one ``jobs:`` mapping, two-space job keys, block
or flow sequences).

``NEWEST_STABLE_PYTHON`` is deliberately pinned in the test as well as in the
workflow: 3.15 is the current stable CPython line (3.15.2, released 2026-10-25)
and actions/setup-python resolves the newest 3.15.x from the live
actions/python-versions manifest. Moving CI to 3.16 must update both places,
which is the point of the cross-check.
"""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CI_SCRIPT = ROOT / "scripts" / "ci.sh"
CI_PS1 = ROOT / "scripts" / "ci.ps1"
GITATTRIBUTES = ROOT / ".gitattributes"

FLOOR_PYTHON = "3.9"
NEWEST_STABLE_PYTHON = "3.15"
NEWEST_STABLE_PATCH = "3.15.2"
RUNNERS = {"macos-latest", "ubuntu-24.04", "windows-2025"}
CI_RUN_STEP = "bash scripts/ci.sh"
WINDOWS_RUN_STEP = "pwsh -NoProfile -File scripts/ci.ps1"
GATE_COMMAND = "-m unittest discover -s tests -q"
GATE_NEEDLES = (
    "tools/ci_hygiene.py",
    "tests/build_fake_dsh.py",
    "tests/sandbox_probe.py --require",
    GATE_COMMAND,
    "CF_REQUIRE_REAL_SANDBOX=1",
    "tests.test_sandbox_boundary",
)

SHA_PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$"
)


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


def _runs_on(block):
    match = re.search(r"(?m)^\s+runs-on:\s*(.+?)\s*$", block)
    if match is None:
        return None
    return match.group(1).split("#", 1)[0].strip()


def _attribute_rules():
    """Map ``pattern -> attributes`` from .gitattributes (comments stripped)."""
    rules = {}
    for raw in GITATTRIBUTES.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rules[parts[0].lstrip("/")] = parts[1:]
    return rules


class CiWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _job_blocks(cls.text)

    def test_workflow_has_exactly_test_and_windows_jobs(self):
        self.assertEqual(
            set(self.jobs), {"test", "windows"},
            "expected exactly the test and windows jobs, found %r"
            % sorted(self.jobs),
        )
        self.assertEqual(len(self.jobs), 2)

    def test_runners_are_the_three_os_matrix(self):
        oses = _flow_list(self.text, "os")
        self.assertIsNotNone(oses, "workflow has no matrix os flow list")
        self.assertEqual(set(oses), {"macos-latest", "ubuntu-24.04"})
        self.assertEqual(len(oses), 2, "matrix.os must have exactly two entries")
        self.assertIn("ubuntu-24.04", oses, "Linux runner must be pinned")
        self.assertNotIn(
            "ubuntu-latest", oses,
            "ubuntu-latest is Ubuntu 26.04, which has no Python 3.9 in "
            "actions/setup-python; pin ubuntu-24.04",
        )
        self.assertNotIn(
            "windows-2025", oses,
            "windows-2025 must be a separate job, not part of the "
            "macOS/Linux matrix",
        )

        self.assertEqual(_runs_on(self.jobs["test"]), "${{ matrix.os }}")
        self.assertEqual(
            _runs_on(self.jobs["windows"]), "windows-2025",
            "the windows job must be pinned to runs-on: windows-2025",
        )
        self.assertIn("windows-2025", self.text)
        for obsolete in ("windows-latest", "windows-2022", "windows-2019"):
            self.assertNotIn(
                obsolete, self.text,
                "workflow requests %r; only windows-2025 is supported"
                % obsolete,
            )
        self.assertEqual(
            RUNNERS, {"macos-latest", "ubuntu-24.04", "windows-2025"}
        )

    def test_matrix_python_is_floor_plus_newest_stable(self):
        versions = _flow_list(self.text, "python")
        self.assertIsNotNone(versions, "workflow has no matrix python flow list")
        self.assertEqual(
            set(versions), {FLOOR_PYTHON, NEWEST_STABLE_PYTHON},
            "matrix.python must be exactly %s and %s, got %r"
            % (FLOOR_PYTHON, NEWEST_STABLE_PYTHON, versions),
        )
        self.assertEqual(len(versions), 2)
        # State the exact current patch, not just the minor line.
        self.assertIn(
            NEWEST_STABLE_PATCH, self.text,
            "workflow must state the newest stable patch (%s)"
            % NEWEST_STABLE_PATCH,
        )

    def test_each_job_has_exactly_three_steps_and_one_run(self):
        expected_runs = {
            "test": [CI_RUN_STEP],
            "windows": [WINDOWS_RUN_STEP],
        }
        for name in ("test", "windows"):
            self.assertIn(name, self.jobs)
            block = self.jobs[name]
            self.assertEqual(
                _run_values(block), expected_runs[name],
                "job %r must have exactly one run step %r; got %r"
                % (name, expected_runs[name], _run_values(block)),
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
                "job %r must have exactly 3 steps (checkout, setup, run); "
                "got %d" % (name, len(steps)),
            )
        self.assertEqual(
            len(re.findall(r"(?m)^\s+run:\s*", self.text)), 2,
            "the workflow must contain exactly two run: steps",
        )

    def test_actions_are_pinned_to_full_sha(self):
        uses = re.findall(r"(?m)^\s*-\s*uses:\s*(\S+)", self.text)
        self.assertEqual(len(uses), 4, "expected four pinned actions; got %r"
                         % uses)
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

    def test_gitattributes_pins_line_endings(self):
        self.assertTrue(
            GITATTRIBUTES.is_file(),
            "missing .gitattributes; CI needs deterministic line endings",
        )
        rules = _attribute_rules()
        self.assertIn(
            "scripts/ci.sh", rules,
            ".gitattributes must pin scripts/ci.sh explicitly",
        )
        ci_attrs = rules["scripts/ci.sh"]
        self.assertIn("text", ci_attrs)
        self.assertIn("eol=lf", ci_attrs)
        self.assertIn("*.ps1", rules, ".gitattributes must cover *.ps1")
        self.assertIn("eol=crlf", rules["*.ps1"])


def _usable_bash():
    r"""Return a bash that can parse POSIX shell, not the Windows WSL stub.

    On windows-2025 ``shutil.which("bash")`` can resolve
    ``C:\Windows\System32\bash.exe``, which exits 1 with "Windows
    Subsystem for Linux has no installed distributions".  Git for Windows
    ships a real bash; prefer it on nt and validate every candidate.
    """
    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ])
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "GNU bash" in (probe.stdout + probe.stderr):
            return candidate
    return None


class CiScriptContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = CI_SCRIPT.read_text(encoding="utf-8")
        cls.ps1 = CI_PS1.read_text(encoding="utf-8")

    def test_scripts_exist_and_are_nonempty(self):
        self.assertTrue(CI_SCRIPT.is_file(), "missing %s" % CI_SCRIPT)
        self.assertGreater(CI_SCRIPT.stat().st_size, 0)
        self.assertTrue(
            os.access(str(CI_SCRIPT), os.X_OK),
            "%s must be executable" % CI_SCRIPT,
        )
        self.assertTrue(CI_PS1.is_file(), "missing %s" % CI_PS1)
        self.assertGreater(CI_PS1.stat().st_size, 0)
        self.assertTrue(self.ps1.strip(), "%s must not be empty" % CI_PS1)

    def test_both_scripts_run_the_same_gate(self):
        for label, text in (("scripts/ci.sh", self.script),
                            ("scripts/ci.ps1", self.ps1)):
            for needle in GATE_NEEDLES:
                self.assertIn(
                    needle, text,
                    "%s lacks the gate token %r" % (label, needle),
                )

    def test_both_scripts_control_the_gate_environment(self):
        for label, text in (("scripts/ci.sh", self.script),
                            ("scripts/ci.ps1", self.ps1)):
            # Accept DSH_OS_SANDBOX=0 and $env:DSH_OS_SANDBOX alike.
            self.assertIn("DSH_OS_SANDBOX", text, label)
            self.assertIn("DSH_REVIEW_PREFLIGHT", text, label)
            self.assertNotIn("CF_REQUIRE_REAL_SANDBOX=0", text, label)

    def test_script_runs_the_exact_gate_command(self):
        self.assertIn(GATE_COMMAND, self.script)
        self.assertIn('PYTHON="${PYTHON:-python3}"', self.script)

    def test_script_requires_the_real_boundary(self):
        self.assertIn("CF_REQUIRE_REAL_SANDBOX=1", self.script)
        self.assertNotIn("CF_REQUIRE_REAL_SANDBOX=0", self.script)
        self.assertIn("tests.test_sandbox_boundary", self.script)

    def test_script_builds_fake_dsh_and_probes(self):
        self.assertIn("tests/build_fake_dsh.py", self.script)
        self.assertIn("tests/sandbox_probe.py --require", self.script)

    def test_scripts_have_no_secrets(self):
        for label, text in (("scripts/ci.sh", self.script),
                            ("scripts/ci.ps1", self.ps1)):
            self.assertNotIn("secrets.", text, label)
            self.assertNotIn("secrets[", text, label)

    def test_bash_script_passes_bash_syntax(self):
        bash = _usable_bash()
        if bash is None:
            self.skipTest("no usable bash is installed on this host; "
                          "scripts/ci.sh syntax is unverified here")
        # Feed the file on stdin: a native D:\... path is mangled by MSYS
        # bash's backslash and drive-prefix handling.
        with open(str(CI_SCRIPT), "rb") as handle:
            completed = subprocess.run(
                [bash, "-n"],
                stdin=handle,
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

    def test_powershell_script_parses_when_pwsh_is_available(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not installed on this host; "
                          "scripts/ci.ps1 syntax is unverified here")
        escaped = str(CI_PS1).replace("'", "''")
        command = (
            "$tokens = $null; $errors = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "'%s', [ref]$tokens, [ref]$errors) | Out-Null; "
            "if (@($errors).Count -gt 0) { "
            "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
            % escaped
        )
        completed = subprocess.run(
            [pwsh, "-NoProfile", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode, 0,
            "PowerShell parse failed for %s\nstdout:\n%s\nstderr:\n%s"
            % (CI_PS1, completed.stdout, completed.stderr),
        )


if __name__ == "__main__":
    unittest.main()
