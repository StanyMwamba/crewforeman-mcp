import datetime as dt
import json
import os
import plistlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import concurrency
from deepseek_mcp.config import load_config
from tests.fakes import write_fake_script


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


def _pinned_cfg(root, **extra_env):
    """load_config with every write root pinned inside ``root``.

    ``load_config(env=explicit)`` ignores ``os.environ``, so hand-built env
    dicts must carry the state/log/jobs roots themselves — otherwise
    ``cfg.log_dir`` falls back to the real ``~/DeepSeek-MCP/runs`` and any
    store write pollutes the operator's home.
    """
    root = Path(root)
    env = {"CREWFOREMAN_STATE_DIR": str(root / "cf-state")}
    env.update(extra_env)
    cfg = load_config(config_path=root / "config.json", env=env)
    for key in ("state_dir", "log_dir", "jobs_dir", "pid_file"):
        target = Path(os.path.expanduser(str(cfg.get(key))))
        if not str(target).startswith(str(root)):
            raise AssertionError(
                "fixture write root escaped the temp dir: %s -> %s" % (key, target))
    return cfg


def _fake_dsh(root, lines, name="fake-dsh", files=None):
    """Tiny fake dsh that writes ``files`` into its cwd, prints recorded
    ``--json`` event lines and exits 0 — never a real Harness."""
    body = (
        "#!" + sys.executable + "\n"
        "import os, sys\n"
        "for _name, _data in %r:\n"
        "    with open(os.path.join(os.getcwd(), _name), 'w') as _handle:\n"
        "        _handle.write(_data)\n"
        "for _line in %r:\n"
        "    sys.stdout.write(_line + chr(10))\n"
        % (sorted((files or {}).items()), list(lines))
    )
    return Path(write_fake_script(Path(root) / name, body))


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
            cfg = _pinned_cfg(tmp, DSH_BIN=str(dsh))
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
        self.cfg = _pinned_cfg(self.root)

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
        cfg = _pinned_cfg(self.root, DSH_MAX_CONCURRENT_CHILDREN="5")
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
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _pinned_cfg(tmp)
            calls = []

            def runner(c, n):
                calls.append((c, n))
                return {"limit": 4}

            result = concurrency.probe_concurrency(cfg, runner=runner, n=9)
        self.assertEqual(result["limit"], 4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 9)


