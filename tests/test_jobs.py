import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import jobs, server
from deepseek_mcp.config import load_config
from tests.fakes import FakeDeepSeekServer, base_env


class JobTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("TMPDIR") or os.getcwd()
        try:
            self.tmp = tempfile.TemporaryDirectory(dir=base)
        except OSError:
            self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_run_job_inline_writes_status_and_result(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        job_dir = self.root / "jobs"
        job_dir.mkdir()
        spec = job_dir / "test.job.json"
        spec.write_text(json.dumps({"tool": "ask_deepseek", "run_id": "test-run",
                                    "args": {"prompt": "hi"}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(spec, {"ask_deepseek": lambda args: "inline result"}, cfg)
        self.assertEqual(jobs.tool_status({"run_id": "test-run"}, cfg), "inline result")
        listing = jobs.tool_status({}, cfg)
        self.assertIn("test-run", listing)

    def test_run_job_restores_worker_globals(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        job_dir = self.root / "jobs"
        job_dir.mkdir()
        spec = job_dir / "restore.job.json"
        spec.write_text(json.dumps({"tool": "ask_deepseek",
                                    "run_id": "restore-run",
                                    "args": {"prompt": "hi"}}),
                        encoding="utf-8")
        sentinel_status = self.root / "sentinel.status.json"
        sentinel_ctx = ("sentinel-work", None, "sentinel-run")
        saved = (jobs._status_path, jobs._work_ctx, jobs._admission_store,
                 jobs._admission_work_id, jobs._admission_token,
                 jobs._admission_last_hb, jobs._admission_released)
        self.addCleanup(lambda: setattr(jobs, "_status_path", saved[0]))
        self.addCleanup(lambda: setattr(jobs, "_work_ctx", saved[1]))
        jobs._status_path = sentinel_status
        jobs._work_ctx = sentinel_ctx
        with mock.patch.dict(os.environ,
                             {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(spec, {"ask_deepseek": lambda args: "inline result"},
                         cfg)
        self.assertEqual(jobs._status_path, sentinel_status)
        self.assertEqual(jobs._work_ctx, sentinel_ctx)
        self.assertFalse(sentinel_status.exists())
        self.assertTrue((job_dir / "restore-run.status.json").exists())

    def test_worker_receives_the_spec_config(self):
        custom = self.root / "custom.json"
        custom.write_text(json.dumps({
            "log_dir": str(self.root / "custom-runs"),
            "jobs_dir": str(self.root / "jobs-custom"),
        }), encoding="utf-8")
        job_dir = self.root / "jobs-custom"
        job_dir.mkdir()
        spec = job_dir / "custom.job.json"
        spec.write_text(json.dumps({"tool": "ask_deepseek", "run_id": "custom-run",
                                    "args": {}, "config_path": str(custom)}), encoding="utf-8")
        seen = {}

        def handler(args, cfg):
            seen["log_dir"] = str(cfg.log_dir)
            return "ok"

        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(spec, {"ask_deepseek": handler})
        self.assertEqual(seen["log_dir"], str(self.root / "custom-runs"))

    def test_cli_job_worker_uses_the_spec_config(self):
        custom = self.root / "cli.json"
        custom.write_text(json.dumps({
            "log_dir": str(self.root / "cli-runs"),
            "jobs_dir": str(self.root / "cli-jobs"),
        }), encoding="utf-8")
        job_dir = self.root / "cli-jobs"
        job_dir.mkdir()
        spec = job_dir / "cli.job.json"
        spec.write_text(json.dumps({"tool": "ask_deepseek", "run_id": "cli-run",
                                    "args": {}, "config_path": str(custom)}), encoding="utf-8")
        seen = {}

        def handler(args, cfg):
            seen["log_dir"] = str(cfg.log_dir)
            return "cli ok"

        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            with mock.patch.object(server, "JOB_IMPL", {"ask_deepseek": handler}):
                code = server.main(["--job", str(spec)])
        self.assertEqual(code, 0)
        self.assertEqual(seen["log_dir"], str(self.root / "cli-runs"))

    def test_spec_tilde_config_path_is_honored(self):
        home = self.root / "home"
        config_dir = home / ".config" / "deepseek-harness-mcp"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({
            "log_dir": str(home / "tilde-runs"),
            "jobs_dir": str(home / "tilde-jobs"),
        }), encoding="utf-8")
        (home / "tilde-jobs").mkdir(parents=True)
        job_dir = self.root / "spec-jobs"
        job_dir.mkdir()
        spec = job_dir / "tilde.job.json"
        spec.write_text(json.dumps({
            "tool": "ask_deepseek",
            "run_id": "tilde-run",
            "args": {},
            "config_path": "~/.config/deepseek-harness-mcp/config.json",
        }), encoding="utf-8")
        seen = {}

        def handler(args, cfg):
            seen["log_dir"] = str(cfg.log_dir)
            seen["config_path"] = cfg.get("_config_path")
            return "ok"

        with mock.patch.dict(os.environ, {
                "HOME": str(home), "USERPROFILE": str(home),
                "DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(spec, {"ask_deepseek": handler})
        self.assertEqual(seen["log_dir"], str(home / "tilde-runs"))
        self.assertEqual(seen["config_path"], str(config_file.resolve()))

    def test_run_job_records_error(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        job_dir = self.root / "jobs"
        job_dir.mkdir()
        spec = job_dir / "bad.job.json"
        spec.write_text(json.dumps({"tool": "ask_deepseek", "run_id": "bad-run",
                                    "args": {}}), encoding="utf-8")

        def fail(_args):
            raise RuntimeError("expected failure")

        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(spec, {"ask_deepseek": fail}, cfg)
        with self.assertRaises(RuntimeError):
            jobs.tool_status({"run_id": "bad-run"}, cfg)
        state = json.loads((job_dir / "bad-run.status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "error")
        self.assertIn("expected failure", state["error"])

    def test_start_job_background_ask(self):
        with FakeDeepSeekServer() as fake:
            env = base_env(self.root, base_url=fake.base_url)
            cfg = load_config(config_path=self.root / "config.json", env=env)
            with mock.patch.dict(os.environ, env, clear=False):
                text = jobs.start_job("ask_deepseek", {"prompt": "background hello"},
                                      cfg=cfg, wait=30)
            self.assertIn("DeepSeek run", text)
            runs = list((self.root / "runs").glob("*.json"))
            self.assertEqual(len(runs), 1)
            run = json.loads(runs[0].read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "ok")
            self.assertIn(run["id"], jobs.tool_status({}, cfg))

    def test_malformed_spec_and_bad_run_id_do_not_escape(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        job_dir = self.root / "jobs"
        job_dir.mkdir()
        broken = job_dir / "broken.job.json"
        broken.write_text("{not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(broken, {}, cfg)
        self.assertEqual(json.loads((job_dir / "broken.status.json").read_text(encoding="utf-8"))["status"],
                         "error")
        escape = job_dir / "escape.job.json"
        escape.write_text(json.dumps({"tool": "ask_deepseek", "run_id": "../escaped",
                                      "args": {}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(escape, {}, cfg)
        self.assertFalse((self.root / "escaped.status.json").exists())
        self.assertTrue((job_dir / "escape.status.json").exists())

    def test_job_spec_stores_an_absolute_config_path(self):
        """A worker may run with a different HOME; "~" in the spec must not
        survive the redaction pass as a relative-to-someone-else path."""
        home = self.root / "spec-home"
        config_dir = home / ".config" / "deepseek-harness-mcp"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({
            "jobs_dir": str(self.root / "spec-jobs"),
        }), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home),
                                          "USERPROFILE": str(home)}, clear=False):
            cfg = load_config(
                config_path="~/.config/deepseek-harness-mcp/config.json", env={})
            with mock.patch.object(jobs.subprocess, "Popen",
                                   side_effect=OSError("no launch")):
                with self.assertRaises(RuntimeError):
                    jobs.start_job("ask_deepseek", {"prompt": "spec path"},
                                   cfg=cfg, wait=0)
        specs = list((self.root / "spec-jobs").glob("*.job.json"))
        self.assertEqual(len(specs), 1)
        spec = json.loads(specs[0].read_text(encoding="utf-8"))
        self.assertTrue(os.path.isabs(spec["config_path"]),
                        "persisted config_path is not absolute: %r"
                        % (spec["config_path"],))
        self.assertEqual(spec["config_path"], str(config_file.resolve()))

    def test_launch_failure_is_recorded(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(jobs.subprocess, "Popen", side_effect=OSError("no launch")):
                with self.assertRaises(RuntimeError):
                    jobs.start_job("ask_deepseek", {"prompt": "x"}, cfg=cfg, wait=0)
        states = list((self.root / "jobs").glob("*.status.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "error")
        self.assertIn("no launch", state["error"])

    def test_job_status_redacts_secrets_and_home_paths(self):
        env = base_env(self.root)
        cfg = load_config(config_path=self.root / "config.json", env=env)
        job_dir = self.root / "jobs"
        job_dir.mkdir()
        spec = job_dir / "redact.job.json"
        spec.write_text(json.dumps({"tool": "ask_deepseek", "run_id": "redact-run",
                                    "args": {}}), encoding="utf-8")
        home = "/" + "Users" + "/" + "alice" + "/work"
        secret = "sk" + "-" + "abcdef123456789"

        def handler(args):
            raise RuntimeError("failed at %s with Bearer %s" % (home, secret))

        with mock.patch.dict(os.environ, {"DEEPSEEK_NO_CAFFEINATE": "1"}, clear=False):
            jobs.run_job(spec, {"ask_deepseek": handler}, cfg)
        text = (job_dir / "redact-run.status.json").read_text(encoding="utf-8")
        self.assertNotIn(home, text)
        self.assertNotIn(secret, text)
        self.assertIn("[redacted", text)

    def test_status_unknown_job(self):
        cfg = load_config(config_path=self.root / "config.json", env=base_env(self.root))
        with self.assertRaises(RuntimeError):
            jobs.tool_status({"run_id": "does-not-exist"}, cfg)


if __name__ == "__main__":
    unittest.main()
