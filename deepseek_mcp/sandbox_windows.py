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
import subprocess
import sys

EXIT_UNAVAILABLE = 125
EXIT_APPLY_FAILED = 126
EXIT_DSH_FAILED = 127
EXIT_TIMEOUT = 124

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
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


# ---------------------------------------------------------------------------
# errors / small helpers


class SandboxError(Exception):
    """Internal failure carrying the launcher exit code and a safe reason."""

    def __init__(self, exit_code, reason):
        super().__init__(reason)
        self.exit_code = int(exit_code)
        self.reason = str(reason)


class PolicyError(SandboxError):
    def __init__(self, reason):
        super().__init__(EXIT_APPLY_FAILED, reason)


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
    try:
        err = ctypes.get_last_error() or int(rc)
    except Exception:
        err = int(rc)
    exc = OSError(int(rc), "%s failed" % name)
    exc.winerror = err
    return exc


def _access_denied(exc):
    winerror = getattr(exc, "winerror", None)
    if winerror == 5:
        return True
    if getattr(exc, "errno", None) == 13:
        return True
    return "access is denied" in str(exc).lower()


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
    return exc.__class__.__name__.lower() or "windows_api_error"


def _emit(code, reason, **extra):
    payload = {"code": int(code), "reason": str(reason)}
    payload.update(extra)
    try:
        sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def last_result():
    """Return the most recent run metadata (tests/run-record integration)."""
    return dict(_LAST_RESULT)


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
        k.GetCurrentProcess.restype = C.c_void_p
        k.CloseHandle.argtypes = [C.c_void_p]
        k.CloseHandle.restype = C.c_int
        k.OpenProcessToken.argtypes = [C.c_void_p, C.c_uint32,
                                       C.POINTER(C.c_void_p)]
        k.OpenProcessToken.restype = C.c_int
        k.GetTokenInformation.argtypes = [C.c_void_p, C.c_int, C.c_void_p,
                                          C.c_uint32,
                                          C.POINTER(C.c_uint32)]
        k.GetTokenInformation.restype = C.c_int
        k.GetLongPathNameW.argtypes = [C.c_wchar_p, C.c_wchar_p, C.c_uint32]
        k.GetLongPathNameW.restype = C.c_uint32
        k.GetDriveTypeW.argtypes = [C.c_wchar_p]
        k.GetDriveTypeW.restype = C.c_uint32
        k.SetHandleInformation.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32]
        k.SetHandleInformation.restype = C.c_int
        k.CreateJobObjectW.argtypes = [C.c_void_p, C.c_wchar_p]
        k.CreateJobObjectW.restype = C.c_void_p
        k.SetInformationJobObject.argtypes = [C.c_void_p, C.c_int, C.c_void_p,
                                              C.c_uint32]
        k.SetInformationJobObject.restype = C.c_int
        k.AssignProcessToJobObject.argtypes = [C.c_void_p, C.c_void_p]
        k.AssignProcessToJobObject.restype = C.c_int
        k.ResumeThread.argtypes = [C.c_void_p]
        k.ResumeThread.restype = C.c_uint32
        k.TerminateProcess.argtypes = [C.c_void_p, C.c_uint32]
        k.TerminateProcess.restype = C.c_int
        k.TerminateJobObject.argtypes = [C.c_void_p, C.c_uint32]
        k.TerminateJobObject.restype = C.c_int
        k.WaitForSingleObject.argtypes = [C.c_void_p, C.c_uint32]
        k.WaitForSingleObject.restype = C.c_uint32
        k.GetExitCodeProcess.argtypes = [C.c_void_p,
                                         C.POINTER(C.c_uint32)]
        k.GetExitCodeProcess.restype = C.c_int
        k.InitializeProcThreadAttributeList.argtypes = [
            C.c_void_p, C.c_uint32, C.c_uint32,
            C.POINTER(C.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = C.c_int
        k.UpdateProcThreadAttribute.argtypes = [
            C.c_void_p, C.c_uint32, C.c_size_t, C.c_void_p, C.c_size_t,
            C.c_void_p, C.c_void_p]
        k.UpdateProcThreadAttribute.restype = C.c_int
        k.CreateProcessAsUserW.argtypes = [
            C.c_void_p, C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p,
            C.c_int, C.c_uint32, C.c_void_p, C.c_wchar_p,
            C.POINTER(_STARTUPINFOEX), C.POINTER(_PROCESS_INFORMATION)]
        k.CreateProcessAsUserW.restype = C.c_int
        a.ConvertStringSidToSidW.argtypes = [C.c_wchar_p,
                                             C.POINTER(C.c_void_p)]
        a.ConvertStringSidToSidW.restype = C.c_int
        a.ConvertSidToStringSidW.argtypes = [C.c_void_p,
                                             C.POINTER(C.c_wchar_p)]
        a.ConvertSidToStringSidW.restype = C.c_int
        a.CreateRestrictedToken.argtypes = [
            C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32,
            C.c_void_p, C.c_uint32,
            C.POINTER(_SID_AND_ATTRIBUTES), C.POINTER(C.c_void_p)]
        a.CreateRestrictedToken.restype = C.c_int
        a.GetLengthSid.argtypes = [C.c_void_p]
        a.GetLengthSid.restype = C.c_uint32
        a.CopySid.argtypes = [C.c_uint32, C.c_void_p, C.c_void_p]
        a.CopySid.restype = C.c_int
        a.GetNamedSecurityInfoW.argtypes = [
            C.c_wchar_p, C.c_int, C.c_uint32, C.POINTER(C.c_void_p),
            C.POINTER(C.c_void_p), C.POINTER(C.c_void_p),
            C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)]
        a.GetNamedSecurityInfoW.restype = C.c_uint32
        a.SetEntriesInAclW.argtypes = [
            C.c_uint32, C.POINTER(_EXPLICIT_ACCESS_W), C.c_void_p,
            C.POINTER(C.c_void_p)]
        a.SetEntriesInAclW.restype = C.c_uint32
        a.SetNamedSecurityInfoW.argtypes = [
            C.c_wchar_p, C.c_int, C.c_uint32, C.c_void_p, C.c_void_p,
            C.c_void_p, C.c_void_p]
        a.SetNamedSecurityInfoW.restype = C.c_uint32
        a.LocalFree.argtypes = [C.c_void_p]
        a.LocalFree.restype = C.c_void_p

    # -- token ---------------------------------------------------------
    def open_process_token(self):
        token = ctypes.c_void_p()
        flags = TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY
        if not self.kernel32.OpenProcessToken(
                self.kernel32.GetCurrentProcess(), flags,
                ctypes.byref(token)):
            raise _win_error(0, "OpenProcessToken")
        return token

    def close_handle(self, handle):
        if handle:
            try:
                self.kernel32.CloseHandle(handle)
            except Exception:
                pass

    def is_admin(self, token):
        value = _TOKEN_ELEVATION()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        if not self.kernel32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(value), size.value,
                ctypes.byref(size)):
            raise _win_error(0, "GetTokenInformation(TokenElevation)")
        return bool(value.TokenIsElevated)

    def make_per_run_sid(self):
        import secrets
        text = "S-1-5-21-%d-%d-%d" % (
            secrets.randbits(31), secrets.randbits(31), secrets.randbits(31))
        sid = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSidToSidW(
                text, ctypes.byref(sid)):
            raise _win_error(0, "ConvertStringSidToSidW")
        return sid

    def token_logon_sid(self, token):
        size = ctypes.c_uint32(0)
        self.kernel32.GetTokenInformation(
            token, TOKEN_LOGON_SID, None, 0, ctypes.byref(size))
        if not size.value:
            raise _win_error(0, "GetTokenInformation(TokenLogonSid)")
        buf = ctypes.create_string_buffer(size.value)
        if not self.kernel32.GetTokenInformation(
                token, TOKEN_LOGON_SID, buf, size.value,
                ctypes.byref(size)):
            raise _win_error(0, "GetTokenInformation(TokenLogonSid)")
        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value
        if not count:
            raise OSError("TokenLogonSid is empty")
        align = ctypes.alignment(_SID_AND_ATTRIBUTES)
        offset = ctypes.sizeof(ctypes.c_uint32)
        offset = (offset + align - 1) // align * align
        first = ctypes.cast(ctypes.byref(buf, offset),
                            ctypes.POINTER(_SID_AND_ATTRIBUTES))[0]
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
            self.advapi32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

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
            raise SandboxError(EXIT_APPLY_FAILED, "handle_list_not_three")
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
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = ctypes.c_void_p(int(handles[0]))
        startup.StartupInfo.hStdOutput = ctypes.c_void_p(int(handles[1]))
        startup.StartupInfo.hStdError = ctypes.c_void_p(int(handles[2]))
        startup.lpAttributeList = attrs
        items = ["%s=%s" % (key, value)
                 for key, value in environment.items()
                 if key and "=" not in key]
        block = ctypes.create_unicode_buffer("\0".join(items) + "\0")
        cmd = ctypes.create_unicode_buffer(str(command_line))
        info = _PROCESS_INFORMATION()
        flags = (CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
                 | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT)
        ok = self.kernel32.CreateProcessAsUserW(
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
    real = os.path.normcase(_strip_extended(long_path))
    parent = os.path.dirname(lexical) or lexical
    parent_real = os.path.realpath(parent)
    try:
        if os.path.commonpath([real, parent_real]) != parent_real:
            raise PolicyError("reparse_escape")
    except ValueError:
        raise PolicyError("reparse_escape")
    try:
        attrs = int(getattr(os.stat(lexical, follow_symlinks=False),
                            "st_file_attributes", 0) or 0)
    except OSError:
        attrs = 0
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT and real == os.path.normcase(
            _strip_extended(lexical)):
        raise PolicyError("reparse_unresolved")
    return real


def _forbidden_root(path, policy):
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    if path == home:
        return "home_root"
    dsh_root = os.path.normcase(os.path.realpath(
        os.path.join(home, ".dsh")))
    if path == dsh_root and policy.get("dsh_home"):
        return "dsh_home_root"
    drive, tail = os.path.splitdrive(path)
    if tail in ("\\", "/") and not drive:
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
            raise SandboxError(EXIT_APPLY_FAILED, _reason(exc))
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
                               else "acl_failed")
        except Exception:
            _restore_acl(rollback, api)
            raise
    return {"granted": granted, "skipped": skipped,
            "residue": bool(granted), "rollback": rollback}


