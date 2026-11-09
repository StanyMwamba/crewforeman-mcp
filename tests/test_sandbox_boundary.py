"""Dispatcher: import the real per-OS boundary module.

One CI command cannot silently skip a platform: the module is imported by
sys.platform and an unavailable boundary fails under CF_REQUIRE_REAL_SANDBOX=1.
"""
import importlib
import os
import sys
import unittest

MODULES = {
    "darwin": "tests.test_sandbox_boundary_macos",
    "linux": "tests.test_sandbox_boundary_linux",
    "win32": "tests.test_sandbox_boundary_windows",
}


def _require_real():
    return os.environ.get("CF_REQUIRE_REAL_SANDBOX") == "1"


class SandboxBoundaryDispatcherTest(unittest.TestCase):
    def test_platform_boundary_module(self):
        name = MODULES.get(sys.platform)
        if name is None:
            if _require_real():
                self.fail("no boundary module for %s" % sys.platform)
            self.skipTest("no boundary module for %s" % sys.platform)
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            if _require_real():
                self.fail("boundary module %s unavailable: %s" % (name, exc))
            self.skipTest("boundary module %s unavailable: %s" % (name, exc))
        runner = getattr(module, "run_boundary", None)
        if runner is None:
            if _require_real():
                self.fail("%s has no run_boundary" % name)
            self.skipTest("%s has no run_boundary" % name)
        runner(self)

    def test_dispatcher_covers_all_three_platforms(self):
        self.assertEqual(set(MODULES), {"darwin", "linux", "win32"})
        for name in MODULES.values():
            self.assertTrue(name.startswith("tests."), name)


if __name__ == "__main__":
    unittest.main()
