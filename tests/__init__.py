"""Test package for the DeepSeek MCP server.

The suite must be runnable from a read-only review sandbox whose ``TMPDIR`` is
missing or points at an unwritable directory.  Python's ``tempfile`` then
raises ``FileNotFoundError: No usable temporary directory`` before any product
assertion runs (the Kimi reviewer hit exactly this).  At import time we honour
a usable ``DSH_TEST_TMPDIR``/``TMPDIR``/``TMP``/``TEMP`` and otherwise install
a verified writable fallback.  The first fallback is the workspace-local
``.dsh-tmp-tests`` directory, which the connector's read-only reviewer profile
grants through its ``<workspace>/.dsh-tmp-*`` write rule; later candidates are
the repository, the current directory, a user cache, an in-memory tmpfs when
present, or the system temp root.  A path is accepted only after a real
create/unlink probe.

Explicit environment candidates are never created: under uid 0 an absolute
path such as ``/no/such`` could otherwise be created, hiding the sandbox
misconfiguration.  Only the deliberate workspace/repository/cache fallbacks
are created.

The unit suite also defaults ``DSH_OS_SANDBOX=0``.  The fake DSH child used by
the harness tests is not a sandbox subject: it writes session and marker files
beside its workspace, which a real Seatbelt profile correctly denies.  Whether
``sandbox-exec`` is usable differs between a developer shell and the connector,
so leaving the default on made the connector's signed suite fail with 41
spurious errors.  Sandbox tests opt in explicitly and still cover the wrapper.
"""

import os
import tempfile
import unittest
from pathlib import Path

# The paid reviewer pre-flight is exercised by its dedicated tests, which set
# DSH_REVIEW_PREFLIGHT=1 explicitly.  Every other test opts out by default so
# the suite never makes an unexpected extra reviewer call.
os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")

# The fake DSH used by the suite writes session logs and marker files beside
# its workspace.  A real macOS Seatbelt profile correctly denies those writes,
# but whether Seatbelt is usable depends on the host process (the connector's
# signed suite runs unsandboxed, where it is).  The unit suite exercises the
# in-process harness logic with a fake child, not the OS sandbox, so default
# the OS wrapper off.  Sandbox-specific tests opt in with an explicit policy
# or DSH_OS_SANDBOX=1 and keep covering the wrapper.
os.environ.setdefault("DSH_OS_SANDBOX", "0")

# The reviewer finalizer waits one second after the direct child exits so a
# background child session can finish writing before the in-flight scan.  The
# fake DSH used here writes every session/marker file synchronously before it
# exits, so the wait only adds wall clock time; the connector's deterministic
# gate gives the whole suite a 900 s budget.  Compress the wait for tests
# only: the product default stays 1.0 s and the in-flight logic is untouched.
from deepseek_mcp import harness as _harness  # noqa: E402

_harness.REVIEWER_ACTIVITY_SETTLE_SECONDS = 0.01

# Retry backoff is a product policy (1 s, 2 s, 4 s ...).  The suite asserts
# the retry *decisions* and call counts, never the wall-clock delay, so cap
# the actual sleep for tests only.  Without this, a handful of retry tests
# alone spent 20-30 s asleep and pushed the 2205-test suite into the
# deterministic gate's timeout.  ``RetryPolicy.delay`` (the policy value) is
# untouched and still asserted by tests/test_p2_health.py.
from deepseek_mcp import health as _health  # noqa: E402

_RETRY_SLEEP_ORIGINAL = _health.RetryPolicy.sleep


def _fast_retry_sleep(self, seconds):
    # An explicitly injected sleeper is part of that test's contract; only
    # the production default (real time.sleep) is compressed.
    if getattr(self, "sleeper", None) is not _health.time.sleep:
        return _RETRY_SLEEP_ORIGINAL(self, seconds)
    try:
        seconds = min(float(seconds or 0.0), 0.05)
    except (TypeError, ValueError):
        seconds = 0.0
    return _RETRY_SLEEP_ORIGINAL(self, seconds)


_health.RetryPolicy.sleep = _fast_retry_sleep

_PROBE_PREFIX = ".dsh-tmp-probe-"
_TEST_OVERRIDE_VARS = ("DSH_TEST_TMPDIR", "DSH_TMPDIR")
_ENV_VARS = ("TMPDIR", "TMP", "TEMP")
_EXPLICIT_VARS = _TEST_OVERRIDE_VARS + _ENV_VARS


def _env_temp_candidates(names=None):
    """Return environment temp candidates in override order."""
    candidates = []
    for name in (names or _EXPLICIT_VARS):
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            candidates.append(Path(os.path.expanduser(raw)))
        except (TypeError, ValueError):
            continue
    return candidates


