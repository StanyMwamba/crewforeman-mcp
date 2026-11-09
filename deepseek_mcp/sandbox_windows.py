"""Windows write-restricted launcher for DSH.

Import-safe on every platform: Windows DLLs, ``ctypes.windll`` and ``msvcrt``
are only touched inside functions/classes that run on Windows.  Standard
library only, Python 3.9 compatible.

CLI::

    python -m deepseek_mcp.sandbox_windows --policy <json> -- <dsh argv...>

The launcher opens the current token, creates a write-restricted token, grants
an inheritable ACE for the restricting SID on the policy roots, starts ``dsh``
suspended in a kill-on-close Job Object with exactly three inherited stdio
handles, resumes it, waits, restores the root DACLs and returns the child's
exit code.  It fails closed with 125/126/127; it never falls back to an
unwrapped child.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys

EXIT_UNAVAILABLE = 125
EXIT_APPLY_FAILED = 126
EXIT_DSH_FAILED = 127
EXIT_TIMEOUT = 124

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
TOKEN_LOGON_SID = 19
TOKEN_ELEVATION = 20

DISABLE_MAX_PRIVILEGE = 0x0001
WRITE_RESTRICTED = 0x0008
LUA_TOKEN = 0x0004

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
GRANT_ACCESS = 1
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
FILE_DELETE_CHILD = 0x00000040
FILE_MODIFY = 0x0001301BF  # FILE_GENERIC_READ|WRITE|EXECUTE|DELETE
ACE_MASK = FILE_MODIFY | FILE_DELETE_CHILD

FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
DRIVE_REMOTE = 4

HANDLE_FLAG_INHERIT = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002

JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_FLAGS = (JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
             | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF

_LAST_RESULT = {}
_STAGE = {"name": "launcher"}


def _stage(name):
    _STAGE["name"] = str(name or "launcher")


# ---------------------------------------------------------------------------
# errors / small helpers


_DIAG_LIMIT = 300
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)"
    r"(\s*[:=]\s*)([^\s;,]+)")
_FORBIDDEN_KEYS = frozenset({
    "env", "environ", "environment", "cmdline", "command_line",
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "authorization", "credential", "credentials",
})


class SandboxError(Exception):
    """Internal failure carrying exit code, safe reason, step and cause."""

    def __init__(self, exit_code, reason, step=None, cause=None,
                 fallback_step=None, fallback_cause=None):
        super().__init__(reason)
        self.exit_code = int(exit_code)
        self.reason = str(reason)
        self.step = str(step or "apply")
        self.cause = cause
        self.fallback_step = fallback_step
        self.fallback_cause = fallback_cause


class PolicyError(SandboxError):
    def __init__(self, reason, step="policy"):
        super().__init__(EXIT_APPLY_FAILED, reason, step=step)


def _is_windows():
    return os.name == "nt" or sys.platform == "win32"


def _strip_extended(path):
    text = str(path or "")
    low = text.lower()
    if low.startswith("\\\\?\\unc\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    if text.startswith("\\??\\"):
        return text[4:]
    return text


def _is_unc(path):
    text = _strip_extended(path)
    return text.startswith("\\\\") or text.startswith("//")


def _win_error(rc, name):
    """Build an OSError carrying the real Win32 error code.

    BOOL-returning APIs pass ``rc=0`` and the code comes from the thread's
    LastError (all DLLs are opened with ``use_last_error=True``).  APIs such
    as GetNamedSecurityInfoW, SetEntriesInAclW and SetNamedSecurityInfoW
    return the error code and leave LastError undefined, so a non-zero ``rc``
    always wins.
    """
    rc = int(rc or 0)
    if rc:
        err = rc
    else:
        getter = getattr(ctypes, "get_last_error", None)
        try:
            err = int(getter() or 0) if getter else 0
        except Exception:
            err = 0
    exc = OSError(err or rc, "%s failed (winerror=%s)" % (name, err))
    exc.winerror = err
    exc.function = name
    return exc


def _access_denied(exc):
    winerror = getattr(exc, "winerror", None)
    if winerror == 5:
        return True
    if getattr(exc, "errno", None) == 13:
        return True
    return "access is denied" in str(exc).lower()


def _one_line(text, limit=_DIAG_LIMIT):
    text = str(text or "").replace("\x00", "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:max(0, limit - 3)] + "..."


def _home_prefixes():
    roots = []
    for raw in (os.path.expanduser("~"), os.environ.get("USERPROFILE"),
                os.environ.get("HOME")):
        if not raw:
            continue
        try:
            real = os.path.realpath(raw)
        except Exception:
            real = raw
        for prefix in (raw, real):
            prefix = str(prefix or "").rstrip("\\/")
            if len(prefix) >= 4 and prefix not in roots:
                roots.append(prefix)
    roots.sort(key=len, reverse=True)
    return roots


def _safe_text(text, limit=_DIAG_LIMIT):
    """Single-line, home-redacted, secret-redacted, length-capped text."""
    out = _one_line(text, limit=limit * 4)
    for prefix in _home_prefixes():
        out = out.replace(prefix, "~")
        out = out.replace(prefix.replace("\\", "/"), "~")
    out = _SECRET_RE.sub(r"\1\2<redacted>", out)
    out = " ".join(out.split())
    return out if len(out) <= limit else out[:max(0, limit - 3)] + "..."


def _safe_message(exc, limit=_DIAG_LIMIT):
    if exc is None:
        return "unknown failure"
    try:
        text = str(exc)
    except Exception:
        text = ""
    return _safe_text(text or exc.__class__.__name__, limit=limit)


def _safe_value(value):
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()
                if str(key).lower() not in _FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return value


def _reason(exc):
    if isinstance(exc, SandboxError):
        return exc.reason
    winerror = getattr(exc, "winerror", None)
    if winerror == 1314:
        return "privilege_not_held"
    if winerror == 5:
        return "access_denied"
    if winerror == 87:
        return "invalid_parameter"
    if winerror == 122:
        return "insufficient_buffer"
    if winerror == 1326:
        return "logon_failure"
    return exc.__class__.__name__.lower() or "windows_api_error"


def _error(exc):
    """Public {type,message} shape, unwrapping a SandboxError cause."""
    cause = exc
    if isinstance(exc, SandboxError) and exc.cause is not None:
        cause = exc.cause
    if cause is None:
        return {"type": "LauncherError", "message": "unknown failure"}
    return {"type": cause.__class__.__name__,
            "message": _safe_message(cause)}


def _diag(step, exc=None, reason=None, message=None, **extra):
    """Build {step, reason, error:{type,message}} without leaking detail."""
    if reason is None:
        reason = _reason(exc) if exc is not None else "unknown"
    error = {"type": exc.__class__.__name__ if exc is not None
             else "LauncherError",
             "message": (_safe_message(exc) if exc is not None
                         else _safe_text(message or reason))}
    diag = {"step": _one_line(step) or "unknown",
            "reason": _one_line(reason) or "unknown",
            "error": error}
    winerror = getattr(exc, "winerror", None) if exc is not None else None
    if winerror is not None:
        diag["winerror"] = int(winerror)
    for key, value in extra.items():
        if value is not None:
            diag[key] = _safe_value(value)
    return diag


def _emit(code, reason, step=None, error=None, **extra):
    """Write exactly one JSON diagnostic line; never a traceback or env dump."""
    if isinstance(error, BaseException):
        error = _error(error)
    if not isinstance(error, dict) or not error.get("type"):
        error = {"type": "LauncherError",
                 "message": _safe_text(reason or "unknown failure")}
    payload = {
        "code": int(code),
        "reason": _one_line(reason) or "unknown",
        "step": _one_line(step) or "launcher",
        "error": {"type": _one_line(error.get("type")) or "LauncherError",
                  "message": _safe_text(error.get("message") or "")},
    }
    for key, value in extra.items():
        if str(key).lower() in _FORBIDDEN_KEYS:
            continue
        payload[key] = _safe_value(value)
    try:
        sys.stderr.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def last_result():
    """Return the most recent run metadata (tests/run-record integration)."""
    return dict(_LAST_RESULT)


def _process_flags():
    """Creation flags for the restricted child.

    ``CREATE_NO_WINDOW`` is deliberately absent, and the child is created
    detached so no console is allocated at all.  Every 3.13.3 boundary child
    exited with STATUS_DLL_INIT_FAILED (0xC0000142) before Python ran; CPython
    records that status as a generic DLL-init failure, likely from low desktop
    heap memory when a console is created.  3.13.4 hid the console with
    STARTF_USESHOWWINDOW/SW_HIDE but still allocated one, and py3.15 hit the
    same status on windows-2025; DETACHED_PROCESS avoids conhost/desktop-heap
    initialization entirely.  Standard handles are still supplied through
    STARTF_USESTDHANDLES, so stdout/stderr remain the normal pipes.
    """
    return (CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
            | EXTENDED_STARTUPINFO_PRESENT | DETACHED_PROCESS)


# ---------------------------------------------------------------------------
# ctypes structures (safe to declare on macOS; no DLL is loaded)


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", ctypes.c_uint32),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", ctypes.c_uint32),
        ("Trustee", _TRUSTEE_W),
    ]


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", ctypes.c_uint32)]


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_void_p),
        ("lpDesktop", ctypes.c_void_p),
        ("lpTitle", ctypes.c_void_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFO),
                ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _JOB_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64)]


class _JOB_EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOB_BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


# ---------------------------------------------------------------------------
# real Windows API facade


class WindowsAPI:
    """Thin, testable facade over the Win32 calls used by the launcher."""

    def __init__(self):
        if not _is_windows():
            raise OSError("Windows APIs are unavailable on this host")
        self.ctypes = ctypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._pinned = []
        self._configure()

    def _configure(self):
        C = self.ctypes
        k = self.kernel32
        a = self.advapi32
        self.bound = []

        def bind(lib, libname, name, argtypes, restype):
            """Bind one export and name it if the DLL does not have it.

            ``ctypes.WinDLL.__getattr__`` raises a bare AttributeError for a
            missing export.  Converting that into a named SandboxError is how
            a future CI run can tell which DLL/function broke.
            """
            fn = getattr(lib, name, None)
            if fn is None:
                raise SandboxError(
                    EXIT_UNAVAILABLE,
                    "dll_symbol_missing:%s!%s" % (libname, name),
                    step="configure")
            fn.argtypes = argtypes
            fn.restype = restype
            self.bound.append("%s!%s" % (libname, name))
            return fn

        # kernel32: process, handle, path, job and local-memory APIs.
        bind(k, "kernel32", "GetCurrentProcess", [], C.c_void_p)
        bind(k, "kernel32", "CloseHandle", [C.c_void_p], C.c_int)
        bind(k, "kernel32", "GetLongPathNameW",
             [C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_uint32)
        bind(k, "kernel32", "GetDriveTypeW", [C.c_wchar_p], C.c_uint32)
        bind(k, "kernel32", "SetHandleInformation",
             [C.c_void_p, C.c_uint32, C.c_uint32], C.c_int)
        bind(k, "kernel32", "CreateJobObjectW",
             [C.c_void_p, C.c_wchar_p], C.c_void_p)
        bind(k, "kernel32", "SetInformationJobObject",
             [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32], C.c_int)
        bind(k, "kernel32", "AssignProcessToJobObject",
             [C.c_void_p, C.c_void_p], C.c_int)
        bind(k, "kernel32", "ResumeThread", [C.c_void_p], C.c_uint32)
        bind(k, "kernel32", "TerminateProcess",
             [C.c_void_p, C.c_uint32], C.c_int)
        bind(k, "kernel32", "TerminateJobObject",
             [C.c_void_p, C.c_uint32], C.c_int)
        bind(k, "kernel32", "WaitForSingleObject",
             [C.c_void_p, C.c_uint32], C.c_uint32)
        bind(k, "kernel32", "GetExitCodeProcess",
             [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int)
        bind(k, "kernel32", "InitializeProcThreadAttributeList",
             [C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_size_t)],
             C.c_int)
        bind(k, "kernel32", "UpdateProcThreadAttribute",
             [C.c_void_p, C.c_uint32, C.c_size_t, C.c_void_p, C.c_size_t,
              C.c_void_p, C.c_void_p], C.c_int)
        bind(k, "kernel32", "LocalFree", [C.c_void_p], C.c_void_p)

        # advapi32: token, security and process-with-token APIs.  These were
        # wrongly bound on kernel32 in 3.13.2 (the windows-2025 AttributeError).
        bind(a, "advapi32", "OpenProcessToken",
             [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_int)
        bind(a, "advapi32", "GetTokenInformation",
             [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32,
              C.POINTER(C.c_uint32)], C.c_int)
        bind(a, "advapi32", "CreateProcessAsUserW",
             [C.c_void_p, C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p,
              C.c_int, C.c_uint32, C.c_void_p, C.c_wchar_p,
              C.POINTER(_STARTUPINFOEX), C.POINTER(_PROCESS_INFORMATION)],
             C.c_int)
        bind(a, "advapi32", "ConvertStringSidToSidW",
             [C.c_wchar_p, C.POINTER(C.c_void_p)], C.c_int)
        bind(a, "advapi32", "ConvertSidToStringSidW",
             [C.c_void_p, C.POINTER(C.c_wchar_p)], C.c_int)
        bind(a, "advapi32", "CreateRestrictedToken",
             [C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32,
              C.c_void_p, C.c_uint32,
              C.POINTER(_SID_AND_ATTRIBUTES), C.POINTER(C.c_void_p)],
             C.c_int)
        bind(a, "advapi32", "GetLengthSid", [C.c_void_p], C.c_uint32)
        bind(a, "advapi32", "CopySid",
             [C.c_uint32, C.c_void_p, C.c_void_p], C.c_int)
        bind(a, "advapi32", "GetNamedSecurityInfoW",
             [C.c_wchar_p, C.c_int, C.c_uint32, C.POINTER(C.c_void_p),
              C.POINTER(C.c_void_p), C.POINTER(C.c_void_p),
              C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)], C.c_uint32)
        bind(a, "advapi32", "SetEntriesInAclW",
             [C.c_uint32, C.POINTER(_EXPLICIT_ACCESS_W), C.c_void_p,
              C.POINTER(C.c_void_p)], C.c_uint32)
        bind(a, "advapi32", "SetNamedSecurityInfoW",
             [C.c_wchar_p, C.c_int, C.c_uint32, C.c_void_p, C.c_void_p,
              C.c_void_p, C.c_void_p], C.c_uint32)
        bind(a, "advapi32", "CheckTokenMembership",
             [C.c_void_p, C.c_void_p, C.POINTER(C.c_int)], C.c_int)

    # -- token ---------------------------------------------------------
    def open_process_token(self):
        token = ctypes.c_void_p()
        flags = (TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY
                 | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID)
        if self.advapi32.OpenProcessToken(
                self.kernel32.GetCurrentProcess(), flags,
                ctypes.byref(token)):
            return token
        # A hardened host can deny the adjust rights.  The probe still needs
        # the reduced token to prove write restriction; if the child launch
        # later needs the missing rights, that failure is reported at spawn
        # with its own winerror instead of making the host look unusable.
        reduced = TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY
        if self.advapi32.OpenProcessToken(
                self.kernel32.GetCurrentProcess(), reduced,
                ctypes.byref(token)):
            return token
        raise _win_error(0, "OpenProcessToken")

    def close_handle(self, handle):
        if handle:
            try:
                self.kernel32.CloseHandle(handle)
            except Exception:
                pass

    def is_admin(self, token):
        value = _TOKEN_ELEVATION()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        if not self.advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(value), size.value,
                ctypes.byref(size)):
            raise _win_error(0, "GetTokenInformation(TokenElevation)")
        return bool(value.TokenIsElevated)

    def local_free(self, pointer):
        if not pointer:
            return
        try:
            if not isinstance(pointer, ctypes.c_void_p):
                pointer = ctypes.cast(pointer, ctypes.c_void_p)
            self.kernel32.LocalFree(pointer)
        except Exception:
            pass

    def _sid_from_string(self, text):
        sid = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSidToSidW(
                str(text), ctypes.byref(sid)):
            raise _win_error(0, "ConvertStringSidToSidW")
        return sid

    def make_per_run_sid(self):
        import secrets
        text = "S-1-5-21-%d-%d-%d" % (
            secrets.randbits(31), secrets.randbits(31), secrets.randbits(31))
        return self._sid_from_string(text)

    def is_admin_member(self, token):
        """True when the token is in BUILTIN\\Administrators.

        Separate from ``is_admin`` (TokenElevation): the GitHub runner account
        is an Administrators member even when UAC filters the process token.
        """
        sid = self._sid_from_string("S-1-5-32-544")
        try:
            member = ctypes.c_int(0)
            if not self.advapi32.CheckTokenMembership(
                    token, sid, ctypes.byref(member)):
                raise _win_error(0, "CheckTokenMembership")
            return bool(member.value)
        finally:
            self.local_free(sid)

    def admin_info(self, token=None):
        """Measure elevation and group membership independently; never raise
        just because one of the two queries failed."""
        owns_token = token is None
        if owns_token:
            token = self.open_process_token()
        try:
            elevated = None
            elevation_error = None
            try:
                elevated = bool(self.is_admin(token))
            except Exception as exc:  # noqa: BLE001 - reported separately
                elevation_error = exc
            member = None
            member_error = None
            try:
                member = bool(self.is_admin_member(token))
            except Exception as exc:  # noqa: BLE001 - reported separately
                member_error = exc
            if elevated is None and member is None:
                raise elevation_error or member_error or OSError(
                    "admin query failed")
            return {"elevated": elevated, "member": member,
                    "source": "token", "elevation_error": elevation_error,
                    "member_error": member_error}
        finally:
            if owns_token:
                self.close_handle(token)

    def token_logon_sid(self, token):
        size = ctypes.c_uint32(0)
        self.advapi32.GetTokenInformation(
            token, TOKEN_LOGON_SID, None, 0, ctypes.byref(size))
        if not size.value:
            raise _win_error(0, "GetTokenInformation(TokenLogonSid)")
        buf = ctypes.create_string_buffer(size.value)
        if not self.advapi32.GetTokenInformation(
                token, TOKEN_LOGON_SID, buf, size.value,
                ctypes.byref(size)):
            raise _win_error(0, "GetTokenInformation(TokenLogonSid)")
        raw = bytes(buf.raw)
        if len(raw) < ctypes.sizeof(ctypes.c_uint32):
            raise OSError("TokenLogonSid buffer too small")
        count = ctypes.c_uint32.from_buffer_copy(raw, 0).value
        if not count:
            raise OSError("TokenLogonSid is empty")
        align = ctypes.alignment(_SID_AND_ATTRIBUTES)
        offset = ctypes.sizeof(ctypes.c_uint32)
        offset = (offset + align - 1) // align * align
        if len(raw) < offset + ctypes.sizeof(_SID_AND_ATTRIBUTES):
            raise OSError("TokenLogonSid buffer too small")
        first = _SID_AND_ATTRIBUTES.from_buffer_copy(raw, offset)
        length = self.advapi32.GetLengthSid(first.Sid)
        if not length:
            raise _win_error(0, "GetLengthSid")
        memory = ctypes.create_string_buffer(int(length))
        if not self.advapi32.CopySid(int(length), memory, first.Sid):
            raise _win_error(0, "CopySid")
        self._pinned.append(memory)
        return ctypes.cast(memory, ctypes.c_void_p)

    def sid_to_string(self, sid):
        text = ctypes.c_wchar_p()
        if not self.advapi32.ConvertSidToStringSidW(
                sid, ctypes.byref(text)):
            raise _win_error(0, "ConvertSidToStringSidW")
        try:
            return str(text.value)
        finally:
            self.local_free(text)

    def create_restricted_token(self, token, sid):
        attrs = _SID_AND_ATTRIBUTES(ctypes.cast(sid, ctypes.c_void_p), 0)
        new_token = ctypes.c_void_p()
        flags = DISABLE_MAX_PRIVILEGE | WRITE_RESTRICTED
        if not self.advapi32.CreateRestrictedToken(
                token, flags, 0, None, 0, None, 1,
                ctypes.byref(attrs), ctypes.byref(new_token)):
            raise _win_error(0, "CreateRestrictedToken")
        return new_token

    # -- paths ---------------------------------------------------------
    def get_long_path_name(self, path):
        size = self.kernel32.GetLongPathNameW(str(path), None, 0)
        if not size:
            return str(path)
        buf = ctypes.create_unicode_buffer(int(size) + 1)
        written = self.kernel32.GetLongPathNameW(str(path), buf,
                                                 len(buf))
        return buf.value if written else str(path)

    def get_drive_type(self, path):
        return int(self.kernel32.GetDriveTypeW(str(path)))

    # -- ACL -----------------------------------------------------------
    def get_named_security_info(self, path):
        old_sd = ctypes.c_void_p()
        old_dacl = ctypes.c_void_p()
        rc = self.advapi32.GetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(old_dacl), None,
            ctypes.byref(old_sd))
        if rc:
            raise _win_error(rc, "GetNamedSecurityInfoW")
        return {"sd": old_sd, "dacl": old_dacl}

    def set_entries_in_acl(self, old_dacl, sid):
        trustee = _TRUSTEE_W(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN,
                             ctypes.cast(sid, ctypes.c_void_p))
        access = _EXPLICIT_ACCESS_W(
            ACE_MASK, GRANT_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            trustee)
        new_acl = ctypes.c_void_p()
        rc = self.advapi32.SetEntriesInAclW(
            1, ctypes.byref(access), old_dacl, ctypes.byref(new_acl))
        if rc:
            raise _win_error(rc, "SetEntriesInAclW")
        return new_acl

    def set_named_security_info(self, path, dacl):
        rc = self.advapi32.SetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            None, None, dacl, None)
        if rc:
            raise _win_error(rc, "SetNamedSecurityInfoW")

    # -- handles / process --------------------------------------------
    def get_osfhandle(self, fd):
        import msvcrt
        return int(msvcrt.get_osfhandle(int(fd)))

    def set_handle_information(self, handle, mask, flags):
        if not self.kernel32.SetHandleInformation(
                ctypes.c_void_p(int(handle)), int(mask), int(flags)):
            raise _win_error(0, "SetHandleInformation")

    def create_job_object(self, flags):
        job = self.kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _win_error(0, "CreateJobObjectW")
        info = _JOB_EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = int(flags)
        if not self.kernel32.SetInformationJobObject(
                job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            self.close_handle(job)
            raise _win_error(0, "SetInformationJobObject")
        return job

    def assign_process_to_job(self, job, process):
        return bool(self.kernel32.AssignProcessToJobObject(job, process))

    def resume_thread(self, thread):
        return int(self.kernel32.ResumeThread(thread)) != 0xFFFFFFFF

    def terminate_process(self, process, code=1):
        return bool(self.kernel32.TerminateProcess(process, int(code)))

    def terminate_job(self, job, code=1):
        return bool(self.kernel32.TerminateJobObject(job, int(code)))

    def wait_for_single_object(self, process, timeout_ms):
        return int(self.kernel32.WaitForSingleObject(process,
                                                     int(timeout_ms)))

    def get_exit_code(self, process):
        code = ctypes.c_uint32(0)
        if not self.kernel32.GetExitCodeProcess(process,
                                                ctypes.byref(code)):
            raise _win_error(0, "GetExitCodeProcess")
        return int(code.value)

    def create_process(self, application, command_line, environment, cwd,
                       handles, token):
        if len(handles) != 3:
            raise SandboxError(EXIT_APPLY_FAILED, "handle_list_not_three",
                               step="create_process")
        handle_array = (ctypes.c_void_p * 3)(
            *[ctypes.c_void_p(int(h)) for h in handles])
        size = ctypes.c_size_t(0)
        self.kernel32.InitializeProcThreadAttributeList(
            None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise _win_error(0, "InitializeProcThreadAttributeList(size)")
        buffer = ctypes.create_string_buffer(size.value)
        attrs = ctypes.cast(buffer, ctypes.c_void_p)
        if not self.kernel32.InitializeProcThreadAttributeList(
                attrs, 1, 0, ctypes.byref(size)):
            raise _win_error(0, "InitializeProcThreadAttributeList")
        if not self.kernel32.UpdateProcThreadAttribute(
                attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_array, ctypes.c_void_p),
                ctypes.sizeof(handle_array), None, None):
            raise _win_error(0, "UpdateProcThreadAttribute")
        startup = _STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEX)
        # Hide the console with STARTF_USESHOWWINDOW/SW_HIDE rather than
        # CREATE_NO_WINDOW.  The 3.13.3 children all died at DLL init with
        # 0xC0000142; CREATE_NO_WINDOW under the write-restricted token is the
        # believed cause (to be confirmed by the next windows-2025 run), and
        # SW_HIDE is the smallest change that still hides the console.
        startup.StartupInfo.dwFlags = (STARTF_USESTDHANDLES
                                       | STARTF_USESHOWWINDOW)
        startup.StartupInfo.wShowWindow = SW_HIDE
        startup.StartupInfo.hStdInput = ctypes.c_void_p(int(handles[0]))
        startup.StartupInfo.hStdOutput = ctypes.c_void_p(int(handles[1]))
        startup.StartupInfo.hStdError = ctypes.c_void_p(int(handles[2]))
        startup.lpAttributeList = attrs
        items = ["%s=%s" % (key, value)
                 for key, value in environment.items()
                 if key and "=" not in key]
        block = ctypes.create_unicode_buffer("\0".join(items) + "\0\0")
        cmd = ctypes.create_unicode_buffer(str(command_line))
        info = _PROCESS_INFORMATION()
        flags = _process_flags()
        ok = self.advapi32.CreateProcessAsUserW(
            token, str(application), cmd, None, None, True, flags,
            ctypes.cast(block, ctypes.c_void_p), str(cwd),
            ctypes.byref(startup), ctypes.byref(info))
        if not ok:
            raise _win_error(0, "CreateProcessAsUserW")
        return {"pid": int(info.dwProcessId),
                "process": info.hProcess,
                "thread": info.hThread}


# ---------------------------------------------------------------------------
# policy / path canonicalization


def canonical_root(path, api=None):
    """Canonicalize one writable root and reject UNC/remote/reparse escapes."""
    if not isinstance(path, str) or not path.strip():
        raise PolicyError("missing_root")
    text = os.path.expandvars(os.path.expanduser(path.strip()))
    text = _strip_extended(text)
    if _is_unc(text):
        raise PolicyError("unc_rejected")
    if not os.path.isabs(text):
        raise PolicyError("not_absolute")
    if not os.path.exists(text):
        raise PolicyError("missing_root")
    lexical = os.path.normpath(text)
    real = os.path.realpath(lexical)
    long_path = real
    if api is not None:
        try:
            candidate = api.get_long_path_name(real)
            if candidate:
                long_path = candidate
        except SandboxError:
            raise
        except Exception:
            pass
        try:
            if int(api.get_drive_type(long_path)) == DRIVE_REMOTE:
                raise PolicyError("remote_rejected")
        except SandboxError:
            raise
        except Exception:
            pass
    real = _strip_extended(long_path)
    compare_real = os.path.normcase(real)
    parent = os.path.dirname(real) or real
    # Compare like with like: both sides are normcased for the containment
    # check, but the returned path stays case-preserving so records and
    # restore callbacks keep the caller's spelling.  The old comparison mixed
    # ``d:\\...`` with ``D:\\...`` and rejected every root as a reparse
    # escape; the old return value then leaked the lower-cased spelling.
    parent_real = os.path.normcase(os.path.realpath(parent))
    try:
        if os.path.commonpath([compare_real, parent_real]) != parent_real:
            raise PolicyError("reparse_escape")
    except ValueError:
        raise PolicyError("reparse_escape")
    try:
        attrs = int(getattr(os.stat(lexical, follow_symlinks=False),
                            "st_file_attributes", 0) or 0)
    except OSError:
        attrs = 0
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT and compare_real == os.path.normcase(
            _strip_extended(lexical)):
        raise PolicyError("reparse_unresolved")
    return real


def _forbidden_root(path, policy):
    folded = os.path.normcase(path)
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    if folded == home:
        return "home_root"
    dsh_root = os.path.normcase(os.path.realpath(
        os.path.join(home, ".dsh")))
    if folded == dsh_root and policy.get("dsh_home"):
        return "dsh_home_root"
    drive, tail = os.path.splitdrive(path)
    if tail in ("\\", "/"):
        return "filesystem_root"
    return None


def _grant_roots(policy):
    mode = str(policy.get("mode") or "workspace-write").strip().lower()
    roots = []
    if mode != "read-only":
        workspace = policy.get("workspace")
        if workspace:
            roots.append(workspace)
        tmpdir = policy.get("tmpdir")
        if tmpdir:
            roots.append(tmpdir)
        extra = policy.get("extra_writes") or ()
        if isinstance(extra, (list, tuple)):
            roots.extend([item for item in extra[:32]
                          if isinstance(item, str) and item.strip()])
        dsh_home = policy.get("dsh_home")
        if isinstance(dsh_home, str) and dsh_home.strip():
            for name in ("sessions", "profiles", "storages", "cache"):
                roots.append(os.path.join(dsh_home, name))
    else:
        tmpdir = policy.get("tmpdir")
        if tmpdir:
            roots.append(tmpdir)
        # A read-only reviewer still needs its private DSH_HOME (passed as an
        # extra write) for sessions/profiles; the workspace root itself is
        # never granted.
        extra = policy.get("extra_writes") or ()
        if isinstance(extra, (list, tuple)):
            roots.extend([item for item in extra[:32]
                          if isinstance(item, str) and item.strip()])
    return roots


def _icacls_grant(path, sid, api):
    try:
        sid_text = api.sid_to_string(sid)
    except Exception:
        return False
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    exe = os.path.join(root, "System32", "icacls.exe")
    try:
        proc = subprocess.run(
            [exe, str(path), "/grant", "*%s:(OI)(CI)M" % sid_text],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0)
        return getattr(proc, "returncode", 1) == 0
    except Exception:
        return False


def _restore_acl(rollback, api):
    failures = 0
    for entry in reversed(rollback or ()):
        if entry.get("method") == "icacls" or entry.get("dacl") is None:
            failures += 1
            continue
        try:
            api.set_named_security_info(entry["path"], entry["dacl"])
        except Exception:
            failures += 1
    return failures


def build_policy_acl(policy, api=None, sid=None):
    """Apply inheritable ACEs and return ``{granted, skipped, residue}``.

    The returned ``rollback`` list is launcher-internal: it holds the previous
    DACL for every root granted through the security APIs.
    """
    policy = policy or {}
    if api is None:
        if not _is_windows():
            return {"granted": [], "skipped": [], "residue": False,
                    "reason": "not_windows", "rollback": []}
        api = WindowsAPI()
    if sid is None:
        try:
            sid = api.make_per_run_sid()
        except Exception as exc:
            raise SandboxError(EXIT_APPLY_FAILED, _reason(exc),
                               step="make_per_run_sid", cause=exc)
    granted = []
    skipped = []
    rollback = []
    seen = set()
    for raw in _grant_roots(policy):
        try:
            path = canonical_root(raw, api)
        except SandboxError as exc:
            skipped.append({"path": str(raw), "reason": exc.reason})
            continue
        if path in seen:
            continue
        seen.add(path)
        forbidden = _forbidden_root(path, policy)
        if forbidden:
            skipped.append({"path": path, "reason": forbidden})
            continue
        if not os.path.isdir(path):
            skipped.append({"path": path, "reason": "not_a_directory"})
            continue
        state = None
        try:
            state = api.get_named_security_info(path)
            new_acl = api.set_entries_in_acl(state["dacl"], sid)
            api.set_named_security_info(path, new_acl)
            granted.append(path)
            rollback.append({"path": path, "dacl": state["dacl"],
                             "sd": state.get("sd")})
        except OSError as exc:
            if _access_denied(exc):
                if _icacls_grant(path, sid, api):
                    granted.append(path)
                    rollback.append({"path": path, "dacl": None,
                                     "method": "icacls"})
                    continue
            _restore_acl(rollback, api)
            raise SandboxError(EXIT_APPLY_FAILED,
                               "acl_denied" if _access_denied(exc)
                               else "acl_failed",
                               step="acl_apply", cause=exc)
        except Exception as exc:
            _restore_acl(rollback, api)
            raise SandboxError(EXIT_APPLY_FAILED, _reason(exc),
                               step="acl_apply", cause=exc)
    return {"granted": granted, "skipped": skipped,
            "residue": bool(granted), "rollback": rollback}


# ---------------------------------------------------------------------------
# token / probe / spawn / run


def _restricted_from_token(api, token):
    first = None
    first_step = None
    try:
        sid = api.make_per_run_sid()
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        first, first_step = exc, "make_per_run_sid"
    if first is None:
        try:
            restricted = api.create_restricted_token(token, sid)
            return restricted, sid, "per-run"
        except Exception as exc:  # noqa: BLE001 - fallback is intentional
            first, first_step = exc, "create_restricted_token"
    try:
        sid = api.token_logon_sid(token)
    except Exception as second:
        raise SandboxError(EXIT_UNAVAILABLE, _reason(first),
                           step=first_step, cause=first,
                           fallback_step="logon_sid",
                           fallback_cause=second)
    try:
        restricted = api.create_restricted_token(token, sid)
        return restricted, sid, "logon"
    except Exception as second:
        raise SandboxError(EXIT_UNAVAILABLE, _reason(second),
                           step="create_restricted_token", cause=second,
                           fallback_step=first_step,
                           fallback_cause=first)


def _acquire_restricted_token(api):
    try:
        token = api.open_process_token()
    except Exception as exc:
        raise SandboxError(EXIT_UNAVAILABLE, _reason(exc),
                           step="open_process_token", cause=exc)
    try:
        restricted, sid, kind = _restricted_from_token(api, token)
        return token, restricted, sid, kind
    except SandboxError:
        api.close_handle(token)
        raise
    except Exception as exc:
        api.close_handle(token)
        raise SandboxError(EXIT_UNAVAILABLE, _reason(exc),
                           step="acquire_token", cause=exc)


def _probe_admin(api, result):
    """Measure admin independently and before the write-restriction probe."""
    try:
        info_fn = getattr(api, "admin_info", None)
        if callable(info_fn):
            info = info_fn()
        else:
            # Compatibility for a simple test double that only has
            # is_admin(token): open its own token instead of reusing the
            # probe token, so a later write-restriction failure cannot change
            # the admin result.
            token = api.open_process_token()
            try:
                info = {"elevated": bool(api.is_admin(token)),
                        "member": None, "source": "token"}
            finally:
                api.close_handle(token)
    except Exception as exc:
        result["admin_error"] = _diag("admin", exc)
        result["admin_source"] = "unavailable"
        return
    result["admin_elevated"] = info.get("elevated")
    result["admin_member"] = info.get("member")
    result["admin_source"] = info.get("source") or "token"
    if info.get("elevation_error") is not None:
        result["admin_error"] = _diag("admin", info["elevation_error"])
    if info.get("member_error") is not None:
        result["admin_member_error"] = _diag("admin_membership",
                                             info["member_error"])
    elevated = info.get("elevated")
    # Legacy ``admin`` stays a token-elevation boolean.  Group membership is
    # exposed only as ``admin_member``; treating it as elevation would repeat
    # the 3.13.2 account-vs-token confusion.
    result["admin"] = bool(elevated) if elevated is not None else False


def probe_status(api=None):
    """Probe token support without spawning DSH. Never raises.

    Admin is measured first, through its own token.  A failure to create the
    write-restricted token is recorded under ``step``/``error`` but never
    overwrites ``admin`` or ``admin_elevated``.
    """
    result = {"ok": False, "reason": None, "admin": False,
              "admin_elevated": None, "admin_member": None,
              "admin_source": "unavailable", "admin_error": None,
              "restricting_sid": None, "write_restricted": False,
              "logon_sid": None, "step": None, "error": None}
    if api is None and not _is_windows():
        result.update(_diag("platform_check", None, reason="not_windows",
                            message="not Windows"))
        return result
    if api is None:
        try:
            api = WindowsAPI()
        except Exception as exc:
            result.update(_diag("init_api", exc))
            result["write_restricted"] = False
            return result

    _probe_admin(api, result)

    token = None
    try:
        try:
            token = api.open_process_token()
        except Exception as exc:
            result.update(_diag("open_process_token", exc))
            result["write_restricted"] = False
            return result

        # TokenLogonSid is the fallback SID source, not a success gate: a
        # per-run SID restricted token is usable even when it is absent.
        try:
            api.token_logon_sid(token)
            result["logon_sid"] = True
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            result["logon_sid"] = False
            result["logon_sid_error"] = _diag("logon_sid", exc)

        try:
            restricted, _sid, kind = _restricted_from_token(api, token)
        except SandboxError as exc:
            diag = _diag(exc.step, exc.cause, reason=exc.reason)
            if exc.fallback_step:
                diag["fallback_step"] = exc.fallback_step
                if exc.fallback_cause is not None:
                    diag["fallback_error"] = _error(exc.fallback_cause)
            result.update(diag)
            result["write_restricted"] = False
            return result

        result.update(ok=True, reason=None, restricting_sid=kind,
                      write_restricted=True, step=None, error=None)
        try:
            api.close_handle(restricted)
        except Exception as exc:  # noqa: BLE001 - success already proven
            result["close_error"] = _diag("close_handle", exc)
        return result
    except Exception as exc:  # defensive: probe_status never raises
        result.update(_diag("probe", exc))
        result["write_restricted"] = False
        return result
    finally:
        if token is not None:
            try:
                api.close_handle(token)
            except Exception:
                pass


def _flag(value):
    return "None" if value is None else ("True" if value else "False")


def format_probe_detail(status):
    """One-line detail for tests/sandbox_probe.py; legacy fields first."""
    status = status or {}
    parts = [
        "write_restricted=%s" % _flag(status.get("write_restricted")),
        "logon_sid=%s" % _flag(status.get("logon_sid")),
        "admin=%s" % _flag(status.get("admin")),
        "admin_member=%s" % _flag(status.get("admin_member")),
        "admin_source=%s" % (_one_line(status.get("admin_source"))
                             or "unavailable"),
    ]
    admin_error = status.get("admin_error") or {}
    if admin_error:
        err = admin_error.get("error") or {}
        parts.append("admin_step=%s" % (admin_error.get("step") or "admin"))
        parts.append("admin_error=%s: %s" % (
            err.get("type") or "LauncherError",
            err.get("message") or ""))
    member_error = status.get("admin_member_error") or {}
    if member_error:
        err = member_error.get("error") or {}
        parts.append("admin_member_step=%s"
                     % (member_error.get("step") or "admin_membership"))
        parts.append("admin_member_error=%s: %s" % (
            err.get("type") or "LauncherError",
            err.get("message") or ""))
    if status.get("step"):
        err = status.get("error") or {}
        parts.append("step=%s" % _one_line(status["step"]))
        parts.append("error=%s: %s" % (
            err.get("type") or "LauncherError",
            err.get("message") or ""))
    parts.append("reason=%s" % (_one_line(status.get("reason")) or "ok"))
    return _safe_text(";".join(parts), limit=480)


def _stdio_handles(api):
    handles = [api.get_osfhandle(fd) for fd in (0, 1, 2)]
    if len(handles) != 3 or any(handle is None or int(handle) <= 0
                                for handle in handles):
        raise SandboxError(EXIT_APPLY_FAILED, "stdio_handles_unavailable",
                           step="stdio_handles")
    for handle in handles:
        api.set_handle_information(handle, HANDLE_FLAG_INHERIT,
                                   HANDLE_FLAG_INHERIT)
    return handles


def _spawn(api, policy, dsh, token):
    if not dsh:
        raise SandboxError(EXIT_DSH_FAILED, "missing_dsh", step="spawn")
    application = os.path.realpath(os.path.abspath(os.path.expanduser(
        str(dsh[0]))))
    if not os.path.isfile(application):
        raise SandboxError(EXIT_DSH_FAILED, "dsh_not_found", step="spawn")
    try:
        application = api.get_long_path_name(application) or application
    except Exception:
        pass
    application = _strip_extended(application)
    command_line = subprocess.list2cmdline([application] + list(dsh[1:]))
    handles = _stdio_handles(api)
    cwd = os.path.realpath(os.path.abspath(str(
        policy.get("workspace") or os.getcwd())))
    try:
        return api.create_process(application, command_line, dict(os.environ),
                                  cwd, handles, token)
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxError(EXIT_DSH_FAILED, _reason(exc),
                           step="create_process", cause=exc)


def _timeout_ms(policy):
    raw = policy.get("timeout")
    if raw in (None, "", 0, "0"):
        return INFINITE
    try:
        return max(1, int(float(raw) * 1000))
    except (TypeError, ValueError):
        return INFINITE


def run(policy, dsh, api=None):
    """Apply the sandbox and run ``dsh``; returns the process exit code.

    ``api`` exists for the mocked unit suite; production passes ``None``.
    Every failure records and emits the failing step plus the exception class
    and a sanitized message.
    """
    global _LAST_RESULT
    result = {"code": None, "reason": None, "probe": None,
              "acl": {"granted": [], "skipped": [], "residue": False},
              "acl_residue": False, "restore_failures": 0,
              "restricting_sid": None, "step": None, "error": None}
    _stage("run")

    def fail(exit_code, diag):
        result.update(code=exit_code, reason=diag.get("reason"),
                      step=diag.get("step"), error=diag.get("error"))
        for key in ("winerror", "fallback_step"):
            if key in diag:
                result[key] = diag[key]
        extra = {}
        if diag.get("winerror") is not None:
            extra["winerror"] = diag["winerror"]
        _emit(exit_code, diag.get("reason") or "unknown",
              step=diag.get("step"), error=diag.get("error"), **extra)
        return exit_code

    if api is None:
        if not _is_windows():
            diag = _diag("platform_check", None, reason="not_windows",
                         message="not Windows")
            code = fail(EXIT_UNAVAILABLE, diag)
            _LAST_RESULT = result
            return code
        _stage("init_api")
        try:
            api = WindowsAPI()
        except Exception as exc:
            diag = _diag("init_api", exc)
            code = fail(EXIT_UNAVAILABLE, diag)
            _LAST_RESULT = result
            return code

    probe = probe_status(api=api)
    result["probe"] = probe
    if not probe.get("ok"):
        diag = {"step": probe.get("step") or "probe",
                "reason": probe.get("reason") or "sandbox_unavailable",
                "error": probe.get("error") or {
                    "type": "LauncherError",
                    "message": "probe returned ok=False"}}
        code = fail(EXIT_UNAVAILABLE, diag)
        _LAST_RESULT = result
        return code

    token = None
    restricted = None
    job = None
    proc = None
    acl = {"granted": [], "skipped": [], "residue": False, "rollback": []}
    try:
        _stage("acquire_token")
        token, restricted, sid, kind = _acquire_restricted_token(api)
        result["restricting_sid"] = kind

        _stage("acl")
        acl = build_policy_acl(policy, api=api, sid=sid)
        result["acl"] = {"granted": list(acl.get("granted") or []),
                         "skipped": list(acl.get("skipped") or []),
                         "residue": bool(acl.get("residue"))}

        _stage("spawn")
        proc = _spawn(api, policy, dsh, restricted)

        _stage("job_create")
        try:
            job = api.create_job_object(JOB_FLAGS)
        except Exception:
            try:
                api.terminate_process(proc["process"], 1)
            except Exception:
                pass
            raise

        _stage("job_assign")
        if not api.assign_process_to_job(job, proc["process"]):
            try:
                api.terminate_process(proc["process"], 1)
            except Exception:
                pass
            diag = _diag("job_assign", None, reason="job_assignment_failed",
                         message="AssignProcessToJobObject returned false")
            return fail(EXIT_APPLY_FAILED, diag)

        _stage("resume")
        resumed = api.resume_thread(proc["thread"])
        if resumed is False or resumed == 0xFFFFFFFF:
            try:
                api.terminate_job(job, 1)
            except Exception:
                pass
            diag = _diag("resume", None, reason="resume_failed",
                         message="ResumeThread returned failure")
            return fail(EXIT_APPLY_FAILED, diag)

        _stage("wait")
        wait = api.wait_for_single_object(proc["process"],
                                          _timeout_ms(policy))
        if wait == WAIT_TIMEOUT:
            try:
                api.terminate_job(job, 1)
            except Exception:
                pass
            diag = _diag("wait", None, reason="timeout",
                         message="WaitForSingleObject timed out")
            return fail(EXIT_TIMEOUT, diag)
        if wait == WAIT_FAILED:
            try:
                api.terminate_job(job, 1)
            except Exception:
                pass
            diag = _diag("wait", None, reason="wait_failed",
                         message="WaitForSingleObject returned WAIT_FAILED")
            return fail(EXIT_APPLY_FAILED, diag)

        _stage("get_exit_code")
        code = int(api.get_exit_code(proc["process"]))
        result.update(code=code, reason=None)
        return code
    except SandboxError as exc:
        diag = _diag(exc.step, exc.cause, reason=exc.reason)
        if exc.fallback_step:
            diag["fallback_step"] = exc.fallback_step
            if exc.fallback_cause is not None:
                diag["fallback_error"] = _error(exc.fallback_cause)
        return fail(exc.exit_code, diag)
    except Exception as exc:
        diag = _diag(_STAGE["name"], exc)
        return fail(EXIT_APPLY_FAILED, diag)
    finally:
        result["restore_failures"] = _restore_acl(
            acl.get("rollback") or [], api)
        result["acl_residue"] = bool(acl.get("granted"))
        _LAST_RESULT = result
        handles = []
        if proc:
            handles.extend([proc.get("thread"), proc.get("process")])
        handles.extend([restricted, token])
        for handle in handles:
            if handle:
                try:
                    api.close_handle(handle)
                except Exception:
                    pass
        if job:
            try:
                api.close_handle(job)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI


def _parse_invocation(argv):
    if "--policy" not in argv:
        raise SandboxError(EXIT_APPLY_FAILED, "missing_policy",
                           step="parse_invocation")
    index = argv.index("--policy")
    if index + 1 >= len(argv):
        raise SandboxError(EXIT_APPLY_FAILED, "missing_policy",
                           step="parse_invocation")
    raw = argv[index + 1]
    rest = argv[index + 2:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        raise SandboxError(EXIT_DSH_FAILED, "missing_dsh",
                           step="parse_invocation")
    try:
        policy = json.loads(raw)
    except Exception as exc:
        raise SandboxError(EXIT_APPLY_FAILED, "invalid_policy",
                           step="parse_invocation", cause=exc)
    if not isinstance(policy, dict):
        raise SandboxError(EXIT_APPLY_FAILED, "invalid_policy",
                           step="parse_invocation")
    return policy, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        policy, dsh = _parse_invocation(argv)
    except SandboxError as exc:
        diag = _diag(exc.step, exc.cause, reason=exc.reason)
        _emit(exc.exit_code, exc.reason, step=diag["step"],
              error=diag["error"])
        return exc.exit_code
    if not _is_windows():
        diag = _diag("platform_check", None, reason="not_windows",
                     message="not Windows")
        _emit(EXIT_UNAVAILABLE, diag["reason"], step=diag["step"],
              error=diag["error"])
        return EXIT_UNAVAILABLE
    return run(policy, dsh)


if __name__ == "__main__":
    sys.exit(main())
