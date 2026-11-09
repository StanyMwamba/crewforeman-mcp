"""Host-independent tests for the sandbox probe's Windows detail line."""

import unittest
from unittest import mock

from deepseek_mcp import sandbox_windows
from tests import sandbox_probe


class WindowsProbeDetailTests(unittest.TestCase):
    def test_detail_carries_class_message_and_step(self):
        status = {
            "ok": False, "reason": "attributeerror",
            "admin": False, "admin_elevated": None, "admin_member": None,
            "admin_source": "unavailable", "admin_error": None,
            "write_restricted": False, "logon_sid": None,
            "restricting_sid": None, "step": "open_process_token",
            "error": {"type": "AttributeError",
                      "message": "function 'OpenProcessToken' not found"},
        }
        with mock.patch.object(sandbox_probe.platform, "system",
                               return_value="Windows"), \
                mock.patch.object(sandbox_windows, "probe_status",
                                  return_value=status):
            result = sandbox_probe._windows()
        self.assertFalse(result["usable"])
        self.assertIn("step=open_process_token", result["detail"])
        self.assertIn("error=AttributeError:", result["detail"])
        self.assertIn("OpenProcessToken", result["detail"])
        self.assertIn("reason=attributeerror", result["detail"])
        self.assertNotIn("\n", result["detail"])

    def test_detail_never_leaks_the_home_directory(self):
        home = "/Users/example-user"
        status = {
            "ok": False, "reason": "oserror", "admin": False,
            "admin_source": "unavailable", "write_restricted": False,
            "logon_sid": False, "step": "acl_apply",
            "error": {"type": "OSError",
                      "message": "open %s/secret.txt failed" % home},
        }
        with mock.patch.object(sandbox_probe.platform, "system",
                               return_value="Windows"), \
                mock.patch.object(sandbox_windows.os.path, "expanduser",
                                  return_value=home), \
                mock.patch.object(sandbox_windows, "probe_status",
                                  return_value=status):
            result = sandbox_probe._windows()
        self.assertNotIn(home, result["detail"])
        self.assertIn("~", result["detail"])

    def test_non_windows_still_reports_not_windows(self):
        with mock.patch.object(sandbox_probe.platform, "system",
                               return_value="Darwin"):
            result = sandbox_probe._windows()
        self.assertEqual(result["detail"], "not Windows")
        self.assertFalse(result["usable"])


if __name__ == "__main__":
    unittest.main()
