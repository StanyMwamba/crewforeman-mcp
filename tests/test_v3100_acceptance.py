"""W1-F independent acceptance tests for 3.10.0 part A (reviewer backup).

These are candidate acceptance tests, written before integration. They encode
the contract in SLICES.md S6 and may fail on the 3.9.5 baseline / partially
integrated tree. Run only this file:

    python3 -m unittest tests.test_v3100_acceptance -v
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import authority, harness, health, work
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh
import tests

os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")
BANNER_PREFIX = "Reviewed by: backup ("


def _walk_values(value, key):
    """Yield values for ``key`` anywhere in a nested dict/list."""
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                yield item
            yield from _walk_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item, key)


@unittest.skipIf(
    os.name == "nt",
    "acceptance fakes are POSIX /bin/sh wrappers executed as DSH_BIN; "
    "Windows uses the compiled launcher from tests.fakes.write_fake_dsh")
class BackupAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.counter = self.root / "reviewer-calls.txt"
        self.fail_main = self.root / "fail-main"
        self.fail_message = self.root / "fail-message.txt"
        self.real = write_fake_dsh(self.root / "fake-dsh")
        self.main = self._wrapper("main-dsh", self.real, "main")
        self.backup = self._wrapper("backup-dsh", self.real, "backup")
        self.env = base_env(self.root, fake_dsh=self.main,
                            sessions=self.sessions)
        self.env.update({
            "DSH_BACKUP_BIN": str(self.backup),
            "DSH_BACKUP_REVIEWER_VENDOR": "Kimi",
            "DSH_BACKUP_REVIEWER_MODEL": "kimi-k2",
            "FAKE_DSH_REVIEW_BLOCKING": "0",
        })

    def _wrapper(self, name, real, role):
        """Main wrapper fails reviewer calls while fail-main exists; backup
        never fails. Every reviewer call is appended to the counter."""
        path = self.root / name
        if role == "main":
            review_call = (
                'echo main-review >> "%s"\n'
                'if [ -f "%s" ]; then\n'
                '  MSG=$(cat "%s" 2>/dev/null || echo "insufficient balance")\n'
                '  exec env FAKE_DSH_FAIL_REVIEW=1 \\\n'
                '    FAKE_DSH_REVIEW_ERROR_MESSAGE="$MSG" \\\n'
                '    "%s" "$@"\n'
                'fi\n'
                'exec "%s" "$@"\n') % (self.counter, self.fail_main,
                                        self.fail_message, real, real)
        else:
            review_call = ('echo backup-review >> "%s"\n'
                           'exec "%s" "$@"\n') % (self.counter, real)
        text = (
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"INDEPENDENT REVIEWER"*)\n'
            '    %s'
            '    ;;\n'
            '  *) exec "%s" "$@" ;;\n'
            'esac\n') % (review_call, real)
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return path

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def _run(self, work_id=None, **fake_env):
        fake_env = dict(fake_env)
        fake_env.setdefault("FAKE_DSH_SESSIONS", str(self.sessions))
        args = {
            "prompt": "Write a concise release note for the fake product",
            "subagents": 1, "quality": True, "quality_rounds": 1,
            "workspace": str(self.workspace),
        }
        if work_id is not None:
            args["_work_id"] = str(work_id)
            args["_attempt_no"] = 1
        with mock.patch.dict(os.environ, fake_env, clear=False):
            return harness.run_harness(args, self.cfg())

    def calls(self):
        if not self.counter.is_file():
            return []
        return [line.strip() for line in
                self.counter.read_text(encoding="utf-8").splitlines() if line.strip()]

    def fail_main_with(self, message):
        self.fail_message.write_text(message, encoding="utf-8")
        self.fail_main.write_text("1", encoding="utf-8")

    def all_output(self, run, text):
        blobs = [text or "", json.dumps(run, default=str)]
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".md", ".log"):
                try:
                    blobs.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        return "\n".join(blobs)

    def authority_seconds(self, run):
        values = list(_walk_values(run, "job_max_seconds"))
        return [v for v in values if isinstance(v, (int, float))]

    def test_backup_switches_on_quota_failure_and_banner_is_recorded(self):
        self.fail_main.write_text("1", encoding="utf-8")
        text, run, _path = self._run()
        self.assertEqual(run.get("reviewed_by"), "backup", run.get("review_status"))
        banner = ("Reviewed by: backup (Kimi/kimi-k2) — main failed: "
                  "credit/budget")
        self.assertIn(banner, text.splitlines()[0],
                      "banner missing from the job result header")
        self.assertIn(banner, self.all_output(run, text))
        self.assertIn("main-review", self.calls())
        self.assertIn("backup-review", self.calls())
        self.assertEqual(run.get("quality_score"), 8.6,
                         "backup's successful review score must be used")

    def test_backup_banner_is_recorded_in_real_work_journal(self):
        cfg = self.cfg()
        admission = work.WorkStore(Path(cfg.state_dir) / "work").admit(
            "fp-3100a-backup-journal", idempotency_key="backup-journal-test")
        work_id = admission["work_id"]
        self.fail_main.write_text("1", encoding="utf-8")
        text, run, _path = self._run(work_id=work_id)
        banner = ("Reviewed by: backup (Kimi/kimi-k2) — main failed: "
                  "credit/budget")
        self.assertEqual(run.get("reviewed_by_line"), banner)
        self.assertEqual((run.get("reviewer_backup") or {}).get("banner"),
                         banner)
        self.assertIn(banner, text.splitlines()[0],
                      "banner missing from the job result header")
        journal = Path(cfg.state_dir) / "work" / work_id / "events.jsonl"
        self.assertTrue(journal.is_file(), "no durable work journal at %s"
                        % journal)
        records = [json.loads(line) for line in
                   journal.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        backups = [item for item in records
                   if item.get("type") == "reviewer_backup"]
        self.assertTrue(backups, "no reviewer_backup event in %s: %r"
                        % (journal, [item.get("type") for item in records]))
        final = backups[-1].get("payload") or {}
        self.assertEqual(final.get("status"), "ok")
        self.assertEqual(final.get("banner"), banner)
        self.assertEqual(final.get("run_id"), run.get("id"))

    def test_fake_failure_classes_switch_to_backup(self):
        cases = {
            "credit/budget": "429 too many requests: insufficient balance",
            "auth": "401 unauthorized: invalid api key",
            "service unavailable (5xx)": "503 service unavailable",
            "stream dropped": "flush on a closed handle: STREAM_CLOSED",
        }
        for reason_class, message in cases.items():
            with self.subTest(reason_class=reason_class):
                self.counter.unlink(missing_ok=True)
                self.fail_main_with(message)
                _text, run, _path = self._run()
                backup = run.get("reviewer_backup") or {}
                self.assertEqual(run.get("reviewed_by"), "backup",
                                 (reason_class, run.get("review_error")))
                self.assertEqual(backup.get("reason_class"), reason_class)
                self.assertIn("Reviewed by: backup (",
                              run.get("reviewed_by_line") or "")
                self.assertIn("main failed: %s" % reason_class,
                              run.get("reviewed_by_line") or "")
                calls = self.calls()
                self.assertIn("main-review", calls)
                self.assertIn("backup-review", calls)
                self.assertEqual(run.get("quality_score"), 8.6)

    def test_no_switch_on_low_score(self):
        text, run, _path = self._run(FAKE_DSH_REVIEW_SCORE="7.0")
        self.assertNotEqual(run.get("reviewed_by"), "backup")
        self.assertNotIn(BANNER_PREFIX, self.all_output(run, text))
        reviews = [c for c in self.calls() if c.endswith("-review")]
        self.assertTrue(reviews, "reviewer was never called")
        self.assertTrue(all(c == "main-review" for c in reviews), reviews)
        self.assertEqual(run.get("quality_score"), 7.0)

    def test_no_switch_on_blocking_defects_or_missing_score(self):
        cases = (
            {"FAKE_DSH_REVIEW_SCORE": "9.5",
             "FAKE_DSH_REVIEW_BLOCKING": "1"},
            {"FAKE_DSH_REVIEW_MISSING_SCORE": "1"},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                self.counter.unlink(missing_ok=True)
                _text, run, _path = self._run(**extra)
                self.assertNotEqual(run.get("reviewed_by"), "backup")
                self.assertNotIn("Reviewed by: backup (",
                                 run.get("reviewed_by_line") or "")
                reviews = [c for c in self.calls()
                           if c.endswith("-review")]
                self.assertTrue(reviews, "reviewer was never called")
                self.assertTrue(all(c == "main-review" for c in reviews),
                                reviews)

    def test_next_job_tries_main_first_again(self):
        self.fail_main.write_text("1", encoding="utf-8")
        _text, first, _path = self._run()
        self.assertEqual(first.get("reviewed_by"), "backup")
        # Main is healthy again for job 2: backup must not be sticky.
        self.fail_main.unlink()
        _text, second, _path = self._run()
        self.assertNotEqual(second.get("reviewed_by"), "backup")
        reviews = [c for c in self.calls() if c.endswith("-review")]
        self.assertEqual(reviews.count("main-review"), 2, reviews)
        self.assertEqual(reviews.count("backup-review"), 1, reviews)
        self.assertNotIn("Reviewed by: backup", json.dumps(second))

    def test_both_reviewers_fail_never_invents_score(self):
        self.fail_main.write_text("1", encoding="utf-8")
        # Ensure backup also fails: overwrite the backup wrapper.
        self.backup.write_text(
            '#!/bin/sh\ncase "$*" in *"INDEPENDENT REVIEWER"*)\n'
            '  echo backup-review >> "%s"\n'
            '  exec env FAKE_DSH_FAIL_REVIEW=1 '
            'FAKE_DSH_REVIEW_ERROR_MESSAGE="503 service unavailable" '
            '"%s" "$@" ;;\n*) exec "%s" "$@" ;;\nesac\n'
            % (self.counter, self.real, self.real), encoding="utf-8")
        self.backup.chmod(0o755)
        text, run, _path = self._run()
        self.assertIsNone(run.get("quality_score"),
                          "both reviewers failed; score must stay None")
        self.assertNotIn("Final quality score: 0.0", text or "")
        self.assertNotIn("Final quality score: 0.0", json.dumps(run, default=str))
        self.assertNotEqual(run.get("review_status"), "accepted")

    def test_explicit_job_max_seconds_survives_default_7200(self):
        with mock.patch.dict(os.environ, {"FAKE_DSH_SESSIONS": str(self.sessions)},
                             clear=False):
            _text, run, _path = harness.run_harness({
                "prompt": "Write a short fake note", "subagents": 1,
                "quality": False, "job_max_seconds": 10800,
                "workspace": str(self.workspace),
            }, self.cfg())
        seconds = self.authority_seconds(run)
        self.assertIn(10800, seconds,
                      "explicit job_max_seconds clamped: %r" % (seconds,))
        self.assertEqual(run["job_budget_initial"]["max_seconds"], 10800.0,
                         "the execution budget was clamped below the caller "
                         "value")

    def test_stream_failures_classify_as_network(self):
        self.assertEqual(health.classify("fetch failed: STREAM_CLOSED"), "network")
        self.assertEqual(health.classify("flush on a closed handle"), "network")
        policy = health.RetryPolicy("reviewer")
        self.assertLessEqual(policy.max_attempts, 3,
                             "reviewer stream retries must stay bounded")

    def test_resume_reruns_failed_review(self):
        text, run, _path = self._run(FAKE_DSH_FAIL_REVIEW="1")
        run_id = run.get("run_id") or run.get("id")
        self.assertTrue(run_id, "run record has no id")
        before = len([c for c in self.calls() if c == "main-review"])
        with mock.patch.dict(os.environ, {"FAKE_DSH_SESSIONS": str(self.sessions)},
                             clear=False):
            _text, resumed, _path = harness.resume_harness(
                {"resume_run_id": run_id}, self.cfg())
        after = len([c for c in self.calls() if c == "main-review"])
        self.assertGreater(after, before,
                          "resume reused the failed review instead of re-running")
        self.assertTrue(resumed.get("resumed"))


def _write_selective_fake(path, real, calls, role, counter,
                          preflight_fail_times=0, preflight_message="",
                          review_fail_times=0, review_message="",
                          preflight_model=None, review_model=None):
    """A dsh wrapper that can fail preflight or review calls selectively.

    Every call appends ``<role>-preflight`` / ``<role>-review`` to *calls* so the
    tests can assert bounded attempt counts without relying on mutable state in
    the real fake.
    """
    source = '''#!/usr/bin/env python3
import json
import os
import sys

REAL = %(real)r
CALLS = %(calls)r
ROLE = %(role)r
COUNTER = %(counter)r
PREFLIGHT_FAILS = %(preflight_fail_times)d
PREFLIGHT_MESSAGE = %(preflight_message)r
REVIEW_FAILS = %(review_fail_times)d
REVIEW_MESSAGE = %(review_message)r
PREFLIGHT_MODEL = %(preflight_model)r
REVIEW_MODEL = %(review_model)r

task = sys.argv[-1] if len(sys.argv) else ""


def bump():
    try:
        with open(COUNTER, "r", encoding="utf-8") as handle:
            value = int(handle.read().strip() or "0")
    except Exception:
        value = 0
    value += 1
    with open(COUNTER, "w", encoding="utf-8") as handle:
        handle.write(str(value))
    return value


def record(kind):
    with open(CALLS, "a", encoding="utf-8") as handle:
        handle.write("%%s-%%s\\n" %% (ROLE, kind))


if "Reply with exactly READY" in task:
    record("preflight")
    attempt = bump()
    if attempt <= PREFLIGHT_FAILS:
        print(json.dumps({"type": "error", "message": PREFLIGHT_MESSAGE}),
              flush=True)
        sys.exit(1)
    print(json.dumps({"type": "final", "text": "READY"}), flush=True)
    sys.exit(0)
if "INDEPENDENT REVIEWER" in task:
    record("review")
    attempt = bump()
    if attempt <= REVIEW_FAILS:
        os.environ["FAKE_DSH_FAIL_REVIEW"] = "1"
        os.environ["FAKE_DSH_REVIEW_ERROR_MESSAGE"] = REVIEW_MESSAGE
    if REVIEW_MODEL:
        os.environ["FAKE_DSH_MODEL"] = REVIEW_MODEL
    os.execv(REAL, [REAL] + sys.argv[1:])
os.execv(REAL, [REAL] + sys.argv[1:])
''' % {
        "real": str(real), "calls": str(calls), "role": role,
        "counter": str(counter),
        "preflight_fail_times": int(preflight_fail_times),
        "preflight_message": preflight_message,
        "review_fail_times": int(review_fail_times),
        "review_message": review_message,
        "preflight_model": preflight_model or "",
        "review_model": review_model or "",
    }
    path = Path(path)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@unittest.skipIf(
    os.name == "nt",
    "acceptance fakes are POSIX /bin/sh wrappers executed as DSH_BIN; "
    "Windows uses the compiled launcher from tests.fakes.write_fake_dsh")
class BackupIdentityRefreshTests(unittest.TestCase):
    """Paid fallback: final metadata must name the successful backup model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.calls = self.root / "calls.txt"
        self.real = write_fake_dsh(self.root / "fake-dsh")
        self.main = _write_selective_fake(
            self.root / "main-dsh", self.real, self.calls, "main",
            self.root / "main-count",
            review_fail_times=999,
            review_message="503 service unavailable",
            review_model="main-model")
        self.backup = _write_selective_fake(
            self.root / "backup-sonnet", self.real, self.calls, "backup",
            self.root / "backup-count", review_model="backup-model")
        self.env = base_env(self.root, fake_dsh=self.main,
                            sessions=self.sessions)
        self.env.update({
            "DSH_BACKUP_BIN": str(self.backup),
            "FAKE_DSH_REVIEW_BLOCKING": "0",
        })

    def cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return load_config(config_path=self.root / "config.json", env=env)

    def calls_for(self, label):
        if not self.calls.is_file():
            return []
        return [line.strip() for line in
                self.calls.read_text(encoding="utf-8").splitlines()
                if line.strip() == label]

    def test_final_metadata_uses_successful_backup_model(self):
        with mock.patch.dict(os.environ,
                             {"FAKE_DSH_SESSIONS": str(self.sessions)},
                             clear=False):
            text, run, _path = harness.run_harness({
                "prompt": "Write a concise release note for the fake product",
                "subagents": 1, "quality": True, "quality_rounds": 1,
                "workspace": str(self.workspace),
            }, self.cfg())
        self.assertEqual(run.get("reviewed_by"), "backup",
                         run.get("review_error"))
        expected = ("Reviewed by: backup (Anthropic/backup-model) — main "
                    "failed: service unavailable (5xx)")
        self.assertEqual(run.get("reviewed_by_line"), expected)
        metadata = run.get("reviewer_backup") or {}
        self.assertEqual(metadata.get("vendor"), "Anthropic")
        self.assertEqual(metadata.get("model"), "backup-model")
        self.assertEqual(metadata.get("banner"), expected)
        self.assertIn(expected, text.splitlines()[0])
        # The main 5xx is retried once before the switch, so two main calls
        # precede the single backup call.
        self.assertEqual(self.calls_for("main-review"),
                         ["main-review", "main-review"])
        self.assertEqual(self.calls_for("backup-review"), ["backup-review"])


