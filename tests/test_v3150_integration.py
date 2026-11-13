"""3.15.0 integration: MiMo executions route through the mimo_worker adapter.

Covers the ``_execute_once`` dispatch hook (builder, auth errors, resume
``-s``), the crosscheck crew wiring (``run['crosscheck_crew']`` and the
vendor-conflict fail-fast), the reviewer preflight READY flow for MiMo
candidates (probe model pings) and the unchanged dsh path.  Fake ``mimo`` and
``dsh`` programs only: no real MiMo, no network.  Standard library only and
Windows/macOS/Linux safe.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import crosscheck as crosscheck_mod
from deepseek_mcp import harness, model_policy
from deepseek_mcp.config import load_config

try:
    from tests import fakes, mimo_fakes
except ImportError:  # tests run as top-level modules
    import fakes
    import mimo_fakes

try:
    from deepseek_mcp import mimo_worker
except Exception:  # W1 lands in parallel; the hook degrades to the dsh path.
    mimo_worker = None

_MIMO_REASON = "deepseek_mcp.mimo_worker adapter is absent"

# mimo_fakes "ok" mode replays tests/fixtures/mimo/mimo-0.1.17-run-events.jsonl
# verbatim: two step_finish parts (input 19925+1354, cache read 10240+31872,
# cache write 0+0, output 104+2299, reasoning 22+98, total 30291+35623) whose
# CLI part.cost values are 0.008813859 + 0.0027891192 (cost_source "vendor").
OK_TOKENS = {"tokens_in": 21279, "cache_read_tokens": 42112,
             "cache_write_tokens": 0, "tokens_out": 2403,
             "reasoning_tokens": 120, "total_tokens": 65914}
OK_COST = 0.008813859 + 0.0027891192  # 0.0116029782, kept at round(cost, 6)
OK_ANSWER = "I'll start by reading the brief and understanding the workspace."
OK_SESSION = "ses_ffe5e2a611a85ffeatM04xDPrk"

# Inline MiMo fake for the reviewer preflight/review flow.  W11's mimo_fakes
# covers READY and the ok/error streams but has no review-report mode (its
# reviews would parse as unscored), so the review flow uses this minimal fake.
# It emits the real mimo 0.1.17 event shape verified in
# tests/fixtures/mimo/mimo-0.1.17-run-events.jsonl: top-level type/timestamp/
# sessionID plus a `part` payload (text as part.text, step_finish tokens with
# the cache {read, write} dict and part.cost, tool_use as part.tool with
# part.state), and errors as top-level error.name / error.data.{message,
# statusCode} like mimo-0.1.17-error-401.jsonl.  With FAKE_MIMO_MODE=error_auth
# it emits that 401 error line and exits 1.  It appends one JSON argv line per
# invocation to FAKE_MIMO_ARGV so a preflight and a review can both be
# inspected; `-s <session>` is echoed as the events' sessionID (the fixture
# recording's own id is used otherwise).
_REVIEWER_FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

argv = sys.argv

capture = os.environ.get("FAKE_MIMO_ARGV")
if capture:
    try:
        entries = []
        try:
            entries = [json.loads(line) for line in
                       Path(capture).read_text(encoding="utf-8").splitlines()
                       if line.strip()]
        except Exception:
            entries = []
        entries.append(argv)
        Path(capture).write_text(
            "\n".join(json.dumps(item) for item in entries) + "\n",
            encoding="utf-8")
    except OSError:
        pass

task = ""
workspace = Path(os.getcwd())
session = ""
positionals = []
index = 1
while index < len(argv):
    arg = argv[index]
    if arg == "--dir":
        index += 1
        if index < len(argv):
            workspace = Path(argv[index])
    elif arg in ("-s", "--session"):
        index += 1
        if index < len(argv):
            session = argv[index]
    elif not arg.startswith("-"):
        positionals.append(arg)
    index += 1
if positionals and positionals[0] == "run" and len(positionals) > 1:
    task = positionals[1]
elif positionals:
    task = positionals[0]

SNAPSHOT = "7108b384f4ddfdd09670d588110e4c6138c44571"
if not session:
    session = "ses_ffe5e2a611a85ffeatM04xDPrk"

_seq = [0]

def _ids():
    _seq[0] += 1
    return ("prt_reviewer_%06d" % _seq[0], "msg_reviewer_%06d" % _seq[0])

def emit(payload):
    try:
        print(json.dumps(payload), flush=True)
    except (BrokenPipeError, OSError):
        raise SystemExit(0)

def emit_text(text):
    started = int(time.time() * 1000)
    ended = int(time.time() * 1000)
    prt, msg = _ids()
    emit({"type": "text", "timestamp": ended, "sessionID": session,
          "part": {"id": prt, "messageID": msg, "sessionID": session,
                   "type": "text", "text": text,
                   "time": {"start": started, "end": ended}}})

def emit_step_start():
    prt, msg = _ids()
    emit({"type": "step_start", "timestamp": int(time.time() * 1000),
          "sessionID": session,
          "part": {"id": prt, "messageID": msg, "sessionID": session,
                   "snapshot": SNAPSHOT, "type": "step-start"}})

def emit_tool(tool, tool_input, output, title):
    started = int(time.time() * 1000)
    ended = int(time.time() * 1000)
    prt, msg = _ids()
    emit({"type": "tool_use", "timestamp": ended, "sessionID": session,
          "part": {"type": "tool", "execution": "started",
                   "callID": "call_reviewer_%06d" % _seq[0], "tool": tool,
                   "state": {"status": "completed", "input": tool_input,
                             "output": output,
                             "metadata": {"truncated": False},
                             "title": title,
                             "time": {"start": started, "end": ended}},
                   "id": prt, "messageID": msg, "sessionID": session}})

def emit_step_finish():
    prt, msg = _ids()
    emit({"type": "step_finish", "timestamp": int(time.time() * 1000),
          "sessionID": session,
          "part": {"id": prt, "reason": "stop", "snapshot": SNAPSHOT,
                   "messageID": msg, "sessionID": session,
                   "type": "step-finish",
                   "tokens": {"total": 134, "input": 70, "output": 38,
                              "reasoning": 6,
                              "cache": {"read": 20, "write": 0}},
                   "cost": 0.00042}})

def emit_error(message, status_code):
    emit({"type": "error", "timestamp": int(time.time() * 1000),
          "sessionID": session,
          "error": {"name": "APIError",
                    "data": {"message": message,
                             "statusCode": status_code,
                             "isRetryable": False,
                             "metadata": {
                                 "providerID": "xiaomi",
                                 "url": ("https://api.xiaomimimo.com/v1/"
                                         "chat/completions")}}}})

REPORT = ("# Independent review\n\n## Score\nFinal quality score: 9.2/10\n\n"
          "## Blocking defects\nNone\n\n## Non-blocking defects\nNone\n\n"
          "## Fix list\nNone\n")

if os.environ.get("FAKE_MIMO_MODE") == "error_auth":
    emit_error("Invalid API Key: Please provide valid API Key", 401)
    raise SystemExit(1)
if task.lstrip().startswith("Reply with exactly READY"):
    emit_text("READY")
    raise SystemExit(0)
if "INDEPENDENT REVIEWER" in task or "SCORE REPROMPT" in task:
    try:
        (workspace / "REVIEW.md").write_text(REPORT, encoding="utf-8")
    except OSError:
        pass
    emit_text(REPORT)
    raise SystemExit(0)
emit_step_start()
emit_tool("bash", {"command": "ls"}, "", "List files")
emit_step_finish()
emit_text("Fake mimo ping done.")
raise SystemExit(0)
'''


def _mimo_provider_entry():
    """providers[] entry for a config module without the 3.15 builtin."""
    return {
        "name": "mimo", "base_url": "", "api": "openai-chat",
        "vendor": "Xiaomi (MiMo)",
        "models": ["xiaomi/mimo-v2.6-pro", "xiaomi/mimo-v2.6-flash",
                   "mimo-v2.6-pro", "mimo-v2.6-flash"],
    }


class _IntegrationBase(unittest.TestCase):
    """Shared temp root, fake dsh, fake mimo and isolated config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.dsh = fakes.write_fake_dsh(self.root / "dsh")
        self.mimo = mimo_fakes.write_fake_mimo(self.root / "mimo-bin", mode="ok")
        self.env = mimo_fakes.base_env(self.root, fake_mimo=str(self.mimo))
        self.env.update({
            "DSH_BIN": str(self.dsh),
            "FAKE_DSH_STATE": str(self.root / "fake-dsh-state.json"),
            "FAKE_DSH_ARGV": str(self.root / "fake-dsh-argv.json"),
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
        })
        self.cfg = load_config(config_path=self.root / "config.json",
                               env=self.env)
        # Tolerate a config module without the 3.15 mimo_* keys and a
        # model_policy without the builtin mimo provider (parallel parts).
        self.cfg._values["mimo_bin"] = str(self.mimo)
        self.cfg._values["providers"] = [_mimo_provider_entry()]

    def _run(self, **args):
        args.setdefault("prompt", "Write one note.")
        args.setdefault("workspace", str(self.ws))
        with mock.patch.dict(os.environ, self.env, clear=False):
            return harness.run_harness(args, self.cfg)

    def _mimo_argv_lists(self):
        """All captured argv lists (W11 overwrites; the inline fake appends)."""
        path = Path(self.env["FAKE_MIMO_ARGV"])
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [data]
        lists = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, list):
                lists.append(item)
        return lists

    def _mimo_argv(self):
        flat = []
        for item in self._mimo_argv_lists():
            flat.extend(item)
        return flat

    def _tasks(self):
        tasks = []
        for item in self._mimo_argv_lists():
            if "run" in item:
                positionals = [part for part in item[1:]
                               if not part.startswith("-")]
                if len(positionals) > 1 and positionals[0] == "run":
                    tasks.append(positionals[1])
        return tasks


