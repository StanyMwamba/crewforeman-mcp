"""Landlock launcher for DSH builders (stdlib only, Python 3.9+).

The launcher is a short-lived process: it builds a Landlock ruleset from a
JSON policy, applies it to *itself*, closes inherited fds and ``exec``s the
real ``dsh`` argv.  Because ``exec`` replaces the image, the PID is preserved
and the connector's existing pipes keep working.

Exit contract::

    125  SANDBOX_UNAVAILABLE   kernel/LSM/ABI cannot provide the boundary
    126  SANDBOX_APPLY_FAILED  the ruleset or a rule could not be applied
    127  DSH_EXEC_FAILED       execvpe of the dsh argv failed

Anything else is dsh's own exit code, passed through by ``exec``.

Failure output is exactly one JSON object on stderr with ``code``, ``abi`` and
``errno``.  It never contains a path, so no absolute path outside the
workspace can leak through an error line.
"""

import ctypes
import errno
import json
import os
import struct
import sys
import threading

# ---------------------------------------------------------------------------
# Kernel ABI constants (linux/landlock.h)
# ---------------------------------------------------------------------------

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13        # ABI >= 2
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14     # ABI >= 3
LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15    # ABI >= 5, deliberately unused

# Rights that can be granted to a *file* inode.  Landlock rejects directory
# rights (REMOVE_DIR/MAKE_*) on a non-directory rule with EINVAL.
FILE_WRITE_ACCESS = LANDLOCK_ACCESS_FS_WRITE_FILE

# Directory write boundary: writes, removals and creations.  Read/execute are
# intentionally absent: Landlock leaves them unrestricted, like Seatbelt's
# ``(allow default)``.
BASE_WRITE_ACCESS = (
    LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_CHAR
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | LANDLOCK_ACCESS_FS_MAKE_SYM
)

RULESET_ATTR_SIZE = 8          # struct landlock_ruleset_attr, handled_access_fs
PATH_BENEATH_ATTR_SIZE = 12    # packed: u64 allowed_access + s32 parent_fd

EXIT_SANDBOX_UNAVAILABLE = 125
EXIT_SANDBOX_APPLY_FAILED = 126
EXIT_DSH_EXEC_FAILED = 127

MAX_EXTRA_WRITES = 32
DEFAULT_MIN_ABI = 3

CODE_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
CODE_APPLY_FAILED = "SANDBOX_APPLY_FAILED"
CODE_EXEC_FAILED = "DSH_EXEC_FAILED"

_PROBE_REASONS = {
    errno.ENOSYS: "kernel_missing_landlock",
    errno.EOPNOTSUPP: "landlock_lsm_disabled",
    errno.EPERM: "landlock_blocked_by_policy",
    errno.E2BIG: "landlock_attr_size_incompatible",
    errno.EINVAL: "landlock_invalid_argument",
}

_LIBC = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SandboxError(Exception):
    """Base launcher failure with the exit/stderr contract attached."""

    code = CODE_APPLY_FAILED
    exit_code = EXIT_SANDBOX_APPLY_FAILED

    def __init__(self, reason="sandbox_error", abi=None, errno_value=None):
        super().__init__(reason)
        self.reason = reason
        self.abi = abi
        self.errno = errno_value


class SandboxUnavailable(SandboxError):
    code = CODE_UNAVAILABLE
    exit_code = EXIT_SANDBOX_UNAVAILABLE


class SandboxApplyError(SandboxError):
    code = CODE_APPLY_FAILED
    exit_code = EXIT_SANDBOX_APPLY_FAILED


class ExecFailed(SandboxError):
    code = CODE_EXEC_FAILED
    exit_code = EXIT_DSH_EXEC_FAILED


# ---------------------------------------------------------------------------
# Small testable seams
# ---------------------------------------------------------------------------


def _platform():
    return sys.platform


def _active_threads():
    return threading.active_count()


def _libc():
    global _LIBC
    if _LIBC is None:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.syscall.restype = ctypes.c_long
        lib.prctl.restype = ctypes.c_int
        _LIBC = lib
    return _LIBC


def _syscall(number, *args):
    """Raw libc ``syscall``; raises OSError with errno on failure."""
    ctypes.set_errno(0)
    result = _libc().syscall(ctypes.c_long(int(number)), *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code or errno.EPERM, os.strerror(code or errno.EPERM))
    return result


def _prctl(option, arg2):
    ctypes.set_errno(0)
    result = _libc().prctl(
        ctypes.c_int(int(option)),
        ctypes.c_ulong(int(arg2)),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code or errno.EPERM, os.strerror(code or errno.EPERM))
    return result


