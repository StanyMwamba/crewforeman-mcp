"""P3.2: resume acceptance must respect carried finding severity."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepseek_mcp import harness, quality
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh
import tests

os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")

_FIRST_BLOCKING = (
    "# Independent review\n\n## Score\nFinal quality score: 7.0/10\n\n"
    "## Blocking defects\n- Missing QR code on page 2\n"
    "  (evidence: page 2 is blank)\n\n"
    "## Non-blocking defects\nNone\n\n"
    "## Fix list\n1. Add the QR code.\n")

_RESUME_WRONG_NON_BLOCKING = (
    "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
    "Blocking defects: 0\n\n"
    "## Blocking defects\nNone\n\n"
    "## Non-blocking defects\n- Missing QR code on page 2\n"
    "  (evidence: page 2 is blank)\n\n"
    "## Fix list\nNone\n")

_FIRST_NON_BLOCKING = (
    "# Independent review\n\n## Score\nFinal quality score: 7.0/10\n\n"
    "## Blocking defects\nNone\n\n"
    "## Non-blocking defects\n- Typo in heading\n"
    "  (evidence: section 2 heading)\n\n"
    "## Fix list\n1. Fix the heading.\n")

_RESUME_WRONG_BLOCKING = (
    "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
    "Blocking defects: 1\n\n"
    "## Blocking defects\n- Typo in heading\n"
    "  (evidence: section 2 heading)\n\n"
    "## Non-blocking defects\nNone\n\n"
    "## Fix list\nNone\n")

# Exact prompt-rendered shape for carried findings: the resume prompt emits
# ``- [blocking] <text> (evidence: <evidence>)`` / ``- [non-blocking] ...``.
# The inline tag agrees with the checkpoint severity but the bullet sits under
# the wrong Markdown heading, so the merge must still relocate it.
_RESUME_INLINE_WRONG_NON_BLOCKING = (
    "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
    "Blocking defects: 0\n\n"
    "## Blocking defects\nNone\n\n"
    "## Non-blocking defects\n"
    "- [blocking] Missing QR code on page 2 (evidence: page 2 is blank)\n\n"
    "## Fix list\nNone\n")

_RESUME_INLINE_WRONG_BLOCKING = (
    "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
    "Blocking defects: 1\n\n"
    "## Blocking defects\n"
    "- [non-blocking] Typo in heading (evidence: section 2 heading)\n\n"
    "## Non-blocking defects\nNone\n\n"
    "## Fix list\nNone\n")

_WRAPPER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
task = sys.argv[-1] if len(sys.argv) else ''
workspace = Path.cwd()
if 'INDEPENDENT REVIEWER' in task:
    resumed = 'Resumed review checkpoint' in task
    source = 'RESUME_REPORT' if resumed else 'FIRST_REPORT'
    report = Path(os.environ[source]).read_text(encoding='utf-8')
    (workspace / 'REVIEW.md').write_text(report, encoding='utf-8')
    if resumed:
        print(json.dumps({'type': 'final', 'text': report}), flush=True)
        sys.exit(0)
    print(json.dumps({'type': 'error', 'message': 'Insufficient Balance'}),
          flush=True)
    sys.exit(1)
os.execv(sys.executable,
         [sys.executable, os.environ['WRAP_REAL_DSH']] + sys.argv[1:])
'''


class ResumeTaskSeverityTests(unittest.TestCase):
    def test_resume_task_renders_severity_and_heading_instruction(self):
        partial = {"defects": [
            {"text": "Missing QR code", "evidence": "page 2",
             "severity": "blocking"},
            {"text": "Typo in heading", "severity": "non-blocking"},
        ]}
        task = quality.build_review_task(
            "Build a report", ["report.pdf"], round_no=1, partial=partial)
        self.assertIn("- [blocking] Missing QR code (evidence: page 2)", task)
        self.assertIn("- [non-blocking] Typo in heading", task)
        self.assertIn("original severity heading", task)
        self.assertIn("## Blocking defects", task)
        self.assertIn("## Non-blocking defects", task)

    def test_resume_parse_prefers_merged_sections_over_summary_count(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\n- Typo in heading\n\n"
            "## Fix list\nNone\n")
        # Without the resume flag the explicit summary count wins, preserving
        # the pre-existing (non-resume) contract.
        self.assertEqual(quality.parse_review(
            report, "Blocking defects: 1")["blocking_defects"], 1)
        # On resume the merged sections are authoritative.
        self.assertEqual(quality.parse_review(
            report, "Blocking defects: 1",
            prefer_sections=True)["blocking_defects"], 0)


class ResumeAcceptanceTests(unittest.TestCase):
    """Same resume merge path as the harness, end to end on a fake DSH."""

    def _run_resume(self, first_report, resume_report):
        tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        fake = write_fake_dsh(root / "dsh")
        wrapper = root / "dsh-resume-acceptance"
        wrapper.write_text(_WRAPPER, encoding="utf-8")
        wrapper.chmod(0o755)
        first_path = root / "first-report.md"
        first_path.write_text(first_report, encoding="utf-8")
        resume_path = root / "resume-report.md"
        resume_path.write_text(resume_report, encoding="utf-8")
        env = base_env(root, fake_dsh=wrapper)
        env.update({
            "WRAP_REAL_DSH": str(fake),
            "FIRST_REPORT": str(first_path),
            "RESUME_REPORT": str(resume_path),
            "DSH_REVIEW_PREFLIGHT": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
            "DSH_QUALITY_CONVERGENCE": "observe",
        })
        args = {"prompt": "Build a tiny note.", "workspace": str(workspace),
                "sandbox": "danger", "quality": True,
                "independent_review": True, "quality_rounds": 1,
                "subagents": 1, "max_seconds": 30, "job_max_seconds": 120}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            _text, first, _path = harness.run_harness(args, cfg)
        self.assertEqual(first.get("review_status"), "error", first)
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            _text2, second, _path2 = harness.resume_harness(
                {"resume_run_id": first["id"]}, cfg)
        reports = [item for item in second.get("review_rounds") or []
                   if item.get("kind") == "review"]
        self.assertEqual(len(reports), 1, second.get("review_rounds"))
        merged_text = (workspace / "REVIEW.md").read_text(encoding="utf-8")
        return reports[0], second, merged_text

    def test_carried_blocking_repeated_non_blocking_is_not_accepted(self):
        report, _result, _merged = self._run_resume(
            _FIRST_BLOCKING, _RESUME_WRONG_NON_BLOCKING)
        self.assertEqual(report.get("score"), 9.0)
        self.assertGreaterEqual(report.get("blocking_defects"), 1)
        self.assertFalse(report.get("accepted"))

    def test_carried_non_blocking_repeated_blocking_stays_accepted(self):
        report, _result, _merged = self._run_resume(
            _FIRST_NON_BLOCKING, _RESUME_WRONG_BLOCKING)
        self.assertEqual(report.get("score"), 9.0)
        self.assertEqual(report.get("blocking_defects"), 0)
        self.assertTrue(report.get("accepted"))

    def test_inline_blocking_under_non_blocking_is_relocated_and_rejected(self):
        report, _result, merged = self._run_resume(
            _FIRST_BLOCKING, _RESUME_INLINE_WRONG_NON_BLOCKING)
        # The stale trailing summary count stays in the report; the merged
        # heading must win on resume.
        self.assertIn("Blocking defects: 0", merged)
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        self.assertIn("Missing QR code on page 2", blocking_part)
        self.assertNotIn("[blocking]", blocking_part)
        non_blocking_part = merged.split("## Non-blocking defects", 1)[1].split(
            "## Fix list", 1)[0]
        self.assertNotIn("Missing QR code on page 2", non_blocking_part)
        self.assertEqual(report.get("score"), 9.0)
        self.assertEqual(report.get("blocking_defects"), 1)
        self.assertFalse(report.get("accepted"))

    def test_inline_non_blocking_under_blocking_is_relocated_and_accepted(self):
        report, _result, merged = self._run_resume(
            _FIRST_NON_BLOCKING, _RESUME_INLINE_WRONG_BLOCKING)
        self.assertIn("Blocking defects: 1", merged)
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        self.assertNotIn("Typo in heading", blocking_part)
        self.assertNotIn("[non-blocking]", blocking_part)
        non_blocking_part = merged.split("## Non-blocking defects", 1)[1].split(
            "## Fix list", 1)[0]
        self.assertIn("Typo in heading", non_blocking_part)
        self.assertEqual(report.get("score"), 9.0)
        self.assertEqual(report.get("blocking_defects"), 0)
        self.assertTrue(report.get("accepted"))


if __name__ == "__main__":
    unittest.main()