@unittest.skipUnless(mimo_worker is not None, _MIMO_REASON)
class BuilderOnMimoTests(_IntegrationBase):
    def test_builder_run_records_tokens_usage_and_mimo_transport(self):
        _text, run, _path = self._run(
            subagents=1, quality=False,
            builder_provider="mimo",
            builder_model="xiaomi/mimo-v2.6-pro")
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["builder_transport"], "mimo")
        self.assertEqual(run["answer"], OK_ANSWER)
        self.assertEqual(run["tokens_in"], OK_TOKENS["tokens_in"])
        self.assertEqual(run["cache_read_tokens"],
                         OK_TOKENS["cache_read_tokens"])
        self.assertEqual(run["tokens_out"], OK_TOKENS["tokens_out"])
        self.assertEqual(run["total_tokens"], OK_TOKENS["total_tokens"])
        self.assertEqual((run.get("usage") or {}).get("reasoning_tokens"),
                         OK_TOKENS["reasoning_tokens"])
        self.assertEqual((run.get("usage") or {}).get("cache_write_tokens"),
                         OK_TOKENS["cache_write_tokens"])
        rows = (run.get("model_usage") or {}).get("models") or {}
        row = rows.get("xiaomi/mimo-v2.6-pro")
        self.assertIsNotNone(row, rows)
        self.assertEqual(row["total_tokens"], OK_TOKENS["total_tokens"])
        self.assertEqual(row["cache_write_tokens"],
                         OK_TOKENS["cache_write_tokens"])
        self.assertEqual(row["executions"], 1)
        self.assertTrue(run.get("cost_known"))
        self.assertEqual(run.get("cost_source"), "vendor")
        self.assertEqual(run.get("estimated_cost_usd"), round(OK_COST, 6))
        self.assertEqual(row["cost_source"], "vendor")
        self.assertEqual(row["estimated_cost_usd"], round(OK_COST, 6))
        argv = self._mimo_argv()
        self.assertIn("-m", argv)
        self.assertIn("xiaomi/mimo-v2.6-pro", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("builder", row.get("roles") or [])

    def test_auth_error_surfaces_mimo_remedy_and_classified_stop_reason(self):
        # The inline fake's error mode emits the real 0.1.17 error shape
        # (top-level error.name / error.data.{message, statusCode}) exactly as
        # tests/fixtures/mimo/mimo-0.1.17-error-401.jsonl records it.
        auth = fakes.write_fake_script(self.root / "mimo-auth", _REVIEWER_FAKE)
        self.env["MIMO_BIN"] = str(auth)
        self.env["FAKE_MIMO_MODE"] = "error_auth"
        self.cfg._values["mimo_bin"] = str(auth)
        with self.assertRaises(RuntimeError) as caught:
            self._run(subagents=1, quality=False,
                      builder_provider="mimo",
                      builder_model="xiaomi/mimo-v2.6-pro")
        runs = harness.load_runs(self.cfg)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        blob = ("%s %s" % (caught.exception, json.dumps(run))).lower()
        self.assertIn("mimo providers login", blob)
        self.assertEqual(run["status"], "error")
        self.assertEqual((run.get("builder") or {}).get("stop_reason"), "auth")


class DshTargetUntouchedTests(_IntegrationBase):
    def test_dsh_target_is_not_intercepted_by_the_mimo_dispatch(self):
        _text, run, _path = self._run(subagents=1, quality=False)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["builder_transport"], "headless")
        self.assertFalse(Path(self.env["FAKE_MIMO_ARGV"]).exists())
        self.assertTrue(Path(self.env["FAKE_DSH_ARGV"]).exists())


