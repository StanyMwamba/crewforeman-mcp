import datetime as dt
import json
import plistlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import concurrency
from deepseek_mcp.config import load_config


def _write_asar(path, files):
    """Write a minimal asar containing plain files (path -> text)."""
    tree = {"files": {}}
    payload = b""
    for inner, text in files.items():
        parts = inner.split("/")
        node = tree
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        data = text.encode("utf-8")
        node["files"][parts[-1]] = {"size": len(data), "offset": str(len(payload))}
        payload += data
    raw = json.dumps(tree).encode("utf-8")
    header_size = len(raw) + 5
    # The reader starts file data at 12 + header_size.  Build a layout that
    # matches the shipped asar closely enough for the parser.
    prefix = struct.pack("<IIII", 4, header_size + 4, header_size, len(raw))
    body = raw + b"\x00"
    body += b"\x00" * max(0, (12 + header_size) - (len(prefix) + len(body)))
    blob = prefix + body + payload
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)


def _fake_app(root, subagent_default=6):
    app = Path(root) / "Test Harness.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": "9.9.9"}, handle)
    dsh = app / "Contents" / "Resources" / "runtime" / "cli" / "bin" / "dsh"
    dsh.parent.mkdir(parents=True)
    dsh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _write_asar(resources / "app.asar", {
        "dsh/node_modules/@deepseek-ai/dsh-subagent/README.md":
            "Set `maxActiveSubagents` on the host plugin. It defaults to `%d` and accepts "
            "positive safe integers." % subagent_default,
        "dsh/node_modules/@deepseek-ai/dsh-workflow-ptc/README.md":
            "| `maxConcurrentAgents` | `0` | Concurrent `agent()` ceiling. |\n",
    })
    return dsh


