"""Tests for config-only providers using a local fake OpenAI-compatible server."""

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deepseek_mcp.providers import (  # noqa: E402
    ProviderError, complete, consult, load_providers, resolve_key,
)

KEY_A = "test-key-alpha"
KEY_B = "test-key-beta"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        server = self.server
        with server.lock:
            index = len(server.requests)
            server.requests.append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            })
        action = server.responder(index, body)
        if action[0] == "sleep":
            time.sleep(action[1])
            status, payload = action[2], action[3]
        else:
            status, payload = action
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def success(body):
    return 200, {
        "model": body.get("model"),
        "choices": [{"message": {"content": "answer from " + str(body.get("model"))}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "prompt_cache_hit_tokens": 2},
    }


def provider(name, base_url, key_env, model="deepseek-flash", priority=10):
    return {
        "name": name,
        "base_url": base_url,
        "api_key_env": key_env,
        "models": [model],
        "priority": priority,
    }


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def start(self, responder):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.requests = []
        server.lock = threading.Lock()
        server.responder = responder
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        return "http://127.0.0.1:%d/v1" % server.server_address[1], server

    def test_fallback_on_429_then_success(self):
        base_url, server = self.start(
            lambda index, body: (429, {"error": "slow down"}) if index == 0
            else success(body)
        )
        env = {"KEY_A": KEY_A, "KEY_B": KEY_B}
        providers = [
            provider("a", base_url, "KEY_A", priority=1),
            provider("b", base_url, "KEY_B", priority=2),
        ]
        result = complete([{"role": "user", "content": "hi"}],
                          providers=providers, env=env)
        self.assertEqual(result["provider"], "b")
        self.assertEqual(result["answer"], "answer from deepseek-flash")
        self.assertEqual([a["status"] for a in result["attempts"]], [429, 200])
        self.assertEqual(result["attempts"][0]["provider"], "a")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(server.requests[1]["auth"], "Bearer " + KEY_B)
        self.assertEqual(server.requests[1]["path"], "/v1/chat/completions")
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertIsNotNone(result["cost"])
        self.assertGreaterEqual(result["seconds"], 0)

    def test_401_does_not_fall_back(self):
        base_url, server = self.start(lambda i, b: (401, {"error": "bad key"}))
        env = {"KEY_A": KEY_A, "KEY_B": KEY_B}
        providers = [
            provider("a", base_url, "KEY_A", priority=1),
            provider("b", base_url, "KEY_B", priority=2),
        ]
        with self.assertRaises(ProviderError) as caught:
            complete([{"role": "user", "content": "hi"}],
                     providers=providers, env=env)
        self.assertEqual(caught.exception.status, 401)
        self.assertFalse(caught.exception.transient)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(len(caught.exception.attempts), 1)

    def test_missing_key_raises_without_value(self):
        with self.assertRaises(ProviderError) as caught:
            complete([{"role": "user", "content": "hi"}],
                     providers=[provider("a", "http://127.0.0.1:1/v1", "MISSING_KEY")],
                     env={}, timeout=0.2)
        message = str(caught.exception)
        self.assertIn("MISSING_KEY", message)
        self.assertNotIn("Bearer", message)
        self.assertNotIn(KEY_A, message)

    def test_inline_api_key_ignored(self):
        cfg = {
            "base_url": "http://deepseek.example/v1",
            "model": "deepseek-flash",
            "providers": [{
                "name": "oai",
                "base_url": "http://other.example/v1",
                "api_key": "sk-" "inline-secret-123456",
                "models": ["gpt-x"],
                "priority": 5,
            }],
        }
        loaded = load_providers(cfg, env={})
        # Built-in DeepSeek is priority 0 (primary); configured providers follow.
        self.assertEqual([p["name"] for p in loaded], ["deepseek", "oai"])
        oai = [p for p in loaded if p["name"] == "oai"][0]
        self.assertEqual(oai["api_key_env"], "OAI_API_KEY")
        self.assertNotIn("api_key", oai)
        self.assertNotIn("sk-" "inline-secret-123456", json.dumps(loaded))
        with self.assertRaises(ProviderError):
            resolve_key(oai, env={})

    def test_consult_two_models_in_parallel(self):
        base_url, server = self.start(lambda i, b: success(b))
        p = provider("solo", base_url, "KEY_A", priority=1)
        p["models"] = ["m-one", "m-two"]
        results = consult("hello", models=["m-one", "m-two"], providers=[p],
                          env={"KEY_A": KEY_A})
        self.assertEqual(len(results), 2)
        self.assertEqual([r["model_requested"] for r in results],
                         ["m-one", "m-two"])
        self.assertEqual([r["model"] for r in results], ["m-one", "m-two"])
        self.assertEqual(results[0]["answer"], "answer from m-one")
        self.assertNotIn(KEY_A, json.dumps(results))
        self.assertEqual(len(server.requests), 2)

    def test_provider_prefixed_consult_model(self):
        base_url, server = self.start(lambda i, b: success(b))
        p = provider("solo", base_url, "KEY_A", model="m-one", priority=1)
        results = consult("hello", models=["solo:m-one"], providers=[p],
                          env={"KEY_A": KEY_A})
        self.assertEqual(results[0]["provider"], "solo")
        self.assertEqual(results[0]["model"], "m-one")
        self.assertEqual(len(server.requests), 1)

    def test_timeout_is_bounded_and_transient(self):
        base_url, _server = self.start(
            lambda i, b: ("sleep", 0.6, 200, success(b)[1])
        )
        started = time.monotonic()
        with self.assertRaises(ProviderError) as caught:
            complete([{"role": "user", "content": "hi"}],
                     providers=[provider("slow", base_url, "KEY_A")],
                     env={"KEY_A": KEY_A}, timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertTrue(caught.exception.transient)
        self.assertNotIn(KEY_A, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
