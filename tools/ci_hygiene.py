#!/usr/bin/env python3
"""Stdlib CI checks: thin workflow wrapper, scripts/ci.sh, versions, pid_alive."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


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


def _workflow_checks(errors):
    text = WORKFLOW.read_text(encoding="utf-8")
    script_path = ROOT / "scripts" / "ci.sh"
    if not script_path.exists():
        errors.append("missing scripts/ci.sh")
        script = ""
    else:
        script = script_path.read_text(encoding="utf-8")
    if "secrets." in text or "secrets[" in text:
        errors.append("workflow reads secrets.*")
    if "pull_request_target" in text:
        errors.append("workflow uses pull_request_target")
    if re.search(r"(?i)\bwindows-(?:latest|20\d\d)\b", text):
        errors.append("workflow requests a Windows runner; Windows CI is PENDING")
    for label in ("macos-latest", "ubuntu-latest"):
        if label not in text:
            errors.append("workflow missing OS label %s" % label)
    if not re.search(r"permissions:\s*\{?\s*contents:\s*read", text):
        errors.append("workflow permissions are not contents: read")
    if "scripts/ci.sh" not in text:
        errors.append("workflow does not call scripts/ci.sh")
    if re.search(r"(?m)^  (?:probe|boundary|hygiene):", text):
        errors.append("workflow still has a separate probe/boundary/hygiene job")
    if len(re.findall(r"(?m)^\s+run:\s*", text)) != 1:
        errors.append("workflow must have exactly one run: step (scripts/ci.sh)")
    if text.count("timeout-minutes:") < 1:
        errors.append("workflow has no timeout-minutes")
    if not re.search(r'python:\s*\[\s*"3\.9"\s*,\s*"3\.15"\s*\]', text):
        errors.append("workflow python matrix is not 3.9 + 3.15")
    for needle, message in (
            ("tests/build_fake_dsh.py",
             "scripts/ci.sh does not build the fake dsh"),
            ("-m unittest discover -s tests -q",
             "scripts/ci.sh lacks the exact gate command"),
            ("CF_REQUIRE_REAL_SANDBOX=1",
             "scripts/ci.sh does not gate the boundary"),
            ("tests/sandbox_probe.py --require",
             "scripts/ci.sh does not run the required sandbox probe"),
            ("tools/ci_hygiene.py",
             "scripts/ci.sh does not run ci_hygiene.py")):
        if needle not in script:
            errors.append(message)


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
