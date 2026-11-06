"""Test package for the DeepSeek MCP server.

The suite must be runnable from a read-only review sandbox whose ``TMPDIR`` is
missing or points at an unwritable directory.  Python's ``tempfile`` then
raises ``FileNotFoundError: No usable temporary directory`` before any product
assertion runs (the Kimi reviewer hit exactly this).  At import time we honour
a usable ``DSH_TEST_TMPDIR``/``TMPDIR``/``TMP``/``TEMP`` and otherwise install
a verified writable fallback under the repository, its parent workspace, the
current directory, a user cache, an in-memory tmpfs when present, or the
system temp root.  A path is accepted only after a real create/unlink probe.
"""

import os
import tempfile
from pathlib import Path


def _usable(directory):
    """True when *directory* exists and can really create/remove a temp file."""
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".dsh-tmp-probe-",
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


def _home_cache():
    try:
        return Path.home() / ".cache" / "dsh-tests"
    except Exception:
        return None


def _select_tempdir():
    candidates = []
    # Explicit test-only override first; then honour a usable
    # TMPDIR/TMP/TEMP.  The operator/reviewer may deliberately point the suite
    # at a writable sandbox path.
    for name in ("DSH_TEST_TMPDIR", "DSH_TMPDIR", "TMPDIR", "TMP", "TEMP"):
        raw = os.environ.get(name)
        if raw:
            candidates.append(Path(os.path.expanduser(raw)))
    repo = Path(__file__).resolve().parent.parent
    # A review sandbox may expose system temp variables that are unwritable
    # while the copied workspace (or its parent) is writable.  Prefer those
    # workspace-local fallbacks before the default temp root.
    candidates.extend((repo / ".tmp", repo.parent / ".tmp",
                       Path.cwd() / ".tmp"))
    home_cache = _home_cache()
    if home_cache is not None:
        candidates.append(home_cache)
    # Linux containers often mount /dev/shm writable even when the repository
    # and the default /tmp are read-only.  XDG_RUNTIME_DIR is the equivalent
    # per-user runtime area on many systems.
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime) / "dsh-tests")
    candidates.append(Path("/dev/shm/dsh-tests"))
    candidates.append(Path("/tmp/dsh-tests"))
    try:
        candidates.append(Path(tempfile.gettempdir()))
    except Exception:
        pass
    seen = []
    for candidate in candidates:
        try:
            key = str(candidate)
        except Exception:
            continue
        if key in seen:
            continue
        seen.append(key)
        if _usable(candidate):
            return candidate.resolve()
    return None


_SELECTED_TMPDIR = _select_tempdir()
if _SELECTED_TMPDIR is not None:
    # Python caches its temp root in tempfile.tempdir; point both the module
    # and the environment at the verified path so child processes agree.
    tempfile.tempdir = str(_SELECTED_TMPDIR)
    os.environ["TMPDIR"] = str(_SELECTED_TMPDIR)
