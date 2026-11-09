"""v3.2.0 R4: log_dir normalisation, visible probe and fallback telemetry.

These tests lock the intended behaviour of ``store.resolve_log_dir`` /
``store.save_run`` for the Drive path that used to fall back silently, and the
model-visible fallback warning produced by ``harness._result_text``.
"""

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, store
from deepseek_mcp.config import load_config
from tests.fakes import base_env


class LogDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fallback = self.root / "fallback-runs"
        patcher = mock.patch.object(store, "FALLBACK_LOG_DIR", self.fallback)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cfg(self, log_dir):
        env = base_env(self.root)
        env["DEEPSEEK_MCP_LOG_DIR"] = str(log_dir)
        return load_config(config_path=self.root / "config.json", env=env)

    def _run(self, run_id):
        return {
            "id": run_id,
            "kind": "single",
            "created": "2026-01-01T00:00:00+00:00",
            "title": "R4 log_dir test",
            "model": "deepseek-flash",
            "status": "ok",
            "prompt": "hello",
            "answer": "world",
            "files": [],
        }

    def test_primary_log_dir_with_spaces_is_used(self):
        primary = self.root / "Google Drive" / "My Drive" / "DeepSeek-MCP" / "runs"
        cfg = self._cfg(primary)
        run = self._run("spaces-run")

        md_path = Path(store.save_run(run, cfg))

        self.assertEqual(md_path, primary / "spaces-run.md")
        self.assertEqual(md_path.parent, primary)
        self.assertTrue(md_path.is_file())
        self.assertTrue(md_path.with_suffix(".json").is_file())
        self.assertNotIn("log_fallback_reason", run)
        self.assertFalse(self.fallback.exists())
        self.assertNotIn("**Log fallback:**", md_path.read_text(encoding="utf-8"))

    def test_unwritable_primary_falls_back_with_reason(self):
        blocker = self.root / "blocker"
        blocker.write_text("a plain file where a directory is needed",
                           encoding="utf-8")
        primary = blocker / "runs"
        cfg = self._cfg(primary)
        run = self._run("fallback-run")

        md_path = Path(store.save_run(run, cfg))

        self.assertEqual(md_path.parent, self.fallback)
        self.assertTrue(md_path.is_file())
        self.assertTrue(md_path.with_suffix(".json").is_file())

        reason = run.get("log_fallback_reason")
        self.assertIsInstance(reason, str)
        self.assertIn("not writable", reason)
        self.assertIn("fallback", reason)
        self.assertIn(str(primary), reason)
        # POSIX reports ENOTDIR.  Windows CreateDirectory reports
        # ERROR_ALREADY_EXISTS (WinError 183) when a parent component is a
        # file; both are the same "not a directory" condition.
        self.assertRegex(
            reason, r"NotADirectoryError|FileExistsError: \[WinError 183\]")

        md_text = md_path.read_text(encoding="utf-8")
        self.assertIn("Log fallback:", md_text)
        self.assertIn("not writable", md_text)

        saved = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIn("log_fallback_reason", saved)
        self.assertIn("not writable", saved["log_fallback_reason"])
        self.assertIn("fallback", saved["log_fallback_reason"])

    def test_raw_tilde_log_dir_is_expanded(self):
        cfg = self._cfg(self.root / "runs")
        unique = "v320-tilde-" + uuid.uuid4().hex[:12]
        cfg._values["log_dir"] = "~/" + unique
        normalised = store._normalise_log_dir(cfg.log_dir)
        home = os.path.expanduser("~")

        self.assertTrue(normalised.is_absolute())
        self.assertTrue(str(normalised).startswith(home))
        self.assertNotIn("~", str(normalised))
        self.assertEqual(normalised, Path(home) / unique)
        # Do not create the real HOME target: only assert it is still absent.
        self.assertFalse(normalised.exists())

        # resolve_log_dir must normalise at the point of use too, without
        # touching the real home directory (the probe is mocked out).
        with mock.patch.object(store, "_probe_dir", return_value=None) as probe:
            directory, reason = store.resolve_log_dir(cfg)
        self.assertEqual(directory, normalised)
        self.assertIsNone(reason)
        probe.assert_called_once_with(normalised)

    def test_outer_whitespace_is_stripped(self):
        spaced = (self.root / "Google Drive" / "My Drive" / "DeepSeek-MCP"
                  / "whitespace-runs")
        padded = "  " + str(spaced) + "  "

        self.assertEqual(store._normalise_log_dir(padded), spaced)

        env = base_env(self.root)
        env["DEEPSEEK_MCP_LOG_DIR"] = padded
        cfg = load_config(config_path=self.root / "config.json", env=env)
        self.assertEqual(cfg.log_dir, str(spaced))
        self.assertFalse(str(cfg.log_dir).startswith(" "))
        self.assertFalse(str(cfg.log_dir).endswith(" "))
        self.assertEqual(store._normalise_log_dir(cfg.log_dir), spaced)

    def test_probe_prefix_is_visible(self):
        self.assertFalse(store.PROBE_PREFIX.startswith("."))
        self.assertIn("write-test", store.PROBE_PREFIX)

        target = self.root / "probe-target"
        self.assertIsNone(store._probe_dir(target))
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_resolve_log_dir_returns_none_reason_when_writable(self):
        primary = self.root / "writable-runs"
        cfg = self._cfg(primary)

        directory, reason = store.resolve_log_dir(cfg)

        self.assertEqual(directory, primary)
        self.assertIsNone(reason)
        self.assertTrue(primary.is_dir())
        self.assertEqual(store.writable_log_dir(cfg), primary)
        self.assertFalse(self.fallback.exists())

    def test_read_paths_use_normalised_log_dir(self):
        primary = (self.root / "Google Drive" / "My Drive" / "DeepSeek-MCP"
                   / "read-runs")
        cfg = self._cfg(primary)
        store.save_run(self._run("read-run"), cfg)

        # A padded raw value must still find the files written to the
        # normalised primary, just as load_runs()/find_run() do in production.
        cfg._values["log_dir"] = "  " + str(primary) + "  "

        run, json_path, md_path = store.find_run("read-run", cfg)
        self.assertEqual(run["id"], "read-run")
        self.assertEqual(json_path.parent, primary)
        self.assertTrue(md_path.is_file())
        self.assertEqual([item["id"] for item in store.load_runs(cfg)],
                         ["read-run"])

    def test_null_log_dir_falls_back_with_reason(self):
        cfg = self._cfg(self.root / "runs")
        cfg._values["log_dir"] = None
        run = self._run("null-logdir-run")

        md_path = Path(store.save_run(run, cfg))

        self.assertEqual(md_path.parent, self.fallback)
        self.assertTrue(md_path.is_file())
        reason = run.get("log_fallback_reason")
        self.assertIsInstance(reason, str)
        self.assertIn("not configured", reason)
        self.assertIn("not writable", reason)
        self.assertIn("fallback", reason)
        self.assertIn("Log fallback:", md_path.read_text(encoding="utf-8"))

    def test_both_unwritable_raises_explicit_error(self):
        blocker_one = self.root / "blocker-one"
        blocker_one.write_text("x", encoding="utf-8")
        blocker_two = self.root / "blocker-two"
        blocker_two.write_text("x", encoding="utf-8")
        primary = blocker_one / "runs"
        fallback = blocker_two / "fallback-runs"

        with mock.patch.object(store, "FALLBACK_LOG_DIR", fallback):
            cfg = self._cfg(primary)
            with self.assertRaises(RuntimeError) as caught:
                store.save_run(self._run("both-fail"), cfg)

        message = str(caught.exception)
        self.assertIn("Cannot write runs to", message)
        self.assertIn(str(primary), message)
        self.assertIn(str(fallback), message)
        self.assertRegex(
            message, r"NotADirectoryError|FileExistsError: \[WinError 183\]")

    def test_result_text_warns_about_log_fallback(self):
        reason = (
            "configured log_dir /tmp/cloud-sync/My Files/DeepSeek-MCP/runs is not "
            "writable (PermissionError: [Errno 1] Operation not permitted); "
            "using fallback /tmp/deepseek-mcp/runs"
        )
        run = {
            "id": "warn-run",
            "seconds": 1,
            "tools_used": {"bash": 1},
            "log_fallback_reason": reason,
            "answer": "done",
        }

        text = harness._result_text(run, "/tmp/warn-run.md")

        self.assertTrue(text.startswith("[WARNING] "))
        self.assertIn("fallback folder", text)
        self.assertIn("not writable", text)
        self.assertIn("configured log_dir", text)
        self.assertNotIn("/" + "Users" + "/", text)
        self.assertNotIn("/" + "home" + "/", text)
        self.assertNotIn("@", text)

        plain = harness._result_text({"id": "plain-run", "answer": "done"},
                                     "/tmp/plain-run.md")
        self.assertFalse(plain.startswith("[WARNING]"))


if __name__ == "__main__":
    unittest.main()