@unittest.skipUnless(mimo_worker is not None, _MIMO_REASON)
class CrosscheckCrewTests(_IntegrationBase):
    def setUp(self):
        super().setUp()
        if getattr(crosscheck_mod, "select_crew_target", None) is None:
            # Test double for W6's select_crew_target (only when the module
            # has not landed yet); the real function is used when present.
            def _stub_select_crew_target(cfg, args, builder_target=None):
                return {"provider": "mimo",
                        "model": "xiaomi/mimo-v2.6-pro",
                        "vendor": "Xiaomi (MiMo)",
                        "builder_vendor": "DeepSeek",
                        "different_vendor": True, "source": "auto"}
            patcher = mock.patch.object(
                crosscheck_mod, "select_crew_target",
                _stub_select_crew_target, create=True)
        else:
            patcher = mock.patch.object(
                crosscheck_mod, "select_crew_target",
                crosscheck_mod.select_crew_target)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_crew_recorded_and_session_plus_fix_rounds_use_crew_target(self):
        _text, run, _path = self._run(
            subagents=2, quality=False, mode="crosscheck",
            crosscheck_max_rounds=2, audit_review=False)
        crew = run.get("crosscheck_crew") or {}
        self.assertTrue(crew.get("different_vendor"), crew)
        self.assertEqual(crew.get("vendor"), "Xiaomi (MiMo)")
        self.assertEqual(crew.get("builder_vendor"), "DeepSeek")
        self.assertEqual(run["builder_transport"], "mimo")
        rows = (run.get("model_usage") or {}).get("models") or {}
        row = rows.get("xiaomi/mimo-v2.6-pro")
        self.assertIsNotNone(row, rows)
        # One crosscheck session plus at least one focused fix round, both on
        # the selected crew target.
        self.assertGreaterEqual(row["executions"], 2)

    def test_crew_vendor_conflict_fails_before_any_paid_execution(self):
        conflict_cls = getattr(
            crosscheck_mod, "CrosscheckCrewVendorConflict", None)
        if conflict_cls is None:
            class _Conflict(ValueError):
                reason = "crosscheck_crew_same_vendor"
            conflict_cls = _Conflict

        def _boom(cfg, args, builder_target=None):
            raise conflict_cls("crosscheck_crew_same_vendor")

        with mock.patch.object(crosscheck_mod, "select_crew_target", _boom,
                               create=True), \
                mock.patch.object(crosscheck_mod, "CrosscheckCrewVendorConflict",
                                  conflict_cls, create=True):
            with self.assertRaises(RuntimeError) as caught:
                self._run(subagents=2, quality=False, mode="crosscheck",
                          crosscheck_max_rounds=1, audit_review=False)
        self.assertIn("different vendor", str(caught.exception).lower())
        runs = harness.load_runs(self.cfg)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["stop_reason"], "crosscheck_crew_vendor_conflict")
        # The conflict fires before any paid execution: no builder phase ran
        # and the MiMo binary was never invoked.  (The dsh binary may still
        # have been probed for concurrency before the crew selection.)
        self.assertFalse(run.get("builder"))
        self.assertFalse(run.get("executions"))
        self.assertFalse(Path(self.env["FAKE_MIMO_ARGV"]).exists())