# ---------------------------------------------------------------------------
# ABI / masks
# ---------------------------------------------------------------------------


def write_access_mask(abi):
    """Return the handled_access_fs write mask supported by ``abi``.

    ABI 1 has no REFER and no TRUNCATE; ABI >= 2 adds REFER; ABI >= 3 adds
    TRUNCATE.  Read and execute are never handled.
    """
    abi = int(abi)
    mask = BASE_WRITE_ACCESS
    if abi >= 2:
        mask |= LANDLOCK_ACCESS_FS_REFER
    if abi >= 3:
        mask |= LANDLOCK_ACCESS_FS_TRUNCATE
    return mask


def file_write_access(abi):
    """File-rule mask: WRITE_FILE plus TRUNCATE when the ABI supports it."""
    mask = FILE_WRITE_ACCESS
    if int(abi) >= 3:
        mask |= LANDLOCK_ACCESS_FS_TRUNCATE
    return mask


def pack_ruleset_attr(handled_access_fs):
    """Pack ``struct landlock_ruleset_attr`` (only the 8-byte first field)."""
    return struct.pack("=Q", int(handled_access_fs) & 0xFFFFFFFFFFFFFFFF)


def pack_path_beneath_attr(allowed_access, parent_fd):
    """Pack ``struct landlock_path_beneath_attr`` (packed, 12 bytes)."""
    return struct.pack("=Qi", int(allowed_access), int(parent_fd))


def probe_abi():
    """Probe Landlock without creating a ruleset.

    Returns ``{"ok": bool, "abi": int|None, "reason": str|None,
    "errno": int|None}``.  EINVAL/E2BIG are reported separately from
    ENOSYS/EOPNOTSUPP/EPERM because they mean a bug or a struct mismatch,
    not a missing kernel feature.
    """
    try:
        abi = _syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            None,
            0,
            LANDLOCK_CREATE_RULESET_VERSION,
        )
    except OSError as exc:
        code = int(getattr(exc, "errno", None) or errno.EINVAL)
        return {
            "ok": False,
            "abi": None,
            "errno": code,
            "reason": _PROBE_REASONS.get(
                code, "landlock_probe_errno_%d" % code
            ),
        }
    except Exception:
        return {
            "ok": False,
            "abi": None,
            "errno": None,
            "reason": "landlock_probe_failed",
        }
    abi = int(abi)
    if abi < 1:
        return {
            "ok": False,
            "abi": abi,
            "errno": None,
            "reason": "landlock_probe_returned_%d" % abi,
        }
    return {"ok": True, "abi": abi, "errno": None, "reason": None}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _canonical(path):
    return os.path.realpath(os.path.expanduser(str(path)))


def _is_dir(path):
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def _within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except (ValueError, OSError):
        return False


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return os.path.isdir(path)
    except OSError:
        return False


def normalize_mode(mode):
    text = str(mode or "").strip().lower().replace("_", "-")
    if text in ("workspace-write", "workspace", "rw", "write"):
        return "workspace-write"
    if text in ("read-only", "readonly", "ro"):
        return "read-only"
    raise ValueError("invalid sandbox mode")


