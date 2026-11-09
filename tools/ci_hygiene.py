#!/usr/bin/env python3
"""Stdlib CI checks: 3-OS/two-job workflow, gate scripts, versions, pid_alive."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# The three runners the contract requires. The first two are the matrix in the
# "test" job; windows-2025 is pinned directly on the separate "windows" job.
RUNNERS = ("macos-latest", "ubuntu-24.04", "windows-2025")
TEST_OS = ("macos-latest", "ubuntu-24.04")
TEST_RUN = "bash scripts/ci.sh"
WINDOWS_RUN = "pwsh -NoProfile -File scripts/ci.ps1"
PYTHONS = ("3.9", "3.15")

WINDOWS_RUNNER_RE = re.compile(r"(?i)\bwindows-(20\d\d)\b")
SHA_PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$"
)

# The same gate must be present in both entry points. In ci.ps1 the
# assignment is "$env:CF_REQUIRE_REAL_SANDBOX=1", which contains the needle
# verbatim; plain substring checks are enough.
SCRIPT_NEEDLES = (
    ("tools/ci_hygiene.py", "does not run tools/ci_hygiene.py"),
    ("tests/build_fake_dsh.py", "does not build the fake dsh"),
    ("tests/sandbox_probe.py --require", "does not run the required sandbox probe"),
    ("-m unittest discover -s tests -q", "lacks the exact gate command"),
    ("CF_REQUIRE_REAL_SANDBOX=1", "does not require the real sandbox boundary"),
    ("tests.test_sandbox_boundary", "does not name tests.test_sandbox_boundary"),
)
ENV_NEEDLES = (
    ("DSH_OS_SANDBOX", "does not control DSH_OS_SANDBOX"),
    ("DSH_REVIEW_PREFLIGHT", "does not control DSH_REVIEW_PREFLIGHT"),
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


def _version_checks(errors):
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    setup_cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    config_py = (ROOT / "deepseek_mcp" / "config.py").read_text(encoding="utf-8")
    m = re.search(r'^requires-python\s*=\s*">=([0-9.]+)"', pyproject, re.M)
    if not m:
        errors.append("pyproject.toml lacks requires-python >=3.9")
        return
    floor = m.group(1)
    if floor != "3.9":
        errors.append("pyproject floor is %s, expected 3.9" % floor)
    if not re.search(r"^python_requires\s*=\s*>=%s" % re.escape(floor),
                     setup_cfg, re.M):
        errors.append("setup.cfg python_requires disagrees with pyproject")
    version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    server = re.search(r'^SERVER_VERSION\s*=\s*"([^"]+)"', config_py, re.M)
    if not version or not server:
        errors.append("cannot read version from pyproject.toml/config.py")
    elif version.group(1) != server.group(1):
        errors.append("version mismatch: pyproject %s vs config %s"
                      % (version.group(1), server.group(1)))


def _script_checks(errors):
    for relative in ("scripts/ci.sh", "scripts/ci.ps1"):
        path = ROOT / relative
        if not path.exists():
            errors.append("missing %s" % relative)
            continue
        script = path.read_text(encoding="utf-8")
        if not script.strip():
            errors.append("%s is empty" % relative)
            continue
        for needle, message in SCRIPT_NEEDLES:
            if needle not in script:
                errors.append("%s %s (missing %r)"
                              % (relative, message, needle))
        for needle, message in ENV_NEEDLES:
            if needle not in script:
                errors.append("%s %s (missing %r)"
                              % (relative, message, needle))
        if "CF_REQUIRE_REAL_SANDBOX=0" in script:
            errors.append("%s disables CF_REQUIRE_REAL_SANDBOX" % relative)


def _workflow_checks(errors):
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _job_blocks(text)

    if "secrets." in text or "secrets[" in text:
        errors.append("workflow reads secrets.*")
    if "pull_request_target" in text:
        errors.append("workflow uses pull_request_target")
    if "ubuntu-latest" in text:
        errors.append("workflow uses floating ubuntu-latest; pin ubuntu-24.04")
    if "windows-latest" in text:
        errors.append("workflow uses floating windows-latest; pin windows-2025")
    for match in WINDOWS_RUNNER_RE.finditer(text):
        if match.group(1) != "2025":
            errors.append("workflow requests windows-%s; only windows-2025 is "
                          "supported" % match.group(1))

    for label in RUNNERS:
        if label not in text:
            errors.append("workflow missing OS label %s" % label)

    if set(jobs) != {"test", "windows"}:
        errors.append("workflow must have exactly the test and windows jobs; "
                      "found %r" % sorted(jobs))
    for name in ("test", "windows"):
        if name not in jobs:
            errors.append("workflow missing the %s job" % name)

    if "test" in jobs:
        test_block = jobs["test"]
        if _runs_on(test_block) != "${{ matrix.os }}":
            errors.append("test job must run on ${{ matrix.os }}")
        oses = _flow_list(test_block, "os")
        if oses is None:
            errors.append("test job has no matrix os flow list")
        else:
            if len(oses) != 2 or set(oses) != set(TEST_OS):
                errors.append("test job matrix os must be exactly %r; got %r"
                              % (list(TEST_OS), oses))
            if "windows-2025" in oses:
                errors.append("windows-2025 must not be added to the "
                              "macOS/Linux matrix")

    if "windows" in jobs:
        windows_block = jobs["windows"]
        if _runs_on(windows_block) != "windows-2025":
            errors.append("windows job must be pinned to runs-on: windows-2025")
        if "windows-2025" not in windows_block:
            errors.append("windows job does not name windows-2025")

    expected_runs = {"test": [TEST_RUN], "windows": [WINDOWS_RUN]}
    for name, block in jobs.items():
        if name not in expected_runs:
            continue
        runs = _run_values(block)
        if runs != expected_runs[name]:
            errors.append("job %s must have exactly one run step %r; got %r"
                          % (name, expected_runs[name], runs))

    uses = re.findall(r"(?m)^\s*-\s*uses:\s*(\S+)", text)
    if len(uses) != 4:
        errors.append("workflow must have exactly 4 action uses (checkout + "
                      "setup-python in each job); got %d" % len(uses))
    for value in uses:
        if not SHA_PINNED_ACTION.match(value):
            errors.append("action %s is not pinned to a full 40-hex commit SHA"
                          % value)

    if not re.search(r"permissions:\s*\{?\s*contents:\s*read", text):
        errors.append("workflow permissions are not contents: read")
    if len(re.findall(r"(?m)^\s+run:\s*", text)) != 2:
        errors.append("workflow must have exactly two run: steps (ci.sh and "
                      "ci.ps1)")
    if text.count("timeout-minutes:") < 1:
        errors.append("workflow has no timeout-minutes")
    pythons = _flow_list(text, "python")
    if pythons is None:
        errors.append("workflow has no matrix python flow list")
    elif len(pythons) != 2 or set(pythons) != set(PYTHONS):
        errors.append("workflow python matrix must be exactly 3.9 + 3.15; "
                      "got %r" % pythons)
    if re.search(r"(?m)^  (?:probe|boundary|hygiene):", text):
        errors.append("workflow still has a separate probe/boundary/hygiene job")

    _script_checks(errors)


def _pid_alive_checks(errors):
    pattern = re.compile(r"os\.kill\([^)]*,\s*0\s*\)")
    for path in sorted((ROOT / "deepseek_mcp").glob("*.py")):
        if path.name == "platforms.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                errors.append("%s:%d uses os.kill(pid, 0); use platforms.pid_alive"
                              % (path.relative_to(ROOT), lineno))


def main():
    errors = []
    if not WORKFLOW.exists():
        errors.append("missing .github/workflows/ci.yml")
    else:
        _workflow_checks(errors)
    _version_checks(errors)
    _pid_alive_checks(errors)
    if errors:
        for error in errors:
            print("ci-hygiene: %s" % error, file=sys.stderr)
        return 1
    print("ci-hygiene: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
