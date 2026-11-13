"""Smoke tests for ``tests.mimo_fakes`` — the fake ``mimo`` programs.

Each mode must emit the documented JSONL event stream (see the module
docstring of ``tests.mimo_fakes``), ``serve`` must answer ``GET /healthz``,
``attach`` must record argv, and the error modes must carry the right
``statusCode``.  No network beyond 127.0.0.1, no real ``mimo``, and no long
sleeps (``FAKE_MIMO_SLEEP`` is kept small).
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests import build_fake_mimo, tempdir
from tests.mimo_fakes import base_env, write_fake_mimo

TOKEN_KEYS = ("total", "input", "output", "reasoning", "cache")
FINAL_ANSWER = "Fake mimo final answer: done."


def parse_jsonl(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


class MimoFakesSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempdir())
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.fake = write_fake_mimo(self.root / "bin" / "mimo")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra):
        env = dict(os.environ)
        env.update(base_env(self.root, fake_mimo=self.fake, **extra))
        return env

    def _argv(self, *extra):
        args = ["run", "Write one note.", "--format", "json",
                "--dir", str(self.ws), "-m", "xiaomi/mimo-v2.6-pro"]
        args.extend(extra)
        return args

    def _run(self, args, env=None, timeout=20):
        return subprocess.run([str(self.fake)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True,
                              env=env or self._env(), timeout=timeout,
                              cwd=str(self.ws))

    # -- fixtures ---------------------------------------------------------

    def test_write_fake_mimo_returns_executable(self):
        self.assertTrue(self.fake.exists())
        if os.name != "nt":
            self.assertTrue(os.access(str(self.fake), os.X_OK))
            self.assertEqual(self.fake, self.root / "bin" / "mimo")

    def test_base_env_keys_are_isolated_under_root(self):
        env = base_env(self.root, fake_mimo=self.fake)
        for key in ("DEEPSEEK_MCP_CONFIG", "DEEPSEEK_MCP_LOG_DIR",
                    "DEEPSEEK_MCP_JOBS_DIR", "DEEPSEEK_MCP_PID_FILE",
                    "DSH_SKILLS_DIR", "DSH_WORKSPACE", "DSH_SESSIONS_DIR",
                    "DSH_STATE_DIR", "DEEPSEEK_API_KEY", "DSH_OS_SANDBOX",
                    "DSH_REVIEW_PREFLIGHT", "MIMO_BIN", "FAKE_MIMO_ARGV",
                    "FAKE_MIMO_MODE", "FAKE_MIMO_SLEEP",
                    "FAKE_MIMO_SERVE_PORT", "FAKE_MIMO_SESSIONS",
                    "FAKE_MIMO_SERVE_URL", "FAKE_MIMO_STATE"):
            self.assertIn(key, env, "missing env key %r" % key)
        self.assertEqual(env["MIMO_BIN"], str(self.fake))
        self.assertEqual(env["DSH_OS_SANDBOX"], "0")
        self.assertEqual(env["DSH_REVIEW_PREFLIGHT"], "0")
        for key in ("DEEPSEEK_MCP_CONFIG", "DEEPSEEK_MCP_LOG_DIR",
                    "DEEPSEEK_MCP_JOBS_DIR", "DEEPSEEK_MCP_PID_FILE",
                    "DSH_SKILLS_DIR", "DSH_WORKSPACE", "DSH_SESSIONS_DIR",
                    "DSH_STATE_DIR", "FAKE_MIMO_ARGV",
                    "FAKE_MIMO_SESSIONS", "FAKE_MIMO_SERVE_URL",
                    "FAKE_MIMO_STATE"):
            self.assertTrue(str(env[key]).startswith(str(self.root)),
                            "%s=%s escapes the temp root" % (key, env[key]))
        overridden = base_env(self.root, fake_mimo=self.fake,
                              FAKE_MIMO_MODE="slow", EXTRA_KEY="1")
        self.assertEqual(overridden["FAKE_MIMO_MODE"], "slow")
        self.assertEqual(overridden["EXTRA_KEY"], "1")

    def test_build_fake_mimo_launcher_runs_script(self):
        script = self.root / "tiny.py"
        script.write_text("import sys\nprint('launcher-ok')\nsys.exit(0)\n",
                          encoding="utf-8")
        try:
            launcher = build_fake_mimo.build_launcher(script)
        except RuntimeError as exc:
            raise unittest.SkipTest(
                "environment-only: cannot build the launcher: %s" % exc)
        out = subprocess.run([str(launcher)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True,
                             timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("launcher-ok", out.stdout)

    # -- event-stream modes ----------------------------------------------

    def test_ok_mode_emits_documented_events(self):
        result = self._run(self._argv())
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        self.assertTrue(events, "no JSONL events on stdout")
        self.assertEqual(events[0]["type"], "text")
        self.assertIn("text", events[0]["data"])
        starts = [e for e in events if e["type"] == "step_start"]
        finishes = [e for e in events if e["type"] == "step_finish"]
        tools = [e for e in events if e["type"] == "tool_use"]
        self.assertGreaterEqual(len(starts), 2, "ok must include a couple of steps")
        self.assertGreaterEqual(len(finishes), 2, "token accumulation needs steps")
        for event in finishes:
            tokens = event["data"]["tokens"]
            for key in TOKEN_KEYS:
                self.assertIn(key, tokens)
        totals = [e["data"]["tokens"]["total"] for e in finishes]
        self.assertGreater(sum(totals), max(totals),
                           "step_finish usage must accumulate across steps")
        for event in tools:
            self.assertIn("name", event["data"])
            self.assertIn("input", event["data"])
        self.assertEqual(events[-1]["type"], "text")
        self.assertEqual(events[-1]["data"]["text"], FINAL_ANSWER)

    def test_text_only_mode_emits_only_text(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-text", mode="text_only")
        result = subprocess.run(
            [str(fake)] + self._argv(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
            env=self._env(), timeout=20, cwd=str(self.ws))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["type"], "text")
        self.assertEqual(events[-1]["data"]["text"], FINAL_ANSWER)

    def test_verbose_mode_has_extra_tool_use(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-verbose", mode="verbose")
        ok_events = parse_jsonl(self._run(self._argv()).stdout)
        verbose_events = parse_jsonl(subprocess.run(
            [str(fake)] + self._argv(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
            env=self._env(), timeout=20, cwd=str(self.ws)).stdout)
        ok_tools = [e for e in ok_events if e["type"] == "tool_use"]
        verbose_tools = [e for e in verbose_events if e["type"] == "tool_use"]
        self.assertGreaterEqual(len(verbose_tools), 5,
                                "verbose must emit extra tool_use events")
        self.assertGreater(len(verbose_tools), len(ok_tools))
        self.assertEqual(verbose_events[-1]["data"]["text"], FINAL_ANSWER)

    def test_slow_mode_honours_fake_mimo_sleep(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-slow", mode="slow")
        env = self._env(FAKE_MIMO_SLEEP="0.5")
        started = time.time()
        result = subprocess.run([str(fake)] + self._argv(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=env, timeout=20,
                                cwd=str(self.ws))
        elapsed = time.time() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(elapsed, 0.3, "slow must honour FAKE_MIMO_SLEEP")
        events = parse_jsonl(result.stdout)
        types = [e["type"] for e in events]
        self.assertIn("text", types)
        self.assertIn("step_start", types)
        self.assertIn("step_finish", types)
        self.assertEqual(events[-1]["type"], "text")
        self.assertEqual(events[-1]["data"]["text"], FINAL_ANSWER)

    def test_silent_mode_emits_nothing_and_stalls(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-silent", mode="silent")
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run([str(fake)] + self._argv(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=self._env(),
                           timeout=1.2, cwd=str(self.ws))

    def test_flaky_mode_fails_once_then_succeeds(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-flaky", mode="flaky")
        first = subprocess.run([str(fake)] + self._argv(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, env=self._env(),
                               timeout=20, cwd=str(self.ws))
        self.assertEqual(first.returncode, 1)
        events = parse_jsonl(first.stdout)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("ECONNRESET", events[0]["error"]["data"]["message"])
        second = subprocess.run([str(fake)] + self._argv(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=self._env(),
                                timeout=20, cwd=str(self.ws))
        self.assertEqual(second.returncode, 0, second.stderr)
        events = parse_jsonl(second.stdout)
        self.assertEqual(events[-1]["data"]["text"], FINAL_ANSWER)
        self.assertTrue(any(e["type"] == "step_start" for e in events))

    def test_env_mode_overrides_baked_mode(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-override",
                               mode="text_only")
        events = parse_jsonl(subprocess.run(
            [str(fake)] + self._argv(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
            env=self._env(FAKE_MIMO_MODE="ok"), timeout=20,
            cwd=str(self.ws)).stdout)
        self.assertTrue(any(e["type"] == "step_start" for e in events),
                        "non-empty FAKE_MIMO_MODE must override the baked mode")

    def test_ping_task_answers_ready(self):
        result = self._run(["run", "Reply with exactly READY and nothing else.",
                            "--format", "json", "--dir", str(self.ws),
                            "-m", "xiaomi/mimo-v2.6-flash"])
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        self.assertEqual(events[-1]["type"], "text")
        self.assertEqual(events[-1]["data"]["text"], "READY")

    # -- error modes ------------------------------------------------------

    def test_error_auth_carries_status_code_401(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-auth", mode="error_auth")
        result = subprocess.run([str(fake)] + self._argv(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=self._env(),
                                timeout=20, cwd=str(self.ws))
        self.assertEqual(result.returncode, 1)
        events = parse_jsonl(result.stdout)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        data = events[0]["error"]["data"]
        self.assertEqual(data["statusCode"], 401)
        self.assertIn("Unauthorized", data["message"])
        self.assertIn("api key", data["message"])

    def test_error_limit_carries_status_code_429(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-limit", mode="error_limit")
        result = subprocess.run([str(fake)] + self._argv(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=self._env(),
                                timeout=20, cwd=str(self.ws))
        self.assertEqual(result.returncode, 1)
        events = parse_jsonl(result.stdout)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        data = events[0]["error"]["data"]
        self.assertEqual(data["statusCode"], 429)
        self.assertIn("rate limit", data["message"].lower())

    def test_error_network_message_and_exit(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-net", mode="error_network")
        result = subprocess.run([str(fake)] + self._argv(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=self._env(),
                                timeout=20, cwd=str(self.ws))
        self.assertEqual(result.returncode, 1)
        events = parse_jsonl(result.stdout)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        message = events[0]["error"]["data"]["message"]
        self.assertIn("fetch failed: ECONNRESET", message)
        self.assertIn("network", message.lower())

    # -- attach / serve / argv capture -----------------------------------

    def test_attach_mode_records_argv_and_behaves_like_ok(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-attach", mode="attach")
        env = self._env()
        args = self._argv("--variant", "v1", "-s", "sess-7",
                          "--attach", "http://127.0.0.1:9",
                          "--dangerously-skip-permissions")
        result = subprocess.run([str(fake)] + args,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=env, timeout=20,
                                cwd=str(self.ws))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        self.assertEqual(events[-1]["data"]["text"], FINAL_ANSWER)
        self.assertTrue(any(e["type"] == "step_finish" for e in events))
        recorded = json.loads(Path(env["FAKE_MIMO_ARGV"]).read_text(
            encoding="utf-8"))
        self.assertEqual(recorded[1:], args)

    def test_serve_subcommand_answers_healthz(self):
        self._check_serve(["serve", "--host", "127.0.0.1", "--port", "0"],
                          self._env())

    def test_serve_mode_answers_healthz(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-serve", mode="serve")
        self._check_serve(["run", "ignored", "--format", "json",
                           "--dir", str(self.ws), "-m", "xiaomi/mimo-v2.6-pro"],
                          self._env(), fake=fake)

    def test_session_record_when_sessions_dir_set(self):
        env = self._env()
        result = self._run(self._argv("-s", "sess-42"), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = sorted(Path(env["FAKE_MIMO_SESSIONS"]).glob("session-*.json"))
        self.assertTrue(records, "FAKE_MIMO_SESSIONS must receive a record")
        data = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(data["session"], "sess-42")

    def _check_serve(self, args, env, fake=None):
        fake = fake or self.fake
        proc = subprocess.Popen([str(fake)] + args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                universal_newlines=True, env=env,
                                cwd=str(self.ws))
        try:
            capture = Path(env["FAKE_MIMO_SERVE_URL"])
            url = None
            deadline = time.time() + 8
            while time.time() < deadline:
                if capture.exists():
                    try:
                        url = json.loads(capture.read_text(
                            encoding="utf-8"))["url"]
                        break
                    except Exception:
                        pass
                time.sleep(0.05)
            self.assertIsNotNone(url, "serve URL capture file never appeared")
            with urllib.request.urlopen(url + "/healthz", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"ok")
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(url + "/nope", timeout=5)
        finally:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate(timeout=10)
        if os.name != "nt":
            # SIGTERM must end the fake cleanly.
            self.assertEqual(proc.returncode, 0, err)
        events = parse_jsonl(out)
        serve_events = [e for e in events if e["type"] == "serve"]
        self.assertTrue(serve_events, "serve mode must print its url event")
        self.assertEqual(serve_events[0]["url"], url)


if __name__ == "__main__":
    unittest.main()
