import concurrent.futures
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import store
from deepseek_mcp.config import load_config
from tests.fakes import base_env


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config(config_path=self.root / "config.json", env=base_env(self.root))
        self.fallback = self.root / "fallback-runs"
        patcher = mock.patch.object(store, "FALLBACK_LOG_DIR", self.fallback)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, run_id="store-run"):
        return {
            "id": run_id, "kind": "single", "created": "2026-01-01T00:00:00+00:00",
            "title": "Store test", "model": "deepseek-flash", "status": "ok",
            "prompt": "hello", "answer": "world", "files": [],
        }

    def test_save_load_and_read_markdown(self):
        path = store.save_run(self._run(), self.cfg)
        self.assertTrue(Path(path).is_file())
        runs = store.load_runs(self.cfg)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], "store-run")
        run, json_path, md_path = store.find_run("store-run", self.cfg)
        self.assertEqual(run["answer"], "world")
        self.assertTrue(json_path.is_file())
        self.assertIn("# Store test", md_path.read_text(encoding="utf-8"))

    def test_save_redacts_configured_api_key(self):
        key = "unit-test-key-123456789"
        run = self._run("redact-run")
        run["answer"] = "request used " + key
        run["prompt"] = "prompt also had " + key
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}, clear=False):
            path = store.save_run(run, self.cfg)
        for saved in (Path(path), Path(path).with_suffix(".json")):
            text = saved.read_text(encoding="utf-8")
            self.assertNotIn(key, text)
            self.assertIn("[redacted-api-key]", text)

    def test_save_redacts_home_directory_paths(self):
        home_path = "/" + "Users" + "/" + "alice" + "/project/file.txt"
        run = self._run("home-run")
        run["answer"] = "wrote " + home_path
        path = store.save_run(run, self.cfg)
        for saved in (Path(path), Path(path).with_suffix(".json")):
            self.assertNotIn(home_path, saved.read_text(encoding="utf-8"))

    def test_concurrent_saves_do_not_collide(self):
        def save(index):
            run = self._run("concurrent-%02d" % index)
            return store.save_run(run, self.cfg)

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            paths = list(pool.map(save, range(24)))
        self.assertEqual(len(paths), 24)
        self.assertEqual(len(list((self.root / "runs").glob("concurrent-*.json"))), 24)

    def test_invalid_run_id_rejected(self):
        with self.assertRaises(RuntimeError):
            store.find_run("../escape", self.cfg)
        with self.assertRaises(RuntimeError):
            store.find_run("", self.cfg)


if __name__ == "__main__":
    unittest.main()
