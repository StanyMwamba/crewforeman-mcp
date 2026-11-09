"""Stdlib-only per-test isolation of every mutable root (P3.4a).

This module is the single source of truth for the test isolation contract.
It deliberately imports only the standard library so ``python -m unittest``
can use it without pytest.  Three callers share it:

* ``tests/__init__.py`` wraps every test discovered by the standard library
  runner in :func:`isolated_case` (see ``load_tests``);
* ``tests/test_p34a_isolation.py`` enters the context explicitly in
  ``setUp`` when it is not already active, so a focused
  ``python -m unittest tests.test_p34a_isolation`` run is isolated too;
* ``tests/conftest.py`` is a thin, optional pytest adapter.  pytest is not
  required to run or verify the suite.

Each test gets one fresh ``case-*`` directory under
``tests.workspace_temp_root()`` (normally ``<workspace>/.dsh-tmp-tests``).
State, runs, jobs, sessions, skills, pid file and all temp paths point inside
it; ``HOME``/``USERPROFILE``/``XDG_*`` are deliberately untouched so
``api._HOME_PATH_RE``'s ``/Users/<name>`` -> ``~`` rewrite keeps round-tripping
in resume/trace tests (redirecting HOME makes those paths re-expand under the
fake home and breaks 9 tests).

The per-key aliases (``DEEPSEEK_MCP_LOG_DIR``, ``DEEPSEEK_MCP_JOBS_DIR``,
``DEEPSEEK_MCP_PID_FILE``, ``DEEPSEEK_DSH_SESSIONS_DIR``) outrank the derived
state root.  They are therefore *removed* for the duration of a case and
restored afterwards: an operator environment can never direct a test write
outside its case directory, while a test that deliberately sets one in a child
environment is unaffected.  ``DSH_SESSIONS_DIR`` is instead redirected to the
case (B1: sessions are no longer derived from ``state_root``).
"""

import contextlib
import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_MISSING = object()

# Snapshot of the identity variables at import time; the isolation layer must
# never change them (PROJECT_MAP section 6: redirecting HOME breaks 9 tests).
_HOME_VARS = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
              "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")
HOME_AT_IMPORT = {name: os.environ.get(name) for name in _HOME_VARS}

# Every env var the isolation layer redirects.  Directory-valued entries are
# created; the single file entry only needs its parent (the case root).
DIR_ENV_VARS = (
    "CREWFOREMAN_STATE_DIR",
    "DSH_STATE_DIR",
    "DEEPSEEK_MCP_STATE_DIR",
    "DSH_SESSIONS_DIR",
    "DSH_SKILLS_DIR",
    "DEEPSEEK_SKILLS_DIR",
    "DSH_WORKSPACE",
    "DEEPSEEK_WORKSPACE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "PYTHONPYCACHEPREFIX",
)
FILE_ENV_VARS = ("CREWFOREMAN_CREDENTIALS_FILE", "DSH_CREDENTIALS_FILE")
CONFIG_ENV_VARS = ("DEEPSEEK_MCP_CONFIG",)

# Per-key path aliases that outrank the state root.  These are cleared (not
# redirected) during a case and restored in ``finally``.  They are not set to a
# case path because a job spec's own config file must still be able to win for
# tests that exercise custom configs.
CLEARED_ENV_VARS = (
    "DEEPSEEK_MCP_LOG_DIR",
    "DEEPSEEK_MCP_JOBS_DIR",
    "DEEPSEEK_MCP_PID_FILE",
    "DEEPSEEK_DSH_SESSIONS_DIR",
)

# Internal marker so nested ``isolated_case`` calls (explicit setUp under a
# runner that already isolates, or a pytest fixture) can be detected without
# changing the product configuration.
CASE_ENV_VAR = "DSH_TEST_CASE_DIR"

ISOLATED_ENV_VARS = DIR_ENV_VARS + FILE_ENV_VARS + CONFIG_ENV_VARS
TRACKED_ENV_VARS = ISOLATED_ENV_VARS + CLEARED_ENV_VARS + (CASE_ENV_VAR,)


def case_env(case):
    """Return the complete env overlay for one per-test *case* directory."""
    case = Path(case)
    root = case / "root"
    return {
        "CREWFOREMAN_STATE_DIR": str(root),
        "DSH_STATE_DIR": str(root / "state"),
        "DEEPSEEK_MCP_STATE_DIR": str(root / "state"),
        "DSH_SESSIONS_DIR": str(root / "sessions"),
        "DSH_SKILLS_DIR": str(case / "skills"),
        "DEEPSEEK_SKILLS_DIR": str(case / "skills"),
        "DSH_WORKSPACE": str(case / "workspace"),
        "DEEPSEEK_WORKSPACE": str(case / "workspace"),
        "TMPDIR": str(case / "tmp"),
        "TMP": str(case / "tmp"),
        "TEMP": str(case / "tmp"),
        "PYTHONPYCACHEPREFIX": str(case / "pycache"),
        # Tests must never read the operator's real provider keys.  The case
        # path does not exist, so the resolver falls back to the explicit
        # temp file each test configures.
        "CREWFOREMAN_CREDENTIALS_FILE": str(case / "credentials.yaml"),
        "DSH_CREDENTIALS_FILE": str(case / "credentials.yaml"),
        # An empty config file keeps load_config() away from the operator's
        # real ~/.config/deepseek-harness-mcp/config.json and its custom paths.
        "DEEPSEEK_MCP_CONFIG": str(case / "config.json"),
        CASE_ENV_VAR: str(case),
    }


