#!/usr/bin/env bash
# Single CI entry point for crewforeman-mcp.
#
# The GitHub workflow is a thin wrapper: it checks out, installs the matrix
# Python, then runs this script. Everything else (Hyrum's "one script") lives
# here so the same gate can be reproduced locally:
#
#   PATH="/path/to/python/bin:$PATH" bash scripts/ci.sh
#
# No secrets, no network calls of our own, no git writes and no artifact
# upload. A C compiler is required for the test-only fake dsh launcher.
set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd -P) || exit 1
cd "$REPO_ROOT" || exit 1

PYTHON="${PYTHON:-python3}"
FAKE_DIR="${TMPDIR:-/tmp}/cf-ci-fake-dsh.$$"

fail() {
    echo "ci: $*" >&2
    exit 1
}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    fail "python interpreter not found: $PYTHON"
fi

trap 'rm -rf "$FAKE_DIR"' EXIT

echo "== environment =="
"$PYTHON" --version || fail "cannot run $PYTHON"
uname -a
echo "repository:  $REPO_ROOT"
echo "interpreter: $(command -v "$PYTHON")"

echo "== static CI contract =="
"$PYTHON" tools/ci_hygiene.py || fail "ci_hygiene.py failed"

echo "== commit identity gate =="
GATE_BASE="${GATE_BASE:-}"
if [ -z "$GATE_BASE" ] && [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    fail "GATE_BASE is unset in CI; refusing to skip the identity gate"
fi
if [ -z "$GATE_BASE" ] && git rev-parse --verify origin/main >/dev/null 2>&1; then
    GATE_BASE=origin/main
fi
if [ -n "$GATE_BASE" ]; then
    case "$GATE_BASE" in
        0000000000000000000000000000000000000000)
            GATE_BASE=$(git rev-list --max-parents=0 HEAD) ;;
    esac
    if ! git cat-file -e "${GATE_BASE}^{commit}" 2>/dev/null; then
        GATE_BASE=$(git rev-list --max-parents=0 HEAD)
    fi
    PYTHONPATH="$REPO_ROOT" "$PYTHON" -m deepseek_mcp.git_identity \
        --repo "$REPO_ROOT" --base "$GATE_BASE" --head HEAD --remote origin \
        || fail "commit identity/content gate failed"
fi

echo "== install package =="
"$PYTHON" -m pip install --disable-pip-version-check --no-input . \
    || fail "pip install . failed"

echo "== build test fake dsh =="
mkdir -p "$FAKE_DIR" || fail "cannot create $FAKE_DIR"
"$PYTHON" tests/build_fake_dsh.py "$FAKE_DIR/fake_dsh.py" \
    || fail "fake dsh build failed (a C compiler is required)"

echo "== sandbox probe (require) =="
"$PYTHON" tests/sandbox_probe.py --require --json \
    || fail "no enforced OS sandbox on this host"

suite_rc=0
boundary_rc=0

# Exact documented gate command (docs/OPERATIONS.md, "Running the full suite").
# The two variables are scoped to this command so the boundary run below is not
# accidentally put into "OS sandbox disabled" mode.
echo "== full test suite (gate command) =="
DSH_OS_SANDBOX=0 DSH_REVIEW_PREFLIGHT=0 \
    "$PYTHON" -m unittest discover -s tests -q || suite_rc=$?

# Run even when the suite failed. CF_REQUIRE_REAL_SANDBOX=1 turns every
# platform/boundary skip in the dispatcher into a hard failure, so the real
# Seatbelt/Landlock boundary cannot silently disappear on a CI runner.
echo "== sandbox boundary (CF_REQUIRE_REAL_SANDBOX=1; skips fail) =="
CF_REQUIRE_REAL_SANDBOX=1 \
    "$PYTHON" -m unittest -v tests.test_sandbox_boundary || boundary_rc=$?

echo "== summary =="
echo "full suite exit code:       $suite_rc"
echo "sandbox boundary exit code: $boundary_rc"
if [ "$suite_rc" -ne 0 ] || [ "$boundary_rc" -ne 0 ]; then
    echo "CI FAILED" >&2
    exit 1
fi
echo "CI PASSED"