class ProbeProcessTests(unittest.TestCase):
    """Regression tests for the DSH probe bug (probe produced no readable
    result being cached as ``source: fallback``).

    A tiny inline fake dsh emits realistic ``--json`` event streams — never a
    real Harness — including the shapes the old parser missed: final-ish event
    types other than ``final``, answer keys other than text/answer, and probe
    JSON escaped inside a string value of a JSON event line.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_detect(self, lines, **detect_kwargs):
        dsh = _fake_dsh(self.root, lines)
        cfg = _pinned_cfg(self.root, DSH_BIN=str(dsh))
        return concurrency.detect(cfg, **detect_kwargs), cfg

    def test_probe_reads_final_result_from_escaped_text(self):
        # The probe JSON arrives escaped inside the string value of a text
        # event — the common shape for streamed assistant text in --json mode.
        # Old code: type != "final" leaves final=None and the raw-stdout scan
        # cannot raw_decode the escaped inner object -> "probe produced no
        # readable result".  The limit is deliberately not FALLBACK_CONCURRENCY
        # so a fallback cannot masquerade as a probed value.
        stream = [
            '{"type": "step_start", "data": {"name": "workflow"}}',
            '{"type": "text", "data": {"text": "Running the probe..."}}',
            '{"type": "text", "text": "CONCURRENCY_PROBE {\\"maxActive\\": 3, '
            '\\"started\\": 16, \\"failures\\": []}"}',
            '{"type": "done", "data": {"output": "finished"}}',
        ]
        result, cfg = self.run_detect(stream)
        self.assertEqual(result["source"], "probe")
        self.assertEqual(result["limit"], 3)
        self.assertIsNone(result.get("probe_error"))
        cached = concurrency.read_cache(cfg)
        self.assertEqual(cached["source"], "probe")
        self.assertEqual(cached["max_concurrent_children"], 3)

    def test_probe_reads_result_type_final_with_answer_keys(self):
        # Real harness final-ish types are final/result/done/completion/
        # completed (harness.py FINAL_TYPES) and the answer may live under
        # text/answer/message/content/result — old code only accepted
        # type=="final" with text/answer.
        stream = [
            '{"type": "assistant/message", "data": {"message": {"role": '
            '"assistant", "content": "working"}}}',
            '{"type": "result", "result": "CONCURRENCY_PROBE {\\"maxActive\\": '
            '5, \\"started\\": 16, \\"failures\\": []}"}',
        ]
        result, _cfg = self.run_detect(stream)
        self.assertEqual(result["source"], "probe")
        self.assertEqual(result["limit"], 5)

    def test_probe_reads_completion_type_final_under_data(self):
        stream = [
            '{"type": "completion", "data": {"content": "CONCURRENCY_PROBE '
            '{\\"maxActive\\": 6, \\"started\\": 16, \\"failures\\": []}"}}',
        ]
        result, _cfg = self.run_detect(stream)
        self.assertEqual(result["source"], "probe")
        self.assertEqual(result["limit"], 6)

    def test_probe_falls_back_to_probe_file_when_stream_unreadable(self):
        # The prompt asks the model to write concurrency-probe.json into the
        # probe workspace (the dsh cwd); that channel must stay usable.
        dsh = _fake_dsh(
            self.root, ['{"type": "final", "text": "done"}'],
            files={"concurrency-probe.json": json.dumps(
                {"maxActive": 7, "started": 16, "failures": []})})
        cfg = _pinned_cfg(self.root, DSH_BIN=str(dsh))
        with mock.patch.object(concurrency, "discover_documented",
                               return_value={}):
            result = concurrency.detect(cfg)
        self.assertEqual(result["source"], "probe")
        self.assertEqual(result["limit"], 7)

    def test_failed_probe_is_not_cached_as_truth(self):
        # A probe that produced no readable result must not become a permanent
        # cached truth: the soft default is returned for this call, but the
        # next detect() re-probes instead of serving the stale fallback.
        bad_stream = ['{"type": "final", "text": "no numbers here"}']
        first, cfg = self.run_detect(bad_stream)
        self.assertEqual(first["limit"], concurrency.FALLBACK_CONCURRENCY)
        self.assertEqual(first["source"], "fallback")
        self.assertTrue(first.get("probe_error"))
        # Nothing authoritative was written: at most a non-authoritative
        # record carrying the probe error.
        cached = concurrency.read_cache(cfg)
        self.assertTrue(
            cached is None or cached.get("source") != "probe",
            "failed probe must not be cached as detection truth")
        if cached is not None:
            self.assertTrue(
                cached.get("probe", {}).get("probe_error")
                or cached.get("probe_error"),
                "cached fallback must record why the probe failed")

        def good_runner(c, n):
            return {"limit": 5, "details": {"max_active": 5}}

        with mock.patch.object(concurrency, "discover_documented",
                               return_value={}):
            second = concurrency.detect(cfg, probe_runner=good_runner)
        self.assertEqual(second["limit"], 5)
        self.assertEqual(second["source"], "probe")

    def test_failed_probe_still_soft_default_without_probe_runner(self):
        # Even without an injectable runner, a failed probe returns the soft
        # fallback for this call and leaves no authoritative cache behind.
        first, cfg = self.run_detect(['{"type": "error", "error": "boom"}'])
        self.assertEqual(first["limit"], concurrency.FALLBACK_CONCURRENCY)
        self.assertEqual(first["source"], "fallback")
        self.assertTrue(first.get("probe_error"))
        cached = concurrency.read_cache(cfg)
        self.assertTrue(
            cached is None or cached.get("source") != "fallback"
            or cached.get("probe", {}).get("probe_error"))


if __name__ == "__main__":
    unittest.main()
