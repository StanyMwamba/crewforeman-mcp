"""Tests for deepseek_mcp.mimo_concurrency (MiMo concurrency detection + backoff).

Isolation contract: no test may read the operator's real
``~/.config/mimocode/mimocode.jsonc``.  ``setUp`` therefore points the module's
``GLOBAL_CONFIG_PATH`` constant at a temp file and every call either passes
``global_path=`` explicitly or relies on that patched constant.  All temp files
use ``tempfile`` (redirected under the case dir by the unittest isolation hook)
and ``note_limit_error`` state goes only under ``cfg.state_dir``.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp import mimo_concurrency


def _expected_default():
    return min(16, 2 * (os.cpu_count() or 1))


class _AttrCfg(object):
    """Minimal attribute-style config exercising the ``getattr`` path."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _write_jsonc(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_limit(path, value):
    return _write_jsonc(path, json.dumps(
        {"workflow": {"maxConcurrentAgents": value}}, indent=2))


class ReadMimoLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.global_cfg = self.root / "global" / "mimocode.jsonc"
        self.project_dir = self.root / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.project_cfg = self.project_dir / "mimocode.jsonc"
        # Guarantee the operator's real ~/.config is never consulted, even if a
        # call forgets to pass global_path=.
        self._old_global = mimo_concurrency.GLOBAL_CONFIG_PATH
        mimo_concurrency.GLOBAL_CONFIG_PATH = str(
            self.root / "guarded" / "mimocode.jsonc")
        self.addCleanup(
            setattr, mimo_concurrency, "GLOBAL_CONFIG_PATH", self._old_global)

    def read(self, cfg=None):
        return mimo_concurrency.read_mimo_limit(
            cfg, project_dir=str(self.project_dir),
            global_path=str(self.global_cfg))

    # ----------------------------------------------------------- max / fallback

    def test_global_and_project_max_wins(self):
        _write_limit(self.global_cfg, 8)
        _write_limit(self.project_cfg, 12)
        result = self.read()
        self.assertEqual(result["limit"], 12)
        self.assertEqual(result["source"], "project_config")

    def test_project_and_global_max_wins_regardless_of_order(self):
        _write_limit(self.global_cfg, 12)
        _write_limit(self.project_cfg, 8)
        result = self.read()
        self.assertEqual(result["limit"], 12)
        self.assertEqual(result["source"], "global_config")

    def test_only_global_uses_that_value(self):
        _write_limit(self.global_cfg, 8)
        result = self.read()
        self.assertEqual(result["limit"], 8)
        self.assertEqual(result["source"], "global_config")

    def test_only_project_uses_that_value(self):
        _write_limit(self.project_cfg, 7)
        result = self.read()
        self.assertEqual(result["limit"], 7)
        self.assertEqual(result["source"], "project_config")

    def test_no_configs_uses_builtin_default(self):
        result = self.read()
        self.assertEqual(result["limit"], _expected_default())
        self.assertEqual(result["source"], "default")
        self.assertEqual(result["values_seen"]["default"], _expected_default())

    def test_default_limit_formula(self):
        self.assertEqual(mimo_concurrency.DEFAULT_LIMIT(4), 8)
        self.assertEqual(mimo_concurrency.DEFAULT_LIMIT(32), 16)
        self.assertEqual(mimo_concurrency.default_limit(), _expected_default())

    def test_configured_limit_below_default_is_respected(self):
        # The built-in default is a fallback, not a floor: an explicit
        # configured cap of 1 must win on every machine.
        _write_limit(self.global_cfg, 1)
        result = self.read()
        self.assertEqual(result["limit"], 1)
        self.assertEqual(result["source"], "global_config")

    # -------------------------------------------------------------- cfg override

    def test_cfg_override_wins(self):
        _write_limit(self.global_cfg, 8)
        _write_limit(self.project_cfg, 12)
        result = self.read({"mimo_max_concurrent_agents": 6})
        self.assertEqual(result["limit"], 6)
        self.assertEqual(result["source"], "config")
        self.assertEqual(result["values_seen"]["config"], 6)

    def test_cfg_override_wins_attribute_style_cfg(self):
        _write_limit(self.global_cfg, 8)
        cfg = _AttrCfg(mimo_max_concurrent_agents=3)
        result = self.read(cfg)
        self.assertEqual(result["limit"], 3)
        self.assertEqual(result["source"], "config")

    def test_cfg_override_zero_is_auto_detect(self):
        # W7 defaults the key to 0 ("auto-detect"); 0 must not win.
        _write_limit(self.global_cfg, 8)
        result = self.read({"mimo_max_concurrent_agents": 0})
        self.assertEqual(result["limit"], 8)
        self.assertEqual(result["source"], "global_config")

    def test_invalid_values_are_absent(self):
        for raw in (-3, "lots", True, None):
            with self.subTest(raw=raw):
                _write_limit(self.global_cfg, raw)
                result = self.read()
                self.assertEqual(result["limit"], _expected_default())
                self.assertEqual(result["source"], "default")

    # --------------------------------------------------------------------- JSONC

    def test_jsonc_comments_and_trailing_commas_parse(self):
        _write_jsonc(self.project_cfg, """{
  // line comment with a URL https://example.invalid/a,b
  "$schema": "https://mimo.xiaomi.com/mimocode/config.json",
  /* block
     comment */
  "workflow": {
    "maxDepth": 3,
    "maxConcurrentAgents": 10, // trailing line comment
  },
  "permission": {
    "read": "allow",
  },
}
""")
        result = self.read()
        self.assertEqual(result["limit"], 10)
        self.assertEqual(result["source"], "project_config")

    def test_plain_json_and_bom_parse(self):
        _write_jsonc(self.project_cfg, "\ufeff" + json.dumps(
            {"workflow": {"maxConcurrentAgents": 6}}))
        result = self.read()
        self.assertEqual(result["limit"], 6)

    def test_unparseable_file_is_absent(self):
        _write_jsonc(self.global_cfg, "{not json")
        result = self.read()
        self.assertEqual(result["limit"], _expected_default())
        self.assertEqual(result["source"], "default")

    # ------------------------------------------------------- zero / values_seen

    def test_zero_treated_as_absent_and_noted(self):
        _write_limit(self.project_cfg, 0)
        result = self.read()
        self.assertEqual(result["limit"], _expected_default())
        self.assertEqual(result["source"], "default")
        self.assertEqual(result["values_seen"].get("project_config"), 0)

    def test_zero_does_not_block_other_values(self):
        _write_limit(self.global_cfg, 0)
        _write_limit(self.project_cfg, 5)
        result = self.read()
        self.assertEqual(result["limit"], 5)
        self.assertEqual(result["source"], "project_config")
        self.assertEqual(result["values_seen"].get("global_config"), 0)

    def test_values_seen_records_all_sources(self):
        _write_limit(self.global_cfg, 8)
        _write_limit(self.project_cfg, 12)
        result = self.read({"mimo_max_concurrent_agents": 0})
        seen = result["values_seen"]
        self.assertEqual(seen["global_config"], 8)
        self.assertEqual(seen["project_config"], 12)
        self.assertEqual(seen["config"], 0)
        self.assertEqual(seen["default"], _expected_default())

    # -------------------------------------------------------- read-only guarantee

    def test_read_only_never_creates_missing_configs(self):
        result = self.read()
        self.assertEqual(result["source"], "default")
        self.assertFalse(self.global_cfg.exists())
        self.assertFalse(self.project_cfg.exists())
        self.assertEqual(list((self.root / "guarded").glob("*")), [])

    def test_read_only_leaves_configs_unchanged(self):
        _write_limit(self.global_cfg, 8)
        _write_limit(self.project_cfg, 12)
        before = {}
        for path in (self.global_cfg, self.project_cfg):
            raw = path.read_bytes()
            stat = path.stat()
            before[str(path)] = (raw, stat.st_mtime_ns, stat.st_size)
        for _ in range(3):
            self.read()
        mimo_concurrency.note_limit_error(
            {"state_dir": str(self.root / "state")},
            project_dir=str(self.project_dir),
            global_path=str(self.global_cfg))
        for path in (self.global_cfg, self.project_cfg):
            raw = path.read_bytes()
            stat = path.stat()
            self.assertEqual(before[str(path)],
                             (raw, stat.st_mtime_ns, stat.st_size))

    def test_global_config_path_constant_is_honored(self):
        _write_limit(Path(mimo_concurrency.GLOBAL_CONFIG_PATH), 7)
        result = mimo_concurrency.read_mimo_limit(
            project_dir=str(self.project_dir))
        self.assertEqual(result["limit"], 7)
        self.assertEqual(result["source"], "global_config")

    # ---------------------------------------------------------------- convenience

    def test_get_concurrency_returns_int(self):
        _write_limit(self.global_cfg, 8)
        _write_limit(self.project_cfg, 12)
        value = mimo_concurrency.get_concurrency(
            project_dir=str(self.project_dir),
            global_path=str(self.global_cfg))
        self.assertIsInstance(value, int)
        self.assertEqual(value, 12)


class NoteLimitErrorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.global_cfg = self.root / "global" / "mimocode.jsonc"
        self.project_dir = self.root / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self._old_global = mimo_concurrency.GLOBAL_CONFIG_PATH
        mimo_concurrency.GLOBAL_CONFIG_PATH = str(
            self.root / "guarded" / "mimocode.jsonc")
        self.addCleanup(
            setattr, mimo_concurrency, "GLOBAL_CONFIG_PATH", self._old_global)

    def note(self, cfg, source="limit_error"):
        return mimo_concurrency.note_limit_error(
            cfg, source=source, project_dir=str(self.project_dir),
            global_path=str(self.global_cfg))

    def test_writes_under_state_dir_and_halves(self):
        cfg = {"state_dir": str(self.state_dir),
               "mimo_max_concurrent_agents": 6}
        result = self.note(cfg)
        self.assertEqual(result["previous"], 6)
        self.assertEqual(result["suggested_limit"], 3)
        self.assertIsNotNone(result["path"])
        path = Path(result["path"])
        self.assertEqual(path.parent, self.state_dir)
        self.assertEqual(path.name, mimo_concurrency.STATE_FILENAME)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["previous_limit"], 6)
        self.assertEqual(record["suggested_limit"], 3)
        self.assertEqual(record["source"], "limit_error")
        self.assertEqual(record["recorded_at"], result["recorded_at"])

    def test_floor_is_one(self):
        cfg = {"state_dir": str(self.state_dir),
               "mimo_max_concurrent_agents": 1}
        result = self.note(cfg)
        self.assertEqual(result["previous"], 1)
        self.assertEqual(result["suggested_limit"], 1)

    def test_halves_config_file_limit_when_no_override(self):
        _write_limit(self.global_cfg, 10)
        cfg = {"state_dir": str(self.state_dir)}
        result = self.note(cfg)
        self.assertEqual(result["previous"], 10)
        self.assertEqual(result["suggested_limit"], 5)

    def test_custom_source_recorded(self):
        cfg = {"state_dir": str(self.state_dir),
               "mimo_max_concurrent_agents": 4}
        result = self.note(cfg, source="limit_429")
        record = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["source"], "limit_429")

    def test_empty_state_dir_returns_suggestion_without_writing(self):
        _write_limit(self.global_cfg, 4)
        for cfg in ({}, {"state_dir": ""}, {"state_dir": None}):
            with self.subTest(cfg=cfg):
                result = self.note(cfg)
                self.assertEqual(result["previous"], 4)
                self.assertEqual(result["suggested_limit"], 2)
                self.assertIsNone(result["path"])
        self.assertFalse(self.global_cfg.parent.joinpath(
            mimo_concurrency.STATE_FILENAME).exists())
        self.assertFalse(self.project_dir.joinpath(
            mimo_concurrency.STATE_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