class AsarAndDocsTests(unittest.TestCase):
    def test_asar_reader_and_documented_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            dsh = _fake_app(tmp)
            cfg = load_config(config_path=Path(tmp) / "config.json", env={
                "DSH_BIN": str(dsh), "DSH_STATE_DIR": str(Path(tmp) / "state")})
            text = concurrency._asar_read_text(
                concurrency.app_asar(cfg),
                "dsh/node_modules/@deepseek-ai/dsh-subagent/README.md")
            self.assertIn("maxActiveSubagents", text)
            self.assertEqual(concurrency.harness_version(cfg), "9.9.9")
            docs = concurrency.discover_documented(cfg)
            self.assertEqual(docs["limit"], 6)
            self.assertEqual(docs["subagent_limit"], 6)
            self.assertEqual(docs["source"], "documented")


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config(config_path=self.root / "config.json", env={
            "DSH_STATE_DIR": str(self.root / "state")})

    def detect(self, docs=None, version="1.0", **kwargs):
        docs = {"limit": 8, "subagent_limit": 8, "engine_limit": 9,
                "source": "documented"} if docs is None else docs
        with mock.patch.object(concurrency, "harness_version", return_value=version), \
                mock.patch.object(concurrency, "discover_documented", return_value=docs):
            return concurrency.detect(self.cfg, **kwargs)

    def test_cache_is_written_and_reused(self):
        first = self.detect()
        self.assertEqual(first["limit"], 8)
        self.assertEqual(first["source"], "documented")
        self.assertTrue(concurrency.cache_path(self.cfg).is_file())
        second = self.detect(docs={"limit": 99, "limit": 99}, version="1.0")
        self.assertEqual(second["limit"], 8)
        self.assertTrue(second["cached"])

    def test_cache_expires_after_seven_days(self):
        self.detect()
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
        fresh = self.detect(docs={"limit": 12, "subagent_limit": 12,
                                  "engine_limit": 16}, now=later)
        self.assertEqual(fresh["limit"], 12)
        self.assertFalse(fresh["cached"])

    def test_version_change_forces_redetection(self):
        self.detect(version="1.0")
        changed = self.detect(docs={"limit": 10, "subagent_limit": 10,
                                    "engine_limit": 16}, version="2.0")
        self.assertEqual(changed["limit"], 10)

    def test_config_override_wins_and_skips_cache(self):
        cfg = load_config(config_path=self.root / "config.json", env={
            "DSH_STATE_DIR": str(self.root / "state"),
            "DSH_MAX_CONCURRENT_CHILDREN": "5"})
        result = concurrency.detect(cfg)
        self.assertEqual(result["limit"], 5)
        self.assertEqual(result["source"], "config")
        self.assertFalse(concurrency.cache_path(cfg).exists())

    def test_probe_fallback_when_docs_missing(self):
        probe = {"limit": 7, "source": "probe", "details": {"max_active": 7}}
        with mock.patch.object(concurrency, "harness_version", return_value="1.0"), \
                mock.patch.object(concurrency, "discover_documented", return_value={}), \
                mock.patch.object(concurrency, "probe_concurrency", return_value=probe):
            result = concurrency.detect(self.cfg)
        self.assertEqual(result["limit"], 7)
        self.assertEqual(result["source"], "probe")

    def test_force_probe_skips_documented_value(self):
        probe = {"limit": 6, "source": "probe", "details": {"max_active": 6}}
        with mock.patch.object(concurrency, "harness_version", return_value="1.0"), \
                mock.patch.object(concurrency, "discover_documented",
                                  return_value={"limit": 8}), \
                mock.patch.object(concurrency, "probe_concurrency", return_value=probe):
            result = concurrency.detect(self.cfg, force_probe=True)
        self.assertEqual(result["limit"], 6)
        self.assertEqual(result["source"], "probe")

    def test_probe_failure_falls_back_to_documented_then_eight(self):
        with mock.patch.object(concurrency, "harness_version", return_value="1.0"), \
                mock.patch.object(concurrency, "discover_documented",
                                  return_value={"limit": 8}), \
                mock.patch.object(concurrency, "probe_concurrency",
                                  return_value={"limit": None, "error": "no key"}):
            result = concurrency.detect(self.cfg, force_probe=True)
        self.assertEqual(result["limit"], 8)
        self.assertEqual(result["source"], "documented")
        concurrency.invalidate(self.cfg)
        with mock.patch.object(concurrency, "harness_version", return_value="1.0"), \
                mock.patch.object(concurrency, "discover_documented", return_value={}), \
                mock.patch.object(concurrency, "probe_concurrency",
                                  return_value={"limit": None, "error": "no key"}):
            fallback = concurrency.detect(self.cfg)
        self.assertEqual(fallback["limit"], 8)
        self.assertEqual(fallback["source"], "fallback")

    def test_agent_limit_note_invalidates_and_redetects(self):
        self.detect()
        self.assertTrue(concurrency.cache_path(self.cfg).is_file())
        probe = {"limit": 6, "source": "probe", "details": {"max_active": 6}}
        with mock.patch.object(concurrency, "harness_version", return_value="1.0"), \
                mock.patch.object(concurrency, "discover_documented",
                                  return_value={"limit": 8}), \
                mock.patch.object(concurrency, "probe_concurrency", return_value=probe):
            result = concurrency.note_agent_limit(self.cfg)
        self.assertEqual(result["limit"], 6)
        self.assertEqual(result["source"], "probe")


class ProbeParserTests(unittest.TestCase):
    def test_probe_value_detects_limit_error(self):
        value, details = concurrency._probe_value({
            "maxActive": 8, "started": 16,
            "failures": ["ACTIVATION_LIMIT_REACHED: active child limit: 8"]})
        self.assertEqual(value, 8)
        self.assertTrue(details["limit_error"])

    def test_probe_result_is_extracted_from_text(self):
        text = 'workflow result: {"maxActive": 5, "started": 5, "failures": []}'
        result = concurrency._extract_probe_result(text)
        self.assertEqual(result["maxActive"], 5)

    def test_probe_runner_is_injectable(self):
        cfg = load_config(config_path="/nonexistent/config.json", env={})
        calls = []

        def runner(c, n):
            calls.append((c, n))
            return {"limit": 4}

        result = concurrency.probe_concurrency(cfg, runner=runner, n=9)
        self.assertEqual(result["limit"], 4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 9)


if __name__ == "__main__":
    unittest.main()