# ---------------------------------------------------------------------------
# token / probe / spawn / run


def _restricted_from_token(api, token):
    try:
        sid = api.make_per_run_sid()
        restricted = api.create_restricted_token(token, sid)
        return restricted, sid, "per-run"
    except Exception:
        sid = api.token_logon_sid(token)
        restricted = api.create_restricted_token(token, sid)
        return restricted, sid, "logon"


def _acquire_restricted_token(api):
    token = api.open_process_token()
    try:
        restricted, sid, kind = _restricted_from_token(api, token)
        return token, restricted, sid, kind
    except Exception as exc:
        api.close_handle(token)
        raise SandboxError(EXIT_UNAVAILABLE, _reason(exc))


def probe_status(api=None):
    """Probe token support without spawning DSH. Never raises."""
    result = {"ok": False, "reason": None, "admin": False,
              "restricting_sid": None, "write_restricted": False}
    if api is None and not _is_windows():
        result["reason"] = "not_windows"
        return result
    try:
        api = api or WindowsAPI()
        token = api.open_process_token()
        try:
            try:
                result["admin"] = bool(api.is_admin(token))
            except Exception:
                result["admin"] = False
            try:
                api.token_logon_sid(token)
                result["logon_sid"] = True
            except Exception:
                result["logon_sid"] = False
            restricted, _sid, kind = _restricted_from_token(api, token)
            api.close_handle(restricted)
            result.update(ok=bool(result["logon_sid"]), reason=None,
                          restricting_sid=kind, write_restricted=True)
            if not result["ok"]:
                result["reason"] = "logon_sid_unavailable"
        finally:
            api.close_handle(token)
    except Exception as exc:
        result["reason"] = _reason(exc)
        result["write_restricted"] = False
    return result


