"""P1-1 / P1-3 integration tests (candidate, stdlib unittest only).

Run after merging the integration snippets:

    python -m unittest tests.test_p1_integration -v

These tests are intentionally narrow and do not spawn a real DSH process:
`start_job` is exercised with a mocked `Popen`, and `run_harness` is pointed at
a non-existent `dsh_bin`, so it fails *after* the work-id adoption block. The
tests skip (rather than fail) when the new `work`/`events` modules have not been
merged yet.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(os.environ.get(
    "CREWFOREMAN_REPO",
    Path(__file__).resolve().parents[1] / "repo",
)).resolve()
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:  # the two new modules are P1 deliverables from other builders
    from deepseek_mcp import events, harness, jobs, server, store, work
    HAS_P1_MODULES = True
except Exception:  # pragma: no cover - pre-merge checkout
    events = harness = jobs = store = work = None
    HAS_P1_MODULES = False

try:
    from deepseek_mcp import events as _events  # noqa: F401
    HAS_EVENTS = True
except Exception:  # pragma: no cover
    HAS_EVENTS = False


class FakeCfg(dict):
    """Minimal dict/attribute config; mirrors the real Config's dual access."""

    def __init__(self, **kwargs):
        super().__init__(kwargs)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def make_cfg(root):
    root = Path(root)
    cfg = FakeCfg(
        jobs_dir=str(root / "jobs"),
        state_dir=str(root / "state"),
        log_dir=str(root / "logs"),
        harness_workspace=str(root / "ws"),
        wait_seconds=0,
        dedupe_active=True,
        task_completion="job",
        model="deepseek-flash",
        reasoning="default",
        # harness.run_harness reads these before the missing-dsh check.
        dsh_bin=str(root / "no-such-dsh"),
        stall_seconds=1800,
        stall_max_seconds=3600,
        stall_cap_hard_max_seconds=7200,
        max_explicit_subagents=32,
        auto_skills="off",
    )
    for key in ("jobs", "state", "logs", "ws"):
        (root / key).mkdir(parents=True, exist_ok=True)
    return cfg


@unittest.skipUnless(HAS_P1_MODULES, "work/events modules not merged yet")
class StartJobAdmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = make_cfg(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_job_dedupes_spawn_and_writes_work_fields(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        args = {"prompt": "build one thing", "workspace": str(self.root / "ws")}
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen",
                                  return_value=proc) as popen:
            first = jobs.start_job("harness_deepseek", dict(args),
                                   cfg=self.cfg, wait=0)
            second = jobs.start_job("harness_deepseek", dict(args),
                                    cfg=self.cfg, wait=0)

        self.assertEqual(popen.call_count, 1, "the duplicate must not spawn")
        self.assertIn("still running", first.lower())

        specs = list(Path(self.cfg.jobs_dir).glob("*.job.json"))
        self.assertEqual(len(specs), 1)
        spec = json.loads(specs[0].read_text(encoding="utf-8"))
        work_id = spec.get("work_id")
        self.assertTrue(work_id, "job spec needs a top-level work_id")
        self.assertEqual(spec.get("attempt_no"), 1)
        self.assertEqual(spec["args"].get("_work_id"), work_id)
        self.assertEqual(spec["args"].get("_attempt_no"), 1)
        self.assertEqual(spec["args"].get("_run_id"), spec["run_id"])

        status_path = Path(self.cfg.jobs_dir) / (spec["run_id"] + ".status.json")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status.get("work_id"), work_id)
        self.assertEqual(status.get("attempt_no"), 1)

        wstore = work.WorkStore(Path(self.cfg.state_dir) / "work")
        admitted = wstore.load(work_id)
        self.assertIsNotNone(admitted)
        self.assertEqual(admitted.get("canonical_run_id"), spec["run_id"])
        self.assertEqual(admitted.get("state"), "active")

        self.assertIn(spec["run_id"], second,
                      "reused response must name the existing run id")

    def test_store_new_run_copies_work_fields(self):
        run = store.new_run(
            "harness",
            {"_run_id": "fixture-run-001",
             "_work_id": "w0123456789abcdef", "_attempt_no": 3},
            "prompt text",
            FakeCfg(model="deepseek-flash", reasoning="default"),
        )
        self.assertEqual(run.get("work_id"), "w0123456789abcdef")
        self.assertEqual(run.get("attempt_no"), 3)
        self.assertEqual(run.get("id"), "fixture-run-001")

    def test_tool_status_events_since_returns_dict_and_plain_path_is_string(self):
        run_id = "fixture-run-001"
        work_id = "w0123456789abcdef"
        status_path = Path(self.cfg.jobs_dir) / (run_id + ".status.json")
        status_path.write_text(json.dumps({
            "status": "running", "progress": "builder", "run_id": run_id,
            "work_id": work_id, "updated": "2025-01-01T12:00:00+00:00",
        }), encoding="utf-8")
        log = events.EventLog(Path(self.cfg.state_dir) / "work" / work_id)
        log.append("phase", {"phase": "builder"})
        log.append("progress", {"percent": 50})

        structured = jobs.tool_status(
            {"run_id": run_id, "events_since": 0}, cfg=self.cfg)
        self.assertIsInstance(structured, dict)
        self.assertGreaterEqual(structured.get("next_seq"), 1)
        self.assertEqual([e["type"] for e in structured["events"]],
                         ["phase", "progress"])
        self.assertIn("stage", structured)

        legacy = jobs.tool_status({"run_id": run_id}, cfg=self.cfg)
        self.assertIsInstance(legacy, str)
        self.assertIn(run_id, legacy)