class PreflightRetryTests(unittest.TestCase):
    """Default preflight: retry transient main failures before failover."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.case_seq = 0

    def _case(self, main_message, main_fails=2, backup_fails=0,
              backup_message="503 service unavailable"):
        self.case_seq += 1
        tag = "%s-%d" % (self._testMethodName, self.case_seq)
        calls = self.root / ("calls-%s.txt" % tag)
        calls.unlink(missing_ok=True)
        real = write_fake_dsh(self.root / ("fake-dsh-%s" % tag))
        main = _write_selective_fake(
            self.root / ("main-%s" % tag), real, calls,
            "main", self.root / ("main-count-%s" % tag),
            preflight_fail_times=main_fails,
            preflight_message=main_message)
        backup = _write_selective_fake(
            self.root / ("backup-sonnet-%s" % tag), real,
            calls, "backup",
            self.root / ("backup-count-%s" % tag),
            preflight_fail_times=backup_fails,
            preflight_message=backup_message)
        env = base_env(self.root, fake_dsh=main, sessions=self.sessions)
        env.update({
            "DSH_BACKUP_BIN": str(backup),
            "DSH_REVIEW_PREFLIGHT": "1",
            "FAKE_DSH_REVIEW_BLOCKING": "0",
        })
        slept = []

        def fake_sleep(policy, seconds):
            slept.append(seconds)

        with mock.patch.dict(os.environ,
                             {"FAKE_DSH_SESSIONS": str(self.sessions)},
                             clear=False):
            with mock.patch.object(health.RetryPolicy, "sleep", fake_sleep):
                text, run, _path = harness.run_harness({
                    "prompt": "Write a concise release note for the fake product",
                    "subagents": 1, "quality": True, "quality_rounds": 1,
                    "workspace": str(self.workspace),
                }, load_config(config_path=self.root / "config.json", env=env))
        lines = ([line.strip() for line in
                  calls.read_text(encoding="utf-8").splitlines()
                  if line.strip()] if calls.is_file() else [])
        return text, run, lines, slept

    def test_transient_preflight_failures_retry_main_twice_then_switch(self):
        cases = {
            "service unavailable (5xx)": "503 service unavailable",
            "stream dropped": "STREAM_CLOSED",
        }
        for reason_class, message in cases.items():
            with self.subTest(reason_class=reason_class):
                text, run, lines, slept = self._case(message)
                self.assertEqual(run.get("reviewed_by"), "backup",
                                 run.get("review_error"))
                self.assertEqual(lines.count("main-preflight"), 2, lines)
                self.assertEqual(lines.count("backup-preflight"), 1, lines)
                self.assertEqual(lines.count("main-review"), 0, lines)
                self.assertEqual(lines.count("backup-review"), 1, lines)
                self.assertEqual(slept, [1.0],
                                 "one bounded backoff before the second attempt")
                preflight = run.get("reviewer_preflight") or {}
                self.assertEqual(preflight.get("status"), "ok")
                self.assertEqual(preflight.get("attempts"), 2)
                self.assertEqual(preflight.get("backend"), "backup")
                metadata = run.get("reviewer_backup") or {}
                self.assertEqual(metadata.get("status"), "ok")
                self.assertEqual(metadata.get("reason_class"), reason_class)
                self.assertIn("Reviewed by: backup (",
                              run.get("reviewed_by_line") or "")
                self.assertIn("main failed: %s" % reason_class,
                              run.get("reviewed_by_line") or "")
                self.assertIn(run["reviewed_by_line"], text.splitlines()[0])

    def test_auth_and_credit_preflight_switch_immediately(self):
        cases = {
            "auth": "401 unauthorized: invalid api key",
            "credit/budget": "HTTP 402 Payment Required: insufficient balance",
        }
        for reason_class, message in cases.items():
            with self.subTest(reason_class=reason_class):
                _text, run, lines, slept = self._case(message)
                self.assertEqual(run.get("reviewed_by"), "backup",
                                 run.get("review_error"))
                self.assertEqual(lines.count("main-preflight"), 1, lines)
                self.assertEqual(lines.count("backup-preflight"), 1, lines)
                self.assertEqual(slept, [],
                                 "non-retryable auth/credit must not sleep")
                self.assertEqual(
                    (run.get("reviewer_backup") or {}).get("reason_class"),
                    reason_class)

    def test_both_preflights_fail_scoreless_and_clear(self):
        _text, run, lines, slept = self._case(
            "503 service unavailable", main_fails=999, backup_fails=999)
        self.assertEqual(lines.count("main-preflight"), 2, lines)
        self.assertEqual(lines.count("backup-preflight"), 1, lines)
        self.assertEqual(lines.count("main-review"), 0, lines)
        self.assertEqual(lines.count("backup-review"), 0, lines)
        self.assertEqual(slept, [1.0])
        self.assertIsNone(run.get("quality_score"),
                          "both reviewers failed; score must stay None")
        self.assertEqual(run.get("review_status"), "error")
        metadata = run.get("reviewer_backup") or {}
        self.assertEqual(metadata.get("status"), "error")
        self.assertIn("503 service unavailable",
                      metadata.get("main_error") or "")
        self.assertIn("503 service unavailable",
                      metadata.get("backup_error") or "")
        message = run.get("review_error") or ""
        # 3.11.0: the terminal message is attributed to the reviewer that
        # actually ran (vendor/model), not "main + raw backup text".
        self.assertIn("reviewer failed", message)
        self.assertIn("Anthropic", message)
        self.assertNotIn("DeepSeek account balance is empty", message)
        self.assertNotIn("503 service unavailable", message,
                         "raw provider text must stay in backup_error, not "
                         "the user-facing attributed sentence")
        self.assertNotIn("Final quality score: 0.0", json.dumps(run, default=str))


if __name__ == "__main__":
    unittest.main()
