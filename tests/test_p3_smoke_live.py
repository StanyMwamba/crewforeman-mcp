"""The live smoke is explicit, offline-safe, and has reusable checks."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from deepseek_mcp.config import SERVER_VERSION

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "smoke_live.py"
sys.path.insert(0, str(ROOT / "scripts"))
import smoke_live  # noqa: E402


class SmokeCliTests(unittest.TestCase):
    def test_help_works(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--yes", result.stdout)

    def test_refuses_without_yes_and_does_not_run(self):
        result = subprocess.run([sys.executable, str(SCRIPT)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("LIVE PAID", result.stderr)


class CheckLiveRunTests(unittest.TestCase):
    def good_run(self):
        return {"status": "ok", "builder": {"status": "ok"},
                "quality_score": 8.7, "review_status": "accepted"}

    def test_accepts_a_good_run(self):
        ok, error = smoke_live.check_live_run(self.good_run(), SERVER_VERSION)
        self.assertTrue(ok, error)
        self.assertIsNone(error)

    def test_rejects_missing_score(self):
        run = self.good_run()
        run["quality_score"] = None
        ok, error = smoke_live.check_live_run(run, SERVER_VERSION)
        self.assertFalse(ok)
        self.assertIn("quality_score", error)

    def test_rejects_builder_error(self):
        run = self.good_run()
        run["builder"] = {"status": "error"}
        ok, error = smoke_live.check_live_run(run, SERVER_VERSION)
        self.assertFalse(ok)
        self.assertIn("builder", error)

    def test_rejects_wrong_version(self):
        ok, _error = smoke_live.check_live_run(self.good_run(), "0.0.1")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