def parse_policy(raw):
    """Parse and canonicalize a policy mapping or JSON string.

    Only the documented keys are consumed: workspace, mode, tmpdir,
    extra_writes, dsh_home, min_abi, required.  Unknown keys are ignored.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("policy must be a JSON object")

    workspace_raw = raw.get("workspace")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise ValueError("workspace is required")
    workspace = _canonical(workspace_raw)
    if not os.path.isabs(workspace) or not _is_dir(workspace):
        raise ValueError("workspace must be an existing absolute directory")

    mode = normalize_mode(raw.get("mode") or "workspace-write")

    tmpdir = None
    if raw.get("tmpdir"):
        tmpdir = _canonical(raw.get("tmpdir"))

    extra = []
    seen = set()
    extras_raw = raw.get("extra_writes") or ()
    if not isinstance(extras_raw, (list, tuple)):
        raise ValueError("extra_writes must be a list")
    for item in extras_raw:
        if len(extra) >= MAX_EXTRA_WRITES:
            break
        if not isinstance(item, str) or not item.strip():
            continue
        real = _canonical(item)
        if real in seen:
            continue
        # Keep missing roots in the normalized policy so collect_grants can
        # record the omission instead of silently dropping it.
        if not os.path.isabs(real):
            continue
        if real == os.path.sep or real == _canonical("~"):
            continue
        seen.add(real)
        extra.append(real)

    dsh_home = None
    if raw.get("dsh_home"):
        candidate = _canonical(raw.get("dsh_home"))
        if os.path.isabs(candidate):
            dsh_home = candidate
    if dsh_home is None and mode == "workspace-write":
        dsh_home = _canonical(os.path.join("~", ".dsh"))

    try:
        min_abi = int(raw.get("min_abi", DEFAULT_MIN_ABI))
    except (TypeError, ValueError):
        min_abi = DEFAULT_MIN_ABI
    if min_abi < 1:
        min_abi = 1

    return {
        "workspace": workspace,
        "mode": mode,
        "tmpdir": tmpdir,
        "extra_writes": extra,
        "dsh_home": dsh_home,
        "min_abi": min_abi,
        "required": bool(raw.get("required", False)),
    }


def policy_json(policy):
    """Serialize a normalized policy deterministically (JSON round-trip)."""
    normalized = parse_policy(policy)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Rule planning
# ---------------------------------------------------------------------------


def collect_grants(policy, abi):
    """Build the ordered grant list without applying anything.

    Returns ``(rules, skipped, review_write)``.  Rules are dicts with
    ``path``, ``access``, ``is_dir`` and ``source``.  Missing paths are
    recorded in ``skipped`` with a short reason and never raise.
    """
    policy = parse_policy(policy)
    abi = int(abi)
    dir_access = write_access_mask(abi) & write_access_mask(abi)
    file_access = file_write_access(abi)
    workspace = policy["workspace"]
    mode = policy["mode"]
    rules = []
    skipped = []
    seen = {}

    def add(path, access, source, ensure=False, require_inside=None):
        if not path:
            return
        real = _canonical(path)
        if require_inside is not None and not _within(real, require_inside):
            skipped.append({"path": real, "reason": "outside_root",
                            "source": source})
            return
        if ensure and not os.path.exists(real):
            if not _ensure_dir(real):
                skipped.append({"path": real, "reason": "cannot_create",
                                "source": source})
                return
        if not os.path.exists(real):
            skipped.append({"path": real, "reason": "missing",
                            "source": source})
            return
        is_dir = _is_dir(real)
        rule_access = dir_access if is_dir else file_access
        if not is_dir:
            # A literal file rule only accepts file rights.  Masking is
            # mandatory: add_rule returns EINVAL otherwise.
            rule_access = access & file_access
            if not rule_access:
                skipped.append({"path": real, "reason": "no_file_rights",
                                "source": source})
                return
        if real in seen:
            # Merge overlapping grants for the same inode.
            seen[real]["access"] |= rule_access
            return
        rule = {
            "path": real,
            "access": rule_access,
            "is_dir": is_dir,
            "source": source,
        }
        seen[real] = rule
        rules.append(rule)

    if mode == "workspace-write":
        add(workspace, dir_access, "workspace")

    # The report literal, evidence dir and exact run tmpdir are granted in
    # both modes.  There is no regex: only the exact tmpdir is passed.
    add(os.path.join(workspace, "REVIEW.md"), file_access, "review",
        require_inside=workspace)
    add(os.path.join(workspace, ".browser-evidence"), dir_access,
        "browser_evidence", ensure=True, require_inside=workspace)
    if policy.get("tmpdir"):
        add(policy["tmpdir"], dir_access, "tmpdir", ensure=True)

    if mode == "workspace-write" and policy.get("dsh_home"):
        home = policy["dsh_home"]
        private = home != _canonical(os.path.join("~", ".dsh"))
        runtime = ["sessions"]
        if not private:
            runtime.extend(["profiles", "storages", "cache"])
        for name in runtime:
            add(os.path.join(home, name), dir_access, "dsh_home_" + name,
                ensure=True)

    for extra in policy.get("extra_writes") or ():
        add(extra, dir_access, "extra_write")

    review_write = any(rule["source"] == "review" for rule in rules)
    return rules, skipped, review_write


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _add_rule(ruleset_fd, path, access):
    flags = getattr(os, "O_PATH", os.O_RDONLY)
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(path, flags)
    try:
        attr = ctypes.create_string_buffer(
            pack_path_beneath_attr(access, parent_fd), PATH_BENEATH_ATTR_SIZE
        )
        _syscall(
            SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def apply_landlock(policy, exec_fn=None):
    """Apply a Landlock ruleset to this process.

    With ``exec_fn`` set the function calls it after the boundary is live
    (the CLI passes ``os.execvpe``); otherwise it returns a result dict.
    All kernel entry points are module-level seams (``_syscall``,
    ``_prctl``, ``probe_abi``) so the ABI/errno matrices are unit-testable
    on any host.
    """
    policy = parse_policy(policy)

    if _platform() != "linux":
        raise SandboxUnavailable("not_linux")
    if _active_threads() != 1:
        raise SandboxApplyError("threaded_process")

    probe = probe_abi()
    if not probe.get("ok"):
        raise SandboxUnavailable(
            probe.get("reason") or "landlock_unavailable",
            abi=probe.get("abi"),
            errno_value=probe.get("errno"),
        )

    abi = int(probe["abi"])
    min_abi = int(policy["min_abi"])
    degraded = abi < min_abi
    if degraded and policy["required"]:
        raise SandboxUnavailable("abi_below_minimum", abi=abi)

    handled = write_access_mask(abi)
    rules, skipped, review_write = collect_grants(policy, abi)

    ruleset_fd = None
    try:
        attr = ctypes.create_string_buffer(
            pack_ruleset_attr(handled), RULESET_ATTR_SIZE
        )
        ruleset_fd = _syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            RULESET_ATTR_SIZE,
            0,
        )
        for rule in rules:
            try:
                _add_rule(ruleset_fd, rule["path"], rule["access"])
            except OSError as exc:
                raise SandboxApplyError(
                    "add_rule_%s" % rule["source"],
                    abi=abi,
                    errno_value=exc.errno,
                )
        try:
            _prctl(PR_SET_NO_NEW_PRIVS, 1)
        except OSError as exc:
            raise SandboxApplyError("prctl_no_new_privs", abi=abi,
                                    errno_value=exc.errno)
        try:
            _syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        except OSError as exc:
            raise SandboxApplyError("restrict_self", abi=abi,
                                    errno_value=exc.errno)
    finally:
        if ruleset_fd is not None and ruleset_fd > 2:
            try:
                os.close(ruleset_fd)
            except OSError:
                pass

    result = {
        "ok": True,
        "abi": abi,
        "min_abi": min_abi,
        "degraded": degraded,
        "handled_access_fs": handled,
        "rules": rules,
        "granted": [rule["path"] for rule in rules],
        "skipped": skipped,
        "review_write": review_write,
        "mode": policy["mode"],
        "workspace": policy["workspace"],
    }

    if exec_fn is not None:
        return exec_fn()
    return result


# ---------------------------------------------------------------------------
# Exec and CLI
# ---------------------------------------------------------------------------


def _max_fd():
    try:
        limit = int(os.sysconf("SC_OPEN_MAX"))
    except (ValueError, OSError, AttributeError):
        limit = 65536
    if limit <= 3 or limit > (1 << 20):
        limit = 1 << 20
    return limit


def close_inherited_fds():
    """Close every fd above stderr so no inherited handle bypasses Landlock."""
    try:
        os.closerange(3, _max_fd())
    except OSError:
        pass


def exec_dsh(argv, env=None):
    """Close inherited fds and exec ``argv``; preserves the PID."""
    argv = [str(item) for item in (argv or ())]
    if not argv:
        raise ExecFailed("empty_argv")
    close_inherited_fds()
    try:
        os.execvpe(argv[0], argv, os.environ if env is None else env)
    except OSError as exc:
        raise ExecFailed("execvpe", errno_value=exc.errno)


def _usage_failure():
    return SandboxApplyError("invalid_arguments")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if "--policy" not in args:
            raise _usage_failure()
        index = args.index("--policy")
        if index + 1 >= len(args):
            raise _usage_failure()
        raw_policy = args[index + 1]
        rest = args[index + 2:]
        if not rest or rest[0] != "--":
            raise _usage_failure()
        command = rest[1:]
        if not command:
            raise ExecFailed("empty_argv")
        policy = parse_policy(raw_policy)
        apply_landlock(policy, exec_fn=lambda: exec_dsh(command))
        raise ExecFailed("exec_returned")
    except SandboxError as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "code": exc.code,
                    "abi": exc.abi,
                    "errno": exc.errno,
                },
                sort_keys=True,
            )
            + "\n"
        )
        sys.stderr.flush()
        return exc.exit_code
    except (ValueError, TypeError, KeyError):
        sys.stderr.write(
            json.dumps(
                {"code": CODE_APPLY_FAILED, "abi": None, "errno": None},
                sort_keys=True,
            )
            + "\n"
        )
        sys.stderr.flush()
        return EXIT_SANDBOX_APPLY_FAILED


if __name__ == "__main__":
    sys.exit(main())