def is_active():
    """True when the current process is already inside an isolated case."""
    return bool(os.environ.get(CASE_ENV_VAR))


@contextlib.contextmanager
def isolated_case():
    """Create one case dir, point the env at it, then restore and delete it.

    Exposed (not just used by the runner) so the isolation regressions can
    prove the cleanup contract without relying on pytest finalisation order.
    """
    import tests  # lazy: avoids a circular import while ``tests`` loads

    base = Path(tests.workspace_temp_root())
    base.mkdir(parents=True, exist_ok=True)
    # mkdtemp is atomic and gives the required fresh ``case-<token>`` name.
    case = Path(tempfile.mkdtemp(prefix="case-", dir=str(base)))
    root = case / "root"
    previous = {name: os.environ.get(name, _MISSING)
                for name in TRACKED_ENV_VARS}
    previous_tempdir = tempfile.tempdir
    try:
        overlay = case_env(case)
        for name in DIR_ENV_VARS:
            Path(overlay[name]).mkdir(parents=True, exist_ok=True)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / "jobs").mkdir(parents=True, exist_ok=True)
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        Path(overlay["DEEPSEEK_MCP_CONFIG"]).write_text("{}\n", encoding="utf-8")
        # Clear inherited per-key aliases before applying the overlay.  Leaving
        # them in place would let an outer operator environment override the
        # state-root-derived log/jobs/sessions paths.
        for name in CLEARED_ENV_VARS:
            os.environ.pop(name, None)
        os.environ.update(overlay)
        # Keep the suite's fake DSH from trying to nest sandbox-exec.
        os.environ.setdefault("DSH_OS_SANDBOX", "0")
        os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")
        tempfile.tempdir = overlay["TMPDIR"]
        yield case
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        tempfile.tempdir = previous_tempdir
        shutil.rmtree(str(case), ignore_errors=True)


def _record_environment_skip(result, test, reason):
    """Record one counted skip without running the test body (B2)."""
    result.startTest(test)
    try:
        result.addSkip(test, reason)
    finally:
        result.stopTest(test)
    return result


_ENVIRONMENT_ERRNOS = frozenset(
    item for item in (getattr(errno, "EROFS", None),
                      getattr(errno, "ENOSPC", None),
                      getattr(errno, "EDQUOT", None),
                      getattr(errno, "EACCES", None))
    if item is not None)


def _environment_oserror(exc):
    """True when an OSError while entering a case is environmental."""
    return isinstance(exc, (PermissionError, FileNotFoundError,
                            NotADirectoryError)) or (
        getattr(exc, "errno", None) in _ENVIRONMENT_ERRNOS)


_HOOK_INSTALLED = False


def install_unittest_hook():
    """Make every ``unittest.TestCase.run`` use a fresh isolated case.

    ``python -m unittest discover -s tests`` imports test modules as top-level
    modules (not as the ``tests`` package), so discovery does not call this
    package's ``load_tests`` hook.  Patching ``TestCase.run`` is layout
    independent: every test class inherits it, and the patch is installed when
    any test module imports ``tests`` (all discovery imports finish before any
    test runs).  The optional pytest adapter is unaffected: if a fixture has
    already opened a case, ``is_active()`` is true and the original run method
    executes directly.
    """
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    original_run = unittest.TestCase.run

    def run(self, result=None):
        if is_active():
            return original_run(self, result)
        if result is None:
            result = self.defaultTestResult()
        # Catch an environment failure only while ENTERING the case.  A
        # SkipTest raised by original_run() is unittest's own contract and
        # must keep propagating to its normal skip handling.
        try:
            case = isolated_case()
            case.__enter__()
        except unittest.SkipTest as exc:
            return _record_environment_skip(result, self, str(exc))
        except OSError as exc:
            # Only an environment failure may become a skip.  A programming
            # error (for example a malformed overlay path) must still fail
            # loudly instead of hiding behind an "environment-only" reason.
            if not _environment_oserror(exc):
                raise
            return _record_environment_skip(
                result, self,
                "environment-only: could not create an isolated case: %s"
                % (exc,))
        try:
            return original_run(self, result)
        finally:
            case.__exit__(None, None, None)

    unittest.TestCase.run = run
    _HOOK_INSTALLED = True
