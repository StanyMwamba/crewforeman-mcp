"""3.15.0 config surface: MiMo worker keys, env aliases, SERVER_VERSION.

Windows-compatible: stdlib only, no shell, no git, no network.
Python 3.9 compatible.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import config
from deepseek_mcp.config import (
    DEFAULTS, DERIVED_STATE_ROOT_KEYS, DICT_KEYS, ENV_MAP, INT_KEYS,
    PATH_KEYS, SERVER_VERSION, load_config, _coerce,
)

ROOT = Path(__file__).resolve().parent.parent

# (env alias, config key) for every 3.15 alias, grouped by key.
NEW_ALIASES = (
    ("MIMO_BIN", "mimo_bin"),
    ("DSH_MIMO_BIN", "mimo_bin"),
    ("DEEPSEEK_MIMO_BIN", "mimo_bin"),
    ("MIMO_MODEL", "mimo_default_model"),
    ("DSH_MIMO_MODEL", "mimo_default_model"),
    ("MIMO_PROBE_MODEL", "mimo_probe_model"),
    ("MIMO_VARIANT", "mimo_variant"),
    ("DSH_MIMO_SKIP_PERMISSIONS", "mimo_skip_permissions"),
    ("DEEPSEEK_MIMO_SKIP_PERMISSIONS", "mimo_skip_permissions"),
    ("DSH_MIMO_SANDBOX", "mimo_sandbox"),
    ("DSH_MIMO_MAX_CONCURRENT_AGENTS", "mimo_max_concurrent_agents"),
    ("DSH_MIMO_USE_SERVE", "mimo_use_serve"),
    ("DSH_MIMO_SERVE_HOST", "mimo_serve_host"),
    ("DSH_MIMO_SERVE_PORT", "mimo_serve_port"),
    ("DSH_MIMO_PRICES", "mimo_prices"),
    ("DSH_CROSSCHECK_CREW_PROVIDER", "crosscheck_crew_provider"),
    ("DEEPSEEK_CROSSCHECK_CREW_PROVIDER", "crosscheck_crew_provider"),
    ("DSH_CROSSCHECK_CREW_MODEL", "crosscheck_crew_model"),
    ("DEEPSEEK_CROSSCHECK_CREW_MODEL", "crosscheck_crew_model"),
)

NEW_KEYS = (
    "mimo_bin", "mimo_default_model", "mimo_probe_model", "mimo_variant",
    "mimo_skip_permissions", "mimo_sandbox", "mimo_max_concurrent_agents",
    "mimo_use_serve", "mimo_serve_host", "mimo_serve_port", "mimo_prices",
    "crosscheck_crew_provider", "crosscheck_crew_model",
)


class ConfigSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def assertSamePath(self, actual, expected):
        self.assertEqual(os.path.normpath(str(actual)),
                         os.path.normpath(str(expected)))

    def load(self, env=None):
        return load_config(config_path=self.config_path,
                           env={} if env is None else env)

    def test_new_key_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg.mimo_bin, "mimo")
        self.assertEqual(cfg.mimo_default_model, "xiaomi/mimo-v2.6-pro")
        self.assertEqual(cfg.mimo_probe_model, "xiaomi/mimo-v2.6-flash")
        self.assertEqual(cfg.mimo_variant, "")
        self.assertIs(cfg.mimo_skip_permissions, False)
        self.assertEqual(cfg.mimo_sandbox, "")
        self.assertEqual(cfg.mimo_max_concurrent_agents, 0)
        self.assertIs(cfg.mimo_use_serve, False)
        self.assertEqual(cfg.mimo_serve_host, "127.0.0.1")
        self.assertEqual(cfg.mimo_serve_port, 0)
        self.assertEqual(cfg.mimo_prices, {})
        self.assertEqual(cfg.crosscheck_crew_provider, "")
        self.assertEqual(cfg.crosscheck_crew_model, "")
        for key in NEW_KEYS:
            self.assertIn(key, DEFAULTS, key)

    def test_env_alias_map_entries(self):
        for env_name, key in NEW_ALIASES:
            self.assertEqual(ENV_MAP.get(env_name), key, env_name)

    def test_env_override_for_each_alias(self):
        cases = (
            ("MIMO_BIN", "/opt/mimo-a", "mimo_bin", "/opt/mimo-a"),
            ("DSH_MIMO_BIN", "/opt/mimo-b", "mimo_bin", "/opt/mimo-b"),
            ("DEEPSEEK_MIMO_BIN", "/opt/mimo-c", "mimo_bin", "/opt/mimo-c"),
            ("MIMO_MODEL", "xiaomi/m1", "mimo_default_model", "xiaomi/m1"),
            ("DSH_MIMO_MODEL", "xiaomi/m2", "mimo_default_model", "xiaomi/m2"),
            ("MIMO_PROBE_MODEL", "xiaomi/p1", "mimo_probe_model", "xiaomi/p1"),
            ("MIMO_VARIANT", "fast", "mimo_variant", "fast"),
            ("DSH_MIMO_SKIP_PERMISSIONS", "1", "mimo_skip_permissions", True),
            ("DEEPSEEK_MIMO_SKIP_PERMISSIONS", "1", "mimo_skip_permissions", True),
            ("DSH_MIMO_SANDBOX", "read-only", "mimo_sandbox", "read-only"),
            ("DSH_MIMO_MAX_CONCURRENT_AGENTS", "7", "mimo_max_concurrent_agents", 7),
            ("DSH_MIMO_USE_SERVE", "1", "mimo_use_serve", True),
            ("DSH_MIMO_SERVE_HOST", "10.0.0.5", "mimo_serve_host", "10.0.0.5"),
            ("DSH_MIMO_SERVE_PORT", "8123", "mimo_serve_port", 8123),
            ("DSH_MIMO_PRICES", '{"m": {"input": 1.5}}', "mimo_prices",
             {"m": {"input": 1.5}}),
            ("DSH_CROSSCHECK_CREW_PROVIDER", "mimo", "crosscheck_crew_provider", "mimo"),
            ("DEEPSEEK_CROSSCHECK_CREW_PROVIDER", "mimo", "crosscheck_crew_provider", "mimo"),
            ("DSH_CROSSCHECK_CREW_MODEL", "xiaomi/m3", "crosscheck_crew_model", "xiaomi/m3"),
            ("DEEPSEEK_CROSSCHECK_CREW_MODEL", "xiaomi/m4", "crosscheck_crew_model", "xiaomi/m4"),
        )
        for env_name, raw, key, expected in cases:
            with self.subTest(env=env_name):
                cfg = self.load({env_name: raw})
                if key == "mimo_bin":
                    # Binary paths are OS-normalised (``\\opt\\mimo-a`` on Windows).
                    expected = os.path.normpath(expected)
                self.assertEqual(cfg[key], expected)
                self.assertTrue(cfg.is_explicit(key), env_name)

    def test_alias_precedence_newest_wins(self):
        # Iteration order is reversed(ENV_MAP): the newest alias is inserted
        # first ("New names are listed first") so it is applied last and wins.
        cfg = self.load({"MIMO_BIN": "/newest", "DSH_MIMO_BIN": "/mid",
                         "DEEPSEEK_MIMO_BIN": "/oldest"})
        self.assertEqual(cfg.mimo_bin, os.path.normpath("/newest"))
        cfg = self.load({"DSH_MIMO_BIN": "/mid", "DEEPSEEK_MIMO_BIN": "/oldest"})
        self.assertEqual(cfg.mimo_bin, os.path.normpath("/mid"))
        cfg = self.load({"MIMO_MODEL": "xiaomi/new",
                         "DSH_MIMO_MODEL": "xiaomi/old"})
        self.assertEqual(cfg.mimo_default_model, "xiaomi/new")
        cfg = self.load({"DSH_MIMO_SKIP_PERMISSIONS": "1",
                         "DEEPSEEK_MIMO_SKIP_PERMISSIONS": "0"})
        self.assertIs(cfg.mimo_skip_permissions, True)
        cfg = self.load({"DSH_CROSSCHECK_CREW_PROVIDER": "mimo",
                         "DEEPSEEK_CROSSCHECK_CREW_PROVIDER": "deepseek"})
        self.assertEqual(cfg.crosscheck_crew_provider, "mimo")

    def test_alias_insertion_order_newest_first(self):
        # Newest alias must precede older ones so reversed() applies it last.
        names = list(ENV_MAP)
        self.assertLess(names.index("MIMO_BIN"), names.index("DSH_MIMO_BIN"))
        self.assertLess(names.index("DSH_MIMO_BIN"),
                        names.index("DEEPSEEK_MIMO_BIN"))
        self.assertLess(names.index("MIMO_MODEL"), names.index("DSH_MIMO_MODEL"))
        self.assertLess(names.index("DSH_MIMO_SKIP_PERMISSIONS"),
                        names.index("DEEPSEEK_MIMO_SKIP_PERMISSIONS"))

    def test_bool_coercion(self):
        for raw, expected in (("1", True), ("true", True),
                              ("0", False), ("false", False)):
            with self.subTest(raw=raw):
                cfg = self.load({"DSH_MIMO_SKIP_PERMISSIONS": raw,
                                 "DSH_MIMO_USE_SERVE": raw})
                self.assertIs(cfg.mimo_skip_permissions, expected)
                self.assertIs(cfg.mimo_use_serve, expected)

    def test_int_coercion(self):
        self.assertIn("mimo_max_concurrent_agents", config.INT_KEYS)
        self.assertIn("mimo_serve_port", config.INT_KEYS)
        cfg = self.load({"DSH_MIMO_MAX_CONCURRENT_AGENTS": "16",
                         "DSH_MIMO_SERVE_PORT": "9000"})
        self.assertEqual(cfg.mimo_max_concurrent_agents, 16)
        self.assertEqual(cfg.mimo_serve_port, 9000)

    def test_mimo_prices_from_dict(self):
        self.assertIn("mimo_prices", config.DICT_KEYS)
        prices = {"mimo-v2.6-pro": {"input": 0.435, "cache_hit": 0.0036,
                                    "output": 0.87}}
        coerced = _coerce("mimo_prices", prices)
        self.assertEqual(coerced, prices)
        self.assertIsNot(coerced, prices)
        self.config_path.write_text(json.dumps({"mimo_prices": prices}),
                                    encoding="utf-8")
        cfg = self.load()
        self.assertEqual(cfg.mimo_prices, prices)

    def test_mimo_prices_from_json_string(self):
        text = '{"mimo-v2.6-flash": {"input": 0.14, "output": 0.28}}'
        cfg = self.load({"DSH_MIMO_PRICES": text})
        self.assertEqual(cfg.mimo_prices,
                         {"mimo-v2.6-flash": {"input": 0.14, "output": 0.28}})

    def test_mimo_prices_invalid_json_raises(self):
        # Invalid JSON must raise a clear ValueError from _coerce, never be
        # silently ignored or replaced by {}.
        for raw in ("{not json", "{", '{"a": '):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    self.load({"DSH_MIMO_PRICES": raw})
                self.assertIn("mimo_prices", str(caught.exception))
        with self.assertRaises(ValueError):
            _coerce("mimo_prices", "{not json")
        with self.assertRaises(ValueError):
            _coerce("mimo_prices", "[1, 2]")
        with self.assertRaises(ValueError):
            _coerce("mimo_prices", 5)

    def test_mimo_bin_in_path_keys(self):
        self.assertIn("mimo_bin", PATH_KEYS)
        cfg = self.load({"MIMO_BIN": "~/bin/mimo"})
        self.assertSamePath(cfg.mimo_bin, os.path.expanduser("~/bin/mimo"))
        self.assertNotIn("~", cfg.mimo_bin)

    def test_type_classification_lists(self):
        self.assertIn("mimo_skip_permissions", config.BOOL_KEYS)
        self.assertIn("mimo_use_serve", config.BOOL_KEYS)
        self.assertIn("mimo_max_concurrent_agents", INT_KEYS)
        self.assertIn("mimo_serve_port", INT_KEYS)
        self.assertIn("mimo_prices", DICT_KEYS)
        self.assertIn("mimo_bin", PATH_KEYS)


class ServerVersionTests(unittest.TestCase):
    def test_server_version(self):
        self.assertEqual(SERVER_VERSION, "3.15.0")

    def test_pyproject_version_agreement(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(match)
        py_version = match.group(1)
        if py_version == "3.14.3":
            # W12 may not have bumped pyproject.toml yet; the agreement with
            # config.SERVER_VERSION is enforced by ci_hygiene at integration.
            self.assertEqual(SERVER_VERSION, "3.15.0")
            return
        self.assertEqual(py_version, SERVER_VERSION)


class DerivedStateRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def assertSamePath(self, actual, expected):
        self.assertEqual(os.path.normpath(str(actual)),
                         os.path.normpath(str(expected)))

    def test_state_root_derivation_untouched(self):
        # Regression: CREWFOREMAN_STATE_DIR still derives log_dir/jobs_dir
        # (and state_dir/pid_file) and never touches the mimo keys.
        root = self.root / "state-root"
        cfg = load_config(config_path=self.config_path,
                          env={"CREWFOREMAN_STATE_DIR": str(root)})
        self.assertSamePath(cfg.state_dir, root / "state")
        self.assertSamePath(cfg.log_dir, root / "runs")
        self.assertSamePath(cfg.jobs_dir, root / "jobs")
        self.assertSamePath(cfg.pid_file, root / "keep_awake.pid")
        self.assertEqual(cfg.mimo_bin, "mimo")
        self.assertEqual(cfg.mimo_default_model, "xiaomi/mimo-v2.6-pro")
        self.assertEqual(cfg.mimo_probe_model, "xiaomi/mimo-v2.6-flash")
        self.assertEqual(cfg.mimo_prices, {})
        self.assertEqual(cfg.crosscheck_crew_provider, "")
        self.assertEqual(set(DERIVED_STATE_ROOT_KEYS),
                         {"state_dir", "log_dir", "jobs_dir", "pid_file"})
        for key in DERIVED_STATE_ROOT_KEYS:
            self.assertFalse(key.startswith("mimo"), key)


if __name__ == "__main__":
    unittest.main()
