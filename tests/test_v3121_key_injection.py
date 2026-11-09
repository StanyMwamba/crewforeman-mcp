"""3.12.1: the resolved API key reaches only the child dsh environment."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deepseek_mcp import harness, model_overlay
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh

# Fake values only, assembled from split literals; never a real credential.
FAKE_VALUE = "fake-" + "inject-" + "7c1d42"
FAKE_ENV_VALUE = "fake-" + "env-" + "9b2e88"
KEY_NAME = "FAKE_INJECT_KEY"


def _sha12(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class ChildEnvUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cred = self.root / "fake-credentials.yaml"
        self.cred.write_text(KEY_NAME + ": " + FAKE_VALUE + "\n",
                             encoding="utf-8")
        os.chmod(str(self.cred), 0o600)
        self.cfg = {"credentials_file": str(self.cred)}
        self.spec = SimpleNamespace(api_key_env=KEY_NAME)
        self.policy = {"sandbox": "workspace-write"}

    def test_credentials_file_injects_without_mutating_environ(self):
        with mock.patch.dict(os.environ, {
                "CREWFOREMAN_CREDENTIALS_FILE": str(self.cred),
                "DSH_CREDENTIALS_FILE": ""}, clear=False):
            os.environ.pop(KEY_NAME, None)
            before = dict(os.environ)
            env = harness._child_env(self.cfg, self.policy, self.spec)
            self.assertEqual(env[KEY_NAME], FAKE_VALUE)
            self.assertEqual(os.environ, before)
            self.assertNotIn(KEY_NAME, os.environ)

    def test_environment_wins_over_credentials_file(self):
        with mock.patch.dict(os.environ, {KEY_NAME: FAKE_ENV_VALUE},
                             clear=False):
            env = harness._child_env(self.cfg, self.policy, self.spec)
            self.assertEqual(env[KEY_NAME], FAKE_ENV_VALUE)
        self.assertNotIn(KEY_NAME, os.environ)

    def test_overlay_carries_only_the_variable_name(self):
        target = SimpleNamespace(
            provider="fake-provider", model="fake-model",
            vendor="Kimi (Moonshot)", base_url="https://example.invalid",
            api="openai-chat", api_key_env=KEY_NAME)
        overlay = model_overlay.create_overlay(str(self.root), target, self.cfg)
        try:
            text = Path(overlay.path).read_text(encoding="utf-8")
        finally:
            overlay.cleanup()
        self.assertIn(KEY_NAME, text)
        self.assertNotIn(FAKE_VALUE, text)
        self.assertNotIn(FAKE_ENV_VALUE, text)

    def test_missing_everywhere_is_absent(self):
        cfg = {"credentials_file": str(self.root / "does-not-exist.yaml")}
        with mock.patch.dict(os.environ, {
                "CREWFOREMAN_CREDENTIALS_FILE": str(self.root / "absent.yaml"),
                "DSH_CREDENTIALS_FILE": ""}, clear=False):
            os.environ.pop(KEY_NAME, None)
            env = harness._child_env(cfg, self.policy, self.spec)
            self.assertNotIn(KEY_NAME, env)


class KeyInjectionEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.dsh = write_fake_dsh(self.root / "dsh")
        self.env_out = self.root / "child-env.json"
        self.progress = []
        self.cred = self.root / "fake-credentials.yaml"
        self.cred.write_text(KEY_NAME + ": " + FAKE_VALUE + "\n",
                             encoding="utf-8")
        os.chmod(str(self.cred), 0o600)
        self.env = base_env(self.root, fake_dsh=self.dsh,
                            sessions=self.root / "sessions")
        self.env.update({
            "CREWFOREMAN_CREDENTIALS_FILE": str(self.cred),
            "FAKE_DSH_KEY_NAME": KEY_NAME,
            "FAKE_DSH_ENV_OUT": str(self.env_out),
            "DSH_TRANSIENT_RETRY": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
        })
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)
        self.cfg._values["credentials_file"] = str(self.cred)
        self.cfg._values["dsh_provider"] = "deepseek-official"
        self.cfg._values["dsh_model"] = "deepseek-flash"
        self.cfg._values["providers"] = [{
            "name": "fake-provider",
            "vendor": "Kimi (Moonshot)",
            "base_url": "https://example.invalid",
            "api": "openai-chat",
            "api_key_env": KEY_NAME,
            "models": [{"id": "fake-model", "name": "fake-model"}],
        }]
    def _run(self):
        # End-to-end: the explicit builder target is resolved by model_policy
        # (no mock), so a missing env var must fall back to the fake
        # credentials file and reach the fake child dsh.
        args = {"prompt": "Write one note.", "subagents": 0,
                "quality": False, "workspace": str(self.ws),
                "builder_provider": "fake-provider",
                "builder_model": "fake-model"}
        with mock.patch.dict(os.environ, self.env, clear=False):
            os.environ.pop(KEY_NAME, None)
            self.assertNotIn(KEY_NAME, os.environ)
            return harness.run_harness(args, self.cfg,
                                       progress_cb=self.progress.append)

    def test_child_receives_resolved_key_and_value_is_never_stored(self):
        _text, run, _path = self._run()
        self.assertEqual(run["status"], "ok")
        key_doc = json.loads(
            Path(str(self.env_out) + ".key").read_text(encoding="utf-8"))
        self.assertEqual(key_doc["name"], KEY_NAME)
        self.assertTrue(key_doc["present"])
        self.assertEqual(key_doc["sha256_12"], _sha12(FAKE_VALUE))

        self.assertNotIn(FAKE_VALUE, json.dumps(run, default=str))
        self.assertNotIn(FAKE_VALUE, "\n".join(self.progress))
        for directory in (self.root / "runs", self.root / "state"):
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        FAKE_VALUE,
                        path.read_text(encoding="utf-8", errors="ignore"),
                        "fake key leaked into %s" % path.name)


if __name__ == "__main__":
    unittest.main()