# Capture the environment handed to the import before _select_tempdir()
# rewrites os.environ["TMPDIR"].  The Harness may grant a workspace-local
# ``.dsh-tmp-*`` directory through TMPDIR; that grant must stay visible to
# workspace_temp_root() even after this module installs its own fallback.
_IMPORT_TEMP_CANDIDATES = tuple(_env_temp_candidates())
_IMPORT_TEST_OVERRIDES = tuple(_env_temp_candidates(_TEST_OVERRIDE_VARS))


def _usable(directory):
    """True when *directory* already exists and can create/remove a file.

    Never creates *directory*.  A nonexistent parent (or a parent that is a
    regular file, which yields ``ENOTDIR`` even for root) returns False.
    """
    try:
        directory = Path(directory)
        if not directory.is_dir():
            return False
        handle, name = tempfile.mkstemp(prefix=_PROBE_PREFIX,
                                        dir=str(directory))
        try:
            os.close(handle)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        return True
    except Exception:
        return False


def _create_and_probe(candidate):
    """Create a deliberate fallback directory, then verify it."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        return None
    if _usable(candidate):
        try:
            return candidate.resolve()
        except OSError:
            return candidate
    return None


def _first_usable(candidates, create):
    seen = set()
    for candidate in candidates:
        try:
            key = str(candidate)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        if create:
            selected = _create_and_probe(candidate)
        else:
            selected = None
            if _usable(candidate):
                try:
                    selected = candidate.resolve()
                except OSError:
                    selected = candidate
        if selected is not None:
            return selected
    return None


def _home_cache():
    try:
        return Path.home() / ".cache" / "dsh-tests"
    except Exception:
        return None


def _select_tempdir():
    # Explicit overrides are probed only when they already exist.  In
    # particular, do not create a nonexistent TMPDIR: root can create paths
    # like /no/such, which would mask the environmental misconfiguration.
    explicit = []
    for name in _EXPLICIT_VARS:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            explicit.append(Path(os.path.expanduser(raw)))
        except (TypeError, ValueError):
            continue
    selected = _first_usable(explicit, create=False)
    if selected is not None:
        return selected

    repo = Path(__file__).resolve().parent.parent
    # The connector's read-only reviewer can write only below
    # ``<workspace>/.dsh-tmp-*`` (see safety.seatbelt_profile).  Prefer that
    # grant over ``repo/.tmp`` so a focused run with an unusable TMPDIR (for
    # example the reviewer's TMPDIR=/tmp) still executes every test instead of
    # skipping the ones that need a scratch directory.
    fallbacks = [repo / ".dsh-tmp-tests", repo / ".tmp"]
    try:
        fallbacks.append(Path.cwd() / ".tmp")
    except OSError:
        pass
    home_cache = _home_cache()
    if home_cache is not None:
        fallbacks.append(home_cache)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        fallbacks.append(Path(runtime) / "dsh-tests")
    # Linux containers often mount /dev/shm writable even when the repository
    # and the default /tmp are read-only.  XDG_RUNTIME_DIR is the equivalent
    # per-user runtime area on many systems.
    fallbacks.extend((Path("/dev/shm/dsh-tests"), Path("/tmp/dsh-tests")))
    selected = _first_usable(fallbacks, create=True)
    if selected is not None:
        return selected

    try:
        default = Path(tempfile.gettempdir())
    except Exception:
        return None
    return _first_usable([default], create=False)


def _inside(path, root):
    """True when *path* resolves to *root* or below it."""
    try:
        path = Path(path).resolve()
        root = Path(root).resolve()
    except (OSError, RuntimeError):
        return False
    return path == root or root in path.parents


def _workspace_grant(candidate, repo):
    """True when *candidate* is an allowed workspace scratch location.

    ``DSH_TEST_TMPDIR``/``DSH_TMPDIR`` are explicit test-run overrides and are
    accepted anywhere (the caller opted in).  ``TMPDIR``/``TMP``/``TEMP`` are
    ambient, so they are accepted only inside the repository or for the
    Harness-granted ``<workspace>/.dsh-tmp-*`` directory whose parent is the
    repository's parent (or ``DSH_WORKSPACE``).  A ``.dsh-tmp-*`` directory
    under ``/tmp`` is not a workspace grant (NB1/S6 risk).
    """
    try:
        candidate = Path(candidate)
    except (TypeError, ValueError):
        return False
    if _inside(candidate, repo):
        return True
    if not candidate.name.startswith(".dsh-tmp-"):
        return False
    roots = [repo.parent]
    workspace = os.environ.get("DSH_WORKSPACE")
    if workspace:
        try:
            roots.append(Path(os.path.expanduser(workspace)))
        except (TypeError, ValueError):
            pass
    return any(_inside(candidate, root) for root in roots)


def workspace_temp_root():
    """Return a verified workspace-local scratch root or raise SkipTest.

    ``DSH_TEST_TMPDIR``/``DSH_TMPDIR`` (explicit test overrides) come first and
    may point anywhere writable.  Then an existing, writable
    TMPDIR/TMP/TEMP candidate is preferred when it is inside the repository or
    is the Harness-granted ``<workspace>/.dsh-tmp-*`` directory.  The
    import-time snapshot keeps that grant visible after ``_select_tempdir``
    rewrites ``TMPDIR``.  Deliberate fallbacks are ``<repo>/.dsh-tmp-tests`` and
    ``<repo>/.tmp``; ``cwd/.tmp`` is accepted only when the cwd is inside the
    repository.  ``repo.parent`` is never a derived candidate (NB1).  If none
    is writable the environment-only skip is raised; the unittest hook turns
    it into a counted per-test skip (B2).
    """
    repo = Path(__file__).resolve().parent.parent
    # Inside an active case, TMPDIR points at <case>/tmp.  That is a child of
    # the scratch base, not the base itself: accepting it would make every
    # call return a nested case-local path (and made the TEST-MAC run fail its
    # "tempdir is under the workspace root" assertion).  Skip any candidate
    # inside the active case.
    active_case = os.environ.get("DSH_TEST_CASE_DIR")
    active_case_path = Path(active_case) if active_case else None

    def _case_local(candidate):
        return (active_case_path is not None
                and _inside(candidate, active_case_path))

    test_overrides = []
    for candidate in (_env_temp_candidates(_TEST_OVERRIDE_VARS)
                      + list(_IMPORT_TEST_OVERRIDES)):
        if candidate not in test_overrides and not _case_local(candidate):
            test_overrides.append(candidate)
    selected = _first_usable(test_overrides, create=False)
    if selected is not None:
        return selected
    env_candidates = []
    for candidate in (_env_temp_candidates(_ENV_VARS)
                      + list(_IMPORT_TEMP_CANDIDATES)):
        if (_workspace_grant(candidate, repo) and not _case_local(candidate)
                and candidate not in env_candidates):
            env_candidates.append(candidate)
    selected = _first_usable(env_candidates, create=False)
    if selected is not None:
        return selected
    fallbacks = [repo / ".dsh-tmp-tests", repo / ".tmp"]
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None
    if cwd is not None and _inside(cwd, repo):
        fallbacks.append(cwd / ".tmp")
    selected = _first_usable(fallbacks, create=True)
    if selected is not None:
        return selected
    raise unittest.SkipTest(
        "environment-only: no writable workspace scratch root inside the "
        "workspace (tried %s); the suite must not write outside the "
        "sandboxed workspace"
        % ", ".join(str(item) for item in test_overrides + env_candidates + fallbacks))


def tempdir():
    """Return a verified writable base directory or skip for environment only.

    Suitable for ``tempfile.TemporaryDirectory(dir=tests.tempdir())``.  The
    current environment is re-resolved on each call so an explicit, existing
    TMPDIR is honoured.
    """
    selected = _select_tempdir()
    if selected is None:
        raise unittest.SkipTest(
            "environment-only: no usable temporary directory; set TMPDIR "
            "(or DSH_TEST_TMPDIR) to an existing writable directory")
    return str(selected)


def make_tempdir(prefix="dsh-test-"):
    """Create and return a fresh directory under the verified temp root."""
    try:
        return tempfile.mkdtemp(prefix=prefix, dir=tempdir())
    except OSError as exc:
        raise unittest.SkipTest(
            "environment-only: could not create a temporary directory: %s"
            % (exc,))


_SELECTED_TMPDIR = _select_tempdir()
if _SELECTED_TMPDIR is not None:
    # Python caches its temp root in tempfile.tempdir; point both the module
    # and the environment at the verified path so child processes agree.
    tempfile.tempdir = str(_SELECTED_TMPDIR)
    os.environ["TMPDIR"] = str(_SELECTED_TMPDIR)

# Install the per-test isolation hook as soon as the test package is imported.
# ``unittest discover -s tests`` imports modules as top-level names but every
# test module imports ``tests`` during discovery, before any test runs.
from tests import isolation as _isolation  # noqa: E402

_isolation.install_unittest_hook()


def load_tests(loader, standard_tests, pattern):
    """Keep the discovery suite unchanged; the per-test hook does the work.

    ``python -m unittest discover -s tests`` imports test modules as top-level
    modules and does not call this hook; importing ``tests`` installs the
    ``unittest.TestCase.run`` hook below.  Package discovery does call this
    hook, and returning the suite unchanged preserves unittest's normal class
    and module fixture handling.  The helper is stdlib-only, so no third-party
    test dependency is needed.
    """
    from tests import isolation

    isolation.install_unittest_hook()
    return standard_tests
