"""Smoke tests for ``tests.mimo_fakes`` — the fake ``mimo`` programs.

Each mode must emit the documented JSONL event stream (see the module
docstring of ``tests.mimo_fakes``), ``serve`` must answer the real-CLI HTTP
surface (``/global/health`` 200, ``/healthz`` 503, ``/doc`` 200), reject
``--host``, print the real ready line, enforce ``MIMOCODE_SERVER_PASSWORD``
basic auth when set, ``attach`` must record argv plus password-env presence,
and the error modes must carry the right ``statusCode``.  No network beyond
127.0.0.1, no real ``mimo``, and no long sleeps (``FAKE_MIMO_SLEEP`` is kept
small).
"""

import base64
import contextlib
import json
import os
import re
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
READY_LINE_RE = re.compile(r"^mimocode server listening on "
                           r"http://127\.0\.0\.1:(\d+)$")


def parse_jsonl(text):
    """Parse the JSONL events; skip plain-text lines (e.g. the ready line)."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            events.append(json.loads(line))
    return events


def fetch(url, password=None, username="mimocode"):
    """GET *url*; return ``(status, body)`` without raising on 4xx/5xx."""
    request = urllib.request.Request(url)
    if password is not None:
        token = base64.b64encode(
            (username + ":" + password).encode("utf-8")).decode("ascii")
        request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        return exc.code, body


class _Served:
    """Handle on a running fake-serve process (outputs set on close)."""

    def __init__(self, proc, url):
        self.proc = proc
        self.url = url
        self.out = ""
        self.err = ""
        self.returncode = None


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
        # Never inherit the real server password: the no-auth cases must see
        # the env var absent, the auth cases set it explicitly.
        env.pop("MIMOCODE_SERVER_PASSWORD", None)
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

    # -- attach / argv capture -------------------------------------------

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

    def test_attach_records_password_presence_without_echoing(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-attach-pw",
                               mode="attach")
        server_pw = "sekrit-value-42"
        env = self._env(MIMOCODE_SERVER_PASSWORD=server_pw)
        args = self._argv("--attach", "http://127.0.0.1:9")
        result = subprocess.run([str(fake)] + args,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=env, timeout=20,
                                cwd=str(self.ws))
        self.assertEqual(result.returncode, 0, result.stderr)
        state_file = Path(env["FAKE_MIMO_STATE"])
        self.assertTrue(state_file.exists(),
                        "password presence must land in the state capture")
        state_text = state_file.read_text(encoding="utf-8")
        state = json.loads(state_text)
        self.assertTrue(state.get("password_env"),
                        "attach run must record MIMOCODE_SERVER_PASSWORD "
                        "presence: %r" % (state,))
        # Presence only: the value must never be echoed or captured.
        argv_text = Path(env["FAKE_MIMO_ARGV"]).read_text(encoding="utf-8")
        for blob in (result.stdout, result.stderr, argv_text, state_text):
            self.assertNotIn(server_pw, blob)

    def test_run_without_password_env_records_nothing(self):
        env = self._env()
        result = self._run(self._argv("--attach", "http://127.0.0.1:9"),
                           env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        state_file = Path(env["FAKE_MIMO_STATE"])
        state = (json.loads(state_file.read_text(encoding="utf-8"))
                 if state_file.exists() else {})
        self.assertNotIn("password_env", state)

    # -- serve: argv contract ---------------------------------------------

    def test_serve_rejects_host_flag(self):
        # Real CLI has no --host: usage/help to stderr, non-zero exit,
        # and no server is started.
        for args in (["serve", "--host", "127.0.0.1", "--port", "0"],
                     ["serve", "--host=127.0.0.1", "--port", "0"]):
            env = self._env()
            try:
                result = self._run(args, env=env, timeout=8)
            except subprocess.TimeoutExpired:
                self.fail("mimo serve --host must not start a server: %r"
                          % (args,))
            self.assertNotEqual(result.returncode, 0,
                                "--host must exit non-zero: %r" % (args,))
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("usage", combined,
                          "--host must print a usage/help blurb: %r" % (args,))
            self.assertIn("hostname", combined,
                          "the blurb must name the real --hostname flag")
            self.assertFalse(Path(env["FAKE_MIMO_SERVE_URL"]).exists(),
                             "--host must not bind/start a server")

    def test_serve_ready_line_matches_real_cli_format(self):
        env = self._env()
        with self._serve(["serve", "--hostname", "127.0.0.1", "--port", "0"],
                         env) as served:
            status, body = fetch(served.url + "/global/health")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"ok")
            port = int(served.url.rsplit(":", 1)[1])
        self._assert_serve_stopped_cleanly(served)
        self.assertGreater(port, 0, "port 0 must yield a real bound port")
        lines = served.out.splitlines()
        self.assertTrue(lines, "serve must print the ready line")
        match = READY_LINE_RE.match(lines[0])
        self.assertIsNotNone(
            match,
            "ready line must be exactly 'mimocode server listening on "
            "http://127.0.0.1:<port>', got %r" % lines[0])
        self.assertEqual(int(match.group(1)), port,
                         "ready line must report the actually-bound port")
        events = parse_jsonl(served.out)
        serve_events = [e for e in events if e["type"] == "serve"]
        self.assertTrue(serve_events, "serve must still emit its url event")
        self.assertEqual(serve_events[0]["url"], served.url)

    # -- serve: HTTP surface ----------------------------------------------

    def test_serve_subcommand_http_surface(self):
        env = self._env()
        with self._serve(["serve", "--hostname", "127.0.0.1", "--port", "0"],
                         env) as served:
            status, body = fetch(served.url + "/global/health")
            self.assertEqual(status, 200, "/global/health must be 200")
            self.assertEqual(body, b"ok")
            status, body = fetch(served.url + "/healthz")
            self.assertEqual(status, 503, "/healthz must be 503")
            status, body = fetch(served.url + "/doc")
            self.assertEqual(status, 200, "/doc must be 200")
            status, body = fetch(served.url + "/nope")
            self.assertEqual(status, 404)
        self._assert_serve_stopped_cleanly(served)

    def test_serve_mode_http_surface(self):
        fake = write_fake_mimo(self.root / "bin" / "mimo-serve", mode="serve")
        env = self._env()
        with self._serve(["run", "ignored", "--format", "json",
                          "--dir", str(self.ws), "-m", "xiaomi/mimo-v2.6-pro"],
                         env, fake=fake) as served:
            status, body = fetch(served.url + "/global/health")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"ok")
            status, body = fetch(served.url + "/healthz")
            self.assertEqual(status, 503)
        self._assert_serve_stopped_cleanly(served)

    # -- serve: MIMOCODE_SERVER_PASSWORD ----------------------------------

    def test_serve_password_env_enforces_basic_auth(self):
        server_pw = "s3kr1t-pass"
        env = self._env(MIMOCODE_SERVER_PASSWORD=server_pw)
        with self._serve(["serve", "--hostname", "127.0.0.1", "--port", "0"],
                         env) as served:
            status, body = fetch(served.url + "/global/health")
            self.assertEqual(status, 401, "no auth must be 401")
            status, body = fetch(served.url + "/global/health",
                                 password="wrong")
            self.assertEqual(status, 401, "wrong password must be 401")
            status, body = fetch(served.url + "/global/health",
                                 password=server_pw, username="")
            self.assertEqual(status, 401,
                             "empty username must be 401 (real CLI expects mimocode)")
            status, body = fetch(served.url + "/global/health",
                                 password=server_pw)
            self.assertEqual(status, 200,
                             'basic auth of "mimocode:" + password must be 200')
            self.assertEqual(body, b"ok")
            # The gate covers every request, not just the health endpoint.
            status, body = fetch(served.url + "/nope")
            self.assertEqual(status, 401)
        self._assert_serve_stopped_cleanly(served)

    def test_serve_without_password_env_needs_no_auth(self):
        env = self._env()
        self.assertNotIn("MIMOCODE_SERVER_PASSWORD", env)
        with self._serve(["serve", "--hostname", "127.0.0.1", "--port", "0"],
                         env) as served:
            status, body = fetch(served.url + "/global/health")
            self.assertEqual(status, 200, "no auth required without the env")
            self.assertEqual(body, b"ok")
            status, body = fetch(served.url + "/global/health",
                                 password="ignored")
            self.assertEqual(status, 200,
                             "auth headers must be ignored when not required")
        self._assert_serve_stopped_cleanly(served)

    # -- sessions ---------------------------------------------------------

    def test_session_record_when_sessions_dir_set(self):
        env = self._env()
        result = self._run(self._argv("-s", "sess-42"), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = sorted(Path(env["FAKE_MIMO_SESSIONS"]).glob("session-*.json"))
        self.assertTrue(records, "FAKE_MIMO_SESSIONS must receive a record")
        data = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(data["session"], "sess-42")

    # -- serve helpers ----------------------------------------------------

    @contextlib.contextmanager
    def _serve(self, args, env, fake=None):
        """Start fake serve, wait for its capture file, yield a ``_Served``.

        The process is stopped on exit; ``out``/``err``/``returncode`` are
        filled in afterwards for assertions.
        """
        fake = fake or self.fake
        proc = subprocess.Popen([str(fake)] + list(args),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                universal_newlines=True, env=env,
                                cwd=str(self.ws))
        served = _Served(proc, None)
        try:
            capture = Path(env["FAKE_MIMO_SERVE_URL"])
            deadline = time.time() + 8
            while time.time() < deadline:
                if capture.exists():
                    try:
                        served.url = json.loads(capture.read_text(
                            encoding="utf-8"))["url"]
                        break
                    except Exception:
                        pass
                time.sleep(0.05)
            self.assertIsNotNone(served.url,
                                 "serve URL capture file never appeared")
            yield served
        finally:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate(timeout=10)
            served.out, served.err = out, err
            served.returncode = proc.returncode

    def _assert_serve_stopped_cleanly(self, served):
        if os.name != "nt":
            # SIGTERM must end the fake cleanly.
            self.assertEqual(served.returncode, 0, served.err)
        events = parse_jsonl(served.out)
        serve_events = [e for e in events if e["type"] == "serve"]
        self.assertTrue(serve_events, "serve mode must print its url event")
        self.assertEqual(serve_events[0]["url"], served.url)


if __name__ == "__main__":
    unittest.main()
