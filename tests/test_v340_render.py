"""v3.4.0: the render-provider probe is bounded, cached and fake-only."""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, render
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_chrome, write_fake_dsh


def fake_chrome(path, **config):
    path = Path(path)
    config_path = Path(str(path) + ".config.json")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    path.write_text('''#!/usr/bin/env python3
import json
import os
import sys
import time

args = sys.argv[1:]
try:
    with open(sys.argv[0] + ".config.json", encoding="utf-8") as handle:
        config = json.load(handle)
except Exception:
    config = {}
log = config.get("log")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")
env_out = config.get("env_out")
if env_out:
    with open(env_out, "w", encoding="utf-8") as handle:
        json.dump({key: os.environ.get(key) for key in
                   ("DEEPSEEK_API_KEY", "DSH_BIN", "HOME", "TMPDIR")}, handle)
if "--version" in args:
    exit_code = config.get("version_exit")
    if exit_code:
        sys.exit(int(exit_code))
    print("Fake Chrome 123.0")
    sys.exit(0)
if "--dump-dom" in args:
    required = config.get("headless")
    if required and required not in args:
        sys.exit(int(config.get("wrong_headless_exit", 9)))
    if config.get("sleep"):
        time.sleep(float(config["sleep"]))
    print("<html><body>ok</body></html>")
    sys.exit(0)
sys.exit(2)
''', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class RenderProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.log = self.root / "chrome.log"
        self.chrome = fake_chrome(self.root / "fake-chrome", log=str(self.log))
        self.cfg = load_config(config_path=self.root / "config.json", env={
            "DSH_CHROME_BIN": str(self.chrome),
            "DSH_STATE_DIR": str(self.root / "state"),
        })

    def _chrome_config(self, **values):
        Path(str(self.chrome) + ".config.json").write_text(
            json.dumps(values), encoding="utf-8")

    def _invocations(self):
        if not self.log.is_file():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def test_find_chrome_prefers_configured_binary(self):
        self.assertEqual(render.find_chrome(self.cfg, self.workspace), self.chrome)

    def test_probe_falls_through_to_the_next_discovered_binary(self):
        first = fake_chrome(self.root / "broken-chrome", version_exit=3)
        second_log = self.root / "working-chrome.log"
        second = fake_chrome(self.root / "working-chrome", log=str(second_log))
        cfg = load_config(config_path=self.root / "multi.json", env={
            "DSH_CHROME_BIN": str(first),
            "DSH_STATE_DIR": str(self.root / "multi-state"),
        })
        with mock.patch.object(render, "APP_PATHS", (str(second),)), \
                mock.patch.object(render.shutil, "which", return_value=None):
            result = render.probe(cfg, self.workspace, timeout=5, force=True)
        self.assertTrue(result["available"])
        self.assertEqual(result["binary"], render.display_path(second))
        self.assertIn("Fake Chrome", result["version"])
        self.assertEqual(result["attempts"], 3)
        self.assertIn("--dump-dom", second_log.read_text(encoding="utf-8"))

    def test_legacy_headless_success_is_reported_exactly(self):
        self._chrome_config(headless="--headless", log=str(self.log))
        result = render.probe(self.cfg, self.workspace, timeout=5, force=True)
        self.assertTrue(result["available"])
        self.assertEqual(result["headless"], "--headless")
        text = render.hint(result)
        self.assertIn("`--headless --no-sandbox", text)
        self.assertNotIn("--headless=new", text)

    def test_workspace_tools_binary_is_not_trusted_by_default(self):
        shell = self.workspace / ".tools" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
        shell.parent.mkdir(parents=True)
        shell.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shell.chmod(0o755)
        app = fake_chrome(self.root / "desktop-chrome")
        cfg = load_config(config_path=self.root / "other.json", env={
            "DSH_CHROME_BIN": "", "DSH_STATE_DIR": str(self.root / "state")})
        with mock.patch.object(render, "APP_PATHS", (str(app),)), \
                mock.patch.object(render.shutil, "which", return_value=None):
            self.assertEqual(render.find_chrome(cfg, self.workspace), app)

    def test_workspace_binary_is_usable_when_operator_opts_in(self):
        shell = self.workspace / ".tools" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
        shell.parent.mkdir(parents=True)
        shell.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shell.chmod(0o755)
        cfg = load_config(config_path=self.root / "opt-in.json", env={
            "DSH_CHROME_BIN": str(shell),
            "DSH_STATE_DIR": str(self.root / "state")})
        self.assertEqual(render.find_chrome(cfg, self.workspace), shell)

    def test_empty_state_dir_disables_the_cache(self):
        from deepseek_mcp.config import Config
        self.assertIsNone(render._cache_path(Config({"state_dir": ""})))
        self.assertIsNone(render._cache_path(Config({"state_dir": "."})))
        self.assertIsNotNone(render._cache_path(
            Config({"state_dir": str(self.root / "state")})))

    def test_successful_probe_and_cache(self):
        first = render.probe(self.cfg, self.workspace, timeout=5)
        second = render.probe(self.cfg, self.workspace, timeout=5)
        self.assertTrue(first["available"])
        self.assertIn("Fake Chrome", first["version"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self._invocations()), 2)
        calls = "\n".join(self._invocations())
        self.assertIn("--user-data-dir=", calls)
        self.assertIn("--no-first-run", calls)
        self.assertIn("--no-default-browser-check", calls)
        self.assertIn("--disable-background-networking", calls)
        self.assertFalse(list(self.workspace.glob(".chrome-probe-*")))

    def test_probe_does_not_inherit_connector_secrets(self):
        env_out = self.root / "chrome-env.json"
        self._chrome_config(log=str(self.log), env_out=str(env_out))
        leaked_value = "connector-secret-should-not-reach-the-browser"
        with mock.patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": leaked_value,
                "DSH_BIN": "/operator/secret-adjacent-value"}, clear=False):
            result = render.probe(self.cfg, self.workspace, timeout=5, force=True)
        self.assertTrue(result["available"])
        data = json.loads(env_out.read_text(encoding="utf-8"))
        self.assertIsNone(data["DEEPSEEK_API_KEY"])
        self.assertIsNone(data["DSH_BIN"])
        self.assertTrue(str(data["HOME"]).startswith(str(self.workspace)))
        self.assertTrue(str(data["TMPDIR"]).startswith(str(self.workspace)))

    def test_timeout_is_bounded_and_reported(self):
        self._chrome_config(log=str(self.log), sleep=5)
        started = time.time()
        result = render.probe(self.cfg, self.workspace, timeout=1, force=True)
        elapsed = time.time() - started
        self.assertFalse(result["available"])
        self.assertIn("timed out", result["reason"])
        self.assertLess(elapsed, 5.0)

    def test_missing_binary_is_unavailable(self):
        with mock.patch("deepseek_mcp.render.find_chromes", return_value=[]):
            result = render.probe(self.cfg, self.workspace)
        self.assertFalse(result["available"])
        self.assertIn("no headless Chrome", result["reason"])
        self.assertIn("Do NOT", render.hint(result))

    def test_hint_for_working_binary_names_exact_flags(self):
        result = {"available": True, "binary": "~/.tools/chrome-headless-shell",
                  "version": "123", "seconds": 0.4, "reason": None}
        text = render.hint(result)
        self.assertIn("~/.tools/chrome-headless-shell", text)
        self.assertIn("--headless=new", text)
        self.assertIn("--user-data-dir", text)
        self.assertIn("hard shell timeout", text)

    def test_is_visual(self):
        self.assertTrue(render.is_visual("creative"))
        self.assertTrue(render.is_visual("code", "poster"))
        self.assertTrue(render.is_visual("code", "web"))
        self.assertFalse(render.is_visual("code", "data"))