@unittest.skipUnless(mimo_worker is not None, _MIMO_REASON)
class ReviewerPreflightTests(_IntegrationBase):
    def setUp(self):
        super().setUp()
        self.reviewer = fakes.write_fake_script(
            self.root / "mimo-reviewer", _REVIEWER_FAKE)
        self.env["MIMO_BIN"] = str(self.reviewer)
        self.cfg._values["mimo_bin"] = str(self.reviewer)
        self.cfg._values["reviewers"] = [{
            "name": "mimo-reviewer",
            "vendor": "Xiaomi (MiMo)",
            "provider": "mimo",
            "model": "xiaomi/mimo-v2.6-pro",
            "dsh_bin": str(self.reviewer),
            "enabled": True,
        }]

    def test_mimo_reviewer_candidate_passes_ready_preflight(self):
        _text, run, _path = self._run(subagents=1, quality=True,
                                      quality_rounds=1)
        choice = run.get("reviewer_choice") or {}
        self.assertEqual(choice.get("status"), "selected", choice)
        self.assertEqual(choice.get("vendor"), "Xiaomi (MiMo)")
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["review_status"], "accepted")
        self.assertIn("Xiaomi (MiMo)", run.get("review_header") or "")

        pings = [item for item in self._mimo_argv_lists()
                 if any(part.lstrip().startswith("Reply with exactly READY")
                        for part in item)]
        self.assertTrue(pings, self._mimo_argv_lists())
        # The readiness ping runs the MiMo probe model (flash); the paid
        # review runs the candidate's own model (pro).
        self.assertTrue(any("xiaomi/mimo-v2.6-flash" in item
                            for item in pings), pings)
        reviews = [item for item in self._mimo_argv_lists()
                   if any("INDEPENDENT REVIEWER" in part for part in item)]
        self.assertTrue(reviews, self._mimo_argv_lists())
        self.assertTrue(any("xiaomi/mimo-v2.6-pro" in item
                            for item in reviews), reviews)
        # The READY answer itself came back through the hook.
        self.assertTrue(any(
            task.lstrip().startswith("Reply with exactly READY")
            for task in self._tasks()))


