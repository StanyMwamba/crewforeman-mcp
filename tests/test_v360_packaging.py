"""Offline packaging tests for v3.6.0 (stdlib unittest only)."""

import base64
import hashlib
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_backend  # noqa: E402
from deepseek_mcp import config  # noqa: E402

VERSION = config.SERVER_VERSION
DIST_INFO = "crewforeman_mcp-%s.dist-info" % VERSION
SKILL_NAMES = {"code.md", "data.md", "document.md", "poster.md", "web.md"}


class PackagingTests(unittest.TestCase):
    def test_versions_agree(self):
        self.assertEqual(build_backend.VERSION, VERSION)
        self.assertEqual(build_backend.PYPROJECT["version"], VERSION)
        self.assertEqual(build_backend.NAME, "crewforeman-mcp")
        self.assertEqual(build_backend.CONFIG_VERSION, VERSION)
        self.assertEqual(build_backend.get_requires_for_build_wheel(), [])
        self.assertEqual(build_backend.get_requires_for_build_sdist(), [])

    def test_wheel_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = build_backend.build_wheel(tmp)
            wheel = Path(tmp) / name
            self.assertTrue(wheel.is_file(), name)
            self.assertTrue(name.endswith("-py3-none-any.whl"))
            with zipfile.ZipFile(str(wheel)) as archive:
                names = archive.namelist()
                # Read once inside the context: a second, unclosed ZipFile
                # keeps the .whl open and Windows tempdir cleanup fails with
                # WinError 32.
                contents = {name: archive.read(name) for name in names}
            self.assertIn(DIST_INFO + "/METADATA", names)
            self.assertIn(DIST_INFO + "/WHEEL", names)
            self.assertIn(DIST_INFO + "/entry_points.txt", names)
            self.assertIn(DIST_INFO + "/LICENSE", names)
            self.assertIn("deepseek_mcp/__init__.py", names)
            self.assertIn("deepseek_mcp/server.py", names)

            metadata = contents[DIST_INFO + "/METADATA"].decode("utf-8")
            self.assertIn("Metadata-Version: 2.1", metadata)
            self.assertIn("Name: crewforeman-mcp", metadata)
            self.assertIn("Version: %s" % VERSION, metadata)
            self.assertIn("Requires-Python: >=3.9", metadata)

            wheel_meta = contents[DIST_INFO + "/WHEEL"].decode("utf-8")
            self.assertIn("Wheel-Version: 1.0", wheel_meta)
            self.assertIn("Root-Is-Purelib: true", wheel_meta)
            self.assertIn("Tag: py3-none-any", wheel_meta)

            entry_points = contents[
                DIST_INFO + "/entry_points.txt"
            ].decode("utf-8")
            self.assertIn("[console_scripts]", entry_points)
            self.assertIn(
                "crewforeman-mcp = deepseek_mcp.server:main",
                entry_points,
            )
            self.assertIn(
                "deepseek-harness-mcp = deepseek_mcp.server:main",
                entry_points,
            )
            self.assertIn(
                "deepseek-mcp = deepseek_mcp.server:main", entry_points
            )

            data_prefix = (
                "crewforeman_mcp-%s.data/data/share/"
                "crewforeman-mcp/skills/" % VERSION
            )
            skills = {
                item.rsplit("/", 1)[-1]
                for item in names
                if item.startswith(data_prefix) and item.endswith(".md")
            }
            self.assertEqual(skills, SKILL_NAMES)
            self.assertEqual(len([n for n in names if n.startswith(data_prefix)]), 5)

            for item in names:
                parts = item.split("/")
                self.assertFalse(
                    any(part.startswith("wave") for part in parts), item
                )
                self.assertNotIn("__pycache__", parts, item)
                self.assertFalse(item.endswith((".pyc", ".log", ".egg-info")), item)

            record = contents[DIST_INFO + "/RECORD"].decode("utf-8")
            record_lines = record.splitlines()
            self.assertTrue(
                record_lines[-1].startswith(DIST_INFO + "/RECORD,"),
                record_lines[-1],
            )
            metadata_line = [
                line for line in record_lines
                if line.startswith(DIST_INFO + "/METADATA,")
            ][0]
            _, digest_part, size_part = metadata_line.split(",")
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(
                    contents[DIST_INFO + "/METADATA"]
                ).digest()
            ).rstrip(b"=").decode("ascii")
            self.assertEqual(digest_part, "sha256=" + expected)
            self.assertEqual(int(size_part), len(contents[DIST_INFO + "/METADATA"]))

    def test_sdist_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = build_backend.build_sdist(tmp)
            archive_path = Path(tmp) / name
            self.assertTrue(archive_path.is_file(), name)
            self.assertEqual(name, "crewforeman_mcp-%s.tar.gz" % VERSION)
            with tarfile.open(str(archive_path), "r:gz") as archive:
                names = archive.getnames()
                modes = {member.name: member.mode
                         for member in archive.getmembers()}

        self.assertTrue(any(item.endswith("/pyproject.toml") for item in names))
        self.assertTrue(any(item.endswith("/build_backend.py") for item in names))
        self.assertTrue(any(item.endswith("/setup.py") for item in names))
        self.assertTrue(any(item.endswith("/setup.cfg") for item in names))
        self.assertTrue(
            any(
                "/deepseek_mcp/" in item and item.endswith(".py")
                for item in names
            )
        )
        self.assertTrue(any("/skills/code.md" in item for item in names))
        self.assertTrue(any("/tests/" in item for item in names))
        self.assertTrue(any("/scripts/" in item for item in names))
        self.assertTrue(any("/bench/run_benchmark.py" in item for item in names))
        self.assertTrue(any(item.endswith("/PKG-INFO") for item in names))

        clean_install = [name for name in modes
                         if name.endswith("/scripts/clean_install.sh")]
        self.assertTrue(clean_install)
        self.assertEqual(modes[clean_install[0]] & 0o111, 0o111,
                         "clean_install.sh lost its executable mode in the sdist")

        for item in names:
            parts = item.split("/")
            self.assertFalse(
                any(part.startswith("wave") for part in parts), item
            )
            self.assertNotIn(".worklogs", parts, item)
            self.assertNotIn("__pycache__", parts, item)
            self.assertNotIn(".git", parts, item)
            self.assertFalse(
                item.endswith((".pyc", ".log", ".egg-info")), item
            )


if __name__ == "__main__":
    unittest.main()