class RenderPathAndHintTests(unittest.TestCase):
    def test_display_path_uses_a_component_boundary(self):
        home = Path.home()
        longer = Path(str(home) + "2")
        self.assertEqual(render.display_path(home), "~")
        self.assertEqual(render.display_path(home / "bin" / "chrome"),
                         "~" + os.sep + "bin" + os.sep + "chrome")
        self.assertEqual(render.display_path(longer), str(longer))

    def test_redact_does_not_rewrite_a_longer_name(self):
        home = str(Path.home())
        longer = home + "2" + os.sep + "secret"
        self.assertEqual(render._redact(longer), longer)
        self.assertEqual(render._redact("boom at " + home + os.sep + "Chrome"),
                         "boom at ~" + os.sep + "Chrome")
        self.assertEqual(render._redact(home), "~")
        self.assertEqual(render._redact(home + "-extra"), home + "-extra")

    def test_hint_for_tilde_binary_explains_login_home(self):
        text = render.hint({"available": True, "binary": "~/.tools/chrome",
                            "version": "123", "seconds": 0.4, "reason": None})
        self.assertIn("Connector health probe passed", text)
        self.assertIn("tilde", text.lower())
        self.assertIn("login home", text)
        self.assertIn("before you override `HOME`", text)
        self.assertIn("Do not download or install another browser", text)

    def test_hint_for_absolute_binary_has_no_tilde_note(self):
        text = render.hint({"available": True, "binary": "/opt/chrome",
                            "version": "123", "seconds": 0.4, "reason": None})
        self.assertNotIn("login home", text)
        self.assertIn("Do not download or install another browser", text)

    def test_negative_hint_contract_is_unchanged(self):
        text = render.hint({"available": False, "reason": "no binary"})
        self.assertIn("Do NOT", text)
        self.assertNotIn("login home", text)


class RenderProbeRunIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.chrome = write_fake_chrome(self.root / "fake-chrome")
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.root / "sessions")
        self.env.update({
            "DSH_RENDER_PROBE": "1",
            "DSH_CHROME_BIN": str(self.chrome),
            "DSH_MAX_CONCURRENT_CHILDREN": "4",
            "FAKE_DSH_STATE": str(self.root / "fake-state.json"),
        })

    def test_visual_run_records_probe_and_injects_environment(self):
        env = dict(os.environ)
        env.update(self.env)
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=self.root / "config.json", env=env)
            _text, run, _path = harness.run_harness({
                "prompt": "Design a print poster with a QR code and five sections",
                "subagents": 2, "quality": False,
                "workspace": str(self.workspace),
            }, cfg)
        self.assertEqual(run["status"], "ok")
        self.assertTrue(run["render_probe"]["available"])
        self.assertIn("Render environment (connector probe)", run["harness_task"])
        self.assertIn(render.display_path(self.chrome), run["harness_task"])
        self.assertIn("Visual-impact contract", run["harness_task"])
        self.assertIn("Render probe", (self.root / "runs" / (
            run["id"] + ".md")).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