@unittest.skipUnless(mimo_worker is not None, _MIMO_REASON)
class SessionResumeTests(_IntegrationBase):
    def test_resume_session_id_is_forwarded_as_dash_s(self):
        _text, run, _path = self._run(
            subagents=1, quality=False,
            builder_provider="mimo",
            builder_model="xiaomi/mimo-v2.6-pro",
            resume_session_id="sess-resume-1")
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["builder_transport"], "mimo")
        argv = self._mimo_argv()
        self.assertIn("-s", argv)
        self.assertIn("sess-resume-1", argv)
        self.assertIn("-m", argv)
        self.assertIn("xiaomi/mimo-v2.6-pro", argv)

    def test_execute_once_forwards_session_id_and_transport_session_id(self):
        spec = model_policy.ModelTarget(
            provider="mimo", model="xiaomi/mimo-v2.6-pro",
            vendor="Xiaomi (MiMo)", base_url="", api="openai-chat",
            api_key_env="MIMO_API_KEY", source="job")
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = harness._execute_once(
                self.cfg, str(self.ws), "resume task",
                model_spec=spec, session_id="sess-resume-2", label="builder")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["transport"], "mimo")
        self.assertEqual(result["transport_session_id"], "sess-resume-2")
        self.assertEqual(result.get("role"), "builder")
        self.assertEqual((result.get("model_spec") or {}).get("model"),
                         "xiaomi/mimo-v2.6-pro")
        self.assertEqual(result.get("provider"), "mimo")
        argv = self._mimo_argv()
        self.assertIn("-s", argv)
        self.assertIn("sess-resume-2", argv)
        with mock.patch.dict(os.environ, self.env, clear=False):
            fresh = harness._execute_once(
                self.cfg, str(self.ws), "fresh task",
                model_spec=spec, session_id=None, label="builder")
        self.assertEqual(fresh["status"], "ok")
        # Without -s the recording's own session id flows through unchanged.
        self.assertEqual(fresh["transport_session_id"], OK_SESSION)


if __name__ == "__main__":
    unittest.main()
