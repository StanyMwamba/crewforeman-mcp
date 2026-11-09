"""Mocked tests for the Windows write-restricted launcher.

The suite runs on macOS/Linux: every Win32 call is replaced by ``FakeAPI``.
No test touches a real token, ACL or process.
"""

import os
import sys
import tempfile
import unittest

from deepseek_mcp import sandbox_windows as sw


class FakeAPI:
    def __init__(self):
        self.events = []
        self.admin = True
        self.fail_per_run = False
        self.assign_ok = True
        self.wait_result = sw.WAIT_OBJECT_0
        self.exit_code = 0
        self.create_error = None
        self.handles = []
        self.created = None
        self.restored = []
        self._active_sid = None
        self._next = 100

    def _event(self, name, **data):
        self.events.append((name, data))

    def _handle(self):
        self._next += 1
        return self._next

    def open_process_token(self):
        self._event("open_token")
        return self._handle()

    def close_handle(self, handle):
        self._event("close_handle", handle=handle)

    def is_admin(self, token):
        self._event("is_admin")
        return self.admin

    def make_per_run_sid(self):
        self._event("make_per_run_sid")
        if self.fail_per_run:
            raise OSError("per-run SID rejected")
        self._active_sid = "S-1-5-21-1-2-3"
        return self._active_sid

    def token_logon_sid(self, token):
        self._event("token_logon_sid")
        self._active_sid = "S-1-5-5-0-1"
        return self._active_sid

    def create_restricted_token(self, token, sid):
        self._event("create_restricted_token", sid=sid)
        return self._handle()

    def sid_to_string(self, sid):
        return str(sid or "S-1-5-21-1-2-3")

    def get_long_path_name(self, path):
        return path

    def get_drive_type(self, path):
        return 3

    def get_named_security_info(self, path):
        self._event("acl_get", path=path)
        return {"sd": "old-sd:%s" % path, "dacl": "old-dacl:%s" % path}

    def set_entries_in_acl(self, old_dacl, sid):
        self._event("acl_entries", sid=sid)
        return "new-dacl:%s" % old_dacl

    def set_named_security_info(self, path, dacl):
        self._event("acl_set", path=path, dacl=dacl)
        if str(dacl).startswith("old-dacl:"):
            self.restored.append((path, dacl))

    def get_osfhandle(self, fd):
        self._event("osfhandle", fd=fd)
        return 1000 + int(fd)

    def set_handle_information(self, handle, mask, flags):
        self._event("inherit_handle", handle=handle)

    def create_process(self, application, command_line, environment, cwd,
                       handles, token):
        self._event("create_process", application=application,
                    handles=list(handles))
        if self.create_error is not None:
            raise self.create_error
        self.handles = list(handles)
        self.created = {"application": application,
                        "command_line": command_line,
                        "cwd": cwd,
                        "handles": list(handles),
                        "token": token}
        return {"pid": 4242, "process": "process-handle",
                "thread": "thread-handle"}

    def create_job_object(self, flags):
        self._event("create_job", flags=flags)
        return "job-handle"

    def assign_process_to_job(self, job, process):
        self._event("assign_job", job=job, process=process)
        return bool(self.assign_ok)

    def resume_thread(self, thread):
        self._event("resume", thread=thread)
        return True

    def terminate_process(self, process, code=1):
        self._event("terminate_process", process=process, code=code)
        return True

    def terminate_job(self, job, code=1):
        self._event("terminate_job", job=job, code=code)
        return True

    def wait_for_single_object(self, process, timeout_ms):
        self._event("wait", process=process, timeout_ms=timeout_ms)
        return self.wait_result

    def get_exit_code(self, process):
        self._event("exit_code", process=process)
        return self.exit_code


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-win-unit-")
        self.workspace = os.path.join(self.tmp, "workspace")
        self.tmpdir = os.path.join(self.tmp, "tmp")
        self.dsh_home = os.path.join(self.tmp, "dsh-home")
        os.makedirs(self.workspace)
        os.makedirs(self.tmpdir)
        for name in ("sessions", "profiles", "storages", "cache"):
            os.makedirs(os.path.join(self.dsh_home, name))
        self.policy = {
            "workspace": self.workspace,
            "mode": "workspace-write",
            "tmpdir": self.tmpdir,
            "extra_writes": [],
            "dsh_home": self.dsh_home,
            "required": True,
        }

    def _run(self, **overrides):
        api = FakeAPI()
        for key, value in overrides.items():
            setattr(api, key, value)
        code = sw.run(self.policy, [sys.executable, "-c", "pass"], api=api)
        return code, api, sw.last_result()

    def test_probe_reports_admin_and_per_run_sid(self):
        api = FakeAPI()
        status = sw.probe_status(api=api)
        self.assertTrue(status["ok"])
        self.assertTrue(status["admin"])
        self.assertEqual(status["restricting_sid"], "per-run")
        self.assertTrue(status["write_restricted"])

    def test_probe_falls_back_to_logon_sid(self):
        api = FakeAPI()
        api.fail_per_run = True
        status = sw.probe_status(api=api)
        self.assertTrue(status["ok"])
        self.assertEqual(status["restricting_sid"], "logon")
        names = [name for name, _ in api.events]
        self.assertIn("token_logon_sid", names)

    def test_import_safe_probe_on_non_windows(self):
        if sw._is_windows():
            self.skipTest("host is Windows")
        status = sw.probe_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "not_windows")

    def test_api_order_token_acl_process_job(self):
        code, api, _result = self._run()
        self.assertEqual(code, 0)
        names = [name for name, _ in api.events]
        token = min(names.index("create_restricted_token"),
                    names.index("token_logon_sid")
                    if "token_logon_sid" in names else 10 ** 6)
        acl = names.index("acl_get")
        process = names.index("create_process")
        job = names.index("create_job")
        resume = names.index("resume")
        self.assertLess(token, acl)
        self.assertLess(acl, process)
        self.assertLess(process, job)
        self.assertLess(job, resume)

    def test_handle_list_has_exactly_three(self):
        code, api, _result = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(len(api.handles), 3)
        self.assertEqual(api.handles, [1000, 1001, 1002])
        inherits = [name for name, _ in api.events
                    if name == "inherit_handle"]
        self.assertEqual(len(inherits), 3)
        self.assertEqual(len(api.created["handles"]), 3)

    def test_assignment_failure_terminates_before_resume(self):
        code, api, result = self._run(assign_ok=False)
        self.assertEqual(code, sw.EXIT_APPLY_FAILED)
        names = [name for name, _ in api.events]
        self.assertIn("terminate_process", names)
        self.assertNotIn("resume", names)
        self.assertLess(names.index("assign_job"),
                        names.index("terminate_process"))
        self.assertEqual(result["reason"], "job_assignment_failed")

    def test_acl_restored_on_process_error(self):
        failure = OSError("CreateProcessAsUserW failed")
        failure.winerror = 1314
        code, api, result = self._run(create_error=failure)
        self.assertEqual(code, sw.EXIT_DSH_FAILED)
        self.assertTrue(api.restored)
        self.assertEqual(result["restore_failures"], 0)
        self.assertTrue(result["acl_residue"])
        restored_paths = {path for path, _ in api.restored}
        self.assertIn(self.workspace, restored_paths)
        self.assertIn(self.tmpdir, restored_paths)

    def test_unc_rejected(self):
        with self.assertRaises(sw.SandboxError) as caught:
            sw.canonical_root(r"\\server\share\workspace", FakeAPI())
        self.assertEqual(caught.exception.reason, "unc_rejected")

    def test_read_only_grants_tmpdir_only(self):
        api = FakeAPI()
        policy = dict(self.policy, mode="read-only")
        acl = sw.build_policy_acl(policy, api=api, sid="S-1-5-21-1-2-3")
        expected = os.path.normcase(os.path.realpath(self.tmpdir))
        self.assertEqual(acl["granted"], [expected])
        paths = [data["path"] for name, data in api.events
                 if name == "acl_set"]
        self.assertEqual(paths, [expected])

    def test_workspace_write_includes_runtime_dirs_and_extra(self):
        extra = os.path.join(self.tmp, "extra")
        os.makedirs(extra)
        api = FakeAPI()
        policy = dict(self.policy, extra_writes=[extra])
        acl = sw.build_policy_acl(policy, api=api, sid="S-1-5-21-1-2-3")
        self.assertGreaterEqual(len(acl["granted"]), 6)
        self.assertTrue(any(path.endswith("sessions")
                            for path in acl["granted"]))
        self.assertIn(os.path.normcase(os.path.realpath(extra)),
                      acl["granted"])

    def test_missing_root_is_skipped_not_granted(self):
        api = FakeAPI()
        policy = dict(self.policy,
                      tmpdir=os.path.join(self.tmp, "does-not-exist"))
        acl = sw.build_policy_acl(policy, api=api, sid="S-1-5-21-1-2-3")
        self.assertNotIn(os.path.normcase(os.path.realpath(
            os.path.join(self.tmp, "does-not-exist"))), acl["granted"])
        self.assertTrue(any(item["reason"] == "missing_root"
                            for item in acl["skipped"]))

    def test_main_wrong_platform_is_125(self):
        if sw._is_windows():
            self.skipTest("host is Windows")
        self.assertEqual(sw.main(["--policy", "{}", "--", "dsh.exe"]), 125)

    def test_main_rejects_missing_policy(self):
        self.assertEqual(sw.main(["--", "dsh.exe"]), sw.EXIT_APPLY_FAILED)


if __name__ == "__main__":
    unittest.main()
