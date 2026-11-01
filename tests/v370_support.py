"""Shared fixtures for the v3.7.0 automatic skill-building tests."""

import json
import shutil
import tempfile
from pathlib import Path

from deepseek_mcp import skill_author
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"


def make_root(prefix="v370-"):
    """Temp root with a copy of the five seed files and a fake DSH."""
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    root = Path(tmp.name)
    shutil.copytree(str(REPO_SKILLS), str(root / "skills"))
    (root / "bin").mkdir(parents=True, exist_ok=True)
    fake = write_fake_dsh(root / "bin" / "dsh")
    env = base_env(root, fake_dsh=fake, sessions=root / "sessions")
    env["DSH_AUTO_SKILLS"] = "auto"
    env["DSH_QUALITY"] = "true"
    env["DSH_INDEPENDENT_REVIEW"] = "true"
    cfg = load_config(config_path=root / "config.json", env=env)
    return tmp, root, cfg, env, fake


def valid_draft(key="podcast-launch", cluster_id="c-test", runs=3,
                digest="sha256:test"):
    return {
        "schema": 1,
        "action": "create",
        "key": key,
        "title": "Podcast launch",
        "description": "Plan and verify a podcast launch.",
        "task_type": "creative",
        "aliases": ["podcast"],
        "keywords": ["podcast", "launch", "cover art", "show notes", "episode"],
        "checklist": [
            {"section": "Brief", "text": "Restate the audience and launch date."},
            {"section": "Brief", "text": "List the cover art and show notes."},
            {"section": "Content", "text": "Write the episode outline and guest questions."},
            {"section": "Content", "text": "Prepare the launch announcement copy."},
            {"section": "Verification", "text": "Verify every link and the RSS feed before launch."},
            {"section": "Verification", "text": "Test the feed with an empty episode list."},
        ],
        "lessons": ["Check the RSS feed before announcing."],
        "evidence": {"cluster_id": cluster_id, "runs": runs,
                     "signature_digest": digest},
    }


def evidence(cluster_id="c-test", runs=3, digest="sha256:test"):
    return {"cluster_id": cluster_id, "runs": runs,
            "signature_digest": digest, "task_type": "creative",
            "keywords": ["podcast", "launch", "cover", "notes"],
            "members": [{"score": 8.0, "status": "ok_below_gate",
                         "blocking": 1} for _ in range(runs)]}


def review(score=9.2, blocking=None, verdict="pass"):
    return {"schema": 1, "score": score,
            "blocking_defects": list(blocking or []),
            "non_blocking_defects": [], "fixes": [],
            "verdict": verdict}


def patch_author_queue(testcase, answers):
    """Patch the single Harness wrapper with a deterministic answer queue."""
    queue = list(answers)

    def fake(cfg, task, workspace, label=None, max_seconds=None,
             cancel_check=None, **kwargs):
        if not queue:
            raise AssertionError("unexpected skill-author/reviewer call")
        answer = queue.pop(0)
        return {"status": "ok", "answer": json.dumps(answer), "seconds": 0.01}

    original = skill_author._run_agent
    skill_author._run_agent = fake
    testcase.addCleanup(lambda: setattr(skill_author, "_run_agent", original))
    return queue
