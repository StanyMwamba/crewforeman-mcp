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

While a case is active the store write guard additionally monkeypatches the
``deepseek_mcp.store`` write entry points so a run record aimed at the real
home state root or the ``<repo>/runs`` fallback raises
:class:`IsolationViolation` instead of polluting the operator's data (see the
guard section below).  The store module is imported lazily at guard install
time, so this file stays standard-library-only at import.
"""

import contextlib
import errno
import os
import shutil
import stat
import sys
import tempfile
import time
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

# Ambient git identity vars are cleared for the same reason: an operator (or
# harness) environment that exports GIT_AUTHOR_*/GIT_COMMITTER_* silently
# overrides a test fixture's ``git -c user.name=...`` or hostile gitconfig, so
# identity tests that build foreign commits stop being foreign and the
# metadata gate rightly sees nothing to block.  A test that wants these vars
# sets them explicitly on its own child env; they are restored in ``finally``.
GIT_IDENTITY_ENV_VARS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)

# Internal marker so nested ``isolated_case`` calls (explicit setUp under a
# runner that already isolates, or a pytest fixture) can be detected without
# changing the product configuration.
CASE_ENV_VAR = "DSH_TEST_CASE_DIR"

ISOLATED_ENV_VARS = DIR_ENV_VARS + FILE_ENV_VARS + CONFIG_ENV_VARS
TRACKED_ENV_VARS = (ISOLATED_ENV_VARS + CLEARED_ENV_VARS
                    + GIT_IDENTITY_ENV_VARS + (CASE_ENV_VAR,))


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


def _rmtree_robust(path):
    """Best-effort rmtree that survives Windows read-only and open files.

    ``shutil.rmtree`` fails on a read-only file and on a file another process
    still has open (common when a child process is winding down).  Clear the
    read-only bit, retry the failing operation, and retry the whole tree a few
    times with a short sleep; missing paths are success.  Never raises and
    never waits for minutes.  POSIX behaviour is unchanged apart from ignoring
    an already-missing tree.
    """
    def _handle(func, target, error):
        exc = error[1] if isinstance(error, tuple) else error
        if isinstance(exc, PermissionError):
            try:
                os.chmod(target, stat.S_IWRITE)
            except OSError:
                pass
        try:
            func(target)
        except OSError:
            pass

    for attempt in range(3):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(str(path), onexc=_handle)
            else:
                shutil.rmtree(str(path), onerror=_handle)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 2:
                return
            time.sleep(0.05)
        except Exception:
            return


# ---------------------------------------------------------------------------
# Store write guard: while a case is active, any deepseek_mcp.store write
# whose resolved target is the real home state root (``~/DeepSeek-MCP`` /
# ``%LOCALAPPDATA%\\DeepSeek-MCP``) or the store's ``<repo>/runs`` fallback
# raises IsolationViolation *before* anything touches the filesystem.  The
# wrappers are installed by :func:`isolated_case` and removed on exit; when a
# test never resolves or performs a store write, none of them ever runs.
# ---------------------------------------------------------------------------


class IsolationViolation(AssertionError):
    """A test attempted to write run state outside its isolated case."""


def real_home_state_root():
    """The real home state root that ``config._base_dir`` derives from.

    Read from the import-time identity snapshot so a test that rewrites
    ``HOME`` cannot hide the real home state root from the guard.
    """
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA")
                or HOME_AT_IMPORT.get("USERPROFILE")
                or os.path.expanduser("~"))
    else:
        base = HOME_AT_IMPORT.get("HOME") or os.path.expanduser("~")
    return Path(os.path.expanduser(str(base))) / "DeepSeek-MCP"


def repo_runs_dir():
    """The store's ``FALLBACK_LOG_DIR`` for this checkout (``<repo>/runs``)."""
    return Path(__file__).resolve().parent.parent / "runs"


def _norm_target(path):
    if path is None:
        return None
    try:
        text = str(path).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return os.path.normcase(os.path.realpath(
            os.path.abspath(os.path.expanduser(text))))
    except (OSError, ValueError, TypeError):
        return None


def _under_target(norm_path, norm_root):
    return (norm_path is not None and norm_root is not None
            and (norm_path == norm_root
                 or norm_path.startswith(norm_root + os.sep)))


# Extra forbidden roots captured from ``store.FALLBACK_LOG_DIR`` when the
# guard installs.  The capture happens before any test body runs, so a test
# that patches the constant to a case-local temp dir is not affected.
_EXTRA_FORBIDDEN_ROOTS = []


def forbidden_target_reason(target):
    """Return why *target* is a forbidden test write root, or ``None``.

    Forbidden: the real home state root (except the ``harness-workspace``
    development area, which on this workspace layout contains the repository
    and every case directory) and the repository ``runs`` fallback.  The
    repository itself is exempt from the home-state-root rule so case
    directories keep working even when the checkout sits under the home root.
    """
    norm = _norm_target(target)
    if norm is None:
        return None
    if _under_target(norm, _norm_target(repo_runs_dir())):
        return "the repository runs fallback %s" % (repo_runs_dir(),)
    home_root = real_home_state_root()
    if (_under_target(norm, _norm_target(home_root))
            and not _under_target(norm,
                                  _norm_target(home_root / "harness-workspace"))
            and not _under_target(
                norm, _norm_target(Path(__file__).resolve().parent.parent))):
        return "the real home state root %s" % (home_root,)
    for root, label in _EXTRA_FORBIDDEN_ROOTS:
        if _under_target(norm, root):
            return label
    return None


