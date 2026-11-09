"""P2-1 authority envelope and resume revalidation (stdlib unittest, fakes/temp dirs only).

No real dsh process and no network are used.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import authority, checkpoints, harness, safety
from deepseek_mcp.config import Config, load_config
from tests.fakes import base_env, write_fake_dsh, write_fake_script


class AuthorityTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self._base = {
            "harness_workspace": str(self.workspace),
            "sandbox": "workspace-write",
            "reviewer_sandbox": "read-only",
            "job_max_seconds": 7200.0,
            "job_max_cost_usd": None,
            "default_subagents": 4,
            "subagent_soft_cap": 8,
            "max_explicit_subagents": 8,
            "allowed_workspace_roots": [str(self.workspace)],
            "allow_writing_reviewer": False,
        }

    def cfg(self, **overrides):
        values = dict(self._base)
        values.update(overrides)
        return Config(values)

    def direct(self, cfg=None, **args):
        args.setdefault("workspace", str(self.workspace))
        return authority.direct(args, cfg or self.cfg())

    # ------------------------------------------------------------------
    # Reviewer policy
    # ------------------------------------------------------------------
    def test_reviewer_escape_rejected_by_default(self):
        for reviewer in ("danger-full-access", "danger", "workspace-write"):
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(authority.ReviewerPolicyError):
                    self.direct(reviewer_sandbox=reviewer)

        # The configured reviewer default is checked the same way.
        with self.assertRaises(authority.ReviewerPolicyError):
            self.direct(cfg=self.cfg(reviewer_sandbox="workspace-write"))

    def test_reviewer_override_is_recorded_when_allowed(self):
        cfg = self.cfg(allow_writing_reviewer=True)
        env = self.direct(cfg=cfg, reviewer_sandbox="workspace-write")
        self.assertEqual(env.reviewer_policy, "workspace-write")
        self.assertTrue(env.reviewer_override)
        self.assertTrue(env.to_dict()["reviewer_override"])
        self.assertEqual(
            authority.reviewer_policy_for(env, cfg), "workspace-write"
        )

        # The recorded override still degrades to read-only if the operator
        # turns allow_writing_reviewer off again.
        strict = self.cfg(allow_writing_reviewer=False)
        self.assertEqual(authority.reviewer_policy_for(env, strict), "read-only")

        clone = authority.AuthorityEnvelope.from_dict(env.to_dict())
        self.assertEqual(clone.reviewer_policy, "workspace-write")
        self.assertTrue(clone.reviewer_override)

    # ------------------------------------------------------------------
    # Continuations and widening
    # ------------------------------------------------------------------
    def test_continuation_cannot_widen_sandbox_cost_agents_or_workspace(self):
        env = self.direct(
            sandbox="workspace-write",
            job_max_seconds=1200,
            job_max_cost_usd=3.5,
            subagents=4,
        )
        cont = env.continuation("resume")
        self.assertEqual(cont.basis, "continuation")
        self.assertEqual(cont.origin, "resume")
        self.assertEqual(cont.workspace_root, str(self.workspace))

        attempts = (
            ("sandbox", {"sandbox": "danger-full-access"}),
            ("job_max_seconds", {"job_max_seconds": 9999}),
            ("job_max_cost_usd", {"job_max_cost_usd": 99.0}),
            ("max_agents", {"max_agents": env.max_agents + 1}),
            ("workspace_root", {"workspace_root": str(self.outside)}),
        )
        for field, kwargs in attempts:
            with self.subTest(field=field):
                with self.assertRaises(authority.WideningError) as caught:
                    cont.narrow(**kwargs)
                self.assertIn(field, str(caught.exception))

        narrowed = cont.narrow(sandbox="read-only", job_max_seconds=60,
                               job_max_cost_usd=1.0, max_agents=1)
        self.assertEqual(narrowed.sandbox, "read-only")
        self.assertEqual(float(narrowed.job_max_seconds), 60.0)
        self.assertEqual(float(narrowed.job_max_cost_usd), 1.0)
        self.assertEqual(narrowed.max_agents, 1)
        self.assertEqual(cont.sandbox, "workspace-write")  # immutable source

    def test_continuation_downgrades_danger(self):
        env = self.direct(sandbox="danger")
        self.assertEqual(env.sandbox, "danger-full-access")
        cont = env.continuation("resume")
        self.assertEqual(cont.sandbox, "workspace-write")
        self.assertEqual(cont.basis, "continuation")

    # ------------------------------------------------------------------
    # Resume policy drift
    # ------------------------------------------------------------------
    def test_resume_under_stricter_config_narrows_and_records_drift(self):
        env = self.direct(
            job_max_seconds=1200,
            job_max_cost_usd=5.0,
            subagents=4,
        )
        strict = self.cfg(
            sandbox="read-only",
            reviewer_sandbox="read-only",
            job_max_seconds=600,
            job_max_cost_usd=2.0,
            default_subagents=2,
            subagent_soft_cap=2,
            max_explicit_subagents=2,
        )
        effective, drift = env.revalidate(strict)

        self.assertEqual(effective.sandbox, "read-only")
        self.assertEqual(float(effective.job_max_seconds), 600.0)
        self.assertEqual(float(effective.job_max_cost_usd), 2.0)
        self.assertLessEqual(effective.max_agents, 2)

        self.assertTrue(drift["stricter"])
        self.assertFalse(drift["legacy_default"])
        self.assertNotEqual(drift["digest_before"], drift["digest_after"])
        self.assertEqual(drift["digest_after"], authority.policy_digest(strict))
        changed = drift["changed"]
        for key, old, new in (
            ("sandbox", "workspace-write", "read-only"),
            ("job_max_seconds", 1200, 600),
            ("job_max_cost_usd", 5.0, 2.0),
        ):
            with self.subTest(key=key):
                self.assertIn(key, changed)
                self.assertEqual(changed[key][0], old)
                self.assertEqual(changed[key][1], new)

    # ------------------------------------------------------------------
    # Legacy 3.8.0 runs
    # ------------------------------------------------------------------
    def test_legacy_380_run_gets_conservative_default(self):
        run = {
            "workspace": str(self.workspace),
            "builder_sandbox": {"preset": "danger-full-access"},
            "sandbox": "danger-full-access",
            "reviewer_sandbox": {"preset": "workspace-write"},
            "job_max_seconds": 99999,
            "job_max_cost_usd": 50.0,
            "subagents": 12,
        }
        metadata = {"spec": {
            "workspace": str(self.workspace),
            "job_max_seconds": 99999,
            "job_max_cost_usd": 50.0,
        }}
        cfg = self.cfg(
            job_max_seconds=600,
            job_max_cost_usd=2.0,
            max_explicit_subagents=6,
        )
        env = authority.legacy_default(run, cfg, metadata)

        self.assertTrue(env.legacy_default)
        self.assertEqual(env.basis, "continuation")
        self.assertEqual(env.origin, "legacy-3.8.0")
        self.assertEqual(env.version, authority.AUTHORITY_VERSION)
        self.assertEqual(env.sandbox, "workspace-write")
        self.assertEqual(env.reviewer_policy, "read-only")
        self.assertEqual(float(env.job_max_seconds), 600.0)
        self.assertEqual(float(env.job_max_cost_usd), 2.0)
        self.assertGreaterEqual(env.max_agents, 1)
        self.assertLessEqual(env.max_agents, 12)
        self.assertEqual(env.workspace_root, str(self.workspace))
        self.assertEqual(env.policy_digest, authority.policy_digest(cfg))

        resumed, drift = authority.revalidate_run(dict(run), cfg, metadata)
        self.assertTrue(getattr(resumed, "legacy_default", False))
        self.assertTrue(drift.get("legacy_default"))

        with self.assertRaises(authority.InvalidAuthority):
            authority.revalidate_run(
                {"authority_version": 1, "workspace": str(self.workspace)},
                cfg,
            )

    # ------------------------------------------------------------------
    # Workspace containment
    # ------------------------------------------------------------------
    def test_workspace_root_refusal_and_boundary(self):
        cfg = self.cfg()
        nested = self.workspace / "nested"
        nested.mkdir()
        self.assertEqual(
            authority.resolve_workspace({"workspace": str(nested)}, cfg),
            os.path.realpath(str(nested)),
        )
        self.assertEqual(
            authority.resolve_workspace({"workspace": str(self.workspace)}, cfg),
            os.path.realpath(str(self.workspace)),
        )
        self.assertEqual(
            authority.resolve_workspace({}, cfg),
            os.path.realpath(str(self.workspace)),
        )

        for outside in (self.outside, Path(str(self.workspace) + "-evil")):
            with self.subTest(outside=str(outside)):
                with self.assertRaises(authority.WorkspaceRootError):
                    authority.resolve_workspace({"workspace": str(outside)}, cfg)
                with self.assertRaises(authority.WorkspaceRootError):
                    self.direct(cfg=cfg, workspace=str(outside))

        self.assertTrue(
            authority.inside_root(str(nested), str(self.workspace))
        )
        self.assertFalse(
            authority.inside_root(str(self.workspace) + "-evil",
                                  str(self.workspace))
        )

    # ------------------------------------------------------------------
    # Policy digest
    # ------------------------------------------------------------------
    def test_resume_soft_cap_narrows_agents(self):
        env = self.direct(subagents=8)
        strict = self.cfg(subagent_soft_cap=2, max_explicit_subagents=8)
        effective, drift = env.revalidate(strict)
        self.assertLessEqual(effective.max_agents, 2)
        self.assertIn("max_agents", drift["changed"])

    def test_naive_expiry_does_not_crash_revalidation(self):
        env = self.direct()
        env.expires = "2030-01-01T00:00:00"
        effective, _drift = env.revalidate(self.cfg())
        self.assertTrue(effective.expires)

    def test_policy_digest_stable_across_key_order_and_absent_keys(self):
        values = {
            "sandbox": "workspace-write",
            "reviewer_sandbox": "read-only",
            "job_max_seconds": 600,
            "job_max_cost_usd": 2.0,
            "max_explicit_subagents": 4,
            "default_subagents": 2,
            "subagent_soft_cap": 8,
            "allow_writing_reviewer": False,
            "allowed_workspace_roots": [str(self.workspace)],
        }
        forward = Config(values)
        reverse = Config(dict(reversed(list(values.items()))))
        self.assertEqual(
            authority.policy_digest(forward),
            authority.policy_digest(reverse),
        )

        # Absent keys and explicit default/empty values canonicalise the same.
        self.assertEqual(
            authority.policy_digest(Config({})),
            authority.policy_digest(Config({"sandbox": "workspace-write"})),
        )
        self.assertEqual(
            authority.policy_digest(Config({})),
            authority.policy_digest(Config({"allowed_workspace_roots": []})),
        )
        self.assertNotEqual(
            authority.policy_digest(forward),
            authority.policy_digest(Config(dict(values, sandbox="read-only"))),
        )




class AuthorityRunIntegrationTests(unittest.TestCase):
    """Authority is persisted, carried in the checkpoint and revalidated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fake = write_fake_dsh(self.root / "fake-dsh")
        self.counter = self.root / "calls.txt"
        self.wrapper = self.root / "count-dsh"
        self.wrapper = write_fake_script(
            self.wrapper,
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "with open(os.environ['P2_COUNT_OUT'], 'a') as handle:\n"
            "    handle.write('call\\n')\n"
            "fake = os.environ['P2_FAKE_DSH']\n"
            "os.execv(fake, [fake] + sys.argv[1:])\n")
        self.env = base_env(self.root, fake_dsh=self.wrapper,
                            sessions=self.root / "sessions")
        self.env.update({
            "P2_COUNT_OUT": str(self.counter),
            "P2_FAKE_DSH": str(self.fake),
            "DSH_AUTO_SKILLS": "off",
            "DSH_LIVE_SCALING": "0",
        })
        self.args = {"prompt": "Build one deliverable", "subagents": 0,
                     "quality": False, "workspace": str(self.workspace)}

    def _cfg(self, **extra):
        env = dict(self.env)
        env.update(extra)
        with mock.patch.dict(os.environ, env, clear=False):
            return load_config(config_path=self.root / "config.json", env=env)

    def _run(self, cfg):
        with mock.patch.dict(os.environ, self.env, clear=False):
            return harness.run_harness(dict(self.args), cfg)

    def test_run_and_checkpoint_record_the_direct_envelope(self):
        cfg = self._cfg()
        _text, run, _path = self._run(cfg)
        envelope = run.get("authority") or {}
        self.assertEqual(envelope.get("basis"), "direct")
        self.assertEqual(envelope.get("sandbox"),
                         safety.resolve(dict(self.args), cfg)["sandbox"])
        self.assertEqual(envelope.get("reviewer_policy"), "read-only")
        self.assertFalse(envelope.get("reviewer_override"))
        self.assertEqual(envelope.get("workspace_root"),
                         os.path.realpath(str(self.workspace)))
        self.assertEqual(run.get("authority_version"),
                         authority.AUTHORITY_VERSION)
        self.assertEqual(envelope.get("policy_digest"),
                         authority.policy_digest(cfg))
        manifest = checkpoints.Checkpoint.find(
            Path(cfg.state_dir) / "checkpoints", run["id"])
        self.assertIsNotNone(manifest)
        stored = (manifest.get("metadata") or {}).get("authority") or {}
        self.assertEqual(stored.get("basis"), envelope["basis"])
        self.assertEqual(stored.get("sandbox"), envelope["sandbox"])
        self.assertEqual(stored.get("reviewer_policy"),
                         envelope["reviewer_policy"])
        self.assertEqual(os.path.expanduser(stored.get("workspace_root")),
                         envelope["workspace_root"])
        self.assertEqual(stored.get("policy_digest"),
                         envelope["policy_digest"])

    def test_resume_under_stricter_config_narrows_and_records_drift(self):
        cfg = self._cfg()
        _text, run, _path = self._run(cfg)
        self.assertEqual(len(self.counter.read_text().splitlines()), 1)
        strict = self._cfg(DSH_SANDBOX="read-only")
        with mock.patch.dict(os.environ, self.env, clear=False):
            _text, resumed, _path = harness.resume_harness(
                {"resume_run_id": run["id"]}, strict)
        drift = resumed.get("policy_drift") or {}
        self.assertTrue(drift.get("stricter"))
        self.assertEqual((drift.get("changed") or {}).get("sandbox"),
                         ["workspace-write", "read-only"])
        self.assertEqual(resumed["authority"]["basis"], "continuation")
        self.assertEqual(resumed["authority"]["sandbox"], "read-only")
        self.assertEqual(
            len(self.counter.read_text().splitlines()), 1,
            "a finished resume must not spawn another dsh process")

    def test_newer_run_without_authority_fails_closed(self):
        cfg = self._cfg()
        _text, run, _path = self._run(cfg)
        paths = sorted((self.root / "runs").glob("*.json"))
        path = paths[-1]
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("authority", None)
        record["authority_version"] = 1
        path.write_text(json.dumps(record), encoding="utf-8")
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                harness.resume_harness({"resume_run_id": run["id"]}, cfg)
        self.assertIn("Authority revalidation failed", str(caught.exception))

    def test_workspace_outside_allowed_roots_is_refused_before_spawn(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        cfg = self._cfg(DSH_ALLOWED_WORKSPACE_ROOTS=str(outside))
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                harness.run_harness(dict(self.args), cfg)
        self.assertIn("allowed_workspace_roots", str(caught.exception))
        self.assertFalse(self.counter.exists(),
                         "no dsh process may start outside the allowed roots")


if __name__ == "__main__":
    unittest.main()
