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

_PROBE_PREFIX = ".dsh-tmp-probe-"
_EXPLICIT_VARS = ("DSH_TEST_TMPDIR", "DSH_TMPDIR", "TMPDIR", "TMP", "TEMP")


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
    fallbacks = [repo.parent / ".dsh-tmp-tests", repo / ".dsh-tmp-tests",
                 repo / ".tmp", repo.parent / ".tmp"]
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
