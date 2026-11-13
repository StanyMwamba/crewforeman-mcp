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

# mimo_fakes "ok" mode accumulates two step_finish events:
#   step 1: input 70, cache 20, output 38, reasoning 6, total 120
#   step 2: input 80, cache 25, output 46, reasoning 7, total 140
OK_TOKENS = {"tokens_in": 150, "cache_read_tokens": 45, "tokens_out": 84,
             "reasoning_tokens": 13, "total_tokens": 260}

# Inline MiMo fake for the reviewer preflight/review flow.  W11's mimo_fakes
# covers READY and the ok/error streams but has no review-report mode (its
# reviews would parse as unscored), so test (e) uses this minimal fake per the
# plan's JSONL event contract.  It appends one JSON argv line per invocation
# to FAKE_MIMO_ARGV so a preflight and a review can both be inspected.
_REVIEWER_FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys
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
positionals = []
index = 1
while index < len(argv):
    arg = argv[index]
    if arg == "--dir":
        index += 1
        if index < len(argv):
            workspace = Path(argv[index])
    elif not arg.startswith("-"):
        positionals.append(arg)
    index += 1
if positionals and positionals[0] == "run" and len(positionals) > 1:
    task = positionals[1]
elif positionals:
    task = positionals[0]

def emit(payload):
    try:
        print(json.dumps(payload), flush=True)
    except (BrokenPipeError, OSError):
        raise SystemExit(0)

REPORT = ("# Independent review\n\n## Score\nFinal quality score: 9.2/10\n\n"
          "## Blocking defects\nNone\n\n## Non-blocking defects\nNone\n\n"
          "## Fix list\nNone\n")

if task.lstrip().startswith("Reply with exactly READY"):
    emit({"type": "text", "data": {"text": "READY"}})
    raise SystemExit(0)
if "INDEPENDENT REVIEWER" in task or "SCORE REPROMPT" in task:
    try:
        (workspace / "REVIEW.md").write_text(REPORT, encoding="utf-8")
    except OSError:
        pass
    emit({"type": "text", "data": {"text": REPORT}})
    raise SystemExit(0)
emit({"type": "step_start", "data": {"name": "step-1"}})
emit({"type": "tool_use", "data": {"name": "bash", "input": {"command": "ls"}}})
emit({"type": "step_finish", "data": {"tokens": {
    "total": 120, "input": 70, "output": 38, "reasoning": 6, "cache": 20}}})
emit({"type": "text", "data": {"text": "Fake mimo ping done."}})
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
        self.assertEqual(run["tokens_in"], OK_TOKENS["tokens_in"])
        self.assertEqual(run["cache_read_tokens"],
                         OK_TOKENS["cache_read_tokens"])
        self.assertEqual(run["tokens_out"], OK_TOKENS["tokens_out"])
        self.assertEqual(run["total_tokens"], OK_TOKENS["total_tokens"])
        self.assertEqual((run.get("usage") or {}).get("reasoning_tokens"),
                         OK_TOKENS["reasoning_tokens"])
        rows = (run.get("model_usage") or {}).get("models") or {}
        row = rows.get("xiaomi/mimo-v2.6-pro")
        self.assertIsNotNone(row, rows)
        self.assertEqual(row["total_tokens"], OK_TOKENS["total_tokens"])
        self.assertEqual(row["executions"], 1)
        self.assertTrue(run.get("cost_known"))
        self.assertGreater(run.get("estimated_cost_usd") or 0.0, 0.0)
        argv = self._mimo_argv()
        self.assertIn("-m", argv)
        self.assertIn("xiaomi/mimo-v2.6-pro", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("builder", row.get("roles") or [])

    def test_auth_error_surfaces_mimo_remedy_and_classified_stop_reason(self):
        auth = mimo_fakes.write_fake_mimo(self.root / "mimo-auth",
                                         mode="error_auth")
        self.env["MIMO_BIN"] = str(auth)
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


if __name__ == "__main__":
    unittest.main()