@unittest.skipUnless(HAS_P1_MODULES, "work/events modules not merged yet")
class HarnessWorkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = make_cfg(self.root)
        self.wstore = work.WorkStore(Path(self.cfg.state_dir) / "work")
        self.admission = self.wstore.admit("a" * 64)
        self.work_id = self.admission["work_id"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_harness_adopts_work_id_with_missing_dsh(self):
        # run_harness binds/instruments the work item before it validates the
        # DSH binary, so a missing binary is a deterministic post-adoption stop.
        args = {
            "prompt": "adopt me",
            "workspace": str(self.root / "ws"),
            "_run_id": "fixture-run-001",
            "_work_id": self.work_id,
            "_attempt_no": 1,
        }
        try:
            harness.run_harness(dict(args), self.cfg)
        except Exception:
            pass
        else:  # pragma: no cover - missing dsh must fail somewhere
            self.fail("run_harness unexpectedly succeeded with a missing dsh")

        admitted = self.wstore.load(self.work_id)
        self.assertIsNotNone(admitted)
        self.assertEqual(admitted.get("canonical_run_id"),
                         args["_run_id"],
                         "run_harness must bind the adopted work id")

    def test_persist_emits_phase_and_heartbeat(self):
        if not hasattr(harness, "_persist") or not hasattr(harness, "_work_store"):
            self.skipTest("harness work instrumentation not merged")
        run = {"id": "fixture-run-001", "work_id": self.work_id,
               "attempt_no": 1, "status": "running"}
        self.wstore.bind_run(self.work_id, 1, run["id"])
        with mock.patch.object(harness, "save_run", return_value=Path("run.json")):
            harness._persist(run, self.cfg, phase="builder")
            harness._persist(run, self.cfg, phase="builder")  # deduped

        log = events.EventLog(self.wstore.path(self.work_id))
        phases = [e for e in log.read_since(0) if e["type"] == "phase"]
        self.assertEqual(len(phases), 1)
        self.assertEqual(phases[0]["payload"].get("phase"), "builder")
        self.assertIsNotNone(self.wstore.load(self.work_id).get("last_heartbeat"))

    def test_save_terminal_writes_result_and_finishes_work(self):
        if not hasattr(harness, "_save_terminal"):
            self.skipTest("harness terminal instrumentation not merged")
        run = {"id": "fixture-run-001", "work_id": self.work_id,
               "attempt_no": 1, "status": "ok", "answer": "the answer"}
        self.wstore.bind_run(self.work_id, 1, run["id"])
        cfg = FakeCfg(**dict(self.cfg))
        with mock.patch.object(harness, "_observe_skills", return_value=None), \
                mock.patch.object(harness, "save_run", return_value=Path("run.json")):
            harness._save_terminal(run, cfg, [], None, None)

        log = events.EventLog(self.wstore.path(self.work_id))
        stored = log.read_result()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("result"), "the answer")
        self.assertTrue(any(e["type"] == "result" for e in log.read_since(0)))
        self.assertEqual(self.wstore.load(self.work_id).get("state"), "finished")

    def test_resume_helper_increments_attempt_and_clears_run(self):
        first = self.wstore.bind_run(self.work_id, 1, "fixture-run-001")
        self.assertEqual(first.get("attempt_no"), 1)
        second = self.wstore.begin_attempt(self.work_id, reason="resume")
        self.assertEqual(second.get("attempt_no"), 2)
        self.assertIsNone(second.get("canonical_run_id"))
        self.assertEqual(second.get("state"), "active")

    def test_checkpoint_spec_keeps_work_id(self):
        if not hasattr(harness, "_job_spec_for_checkpoint"):
            self.skipTest("checkpoint helper not present")
        run = {"prompt": "p", "title": "t", "subagents": 0,
               "work_id": self.work_id, "attempt_no": 2,
               "independent_review": False, "quality_used": False,
               "quality_rounds": 0, "add_dirs": []}
        spec = harness._job_spec_for_checkpoint(
            run, {"_work_id": self.work_id, "_attempt_no": 2},
            self.root / "ws")
        self.assertEqual(spec.get("work_id"), self.work_id)
        self.assertEqual(spec.get("attempt_no"), 2)


@unittest.skipUnless(HAS_P1_MODULES, "work/events modules not merged yet")
class SchemaContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = make_cfg(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tool_names_and_additive_schema_fields(self):
        tools = {tool["name"]: tool for tool in server.TOOLS}
        self.assertEqual(len(tools), 20)
        self.assertIn("idempotency_key",
                      tools["harness_deepseek"]["inputSchema"]["properties"])
        self.assertIn("events_since",
                      tools["deepseek_job_status"]["inputSchema"]["properties"])
        resume = tools["resume_deepseek"]["inputSchema"]["properties"]
        self.assertIn("answer", resume)
        self.assertIn("option", resume)

    def test_dispatch_returns_json_text_and_structured_content(self):
        run_id = "fixture-run-001"
        work_id = "w" + "d" * 19
        jobs.write_status(Path(self.cfg.jobs_dir) / (run_id + ".status.json"),
                          status="running", pid=os.getpid(),
                          progress="builder", work_id=work_id)
        log = events.EventLog(Path(self.cfg.state_dir) / "work" / work_id)
        log.append("phase", {"phase": "builder"})
        response = server.dispatch(
            "tools/call",
            {"name": "deepseek_job_status",
             "arguments": {"run_id": run_id, "events_since": 0}},
            cfg=self.cfg)
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertIsInstance(result["content"][0]["text"], str)
        self.assertEqual(result["structuredContent"]["run_id"], run_id)
        self.assertEqual(result["structuredContent"]["events"][0]["type"],
                         "phase")


if __name__ == "__main__":
    unittest.main()
