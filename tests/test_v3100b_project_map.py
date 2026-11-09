"""Focused tests for BUILD 3.10.0 item 6a: persisted PROJECT_MAP."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from deepseek_mcp import project_map

MAP_BODY = "# Project map\n\n- the map body marker: BODY-MARKER-6A\n"


class ProjectMapTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cf-project-map-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.workspace = os.path.join(self.root, "workspace")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.workspace)
        os.makedirs(self.state_dir)

    def write(self, relative, text, root=None):
        path = os.path.join(root or self.workspace, relative)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def state_path(self):
        digest = hashlib.sha256(
            os.path.abspath(self.workspace).encode("utf-8")
        ).hexdigest()[:16]
        return os.path.join(self.state_dir, "project-maps", digest + ".json")

    def test_absent_then_new_capture_reuse(self):
        absent = project_map.offer(self.workspace, self.state_dir)
        self.assertEqual(absent["state"], "absent")
        self.assertFalse(absent["present"])
        self.assertFalse(absent["reused"])
        self.assertFalse(absent["stale"])
        self.assertEqual(absent["prompt"], "")
        self.assertEqual(absent["summary"]["fingerprint"], "")

        self.write(project_map.NAME, MAP_BODY)
        new = project_map.offer(self.workspace, self.state_dir)
        self.assertEqual(new["state"], "new")
        self.assertTrue(new["present"])
        self.assertFalse(new["reused"])
        self.assertFalse(new["stale"])
        self.assertIn(MAP_BODY, new["prompt"])
        self.assertIn(
            "Reuse this map; verify only what you change, and refresh it if "
            "your change makes it stale.",
            new["prompt"],
        )

        saved = project_map.capture(self.workspace, self.state_dir)
        self.assertTrue(saved["stored"])
        self.assertEqual(saved["state"], "new")
        self.assertEqual(saved["summary"], new["summary"])

        record = project_map.load(self.workspace, self.state_dir)
        self.assertIsInstance(record, dict)
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["workspace"], os.path.abspath(self.workspace))
        self.assertEqual(record["map"], MAP_BODY)
        self.assertEqual(record["fingerprint"]["sha256"][:12], new["summary"]["fingerprint"])

        again = project_map.offer(self.workspace, self.state_dir)
        self.assertEqual(again["state"], "reuse")
        self.assertTrue(again["reused"])
        self.assertFalse(again["stale"])
        self.assertIn("Reuse this map; verify only what you change.", again["prompt"])
        self.assertEqual(again["summary"]["fingerprint"], saved["summary"]["fingerprint"])
        self.assertEqual(len(again["summary"]["fingerprint"]), 12)

    def test_modify_file_makes_map_stale(self):
        self.write(project_map.NAME, MAP_BODY)
        first = project_map.capture(self.workspace, self.state_dir)
        self.assertTrue(first["stored"])
        self.assertEqual(project_map.offer(self.workspace, self.state_dir)["state"], "reuse")

        self.write("src.py", "print('changed')\n")
        stale = project_map.offer(self.workspace, self.state_dir)
        self.assertEqual(stale["state"], "stale")
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["reused"])
        self.assertIn(
            "This map is stale; refresh PROJECT_MAP.md before you finish.",
            stale["prompt"],
        )

        stored = project_map.capture(self.workspace, self.state_dir)
        self.assertEqual(stored["state"], "stale")
        self.assertTrue(stored["stored"])
        self.assertEqual(
            project_map.load(self.workspace, self.state_dir)["fingerprint"]["sha256"][:12],
            stored["summary"]["fingerprint"],
        )

    def test_oversized_map_is_bounded(self):
        oversized = "# giant\n" + ("x" * (project_map.MAX_MAP_CHARS * 2))
        self.write(project_map.NAME, oversized)

        offered = project_map.offer(self.workspace, self.state_dir)
        self.assertEqual(offered["state"], "new")
        self.assertEqual(offered["bytes"], project_map.MAX_MAP_CHARS)
        self.assertLessEqual(offered["bytes"], project_map.MAX_MAP_CHARS)
        self.assertNotIn(oversized, offered["prompt"])
        self.assertEqual(offered["prompt"].count("x"), project_map.MAX_MAP_CHARS - len("# giant\n"))
        self.assertEqual(len(offered["prompt"]), len("Current PROJECT_MAP.md:\n\n") + project_map.MAX_MAP_CHARS + 2 + len("Reuse this map; verify only what you change, and refresh it if your change makes it stale."))

        saved = project_map.capture(self.workspace, self.state_dir)
        self.assertTrue(saved["stored"])
        record = project_map.load(self.workspace, self.state_dir)
        self.assertEqual(len(record["map"]), project_map.MAX_MAP_CHARS)

    @unittest.skipIf(
        os.name == "nt",
        "Windows state privacy is an ACL check, not POSIX 0600/0700 mode bits")
    def test_state_file_and_directory_permissions(self):
        self.write(project_map.NAME, MAP_BODY)
        saved = project_map.capture(self.workspace, self.state_dir)
        self.assertTrue(saved["stored"])

        path = self.state_path()
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        directory = os.path.dirname(path)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)

        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["map"], MAP_BODY)

    def test_summary_has_no_workspace_path_or_map_body(self):
        self.write(project_map.NAME, MAP_BODY)
        offered = project_map.offer(self.workspace, self.state_dir)
        saved = project_map.capture(self.workspace, self.state_dir)
        absolute = os.path.abspath(self.workspace)

        for summary in (offered["summary"], saved["summary"]):
            rendered = json.dumps(summary, sort_keys=True)
            self.assertNotIn(absolute, rendered)
            self.assertNotIn("BODY-MARKER-6A", rendered)
            self.assertEqual(summary["version"], 1)
            # Only a digest, never the body, travels in the summary.
            self.assertEqual(summary["map_sha256"],
                             hashlib.sha256(MAP_BODY.encode("utf-8")).hexdigest())
        self.assertNotIn(absolute, offered["prompt"])
        self.assertNotIn(absolute, json.dumps(saved, sort_keys=True))

    def test_map_digest_tracks_the_body_without_returning_it(self):
        self.assertIsNone(project_map.map_digest(self.workspace))
        self.write(project_map.NAME, MAP_BODY)
        first = project_map.map_digest(self.workspace)
        self.assertEqual(
            first, hashlib.sha256(MAP_BODY.encode("utf-8")).hexdigest())
        self.write(project_map.NAME, MAP_BODY + "\n- extra\n")
        self.assertNotEqual(first, project_map.map_digest(self.workspace))

    def test_capture_without_map_stores_nothing(self):
        result = project_map.capture(self.workspace, self.state_dir)
        self.assertEqual(result["state"], "absent")
        self.assertFalse(result["present"])
        self.assertFalse(result["stored"])
        self.assertIsNone(project_map.load(self.workspace, self.state_dir))

    def test_files_fingerprint_is_deterministic_and_changes(self):
        self.write("a.py", "one\n")
        first = project_map.fingerprint(self.workspace)
        self.assertEqual(first["kind"], "files")
        self.assertGreaterEqual(first["files"], 1)
        self.assertEqual(first["sha256"], project_map.fingerprint(self.workspace)["sha256"])

        self.write("a.py", "two, longer\n")
        changed = project_map.fingerprint(self.workspace)
        self.assertEqual(changed["kind"], "files")
        self.assertNotEqual(first["sha256"], changed["sha256"])

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_git_fingerprint_uses_head_and_status(self):
        def git(*args, **kwargs):
            return subprocess.run(
                ["git", "-C", self.workspace] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs
            )

        if git("init").returncode != 0:
            self.skipTest("git init is unavailable")
        git("config", "user.name", "Test")
        git("config", "user.email", "builder")
        self.write("a.py", "one\n")
        self.assertEqual(git("add", "a.py").returncode, 0)
        self.assertEqual(git("commit", "-m", "init").returncode, 0)

        clean = project_map.fingerprint(self.workspace)
        self.assertEqual(clean["kind"], "git")
        self.assertEqual(len(clean["head"]), 40)
        self.assertEqual(len(clean["status_sha256"]), 64)
        self.assertEqual(clean["sha256"], project_map.fingerprint(self.workspace)["sha256"])

        self.write("a.py", "two\n")
        dirty = project_map.fingerprint(self.workspace)
        self.assertEqual(dirty["kind"], "git")
        self.assertEqual(dirty["head"], clean["head"])
        self.assertNotEqual(dirty["status_sha256"], clean["status_sha256"])
        self.assertNotEqual(dirty["sha256"], clean["sha256"])


if __name__ == "__main__":
    unittest.main()