def _stdio_handles(api):
    handles = [api.get_osfhandle(fd) for fd in (0, 1, 2)]
    if len(handles) != 3 or any(not handle for handle in handles):
        raise SandboxError(EXIT_APPLY_FAILED, "stdio_handles_unavailable")
    for handle in handles:
        api.set_handle_information(handle, HANDLE_FLAG_INHERIT,
                                   HANDLE_FLAG_INHERIT)
    return handles


def _spawn(api, policy, dsh, token):
    if not dsh:
        raise SandboxError(EXIT_DSH_FAILED, "missing_dsh")
    application = os.path.realpath(os.path.abspath(os.path.expanduser(
        str(dsh[0]))))
    if not os.path.isfile(application):
        raise SandboxError(EXIT_DSH_FAILED, "dsh_not_found")
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
        raise SandboxError(EXIT_DSH_FAILED, _reason(exc))


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
    """
    global _LAST_RESULT
    result = {"code": None, "reason": None, "probe": None,
              "acl": {"granted": [], "skipped": [], "residue": False},
              "acl_residue": False, "restore_failures": 0,
              "restricting_sid": None}
    if api is None:
        if not _is_windows():
            result.update(code=EXIT_UNAVAILABLE, reason="not_windows")
            _LAST_RESULT = result
            _emit(EXIT_UNAVAILABLE, "not_windows")
            return EXIT_UNAVAILABLE
        try:
            api = WindowsAPI()
        except Exception as exc:
            result.update(code=EXIT_UNAVAILABLE, reason=_reason(exc))
            _LAST_RESULT = result
            _emit(EXIT_UNAVAILABLE, _reason(exc))
            return EXIT_UNAVAILABLE
    probe = probe_status(api=api)
    result["probe"] = probe
    if not probe.get("ok"):
        result.update(code=EXIT_UNAVAILABLE,
                      reason=probe.get("reason") or "sandbox_unavailable")
        _LAST_RESULT = result
        _emit(EXIT_UNAVAILABLE, result["reason"])
        return EXIT_UNAVAILABLE

    token = None
    restricted = None
    job = None
    acl = {"granted": [], "skipped": [], "residue": False, "rollback": []}
    try:
        token, restricted, sid, kind = _acquire_restricted_token(api)
        result["restricting_sid"] = kind
        acl = build_policy_acl(policy, api=api, sid=sid)
        result["acl"] = {"granted": list(acl.get("granted") or []),
                         "skipped": list(acl.get("skipped") or []),
                         "residue": bool(acl.get("residue"))}
        proc = _spawn(api, policy, dsh, restricted)
        job = api.create_job_object(JOB_FLAGS)
        if not api.assign_process_to_job(job, proc["process"]):
            api.terminate_process(proc["process"], 1)
            result.update(code=EXIT_APPLY_FAILED,
                          reason="job_assignment_failed")
            _emit(EXIT_APPLY_FAILED, "job_assignment_failed")
            return EXIT_APPLY_FAILED
        api.resume_thread(proc["thread"])
        wait = api.wait_for_single_object(proc["process"],
                                          _timeout_ms(policy))
        if wait == WAIT_TIMEOUT:
            api.terminate_job(job, 1)
            result.update(code=EXIT_TIMEOUT, reason="timeout")
            _emit(EXIT_TIMEOUT, "timeout")
            return EXIT_TIMEOUT
        if wait == WAIT_FAILED:
            api.terminate_job(job, 1)
            result.update(code=EXIT_APPLY_FAILED, reason="wait_failed")
            _emit(EXIT_APPLY_FAILED, "wait_failed")
            return EXIT_APPLY_FAILED
        code = int(api.get_exit_code(proc["process"]))
        result.update(code=code, reason=None)
        return code
    except SandboxError as exc:
        result.update(code=exc.exit_code, reason=exc.reason)
        _emit(exc.exit_code, exc.reason)
        return exc.exit_code
    except Exception as exc:
        result.update(code=EXIT_APPLY_FAILED, reason=_reason(exc))
        _emit(EXIT_APPLY_FAILED, _reason(exc))
        return EXIT_APPLY_FAILED
    finally:
        result["restore_failures"] = _restore_acl(
            acl.get("rollback") or [], api)
        result["acl_residue"] = bool(acl.get("granted"))
        _LAST_RESULT = result
        for handle in (restricted, token):
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
        raise SandboxError(EXIT_APPLY_FAILED, "missing_policy")
    index = argv.index("--policy")
    if index + 1 >= len(argv):
        raise SandboxError(EXIT_APPLY_FAILED, "missing_policy")
    raw = argv[index + 1]
    rest = argv[index + 2:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        raise SandboxError(EXIT_DSH_FAILED, "missing_dsh")
    try:
        policy = json.loads(raw)
    except Exception:
        raise SandboxError(EXIT_APPLY_FAILED, "invalid_policy")
    if not isinstance(policy, dict):
        raise SandboxError(EXIT_APPLY_FAILED, "invalid_policy")
    return policy, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        policy, dsh = _parse_invocation(argv)
    except SandboxError as exc:
        _emit(exc.exit_code, exc.reason)
        return exc.exit_code
    if not _is_windows():
        _emit(EXIT_UNAVAILABLE, "not_windows")
        return EXIT_UNAVAILABLE
    return run(policy, dsh)


if __name__ == "__main__":
    sys.exit(main())
