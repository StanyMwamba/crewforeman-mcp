"""B1 regression: a configured state_root must not blind Harness sessions.

The connector reads DSH transcripts from the Harness default ``~/.dsh/sessions``
because dsh keys that home off ``DSH_HOME`` (which worker children do not
receive). Deriving the read path from ``CREWFOREMAN_STATE_DIR`` would silently
break token/cost accounting and child-activity progress.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness
from deepseek_mcp.config import DEFAULTS, load_config


class StateRootSessionAccountingTests(unittest.TestCase):
    def test_state_root_does_not_move_dsh_sessions_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake_home = tmp / "fake-home"
            workspace = tmp / "workspace"
            workspace.mkdir(parents=True)
            state_root = tmp / "state-root"
            empty_config = tmp / "config.json"
            empty_config.write_text("{}", encoding="utf-8")

            encoded = harness.encode_workspace(str(workspace))
            session_dir = fake_home / ".dsh" / "sessions" / encoded
            session_dir.mkdir(parents=True)
            session = session_dir / "session-b1.jsonl"
            session.write_text(json.dumps({"cwd": str(workspace)}) + "\n",
                               encoding="utf-8")

            # A decoy in the old derived location must not be read.
            decoy_dir = state_root / "sessions" / encoded
            decoy_dir.mkdir(parents=True)
            decoy = decoy_dir / "session-decoy.jsonl"
            decoy.write_text(json.dumps({"cwd": str(workspace)}) + "\n",
                             encoding="utf-8")

            env = {
                "CREWFOREMAN_STATE_DIR": str(state_root),
                "HOME": str(fake_home),
                "USERPROFILE": str(fake_home),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                cfg = load_config(config_path=empty_config, env=env)
                self.assertEqual(str(cfg.state_dir), str(state_root / "state"))
                self.assertEqual(str(cfg.dsh_sessions_dir),
                                 str(fake_home / ".dsh" / "sessions"))
                # No "<state_root>/sessions" default, and no derivation at all.
                self.assertNotEqual(str(cfg.dsh_sessions_dir),
                                    str(state_root / "sessions"))
                self.assertNotIn(str(state_root / "sessions"),
                                 cfg.dsh_sessions_dir)
                self.assertFalse(cfg.dsh_sessions_dir.startswith(
                    str(state_root)))
                found = harness.find_session_logs(
                    cfg, str(workspace), since=time.time() - 60)

            paths = [str(path) for path in found]
            self.assertIn(str(session), paths)
            self.assertNotIn(str(decoy), paths)
            self.assertFalse(any(
                path.startswith(str(state_root / "sessions"))
                for path in paths))

    def test_default_session_dir_is_harness_home(self):
        # Guard the literal default so a future change cannot silently
        # reintroduce a state-root-derived path.
        self.assertEqual(
            os.path.expanduser(DEFAULTS["dsh_sessions_dir"]),
            os.path.expanduser("~/.dsh/sessions"))


if __name__ == "__main__":
    unittest.main()