_CFG_STACK = []
_CFG_BY_DIR = {}
_GUARD_REFCOUNT = 0
_GUARD_SAVED = []


def _describe_cfg(cfg):
    if cfg is None:
        return "config unknown"
    try:
        return "config log_dir=%r state_dir=%r _config_path=%r" % (
            cfg.get("log_dir"), cfg.get("state_dir"), cfg.get("_config_path"))
    except Exception:
        return "config %r" % (cfg,)


def _refuse(target, description):
    reason = forbidden_target_reason(target)
    if reason is None:
        return
    raise IsolationViolation(
        "isolation guard: refusing test write to %r (inside %s); %s"
        % (str(target), reason, description or "config unknown"))


def _cfg_hint(directory=None):
    if directory is not None:
        key = _norm_target(directory)
        if key and key in _CFG_BY_DIR:
            return _CFG_BY_DIR[key]
    return _CFG_STACK[-1] if _CFG_STACK else None


def _install_store_guard():
    """Patch deepseek_mcp.store write entry points (re-entrant)."""
    global _GUARD_REFCOUNT
    if _GUARD_REFCOUNT:
        _GUARD_REFCOUNT += 1
        return
    from deepseek_mcp import store
    del _EXTRA_FORBIDDEN_ROOTS[:]
    extra = _norm_target(getattr(store, "FALLBACK_LOG_DIR", None))
    if extra and extra != _norm_target(repo_runs_dir()):
        _EXTRA_FORBIDDEN_ROOTS.append(
            (extra, "the store log-dir fallback %s" % (store.FALLBACK_LOG_DIR,)))
    _CFG_STACK[:] = []
    _CFG_BY_DIR.clear()
    _GUARD_SAVED[:] = []

    def guard_save_run(original):
        def wrapper(run, cfg, *args, **kwargs):
            description = _describe_cfg(cfg)
            _CFG_STACK.append(description)
            try:
                _refuse(getattr(cfg, "log_dir", None), description)
                return original(run, cfg, *args, **kwargs)
            finally:
                _CFG_STACK.pop()
        return wrapper

    def guard_resolve_log_dir(original):
        def wrapper(cfg):
            description = _describe_cfg(cfg)
            _CFG_STACK.append(description)
            try:
                _refuse(getattr(cfg, "log_dir", None), description)
                directory, fallback_reason = original(cfg)
                _refuse(directory, description)
                key = _norm_target(directory)
                if key:
                    _CFG_BY_DIR[key] = description
                return directory, fallback_reason
            finally:
                _CFG_STACK.pop()
        return wrapper

    def guard_probe_dir(original):
        def wrapper(directory):
            _refuse(directory, _cfg_hint())
            return original(directory)
        return wrapper

    def guard_atomic_write(original):
        def wrapper(path, text):
            try:
                hint = _cfg_hint(Path(path).parent)
            except Exception:
                hint = _cfg_hint()
            _refuse(path, hint)
            return original(path, text)
        return wrapper

    def guard_append_run_md(original):
        def wrapper(run, text, cfg):
            description = _describe_cfg(cfg)
            _CFG_STACK.append(description)
            try:
                _refuse(getattr(cfg, "log_dir", None), description)
                try:
                    _run, _json_path, md_path = store.find_run(run["id"], cfg)
                except Exception:
                    md_path = None
                if md_path is not None:
                    _refuse(md_path, description)
                return original(run, text, cfg)
            finally:
                _CFG_STACK.pop()
        return wrapper

    factories = {
        "save_run": guard_save_run,
        "resolve_log_dir": guard_resolve_log_dir,
        "_probe_dir": guard_probe_dir,
        "_atomic_write": guard_atomic_write,
        "append_run_md": guard_append_run_md,
    }
    for name, factory in factories.items():
        original = getattr(store, name)
        wrapped = factory(original)
        _GUARD_SAVED.append((store, name, original))
        setattr(store, name, wrapped)
        # Patch where it is used: modules that did ``from .store import
        # save_run`` at import time hold a bound reference the store-module
        # patch alone would not reach.
        for module in list(sys.modules.values()):
            if module is store:
                continue
            try:
                if module.__dict__.get(name) is original:
                    _GUARD_SAVED.append((module, name, original))
                    setattr(module, name, wrapped)
            except Exception:
                continue
    _GUARD_REFCOUNT = 1


def _remove_store_guard():
    global _GUARD_REFCOUNT
    if _GUARD_REFCOUNT <= 0:
        return
    _GUARD_REFCOUNT -= 1
    if _GUARD_REFCOUNT:
        return
    for module, name, original in reversed(_GUARD_SAVED):
        try:
            setattr(module, name, original)
        except Exception:
            pass
    _GUARD_SAVED[:] = []
    _CFG_STACK[:] = []
    _CFG_BY_DIR.clear()
    del _EXTRA_FORBIDDEN_ROOTS[:]


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
        # state-root-derived log/jobs/sessions paths.  Ambient git identity
        # vars go too: they would override a fixture's own ``-c`` identity.
        for name in CLEARED_ENV_VARS + GIT_IDENTITY_ENV_VARS:
            os.environ.pop(name, None)
        os.environ.update(overlay)
        # Keep the suite's fake DSH from trying to nest sandbox-exec.
        os.environ.setdefault("DSH_OS_SANDBOX", "0")
        os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")
        tempfile.tempdir = overlay["TMPDIR"]
        _install_store_guard()
        try:
            yield case
        finally:
            _remove_store_guard()
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        tempfile.tempdir = previous_tempdir
        _rmtree_robust(case)


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
