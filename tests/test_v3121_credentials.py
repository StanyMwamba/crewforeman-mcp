"""3.12.1 provider-credential resolution tests (slice S1).

Every value in this module is fake and assembled from split string literals.
No test writes, prints or asserts a real credential.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from deepseek_mcp import credentials, model_policy, providers

ENV_NAME = "CREWFOREMAN_FAKE_KEY_S1"
ENV_VALUE = "fake-" + "env-value-abc123"
FILE_VALUE = "fake-" + "file-value-def456"
RECORD_VALUE = "fake-" + "record-value-ghi789"
HOME_VALUE = "fake-" + "home-dir-jkl012"


def _write(path, content, mode=0o600):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, mode)


def _cfg(path):
    return {
        "credentials_file": path,
        "builder_provider": "fake-provider",
        "builder_model": "fake-model",
        "providers": [
            {
                "name": "fake-provider",
                "base_url": "https://fake.example/v1",
                "api": "openai-chat",
                "api_key_env": ENV_NAME,
                "models": ["fake-model"],
                "api_key_env_explicit": True,
            }
        ],
    }


def _no_key_env(**extra):
    """Keep the fake variable and credentials-path overrides out of the env."""
    values = {
        ENV_NAME: "",
        "CREWFOREMAN_CREDENTIALS_FILE": "",
        "DSH_CREDENTIALS_FILE": "",
    }
    values.update(extra)
    return mock.patch.dict(os.environ, values, clear=False)


class CredentialsResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _path(self, name="credentials.yaml"):
        return os.path.join(self._tmp.name, name)

    def test_environment_wins_over_file(self):
        path = self._path()
        _write(path, "%s: %s\n" % (ENV_NAME, FILE_VALUE))
        cfg = _cfg(path)
        with _no_key_env(**{ENV_NAME: ENV_VALUE}):
            self.assertEqual(credentials.resolve_key(ENV_NAME, cfg), ENV_VALUE)
            self.assertEqual(credentials.key_source(ENV_NAME, cfg), "environment")
            target = model_policy.resolve_target(cfg)
            self.assertEqual(target.api_key_env, ENV_NAME)
            rendered = repr(target) + json.dumps(target.as_dict())
        self.assertNotIn(ENV_VALUE, rendered)
        self.assertNotIn(FILE_VALUE, rendered)

    def test_only_file_resolution_model_policy_and_providers(self):
        path = self._path()
        _write(path, "%s: %s\n" % (ENV_NAME, FILE_VALUE))
        cfg = _cfg(path)
        provider = {"name": "fake-provider", "api_key_env": ENV_NAME}
        with _no_key_env():
            target = model_policy.resolve_target(cfg)
            from_policy = providers.resolve_key(provider, {}, cfg)
        self.assertEqual((target.provider, target.model), ("fake-provider", "fake-model"))
        self.assertEqual(from_policy, FILE_VALUE)
        rendered = repr(target) + json.dumps(target.as_dict())
        self.assertNotIn(FILE_VALUE, rendered)
        self.assertNotIn(ENV_VALUE, rendered)

    def test_missing_everywhere_names_only_variable(self):
        cfg = _cfg(self._path("absent.yaml"))
        provider = {"name": "fake-provider", "api_key_env": ENV_NAME}
        with _no_key_env():
            with self.assertRaises(model_policy.BuilderModelError) as caught:
                model_policy.resolve_target(cfg)
        text = str(caught.exception)
        self.assertIn("builder_provider_key_missing", text)
        self.assertIn(ENV_NAME, text)
        self.assertNotIn(FILE_VALUE, text)
        self.assertNotIn(ENV_VALUE, text)

        with _no_key_env():
            with self.assertRaises(providers.ProviderError) as caught_provider:
                providers.resolve_key(provider, {}, cfg)
        provider_text = str(caught_provider.exception)
        self.assertIn(ENV_NAME, provider_text)
        self.assertNotIn(FILE_VALUE, provider_text)
        self.assertNotIn(ENV_VALUE, provider_text)

    def test_group_readable_file_skipped_with_warning(self):
        path = self._path()
        _write(path, "%s: %s\n" % (ENV_NAME, FILE_VALUE), mode=0o644)
        cfg = _cfg(path)
        warnings = []
        with _no_key_env():
            self.assertIsNone(
                credentials.resolve_key(ENV_NAME, cfg, warn=warnings.append))
            with self.assertRaises(model_policy.BuilderModelError) as caught:
                model_policy.resolve_target(cfg)
        self.assertTrue(warnings)
        self.assertIn("0644", warnings[0])
        self.assertNotIn(FILE_VALUE, warnings[0])
        error_text = str(caught.exception)
        self.assertIn(ENV_NAME, error_text)
        self.assertNotIn(FILE_VALUE, error_text)

    def test_records_and_refs_layouts(self):
        records_path = self._path("records.yaml")
        _write(records_path, "records:\n  %s:\n    value: %s\n"
               % (ENV_NAME, RECORD_VALUE))
        provider = {"name": "fake-provider", "api_key_env": ENV_NAME}
        with _no_key_env():
            self.assertEqual(
                credentials.resolve_key(ENV_NAME, _cfg(records_path)),
                RECORD_VALUE)
            self.assertEqual(
                providers.resolve_key(provider, {}, _cfg(records_path)),
                RECORD_VALUE)

        refs_path = self._path("refs.yaml")
        _write(refs_path, "refs:\n  %s: env:HOME\n" % ENV_NAME)
        self.assertEqual(
            credentials.resolve_key(ENV_NAME, _cfg(refs_path), {"HOME": HOME_VALUE}),
            HOME_VALUE)

    def test_credentials_path_honours_config_and_env(self):
        configured = self._path("configured.yaml")
        override = self._path("override.yaml")
        self.assertEqual(
            credentials.credentials_path({"credentials_file": configured}, {}),
            configured)
        self.assertEqual(
            credentials.credentials_path(
                {"credentials_file": configured},
                {"CREWFOREMAN_CREDENTIALS_FILE": override}),
            override)
        self.assertEqual(
            credentials.credentials_path(
                {"credentials_file": configured},
                {"DSH_CREDENTIALS_FILE": override}),
            override)

    def test_target_representation_and_errors_never_carry_values(self):
        path = self._path()
        _write(path, "%s: %s\n" % (ENV_NAME, FILE_VALUE))
        cfg = _cfg(path)
        with _no_key_env():
            target = model_policy.resolve_target(cfg)
        rendered = repr(target) + json.dumps(target.as_dict())
        self.assertNotIn(FILE_VALUE, rendered)
        self.assertNotIn(ENV_VALUE, rendered)

        missing_cfg = _cfg(self._path("missing.yaml"))
        with _no_key_env():
            with self.assertRaises(model_policy.BuilderModelError) as caught:
                model_policy.resolve_target(missing_cfg)
        self.assertNotIn(FILE_VALUE, str(caught.exception))
        self.assertNotIn(ENV_VALUE, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
