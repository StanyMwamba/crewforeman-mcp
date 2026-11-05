"""Contract tests for ``deepseek_mcp.work`` (P1-1 / P1-3, INTERFACES.md section A).

Stdlib unittest only.  Every test uses a temporary directory and an injected
fake clock where the behaviour under test depends on time.  No sleeps longer
than 50 ms and no network / server access.

Run from the repository root, e.g.::

    python -m unittest tests.test_p1_work_identity -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

# Allow running the candidate directly from ``candidates/`` against a sibling
# ``repo`` checkout; when the lead installs the tests into ``tests/`` the
# package is importable in the normal way.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.join(os.path.dirname(_HERE), "repo")
if os.path.isdir(os.path.join(_REPO, "deepseek_mcp")) and _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from deepseek_mcp import checkpoints, events, harness, jobs, store, work  # noqa: E402
from deepseek_mcp.config import load_config  # noqa: E402
from tests.fakes import base_env, write_fake_dsh, write_session_log  # noqa: E402


HEX64 = re.compile(r"^[0-9a-f]{64}$")
POSIX = os.name == "posix"


class FakeClock(object):
    """Deterministic epoch-seconds callable."""

    def __init__(self, now=1_700_000_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def dead_pid():
    """Return a pid that has definitely exited (fresh child, reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class WorkTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cf-work-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.root = os.path.join(self.state, "work")
        self.ws = os.path.join(self.tmp, "workspace")
        os.makedirs(self.ws)
        self.other_ws = os.path.join(self.tmp, "workspace-2")
        os.makedirs(self.other_ws)
        self.clock = FakeClock()

    def store(self, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return work.WorkStore(self.root, **kwargs)

    def work_json(self, work_id):
        path = os.path.join(self.root, work_id, "work.json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def attempt_lines(self, work_id):
        path = os.path.join(self.root, work_id, "attempts.jsonl")
        if not os.path.exists(path):
            return []
        lines = []
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    continue
        return lines

    def admit(self, store, fingerprint, **kwargs):
        return store.admit(fingerprint, **kwargs)

    def new_work(self, fingerprint="fp-abc", args=None, **kwargs):
        store = self.store()
        if args is None:
            args = {"prompt": "hello", "workspace": self.ws}
        fp = work.request_fingerprint("harness_deepseek", args)
        result = store.admit(fp, **kwargs)
        return store, fp, result


class ConstantsAndIdTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(work.WORK_SCHEMA, 1)
        self.assertEqual(work.EXCLUDED_PREFIX, "_")
        self.assertEqual(
            set(work.FINGERPRINT_EXCLUDE),
            {"wait_seconds", "title", "_run_id", "_resume_from", "idempotency_key"},
        )

    def test_work_id_for_plain(self):
        fp = "a" * 64
        wid = work.work_id_for(fp)
        self.assertEqual(wid, "w" + fp[:19])
        self.assertTrue(wid.startswith("w"))
        self.assertEqual(len(wid), 20)
        self.assertEqual(wid, work.work_id_for(fp))

    def test_work_id_for_key_is_stable_and_strips(self):
        wid = work.work_id_for("a" * 64, idempotency_key="job-123")
        self.assertTrue(wid.startswith("k"))
        self.assertEqual(len(wid), 20)
        self.assertEqual(wid, work.work_id_for("b" * 64, idempotency_key="job-123"))
        self.assertEqual(wid, work.work_id_for("b" * 64, idempotency_key="  job-123  "))
        self.assertNotEqual(wid, work.work_id_for("a" * 64, idempotency_key="job-124"))

    def test_work_id_for_rejects_bad_keys(self):
        for bad in ("", "   ", None, 5, b"bytes", "x" * 201):
            with self.assertRaises(ValueError, msg=repr(bad)):
                work.work_id_for("a" * 64, idempotency_key=bad)
        # Boundary: 200 characters is still accepted.
        self.assertTrue(work.work_id_for("a" * 64, idempotency_key="x" * 200).startswith("k"))


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cf-fp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self.ws2 = os.path.join(self.tmp, "ws2")
        os.makedirs(self.ws2)

    def fp(self, args, workspace=None):
        return work.request_fingerprint("harness_deepseek", args, workspace=workspace)

    def test_deterministic_hex(self):
        args = {"prompt": "hello", "workspace": self.ws, "sandbox": "docker"}
        first = self.fp(args)
        self.assertIsInstance(first, str)
        self.assertRegex(first, HEX64)
        self.assertEqual(first, self.fp(dict(args)))
        # Key order in the caller's dict must not matter (canonical JSON).
        reordered = {"sandbox": "docker", "workspace": self.ws, "prompt": "hello"}
        self.assertEqual(first, self.fp(reordered))

    def test_different_tool_differs(self):
        args = {"prompt": "hello", "workspace": self.ws}
        self.assertNotEqual(
            work.request_fingerprint("harness_deepseek", args),
            work.request_fingerprint("ask_deepseek", args),
        )

    def test_excluded_keys_do_not_change_fingerprint(self):
        base = {"prompt": "hello", "workspace": self.ws, "sandbox": "docker"}
        baseline = self.fp(base)
        for key, value in (
            ("wait_seconds", 240),
            ("title", "a friendly title"),
            ("_run_id", "run-123"),
            ("_resume_from", "run-9"),
            ("idempotency_key", "key-1"),
            ("_attempt_no", 4),
            ("_anything", {"nested": True}),
        ):
            changed = dict(base)
            changed[key] = value
            self.assertEqual(baseline, self.fp(changed), "key %r must be excluded" % key)

    def test_included_keys_change_fingerprint(self):
        base = {"prompt": "hello", "workspace": self.ws}
        baseline = self.fp(base)
        variants = {
            "prompt": "different prompt",
            "sandbox": "host",
            "reviewer_sandbox": "docker",
            "quality": True,
            "quality_rounds": 3,
            "independent_review": False,
            "job_max_seconds": 900,
            "job_max_cost_usd": 1.5,
            "skill": "review",
            "subagents": 4,
        }
        for key, value in variants.items():
            changed = dict(base)
            changed[key] = value
            self.assertNotEqual(baseline, self.fp(changed), "key %r must be included" % key)

    def test_workspace_realpath_and_expanduser(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        link = os.path.join(self.tmp, "ws-link")
        try:
            os.symlink(self.ws, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted")
        a = self.fp({"prompt": "x", "workspace": self.ws})
        b = self.fp({"prompt": "x", "workspace": link})
        self.assertEqual(a, b)
        # Trailing slash / "." components must normalise too.
        self.assertEqual(a, self.fp({"prompt": "x", "workspace": self.ws + os.sep}))
        self.assertEqual(a, self.fp({"prompt": "x", "workspace": os.path.join(self.ws, ".")}))
        # args["workspace"] wins over the explicit workspace kwarg.
        self.assertEqual(a, self.fp({"prompt": "x", "workspace": self.ws}, workspace=self.ws2))

    def test_workspace_kwarg_used_when_args_has_none(self):
        a = self.fp({"prompt": "x"}, workspace=self.ws)
        b = self.fp({"prompt": "x", "workspace": self.ws})
        self.assertEqual(a, b)
        # Relative / empty workspace still resolves deterministically.
        self.assertEqual(
            self.fp({"prompt": "x"}), self.fp({"prompt": "x"}, workspace="")
        )

    def test_different_workspace_differs(self):
        self.assertNotEqual(
            self.fp({"prompt": "x", "workspace": self.ws}),
            self.fp({"prompt": "x", "workspace": self.ws2}),
        )

    def test_files_and_add_dirs_realpath(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        target = os.path.join(self.ws, "notes.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("hi")
        other = os.path.join(self.ws, "other.txt")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("bye")
        link = os.path.join(self.tmp, "notes-link.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted")
        a = self.fp({"prompt": "x", "workspace": self.ws, "files": [target]})
        b = self.fp({"prompt": "x", "workspace": self.ws, "files": [link]})
        self.assertEqual(a, b)
        self.assertNotEqual(
            a, self.fp({"prompt": "x", "workspace": self.ws, "files": [other]})
        )
        add_a = self.fp({"prompt": "x", "workspace": self.ws, "add_dirs": [self.ws]})
        add_b = self.fp({"prompt": "x", "workspace": self.ws, "add_dirs": [self.ws + os.sep]})
        self.assertEqual(add_a, add_b)
        self.assertNotEqual(
            add_a, self.fp({"prompt": "x", "workspace": self.ws, "add_dirs": [self.ws2]})
        )


class AdmitLifecycleTest(WorkTestBase):
    def test_new_work_admit(self):
        store, fp, result = self.new_work()
        self.assertEqual(result["work_id"], "w" + fp[:19])
        self.assertEqual(result["attempt_no"], 1)
        self.assertIs(result["reused"], False)
        self.assertIsNone(result["run_id"])
        self.assertIs(result["pending"], True)
        self.assertIn("work", result)
        self.assertEqual(result["work"]["state"], "active")
        self.assertEqual(result["work"]["attempt_no"], 1)
        self.assertTrue(os.path.isdir(os.path.join(self.root, result["work_id"])))
        self.assertTrue(os.path.isfile(os.path.join(self.root, result["work_id"], "work.json")))
        self.assertEqual(store.load(result["work_id"])["work_id"], result["work_id"])
        self.assertEqual(store.path(result["work_id"]),
                         os.path.join(self.root, result["work_id"]))
        self.assertIsNone(store.load("w" + "0" * 19))
        self.assertEqual(store.attempts("w" + "0" * 19), [])

    def test_permissions(self):
        if not POSIX:
            self.skipTest("POSIX permissions only")
        store, _fp, result = self.new_work()
        work_dir = os.path.join(self.root, result["work_id"])
        self.assertEqual(os.stat(work_dir).st_mode & 0o777, 0o700)
        for name in ("work.json", "attempts.jsonl"):
            path = os.path.join(work_dir, name)
            self.assertTrue(os.path.exists(path), name)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, name)
        if os.path.exists(os.path.join(work_dir, ".lock")):
            self.assertEqual(os.stat(os.path.join(work_dir, ".lock")).st_mode & 0o777, 0o600)

    def test_second_active_admit_reuses_attempt(self):
        store, fp, first = self.new_work()
        second = store.admit(fp)
        self.assertIs(second["reused"], True)
        self.assertEqual(second["work_id"], first["work_id"])
        self.assertEqual(second["attempt_no"], 1)
        self.assertIsNone(second["run_id"])
        self.assertIs(second["pending"], True)
        self.assertEqual(len(self.attempt_lines(first["work_id"])), 1)

        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        third = store.admit(fp, note="ignored on reuse")
        self.assertIs(third["reused"], True)
        self.assertEqual(third["attempt_no"], 1)
        self.assertEqual(third["run_id"], "run-1")
        self.assertIs(third["pending"], False)
        self.assertIsNone(third["note"])
        self.assertEqual(len(self.attempt_lines(first["work_id"])), 1)

    def test_find_active_and_key_lookup(self):
        store, fp, first = self.new_work()
        self.assertIsNotNone(store.find_active(fp))
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        self.assertEqual(store.find_active(fp)["work_id"], first["work_id"])
        self.assertIsNone(store.find_active("f" * 64))

    def test_terminal_work_allocates_new_attempt(self):
        store, fp, first = self.new_work()
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        finished = store.finish(first["work_id"], "ok", run_id="run-1", result="done")
        self.assertIsNotNone(finished)
        self.assertEqual(finished["state"], "finished")
        self.assertEqual(finished["terminal_status"], "ok")
        self.assertIn("finished_at", finished)
        events = self.attempt_lines(first["work_id"])
        self.assertEqual(events[-1]["event"], "finished")
        self.assertEqual(events[-1]["status"], "ok")

        again = store.admit(fp)
        self.assertIs(again["reused"], False)
        self.assertEqual(again["work_id"], first["work_id"])
        self.assertEqual(again["attempt_no"], 2)
        self.assertIsNone(again["run_id"])
        self.assertIs(again["pending"], True)
        work_doc = store.load(first["work_id"])
        self.assertEqual(work_doc["state"], "active")
        self.assertEqual(work_doc["attempt_no"], 2)
        self.assertIsNone(work_doc["canonical_run_id"])

    def test_explicit_key_is_stable_and_wins(self):
        store = self.store()
        fp_a = work.request_fingerprint(
            "harness_deepseek", {"prompt": "one", "workspace": self.ws})
        fp_b = work.request_fingerprint(
            "harness_deepseek", {"prompt": "two", "workspace": self.other_ws})
        first = store.admit(fp_a, idempotency_key="job-123")
        self.assertEqual(first["work_id"], work.work_id_for(fp_a, "job-123"))
        self.assertTrue(first["work_id"].startswith("k"))

        # Active keyed work is reused even when the arguments (and so the
        # fingerprint) changed.
        reused = store.admit(fp_b, idempotency_key="job-123")
        self.assertIs(reused["reused"], True)
        self.assertEqual(reused["work_id"], first["work_id"])
        self.assertEqual(reused["attempt_no"], 1)

        # Terminal keyed work is reusable under the same key-derived id.
        store.bind_run(first["work_id"], 1, "run-key", pid=os.getpid())
        store.finish(first["work_id"], "ok", run_id="run-key")
        third = store.admit(fp_b, idempotency_key="job-123")
        self.assertIs(third["reused"], False)
        self.assertEqual(third["work_id"], first["work_id"])
        self.assertEqual(third["attempt_no"], 2)
        self.assertIs(third["pending"], True)

    def test_dedupe_active_false_allocates_attempt(self):
        store, fp, first = self.new_work()
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        second = store.admit(fp, dedupe_active=False)
        self.assertIs(second["reused"], False)
        self.assertEqual(second["work_id"], first["work_id"])
        self.assertEqual(second["attempt_no"], 2)
        self.assertIs(second["pending"], True)
        self.assertIsNone(second["run_id"])
        store.bind_run(second["work_id"], 2, "run-2", pid=os.getpid())
        doc = store.load(first["work_id"])
        self.assertEqual(doc["canonical_run_id"], "run-2")
        # finish() for the superseded attempt must be ignored.
        ignored = store.finish(first["work_id"], "ok", run_id="run-1")
        if ignored is not None:
            self.assertNotEqual(ignored["state"], "finished")
        self.assertEqual(store.load(first["work_id"])["canonical_run_id"], "run-2")
        self.assertEqual(store.load(first["work_id"])["state"], "active")
        done = store.finish(first["work_id"], "ok", run_id="run-2")
        self.assertEqual(done["state"], "finished")
        self.assertEqual(done["terminal_status"], "ok")

    def test_begin_attempt_increments(self):
        store, fp, first = self.new_work()
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        second = store.begin_attempt(first["work_id"], reason="resume")
        self.assertEqual(second["attempt_no"], 2)
        self.assertEqual(second["state"], "active")
        self.assertIsNone(second["canonical_run_id"])
        third = store.begin_attempt(first["work_id"], reason="resume", pid=os.getpid())
        self.assertEqual(third["attempt_no"], 3)
        events = self.attempt_lines(first["work_id"])
        resumed = [e for e in events if e.get("reason") == "resume"]
        self.assertGreaterEqual(len(resumed), 2)
        self.assertEqual(events[-1]["event"], "admitted")

    def test_record_attempt_appends_one_line(self):
        store, fp, first = self.new_work()
        before = len(self.attempt_lines(first["work_id"]))
        updated = store.record_attempt(
            first["work_id"], run_id="run-1", attempt_no=1, pid=os.getpid(),
            status="started", note="go")
        self.assertIsInstance(updated, dict)
        lines = self.attempt_lines(first["work_id"])
        self.assertEqual(len(lines), before + 1)
        rendered = json.dumps(lines[-1])
        self.assertTrue(
            lines[-1].get("status") == "started" or lines[-1].get("event") == "started",
            rendered,
        )
        self.assertEqual(store.load(first["work_id"])["canonical_run_id"], "run-1")

    def test_attempts_are_readable_and_tolerate_partial_line(self):
        store, fp, first = self.new_work()
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        store.finish(first["work_id"], "ok", run_id="run-1")
        parsed = store.attempts(first["work_id"])
        self.assertIsInstance(parsed, list)
        self.assertGreaterEqual(len(parsed), 2)
        self.assertTrue(all(isinstance(row, dict) for row in parsed))
        self.assertEqual(parsed[0]["event"], "admitted")
        self.assertEqual(parsed[-1]["event"], "finished")

        path = os.path.join(self.root, first["work_id"], "attempts.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"event": "admitted", "attem')  # truncated final line
        parsed_again = store.attempts(first["work_id"])
        self.assertGreaterEqual(len(parsed_again), 2)
        self.assertEqual(parsed_again[0]["event"], "admitted")
        self.assertEqual(store.load(first["work_id"])["state"], "finished")

    def test_timestamps_are_iso_aware(self):
        store, _fp, result = self.new_work()
        doc = store.load(result["work_id"])
        for key in ("created", "updated"):
            value = doc.get(key)
            if value is None:
                continue
            parsed = datetime.fromisoformat(value)
            self.assertIsNotNone(parsed.tzinfo, "timestamp %s must be aware" % key)


class StalenessTest(WorkTestBase):
    def test_dead_pid_expires_and_admit_recovers(self):
        store = self.store()
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": "x", "workspace": self.ws})
        first = store.admit(fp)
        store.bind_run(first["work_id"], 1, "run-dead", pid=dead_pid())
        expired = store.expire_stale()
        self.assertIn(first["work_id"], expired)
        self.assertEqual(store.load(first["work_id"])["state"], "expired")
        self.assertEqual(store.load(first["work_id"])["terminal_status"], "expired")
        self.assertIsNone(store.find_active(fp))
        recovered = store.admit(fp)
        self.assertIs(recovered["reused"], False)
        self.assertEqual(recovered["attempt_no"], 2)

    def test_old_heartbeat_expires_and_admit_recovers(self):
        store = self.store(heartbeat_seconds=10.0, stale_after=5.0)
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": "x", "workspace": self.ws})
        first = store.admit(fp)
        store.bind_run(first["work_id"], 1, "run-old", pid=os.getpid())
        self.clock.advance(2.0)
        self.assertIsNotNone(store.find_active(fp))
        self.clock.advance(10.0)  # now - last_heartbeat > stale_after
        self.assertIsNone(store.find_active(fp))
        self.assertEqual(store.load(first["work_id"])["state"], "expired")
        self.assertFalse(store.admit(fp, dedupe_active=True)["reused"])
        self.assertEqual(store.admit(fp)["attempt_no"], 2)

    def test_expire_stale_ignores_bad_directory(self):
        bad = os.path.join(self.root, "garbage")
        os.makedirs(bad)
        with open(os.path.join(bad, "work.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        try:
            result = self.store().expire_stale()
        except Exception as exc:  # pragma: no cover - contract says never raise
            self.fail("expire_stale raised on bad directory: %r" % (exc,))
        self.assertIsInstance(result, list)


class HeartbeatFinishTest(WorkTestBase):
    def test_heartbeat_throttled_and_forced(self):
        store = self.store(heartbeat_seconds=10.0, stale_after=30.0)
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": "x", "workspace": self.ws})
        first = store.admit(fp)
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        base = store.load(first["work_id"])
        self.clock.advance(1.0)
        throttled = store.heartbeat(first["work_id"], run_id="run-1")
        if throttled is not None:
            # Within heartbeat_seconds/2 the timestamp must not move.
            self.assertEqual(throttled.get("last_heartbeat"), base.get("last_heartbeat"))
        self.clock.advance(4.0)
        forced = store.heartbeat(first["work_id"], run_id="run-1", force=True)
        self.assertIsNotNone(forced)
        self.assertNotEqual(forced.get("last_heartbeat"), base.get("last_heartbeat"))

    def test_heartbeat_never_resurrects_terminal(self):
        store, fp, first = self.new_work()
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        store.finish(first["work_id"], "ok", run_id="run-1")
        result = store.heartbeat(first["work_id"], run_id="run-1", force=True)
        if result is not None:
            self.assertNotEqual(result.get("state"), "active")
        self.assertEqual(store.load(first["work_id"])["state"], "finished")

    def test_finish_returns_none_for_unknown(self):
        self.assertIsNone(self.store().finish("w" + "0" * 19, "ok"))
    def test_hold_keeps_a_question_work_active(self):
        store, fp, first = self.new_work()
        store.bind_run(first["work_id"], 1, "run-1", pid=os.getpid())
        store.finish(first["work_id"], "ok", run_id="run-1")
        held = store.hold(first["work_id"], run_id="run-1",
                          status="needs_input")
        self.assertEqual(held.get("state"), "active")
        self.assertIsNone(held.get("terminal_status"))
        self.assertEqual(held.get("last_status"), "needs_input")
        self.assertIsNotNone(store.find_active(fp))
        again = store.admit(fp)
        self.assertIs(again["reused"], True)
        self.assertEqual(again.get("run_id"), "run-1")


class ConcurrencyTest(WorkTestBase):
    def test_eight_concurrent_admits_one_winner(self):
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": "race", "workspace": self.ws})
        barrier = threading.Barrier(8)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                store = work.WorkStore(self.root, clock=self.clock)
                barrier.wait(timeout=5.0)
                result = store.admit(fp)
                with lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - surfaced below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        winners = [r for r in results if r["reused"] is False]
        self.assertEqual(len(winners), 1, results)
        work_ids = {r["work_id"] for r in results}
        self.assertEqual(len(work_ids), 1)
        self.assertEqual(len(self.attempt_lines(work_ids.pop())), 1)
        self.assertEqual(winners[0]["attempt_no"], 1)


class ConfigAndAdmissionTest(unittest.TestCase):
    """Config switches and the jobs.start_job admission path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.env = base_env(self.root)
        self.cfg = load_config(config_path=self.root / "config.json", env=self.env)

    def test_config_defaults_and_operator_switch(self):
        self.assertIs(self.cfg.dedupe_active, True)
        self.assertEqual(self.cfg.task_completion, "job")
        env = dict(self.env, DSH_DEDUPE_ACTIVE="false",
                   DSH_TASK_COMPLETION="request")
        cfg = load_config(config_path=self.root / "config.json", env=env)
        self.assertIs(cfg.dedupe_active, False)
        self.assertEqual(cfg.task_completion, "request")

    def _spawn(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    def test_start_job_admits_and_dedupes_spawn(self):
        args = {"prompt": "build one thing", "workspace": str(self.ws)}
        proc = self._spawn()
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen",
                                  return_value=proc) as popen:
            first = jobs.start_job("harness_deepseek", dict(args),
                                   cfg=self.cfg, wait=0)
            second = jobs.start_job("harness_deepseek", dict(args),
                                    cfg=self.cfg, wait=0)
        self.assertEqual(popen.call_count, 1)
        self.assertIn("still running", first.lower())
        specs = list(Path(self.cfg.jobs_dir).glob("*.job.json"))
        self.assertEqual(len(specs), 1)
        spec = json.loads(specs[0].read_text(encoding="utf-8"))
        self.assertTrue(spec.get("work_id"))
        self.assertEqual(spec.get("attempt_no"), 1)
        self.assertEqual(spec["args"].get("_work_id"), spec["work_id"])
        self.assertEqual(spec["args"].get("_attempt_no"), 1)
        status = json.loads((Path(self.cfg.jobs_dir)
                             / (spec["run_id"] + ".status.json")).read_text(
                                 encoding="utf-8"))
        self.assertEqual(status.get("work_id"), spec["work_id"])
        self.assertEqual(status.get("attempt_no"), 1)
        doc = work.WorkStore(Path(self.cfg.state_dir) / "work").load(
            spec["work_id"])
        self.assertEqual(doc.get("canonical_run_id"), spec["run_id"])
        self.assertEqual(doc.get("state"), "active")
        self.assertIn(spec["run_id"], second)

    def test_reused_held_question_returns_a_note_not_the_old_result(self):
        args = {"prompt": "question task", "workspace": str(self.ws)}
        proc = self._spawn()
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen",
                                  return_value=proc) as popen:
            jobs.start_job("harness_deepseek", dict(args),
                           cfg=self.cfg, wait=0)
            specs = list(Path(self.cfg.jobs_dir).glob("*.job.json"))
            spec = json.loads(specs[0].read_text(encoding="utf-8"))
            work_id = spec["work_id"]
            jobs.write_status(
                Path(self.cfg.jobs_dir) / (spec["run_id"] + ".status.json"),
                status="done", progress="needs_input",
                question="Ship it?", result="old question-bearing result")
            work.WorkStore(Path(self.cfg.state_dir) / "work").hold(
                work_id, run_id=spec["run_id"], status="needs_input")
            second = jobs.start_job("harness_deepseek", dict(args),
                                    cfg=self.cfg, wait=0)
        self.assertEqual(popen.call_count, 1)
        self.assertIn("already asked", second)
        self.assertIn("Ship it?", second)
        self.assertIn("resume_deepseek", second)
        self.assertNotIn("old question-bearing result", second)

    def test_dedupe_disabled_starts_a_new_attempt(self):
        env = dict(self.env, DSH_DEDUPE_ACTIVE="false")
        cfg = load_config(config_path=self.root / "config.json", env=env)
        args = {"prompt": "build twice", "workspace": str(self.ws)}
        proc = self._spawn()
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen",
                                  return_value=proc) as popen:
            jobs.start_job("harness_deepseek", dict(args), cfg=cfg, wait=0)
            jobs.start_job("harness_deepseek", dict(args), cfg=cfg, wait=0)
        self.assertEqual(popen.call_count, 2)
        specs = sorted(Path(cfg.jobs_dir).glob("*.job.json"))
        attempts = sorted(json.loads(p.read_text())["attempt_no"] for p in specs)
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(len({json.loads(p.read_text())["work_id"]
                              for p in specs}), 1)


class HarnessWorkIntegrationTest(unittest.TestCase):
    """A real (fake-DSH) run and resume keep the same durable work identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.sessions = self.root / "sessions"
        self.fake = write_fake_dsh(self.root / "dsh")
        self.env = base_env(self.root, fake_dsh=self.fake,
                            sessions=self.sessions)
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)
        self.wstore = work.WorkStore(Path(self.cfg.state_dir) / "work")

    def _admit(self, prompt="journal task"):
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": prompt, "workspace": str(self.ws)})
        return self.wstore.admit(fp)

    def test_run_harness_writes_journal_result_and_finishes_work(self):
        write_session_log(self.sessions, self.ws)
        admission = self._admit()
        run_id = "journal-run-1"
        self.wstore.bind_run(admission["work_id"], 1, run_id)
        text, run, _path = harness.run_harness({
            "prompt": "journal task", "workspace": str(self.ws),
            "subagents": 0, "quality": False,
            "_run_id": run_id, "_work_id": admission["work_id"],
            "_attempt_no": 1,
        }, self.cfg)
        self.assertEqual(run["work_id"], admission["work_id"])
        self.assertEqual(run["attempt_no"], 1)
        log = events.EventLog(Path(self.cfg.state_dir) / "work"
                              / admission["work_id"])
        kinds = [event["type"] for event in log.read_since(0)]
        self.assertIn("phase", kinds)
        self.assertEqual(kinds.count("result"), 1)
        stored = log.read_result()
        self.assertIsNotNone(stored)
        self.assertIn("Deliverable written", stored["result"])
        doc = self.wstore.load(admission["work_id"])
        self.assertEqual(doc["state"], "finished")
        self.assertEqual(doc["terminal_status"], "ok")
        self.assertEqual(doc["canonical_run_id"], run_id)
        self.assertIn("Deliverable written", text)

    def test_superseded_run_cannot_append_admitted_after_rotation(self):
        admission = self._admit("superseded task")
        work_id = admission["work_id"]
        self.wstore.bind_run(work_id, 1, "run-old")
        self.wstore.begin_attempt(work_id, reason="resume")
        self.wstore.bind_run(work_id, 2, "run-new")
        log = events.EventLog(Path(self.cfg.state_dir) / "work" / work_id)
        # Simulate rotation having dropped the old admission event.
        log.append("admitted", {"run_id": "run-new", "attempt_no": 2})
        stale = {"id": "run-old", "work_id": work_id, "attempt_no": 1,
                 "status": "ok", "answer": "stale answer"}
        harness._finalize_work(stale, self.cfg)
        kinds = [event["type"] for event in log.read_since(0)]
        self.assertEqual(kinds, ["admitted"], kinds)
        self.assertEqual(
            events.latest_admitted_run_id(log.read_since(0)), "run-new")
        self.assertEqual(self.wstore.load(work_id)["state"], "active")

        live = {"id": "run-new", "work_id": work_id, "attempt_no": 2,
                "status": "ok", "answer": "live answer"}
        harness._finalize_work(live, self.cfg)
        current = events.terminal_after_latest_admission(log.read_since(0))
        self.assertEqual(current["payload"]["run_id"], "run-new")
        self.assertEqual(log.read_result()["result"], "live answer")
        self.assertEqual(self.wstore.load(work_id)["state"], "finished")

    def test_harness_attention_defers_terminal_and_holds_work(self):
        admission = self._admit("attention task")
        run_id = "attention-run-1"
        self.wstore.bind_run(admission["work_id"], 1, run_id)
        question = ("Built it.\n\n```attention\n%s\n```\n"
                    % json.dumps({"question": "Ship it?",
                                  "options": ["yes", "no"]}))
        run = {"id": run_id, "work_id": admission["work_id"],
               "attempt_no": 1, "status": "ok", "answer": question}
        harness._finalize_work(run, self.cfg)
        log = events.EventLog(Path(self.cfg.state_dir) / "work"
                              / admission["work_id"])
        kinds = [event["type"] for event in log.read_since(0)]
        self.assertIn("admitted", kinds)
        self.assertIn("needs_input", kinds)
        self.assertNotIn("result", kinds)
        self.assertIsNone(events.terminal_after_latest_admission(
            log.read_since(0)))
        stored = log.read_result()
        self.assertEqual(stored["status"], "needs_input")
        doc = self.wstore.load(admission["work_id"])
        self.assertEqual(doc["state"], "active")
        self.assertIsNone(doc["terminal_status"])
        self.assertEqual(doc["last_status"], "needs_input")

    def test_resume_increments_attempt_under_same_work_id(self):
        write_session_log(self.sessions, self.ws)
        admission = self._admit("resume task")
        work_id = admission["work_id"]
        old_run = "resume-old-1"
        self.wstore.bind_run(work_id, 1, old_run)
        saved_args = {
            "_run_id": old_run, "_work_id": work_id, "_attempt_no": 1,
            "prompt": "resume task", "workspace": str(self.ws),
            "subagents": 0, "quality": True, "independent_review": True,
            "quality_rounds": 1,
        }
        run = store.new_run("harness", saved_args, "resume task", self.cfg)
        run.update(status="running", workspace=str(self.ws), subagents=0,
                   quality_used=True, independent_review=True,
                   quality_rounds=1,
                   builder_sandbox={"preset": "workspace-write"},
                   reviewer_sandbox={"preset": "read-only"})
        store.save_run(run, self.cfg)
        checkpoint = checkpoints.Checkpoint(
            Path(self.cfg.state_dir) / "checkpoints", old_run)
        checkpoint.create(["builder", "review-1"], metadata={"spec": {
            "prompt": "resume task", "workspace": str(self.ws),
            "subagents": 0, "quality_used": True,
            "independent_review": True, "quality_rounds": 1,
            "work_id": work_id, "attempt_no": 1,
        }})
        checkpoint.finish("builder", status="done", answer="builder answer",
                          output_files=["deliverable.md"], seconds=1.0)

        _text, resumed, _path = harness.resume_harness(
            {"resume_run_id": old_run}, self.cfg)
        self.assertEqual(resumed.get("work_id"), work_id)
        self.assertEqual(resumed.get("attempt_no"), 2)
        self.assertEqual(resumed.get("resumed_from"), old_run)
        doc = self.wstore.load(work_id)
        self.assertEqual(doc["attempt_no"], 2)
        self.assertEqual(doc["state"], "finished")
        self.assertEqual(doc["canonical_run_id"], resumed["id"])
        self.assertNotEqual(doc["canonical_run_id"], old_run)
        attempts = self.wstore.attempts(work_id)
        resumed_attempts = [item for item in attempts
                            if item.get("reason") == "resume"]
        self.assertEqual(len(resumed_attempts), 1)
        self.assertEqual(resumed_attempts[0]["attempt_no"], 2)


class ResumeJobIdentityTest(unittest.TestCase):
    """The detached resume job must carry the saved work identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.env = base_env(self.root)
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)
        self.wstore = work.WorkStore(Path(self.cfg.state_dir) / "work")

    def _saved_run(self, run_id="resume-source-1"):
        fp = work.request_fingerprint(
            "harness_deepseek", {"prompt": "resume identity",
                                 "workspace": str(self.ws)})
        admission = self.wstore.admit(fp)
        work_id = admission["work_id"]
        self.wstore.bind_run(work_id, 1, run_id)
        args = {"_run_id": run_id, "_work_id": work_id, "_attempt_no": 1,
                "prompt": "resume identity", "workspace": str(self.ws)}
        run = store.new_run("harness", args, "resume identity", self.cfg)
        run.update(status="running", workspace=str(self.ws), subagents=0)
        store.save_run(run, self.cfg)
        return run_id, work_id

    def test_resume_tools_write_work_id_into_spec_and_status(self):
        run_id, work_id = self._saved_run()
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        cases = [
            ("resume_deepseek", {"run_id": run_id}),
            ("harness_deepseek", {"prompt": "resume identity",
                                  "resume_run_id": run_id}),
        ]
        with mock.patch.object(jobs, "_worker_env", return_value={}), \
                mock.patch.object(jobs.subprocess, "Popen",
                                  return_value=proc) as popen:
            for tool, args in cases:
                jobs.start_job(tool, dict(args), cfg=self.cfg, wait=0)
        self.assertEqual(popen.call_count, 2)
        specs = list(Path(self.cfg.jobs_dir).glob("*.job.json"))
        self.assertEqual(len(specs), 2)
        for path in specs:
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(spec.get("work_id"), work_id, spec)
            self.assertEqual(spec.get("attempt_no"), 1)
            self.assertEqual(spec["args"].get("_work_id"), work_id)
            status = json.loads((Path(self.cfg.jobs_dir)
                                 / (spec["run_id"] + ".status.json")).read_text(
                                     encoding="utf-8"))
            self.assertEqual(status.get("work_id"), work_id)
            self.assertEqual(status.get("attempt_no"), 1)


if __name__ == "__main__":
    unittest.main()
