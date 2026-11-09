"""Host-independent ctypes-binding tests for the Windows sandbox launcher.

Runs on macOS/Linux and loads no Windows DLL.  ``ctypes.WinDLL`` is replaced
by :class:`FakeWinDLL`, which has a curated per-DLL export table.  A symbol
bound to the wrong DLL therefore raises the same ``AttributeError`` that
``GetProcAddress`` produces on a real Windows host.

Regression for GitHub run 42629362611: the first real ``windows-2025`` run
failed because ``WindowsAPI._configure`` bound ``OpenProcessToken``,
``GetTokenInformation`` and ``CreateProcessAsUserW`` on ``kernel32`` and
``LocalFree`` on ``advapi32``.  The probe collapsed that to
``reason="attributeerror"``.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import io
import json
import os
import sys
import unittest
from unittest import mock

from deepseek_mcp import sandbox_windows as sw


# ---------------------------------------------------------------------------
# The oracle: who really exports what (Windows x64 kernel32/advapi32).

KERNEL32_EXPORTS = frozenset({
    "GetCurrentProcess", "CloseHandle", "GetLongPathNameW", "GetDriveTypeW",
    "SetHandleInformation", "CreateJobObjectW", "SetInformationJobObject",
    "AssignProcessToJobObject", "ResumeThread", "TerminateProcess",
    "TerminateJobObject", "WaitForSingleObject", "GetExitCodeProcess",
    "InitializeProcThreadAttributeList", "UpdateProcThreadAttribute",
    "LocalFree", "OpenProcess", "SetThreadExecutionState",
})

ADVAPI32_EXPORTS = frozenset({
    "OpenProcessToken", "GetTokenInformation", "CreateRestrictedToken",
    "ConvertStringSidToSidW", "ConvertSidToStringSidW", "GetLengthSid",
    "CopySid", "GetNamedSecurityInfoW", "SetEntriesInAclW",
    "SetNamedSecurityInfoW", "CreateProcessAsUserW", "CheckTokenMembership",
})

SHELL32_EXPORTS = frozenset({"IsUserAnAdmin"})

REAL_EXPORTS = {
    "kernel32": KERNEL32_EXPORTS,
    "advapi32": ADVAPI32_EXPORTS,
    "shell32": SHELL32_EXPORTS,
}


def _real_owner(name):
    for dll_name, exports in REAL_EXPORTS.items():
        if name in exports:
            return dll_name
    return None


class FakeFunction:
    def __init__(self, dll, name):
        self.dll = dll
        self.name = name
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "fake Win32 function %s.%s was invoked; these tests exercise "
            "binding only" % (self.dll, self.name))

    def __repr__(self):
        return "<FakeFunction %s.%s>" % (self.dll, self.name)


class FakeWinDLL:
    def __init__(self, name, use_last_error=False):
        key = str(name).lower()
        if key.endswith(".dll"):
            key = key[:-4]
        if key not in REAL_EXPORTS:
            raise OSError("no fake export table for WinDLL(%r)" % (name,))
        self.name = key
        self.use_last_error = bool(use_last_error)
        self.lookups = []
        self._cache = {}

    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        self.lookups.append(attr)
        if attr not in REAL_EXPORTS[self.name]:
            owner = _real_owner(attr)
            where = ("it is exported by %s.dll" % owner if owner
                     else "it is not in the curated export table")
            raise AttributeError(
                "function %r not found in %s.dll (%s)"
                % (attr, self.name, where))
        if attr not in self._cache:
            self._cache[attr] = FakeFunction(self.name, attr)
        return self._cache[attr]


def _fake_windll_factory(created):
    def factory(name, use_last_error=False):
        dll = FakeWinDLL(name, use_last_error=use_last_error)
        created.append(dll)
        return dll
    return factory


class ConfigureBindingTests(unittest.TestCase):
    def _instantiate(self):
        created = []
        factory = _fake_windll_factory(created)
        with mock.patch.object(sw, "_is_windows", return_value=True), \
                mock.patch.object(sw.ctypes, "WinDLL", factory, create=True):
            try:
                api = sw.WindowsAPI()
            except AttributeError as exc:
                lookups = []
                for dll in created:
                    for attr in dll.lookups:
                        lookups.append("%s.%s (owner %s)"
                                       % (dll.name, attr,
                                          _real_owner(attr) or "unknown"))
                self.fail("WindowsAPI() raised AttributeError against the "
                          "curated export table: %s\n%s"
                          % (exc, "\n".join(lookups)))
        return api, created

    def test_every_symbol_is_bound_to_its_real_dll(self):
        api, created = self._instantiate()
        self.assertTrue(created)
        wrong = []
        for dll in created:
            for attr in dll.lookups:
                owner = _real_owner(attr)
                if owner != dll.name:
                    wrong.append("%s.%s (owner %s)"
                                 % (dll.name, attr, owner or "unknown"))
        self.assertFalse(wrong, "wrong-DLL lookups:\n  %s"
                         % "\n  ".join(wrong))

    def test_token_and_create_process_are_advapi32(self):
        api, created = self._instantiate()
        by_name = {dll.name: dll for dll in created}
        for name in ("OpenProcessToken", "GetTokenInformation",
                     "CreateProcessAsUserW", "CheckTokenMembership"):
            self.assertIn(name, by_name["advapi32"].lookups)
            self.assertNotIn(name, by_name["kernel32"].lookups)

    def test_local_free_is_kernel32(self):
        api, created = self._instantiate()
        by_name = {dll.name: dll for dll in created}
        self.assertIn("LocalFree", by_name["kernel32"].lookups)
        self.assertNotIn("LocalFree", by_name["advapi32"].lookups)

    def test_configured_functions_have_signatures(self):
        api, _created = self._instantiate()
        self.assertIsNotNone(api.kernel32.GetCurrentProcess.restype)
        self.assertIsNotNone(api.kernel32.LocalFree.argtypes)
        self.assertIsNotNone(api.advapi32.OpenProcessToken.argtypes)
        self.assertIsNotNone(api.advapi32.CreateProcessAsUserW.argtypes)
        self.assertIsNotNone(api.advapi32.CheckTokenMembership.argtypes)

    def test_methods_do_not_reintroduce_the_wrong_dll_calls(self):
        """The 3.13.2 failure was a method call after a wrong _configure."""
        with open(sw.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        for wrong in ("self.kernel32.OpenProcessToken",
                      "self.kernel32.GetTokenInformation",
                      "self.kernel32.CreateProcessAsUserW",
                      "self.advapi32.LocalFree"):
            self.assertNotIn(wrong, source)
        for right in ("self.advapi32.OpenProcessToken",
                      "self.advapi32.GetTokenInformation",
                      "self.advapi32.CreateProcessAsUserW",
                      "self.kernel32.LocalFree"):
            self.assertIn(right, source)


# ---------------------------------------------------------------------------
# Static audit: every ctypes/wintypes name used by the Windows path.


CTYPES_WINDOWS_ONLY = frozenset({
    "WinDLL", "windll", "get_last_error", "WinError",
})


def _dotted_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _windows_sources():
    package = os.path.dirname(os.path.abspath(sw.__file__))
    return [
        os.path.abspath(sw.__file__),
        os.path.join(package, "platforms.py"),
        os.path.join(package, "mac.py"),
    ]


class CtypesSourceAuditTests(unittest.TestCase):
    def test_wintypes_names_exist_in_cpython(self):
        missing = []
        checked = 0
        for path in _windows_sources():
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                dotted = _dotted_name(node)
                if not dotted:
                    continue
                if dotted.startswith("ctypes.wintypes."):
                    name = dotted.rsplit(".", 1)[1]
                elif dotted.startswith("wintypes.") and dotted.count(".") == 1:
                    name = dotted.split(".", 1)[1]
                else:
                    continue
                checked += 1
                if name not in wintypes.__dict__:
                    missing.append("%s: wintypes.%s"
                                   % (os.path.basename(path), name))
        self.assertGreater(checked, 0, "no wintypes.* references found")
        self.assertFalse(missing, "missing ctypes.wintypes names: %s"
                         % ", ".join(missing))

    def test_ctypes_attributes_are_real_or_windows_only(self):
        violations = []
        checked = 0
        for path in _windows_sources():
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                dotted = _dotted_name(node)
                if not dotted or not dotted.startswith("ctypes."):
                    continue
                parts = dotted.split(".")
                if len(parts) != 2:
                    continue
                attr = parts[1]
                if attr == "wintypes" or attr in CTYPES_WINDOWS_ONLY:
                    continue
                checked += 1
                if not hasattr(ctypes, attr):
                    violations.append("%s: ctypes.%s"
                                      % (os.path.basename(path), attr))
        self.assertGreater(checked, 0, "no ctypes.* references found")
        self.assertFalse(violations, "absent ctypes names: %s"
                         % ", ".join(violations))


# ---------------------------------------------------------------------------
# probe: structured error, admin independent of the token probe


class ProbeAttributeErrorAPI:
    def __init__(self):
        self.calls = []

    def admin_info(self):
        self.calls.append("admin_info")
        return {"elevated": True, "member": False, "source": "token",
                "elevation_error": None, "member_error": None}

    def open_process_token(self):
        self.calls.append("open_process_token")
        raise AttributeError(
            "function 'OpenProcessToken' not found in kernel32.dll "
            "(it is exported by advapi32.dll)")

    def close_handle(self, handle):
        self.calls.append("close_handle")


class ProbeDiagnosticsTests(unittest.TestCase):
    def test_attribute_error_is_structured_and_admin_is_independent(self):
        status = sw.probe_status(api=ProbeAttributeErrorAPI())
        self.assertFalse(status["ok"])
        self.assertTrue(status["admin"],
                        "admin must survive a write-restricted probe failure")
        self.assertTrue(status["admin_elevated"])
        self.assertFalse(status["admin_member"])
        self.assertEqual(status["admin_source"], "token")
        self.assertEqual(status["step"], "open_process_token")
        self.assertEqual(status["error"]["type"], "AttributeError")
        self.assertIn("OpenProcessToken", status["error"]["message"])
        self.assertEqual(status["reason"], "attributeerror")

    def test_admin_failure_is_also_structured(self):
        class NoAdminAPI(ProbeAttributeErrorAPI):
            def admin_info(self):
                raise AttributeError("CheckTokenMembership missing")
        status = sw.probe_status(api=NoAdminAPI())
        self.assertEqual(status["admin_source"], "unavailable")
        self.assertEqual(status["admin_error"]["step"], "admin")
        self.assertEqual(status["admin_error"]["error"]["type"],
                         "AttributeError")
        self.assertEqual(status["step"], "open_process_token")


class RestrictedTokenFailureAPI:
    def __init__(self):
        self.closed = []

    def admin_info(self):
        return {"elevated": True, "member": True, "source": "token"}

    def open_process_token(self):
        return 101

    def close_handle(self, handle):
        self.closed.append(handle)

    def token_logon_sid(self, token):
        return "S-1-5-5-0-1"

    def make_per_run_sid(self):
        return "S-1-5-21-1-2-3"

    def create_restricted_token(self, token, sid):
        raise AttributeError(
            "function 'CreateRestrictedToken' not found in advapi32.dll")


class ProbeRestrictedFailureTests(unittest.TestCase):
    def test_restricted_token_failure_keeps_admin_true(self):
        status = sw.probe_status(api=RestrictedTokenFailureAPI())
        self.assertFalse(status["ok"])
        self.assertTrue(status["admin"])
        self.assertEqual(status["step"], "create_restricted_token")
        self.assertEqual(status["error"]["type"], "AttributeError")
        self.assertIn("CreateRestrictedToken", status["error"]["message"])

    def test_missing_logon_sid_does_not_fail_a_working_token(self):
        class NoLogonSid(RestrictedTokenFailureAPI):
            def token_logon_sid(self, token):
                raise OSError("TokenLogonSid is empty")

            def create_restricted_token(self, token, sid):
                return 202
        status = sw.probe_status(api=NoLogonSid())
        self.assertTrue(status["ok"])
        self.assertTrue(status["write_restricted"])
        self.assertFalse(status["logon_sid"])
        self.assertIsNone(status["reason"])
        self.assertIn("logon_sid_error", status)


class ProbeDetailTests(unittest.TestCase):
    def test_detail_has_class_message_and_step(self):
        detail = sw.format_probe_detail({
            "write_restricted": False, "logon_sid": None, "admin": True,
            "admin_member": False, "admin_source": "token",
            "step": "open_process_token",
            "error": {"type": "AttributeError",
                      "message": "function 'OpenProcessToken' not found"},
            "reason": "attributeerror"})
        self.assertIn("step=open_process_token", detail)
        self.assertIn("error=AttributeError:", detail)
        self.assertIn("OpenProcessToken", detail)
        self.assertIn("reason=attributeerror", detail)
        self.assertNotIn("\n", detail)


# ---------------------------------------------------------------------------
# launcher: the same class/message/step contract in result and stderr


class LauncherAPI:
    def __init__(self):
        self.events = []

    def admin_info(self):
        return {"elevated": True, "member": True, "source": "token"}

    def open_process_token(self):
        self.events.append("open_process_token")
        return 101

    def close_handle(self, handle):
        self.events.append(("close_handle", handle))

    def token_logon_sid(self, token):
        return "S-1-5-5-0-1"

    def make_per_run_sid(self):
        return "S-1-5-21-1-2-3"

    def create_restricted_token(self, token, sid):
        return 202

    def get_long_path_name(self, path):
        return str(path)

    def get_drive_type(self, path):
        return 3

    def get_named_security_info(self, path):
        return {"sd": "old-sd", "dacl": "old-dacl"}

    def set_entries_in_acl(self, old_dacl, sid):
        return "new-dacl"

    def set_named_security_info(self, path, dacl):
        self.events.append(("acl_set", path, dacl))

    def get_osfhandle(self, fd):
        return 1000 + int(fd)

    def set_handle_information(self, handle, mask, flags):
        pass

    def create_process(self, application, command_line, environment, cwd,
                       handles, token):
        raise AttributeError(
            "function 'CreateProcessAsUserW' not found in kernel32.dll "
            "(it is exported by advapi32.dll)")


class LauncherDiagnosticsTests(unittest.TestCase):
    def test_create_process_error_reaches_result_and_emit(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="dsh-win-diag-")
        policy = {"workspace": tmp, "mode": "read-only",
                  "tmpdir": tmp, "timeout": 5}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = sw.run(policy, [sw.__file__, "--version"], api=LauncherAPI())
        self.assertEqual(code, sw.EXIT_DSH_FAILED)
        result = sw.last_result()
        self.assertEqual(result["step"], "create_process")
        self.assertEqual(result["error"]["type"], "AttributeError")
        self.assertIn("CreateProcessAsUserW", result["error"]["message"])

        lines = [line for line in stderr.getvalue().splitlines() if line]
        self.assertTrue(lines)
        payload = json.loads(lines[-1])
        self.assertEqual(payload["code"], sw.EXIT_DSH_FAILED)
        self.assertEqual(payload["step"], "create_process")
        self.assertEqual(payload["error"]["type"], "AttributeError")
        self.assertIn("CreateProcessAsUserW", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
