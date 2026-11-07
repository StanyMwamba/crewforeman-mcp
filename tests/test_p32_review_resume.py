"""P3.2: incremental reviewer checkpointing + resumable reviews."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from deepseek_mcp import checkpoints, harness, quality, review_progress
from deepseek_mcp.config import load_config
from tests.fakes import base_env, write_fake_dsh
import tests

os.environ.setdefault("DSH_REVIEW_PREFLIGHT", "0")


def _partial(**overrides):
    data = {
        "version": 1,
        "run_id": "run-1",
        "round": 2,
        "status": "interrupted",
        "started": "2025-01-01T00:00:00+00:00",
        "updated": "2025-01-01T00:01:00+00:00",
        "heartbeat_at": "2025-01-01T00:01:00+00:00",
        "elapsed_seconds": 60.0,
        "areas": [
            {"key": "report.pdf", "label": "report.pdf", "status": "checked"},
            {"key": "site/index.html", "label": "site/index.html",
             "status": "pending"},
        ],
        "defects": [
            {"text": "Missing QR code on page 2", "evidence": "page 2 is blank",
             "severity": "blocking", "carried_over": True},
        ],
        "tests_rerun": [],
        "last_event": "interrupted: reviewer killed",
    }
    data.update(overrides)
    return data


class ProgressFileTests(unittest.TestCase):
    def test_partial_files_update_with_heartbeat_while_sleeping(self):
        with tempfile.TemporaryDirectory(dir=tests.tempdir()) as tmp:
            progress = review_progress.ReviewProgress(
                tmp, run_id="run-1", round_no=1,
                areas=["report.pdf", "site/index.html"],
                heartbeat_interval=0.2)
            progress.start()
            try:
                time.sleep(0.05)
                first = Path(progress.json_path).stat().st_mtime_ns
                time.sleep(0.7)
                second = Path(progress.json_path).stat().st_mtime_ns
                self.assertGreater(second, first)
                snapshot = review_progress.ReviewProgress.load(tmp)
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot["status"], "running")
                self.assertEqual(len(snapshot["areas"]), 2)
                self.assertTrue(snapshot["heartbeat_at"])
                markdown = Path(progress.md_path).read_text(encoding="utf-8")
                self.assertIn("## Areas", markdown)
                self.assertIn("Heartbeat:", markdown)
            finally:
                progress.stop()

    def test_interruption_stores_defects_and_keeps_files(self):
        with tempfile.TemporaryDirectory(dir=tests.tempdir()) as tmp:
            progress = review_progress.ReviewProgress(
                tmp, run_id="run-1", round_no=1, areas=["report.pdf"])
            progress.start()
            progress.add_defect("Missing QR code", evidence="page 2 blank",
                                severity="blocking", carried_over=True)
            progress.add_test("python3 -m unittest", status="ok", seconds=1.5)
            progress.interrupt("reviewer killed")
            snapshot = review_progress.ReviewProgress.load(tmp)
            self.assertEqual(snapshot["status"], "interrupted")
            self.assertEqual(snapshot["defects"][0]["text"], "Missing QR code")
            self.assertEqual(snapshot["defects"][0]["evidence"], "page 2 blank")
            self.assertEqual(snapshot["tests_rerun"][0]["command"],
                             "python3 -m unittest")
            markdown = Path(progress.md_path).read_text(encoding="utf-8")
            self.assertIn("Missing QR code", markdown)
            self.assertIn("Evidence: page 2 blank", markdown)
            self.assertIn("python3 -m unittest", markdown)

    def test_add_defect_dedupes_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory(dir=tests.tempdir()) as tmp:
            progress = review_progress.ReviewProgress(tmp, run_id="run-1")
            self.assertTrue(progress.add_defect("Same defect", evidence="first"))
            self.assertFalse(progress.add_defect("same defect", evidence="second"))
            snapshot = progress.snapshot()
            self.assertEqual(len(snapshot["defects"]), 1)
            self.assertIn("first", snapshot["defects"][0]["evidence"])
            self.assertIn("second", snapshot["defects"][0]["evidence"])
            progress.stop()

    def test_load_file_handles_corrupt_json(self):
        with tempfile.TemporaryDirectory(dir=tests.tempdir()) as tmp:
            path = Path(tmp) / review_progress.JSON_NAME
            path.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(review_progress.ReviewProgress.load_file(path))


class ReviewTaskAndMergeTests(unittest.TestCase):
    def test_resume_task_contains_remaining_areas_and_prior_findings(self):
        task = quality.build_review_task(
            "Build a report", ["report.pdf", "site/index.html"], round_no=2,
            partial=_partial())
        self.assertIn("Resumed review checkpoint", task)
        self.assertIn("site/index.html", task)
        self.assertIn("Remaining areas to check", task)
        self.assertIn("Missing QR code on page 2", task)
        self.assertIn("page 2 is blank", task)
        self.assertIn("do not redo", task)

    def test_completed_partial_is_not_discarded(self):
        task = quality.build_review_task(
            "Build a report", ["report.pdf"], round_no=1,
            partial=_partial(round=1, status="completed"))
        self.assertIn("previous reviewer finished", task)
        self.assertIn("Missing QR code on page 2", task)
        self.assertIn("page 2 is blank", task)

    def test_merge_keeps_carried_defect_and_adds_new_one(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 7/10\n\n"
            "## Blocking defects\n- Broken link on the home page\n")
        merged, carried = review_progress.merge_review(report, _partial())
        self.assertIn("Missing QR code on page 2", merged)
        self.assertIn("Broken link on the home page", merged)
        self.assertIn("page 2 is blank", merged)
        self.assertEqual(len(carried), 1)
        defects = review_progress.extract_defects(merged)
        self.assertTrue(any("Missing QR code" in item for item in defects))
        self.assertTrue(any("Broken link" in item for item in defects))
        merged_again, _ = review_progress.merge_review(merged, _partial())
        self.assertEqual(merged_again.count("Missing QR code on page 2"), 1)

    def test_old_round_behavior_unchanged(self):
        plain = quality.build_review_task("Brief", ["a.txt"], round_no=1)
        with_none = quality.build_review_task(
            "Brief", ["a.txt"], round_no=1, partial=None)
        self.assertEqual(plain, with_none)
        self.assertNotIn("Resumed review checkpoint", plain)
        focused = quality.build_review_task(
            "Brief", ["a.txt"], round_no=2, prior_defects=["old defect"])
        self.assertIn("Focused round 2", focused)
        self.assertIn("old defect", focused)
        self.assertNotIn("Resumed review checkpoint", focused)


class ResumeIntegrationTests(unittest.TestCase):
    """A failed reviewer leaves a partial; resume hands it to a new reviewer."""

    def _wrapper(self, root, fake):
        path = root / "dsh-resume-review"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "task = sys.argv[-1] if len(sys.argv) else ''\n"
            "if 'INDEPENDENT REVIEWER' in task:\n"
            "    if 'Resumed review checkpoint' in task:\n"
            "        report = ('# Independent review\\n\\n## Score\\n'\n"
            "                  'Final quality score: 9.0/10\\n\\n'\n"
            "                  '## Blocking defects\\nNone\\n\\n'\n"
            "                  '## Non-blocking defects\\nNone\\n\\n'\n"
            "                  '## Fix list\\nNone\\n')\n"
            "        open('REVIEW.md', 'w').write(report)\n"
            "        print(json.dumps({'type': 'final', 'text': report}),\n"
            "              flush=True)\n"
            "        sys.exit(0)\n"
            "    open('REVIEW.md', 'w').write(\n"
            "        '# Independent review\\n\\n## Score\\n'\n"
            "        'Final quality score: 7.0/10\\n\\n'\n"
            "        '## Blocking defects\\n- Missing QR code on page 2\\n'\n"
            "        '  (evidence: page 2 is blank)\\n\\n'\n"
            "        '## Non-blocking defects\\nNone\\n\\n'\n"
            "        '## Fix list\\n1. Add the QR code.\\n')\n"
            "    print(json.dumps({'type': 'error',\n"
            "                      'message': 'Insufficient Balance'}), flush=True)\n"
            "    sys.exit(1)\n"
            "os.execv(sys.executable,\n"
            "         [sys.executable, os.environ['WRAP_REAL_DSH']] + sys.argv[1:])\n",
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_interrupted_review_resumes_with_prior_findings_and_merges(self):
        tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        fake = write_fake_dsh(root / "dsh")
        wrapper = self._wrapper(root, fake)
        env = base_env(root, fake_dsh=wrapper)
        env.update({
            "WRAP_REAL_DSH": str(fake),
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
        self.assertEqual(first.get("review_status"), "error")
        partial = review_progress.ReviewProgress.load(workspace)
        self.assertIsNotNone(partial)
        self.assertEqual(partial.get("status"), "interrupted")
        self.assertTrue(any("Missing QR code" in item.get("text", "")
                            for item in partial.get("defects") or []))
        manifest = checkpoints.Checkpoint.find(
            Path(cfg.state_dir) / "checkpoints", first["id"])
        self.assertEqual(manifest["phases"]["review-1"]["status"], "failed")

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            _text2, second, _path2 = harness.resume_harness(
                {"resume_run_id": first["id"]}, cfg)
        reports = [item for item in second.get("review_rounds") or []
                   if item.get("kind") == "review"]
        self.assertEqual(len(reports), 1, second.get("review_rounds"))
        self.assertEqual(reports[0].get("status"), "ok")
        merged_text = (workspace / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("Missing QR code on page 2", merged_text)
        self.assertIn("Final quality score: 9.0/10", merged_text)
        self.assertIn("carried over from interrupted review", merged_text)
        self.assertTrue(any("Missing QR code" in str(item)
                            for item in reports[0].get("defects") or []))
        # The carried blocking finding keeps the review below the gate even
        # though the new reviewer scored 9.0.
        self.assertEqual(reports[0].get("blocking_defects"), 1)
        self.assertEqual(second.get("review_status"), "stopped")
        self.assertEqual((second.get("review_partial") or {}).get("status"),
                         "completed")


class ProgressProtocolTests(unittest.TestCase):
    def test_split_chunks_parse_area_defect_and_test_events(self):
        protocol = review_progress.ProgressProtocol()
        self.assertEqual(
            protocol.feed("@@REVIEW_PROGRESS@@ area=deliverables | status=che"),
            [])
        events = protocol.feed(
            "cked\n"
            "@@REVIEW_PROGRESS@@ defect=non-blocking | area=correctness | "
            "text=Typo in heading | evidence=heading 2\n"
            "@@REVIEW_PROGRESS@@ test=python3 -m unittest | status=ok | "
            "seconds=1.5\n")
        self.assertEqual([item["type"] for item in events],
                         ["area", "defect", "test"])
        self.assertEqual(events[0]["key"], "deliverables")
        self.assertEqual(events[0]["status"], "checked")
        self.assertEqual(events[1]["severity"], "non-blocking")
        self.assertEqual(events[1]["text"], "Typo in heading")
        self.assertEqual(events[1]["evidence"], "heading 2")
        self.assertEqual(events[2]["command"], "python3 -m unittest")
        self.assertEqual(events[2]["seconds"], 1.5)

    def test_feed_progress_persists_state_and_strips_report_marker(self):
        with tempfile.TemporaryDirectory(dir=tests.tempdir()) as tmp:
            progress = review_progress.ReviewProgress(
                tmp, run_id="run-1", round_no=1,
                areas=[{"key": "deliverables", "label": "Deliverables"},
                       {"key": "tests", "label": "Tests"}])
            progress.start()
            progress.feed_progress(
                "@@REVIEW_PROGRESS@@ area=deliverables | status=checked\n"
                "@@REVIEW_PROGRESS@@ defect=non-blocking | text=Typo | "
                "evidence=p2\n"
                "@@REVIEW_PROGRESS@@ test=python3 -m unittest | status=ok | "
                "seconds=0.5\n")
            snapshot = review_progress.ReviewProgress.load(tmp)
            self.assertEqual(snapshot["areas"][0]["status"], "checked")
            self.assertEqual(snapshot["defects"][0]["severity"], "non-blocking")
            self.assertEqual(snapshot["tests_rerun"][0]["status"], "ok")
            stripped = review_progress.strip_progress_lines(
                "intro\n@@REVIEW_PROGRESS@@ area=x | status=checked\nbody")
            self.assertEqual(stripped, "intro\nbody")
            progress.stop()


class SeverityMergeTests(unittest.TestCase):
    def test_carried_non_blocking_finding_keeps_blocking_count_zero(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\nNone\n\n## Fix list\nNone\n")
        partial = {"defects": [
            {"text": "Typo in heading", "evidence": "section 2 heading",
             "severity": "non-blocking"}]}
        merged, carried = review_progress.merge_review(report, partial)
        self.assertEqual(len(carried), 1)
        self.assertIn("Typo in heading", merged)
        # The finding must live under the non-blocking heading, not blocking.
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        self.assertNotIn("Typo in heading", blocking_part)
        parsed = quality.parse_review(merged)
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertIn("Typo in heading", " ".join(parsed["defects"]))

    def test_interrupted_extraction_preserves_section_severity(self):
        text = (
            "# Independent review\n\n## Score\nFinal quality score: 7.0/10\n\n"
            "## Blocking defects\n- Missing QR code on page 2\n"
            "  (evidence: page 2 is blank)\n\n"
            "## Non-blocking defects\n- Typo in heading\n")
        records = review_progress.extract_defect_records(text)
        by_text = {item["text"]: item for item in records}
        self.assertEqual(by_text["Missing QR code on page 2"]["severity"],
                         "blocking")
        self.assertEqual(by_text["Missing QR code on page 2"]["evidence"],
                         "page 2 is blank")
        self.assertEqual(by_text["Typo in heading"]["severity"],
                         "non-blocking")

    def test_carried_blocking_repeated_under_non_blocking_is_moved(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9/10\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\n- Data loss on resume "
            "(evidence: reproduced in run 7)\n\n"
            "## Fix list\nNone\n")
        partial = {"defects": [
            {"text": "Data loss on resume", "evidence": "checkpoint evidence",
             "severity": "blocking"}]}
        merged, carried = review_progress.merge_review(report, partial)
        self.assertEqual(len(carried), 1)
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        non_blocking_part = merged.split("## Non-blocking defects", 1)[1].split(
            "## Fix list", 1)[0]
        self.assertIn("Data loss on resume", blocking_part)
        self.assertNotIn("Data loss on resume", non_blocking_part)
        self.assertIn("checkpoint evidence", blocking_part)
        self.assertIn("reproduced in run 7", blocking_part)
        parsed = quality.parse_review(merged)
        self.assertGreaterEqual(parsed["blocking_defects"], 1)

    def test_carried_non_blocking_repeated_under_blocking_is_moved(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9/10\n\n"
            "## Blocking defects\n- Minor typo in heading "
            "(evidence: section 2)\n\n"
            "## Non-blocking defects\nNone\n\n## Fix list\nNone\n")
        partial = {"defects": [
            {"text": "Minor typo in heading", "evidence": "checkpoint evidence",
             "severity": "non-blocking"}]}
        merged, carried = review_progress.merge_review(report, partial)
        self.assertEqual(len(carried), 1)
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        non_blocking_part = merged.split("## Non-blocking defects", 1)[1].split(
            "## Fix list", 1)[0]
        self.assertNotIn("Minor typo in heading", blocking_part)
        self.assertIn("Minor typo in heading", non_blocking_part)
        parsed = quality.parse_review(merged)
        self.assertEqual(parsed["blocking_defects"], 0)
        self.assertIn("Minor typo in heading", " ".join(parsed["defects"]))

    def test_merge_again_after_severity_reconcile_is_idempotent(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9/10\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\n- Data loss on resume "
            "(evidence: reproduced in run 7)\n\n"
            "## Fix list\nNone\n")
        partial = {"defects": [
            {"text": "Data loss on resume", "evidence": "checkpoint evidence",
             "severity": "blocking"}]}
        merged, _ = review_progress.merge_review(report, partial)
        merged_again, _ = review_progress.merge_review(merged, partial)
        self.assertEqual(merged_again, merged)
        self.assertEqual(merged_again.count("Data loss on resume"), 1)
        first = quality.parse_review(merged)
        second = quality.parse_review(merged_again)
        self.assertEqual(first["blocking_defects"],
                         second["blocking_defects"])
        self.assertEqual(first["score"], second["score"])

    def test_inline_blocking_tag_under_non_blocking_is_relocated(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
            "Blocking defects: 0\n\n"
            "## Blocking defects\nNone\n\n"
            "## Non-blocking defects\n- [blocking] Data loss on resume "
            "(evidence: reproduced in run 7)\n\n"
            "## Fix list\nNone\n")
        partial = {"defects": [
            {"text": "Data loss on resume", "evidence": "checkpoint evidence",
             "severity": "blocking"}]}
        merged, carried = review_progress.merge_review(report, partial)
        self.assertNotEqual(merged, report)
        self.assertEqual(len(carried), 1)
        records = review_progress.extract_defect_records(report)
        self.assertEqual(records[0]["severity"], "blocking")
        self.assertEqual(records[0].get("section"), "non-blocking")
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        self.assertIn("Data loss on resume", blocking_part)
        self.assertNotIn("[blocking]", blocking_part)
        self.assertIn("checkpoint evidence", blocking_part)
        self.assertIn("reproduced in run 7", blocking_part)
        non_blocking_part = merged.split(
            "## Non-blocking defects", 1)[1].split("## Fix list", 1)[0]
        self.assertNotIn("Data loss on resume", non_blocking_part)
        self.assertIn("Blocking defects: 0", merged)
        parsed = quality.parse_review(merged, "Blocking defects: 0",
                                      prefer_sections=True)
        self.assertEqual(parsed["blocking_defects"], 1)
        accepted = (parsed["score"] is not None
                    and parsed["score"] >= quality.DEFAULT_QUALITY_TARGET
                    and parsed["blocking_defects"] == 0)
        self.assertFalse(accepted)

    def test_inline_non_blocking_tag_under_blocking_is_relocated(self):
        report = (
            "# Independent review\n\n## Score\nFinal quality score: 9.0/10\n\n"
            "Blocking defects: 1\n\n"
            "## Blocking defects\n- [non-blocking] Typo in heading "
            "(evidence: section 2 heading)\n\n"
            "## Non-blocking defects\nNone\n\n"
            "## Fix list\nNone\n")
        partial = {"defects": [
            {"text": "Typo in heading", "evidence": "checkpoint evidence",
             "severity": "non-blocking"}]}
        merged, carried = review_progress.merge_review(report, partial)
        self.assertNotEqual(merged, report)
        self.assertEqual(len(carried), 1)
        records = review_progress.extract_defect_records(report)
        self.assertEqual(records[0]["severity"], "non-blocking")
        self.assertEqual(records[0].get("section"), "blocking")
        blocking_part = merged.split("## Blocking defects", 1)[1].split(
            "## Non-blocking defects", 1)[0]
        self.assertNotIn("Typo in heading", blocking_part)
        self.assertNotIn("[non-blocking]", blocking_part)
        non_blocking_part = merged.split(
            "## Non-blocking defects", 1)[1].split("## Fix list", 1)[0]
        self.assertIn("Typo in heading", non_blocking_part)
        self.assertIn("Blocking defects: 1", merged)
        parsed = quality.parse_review(merged, "Blocking defects: 1",
                                      prefer_sections=True)
        self.assertEqual(parsed["blocking_defects"], 0)
        accepted = (parsed["score"] is not None
                    and parsed["score"] >= quality.DEFAULT_QUALITY_TARGET
                    and parsed["blocking_defects"] == 0)
        self.assertTrue(accepted)


class LiveProgressResumeTests(unittest.TestCase):
    """Emit protocol events, hard-kill the reviewer, then resume."""

    def _wrapper(self, root):
        path = root / "dsh-live-progress"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, signal, sys\n"
            "from pathlib import Path\n"
            "task = sys.argv[-1] if len(sys.argv) else ''\n"
            "workspace = Path.cwd()\n"
            "if 'INDEPENDENT REVIEWER' in task:\n"
            "    if 'Resumed review checkpoint' not in task:\n"
            "        Path(os.environ['LIVE_FIRST_TASK']).write_text(task)\n"
            "        progress = (\n"
            "            '@@REVIEW_PROGRESS@@ area=deliverables | status=checked\\n'\n"
            "            '@@REVIEW_PROGRESS@@ defect=blocking | area=deliverables | '\n"
            "            'text=Missing QR code on page 2 | evidence=page 2 is blank\\n'\n"
            "            '@@REVIEW_PROGRESS@@ test=python3 -m unittest | '\n"
            "            'status=ok | seconds=1.2\\n')\n"
            "        print(json.dumps({'type': 'assistant/message', 'seq': 1,\n"
            "              'data': {'message': {'id': 'p1', 'role': 'assistant',\n"
            "                       'content': [{'type': 'text',\n"
            "                                    'text': progress}]}}}), flush=True)\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            "    Path(os.environ['LIVE_RESUME_TASK']).write_text(task)\n"
            "    report = ('# Independent review\\n\\n## Score\\n'\n"
            "              'Final quality score: 9.0/10\\n\\n'\n"
            "              '## Blocking defects\\nNone\\n\\n'\n"
            "              '## Non-blocking defects\\nNone\\n\\n'\n"
            "              '## Fix list\\nNone\\n')\n"
            "    (workspace / 'REVIEW.md').write_text(report)\n"
            "    print(json.dumps({'type': 'final', 'text': report}), flush=True)\n"
            "    sys.exit(0)\n"
            "(workspace / 'deliverable.md').write_text('# Deliverable\\n')\n"
            "print(json.dumps({'type': 'final',\n"
            "                  'text': 'Deliverable written.'}), flush=True)\n"
            "sys.exit(0)\n",
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_hard_kill_keeps_finding_and_resume_skips_checked_area(self):
        tmp = tempfile.TemporaryDirectory(dir=tests.tempdir())
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        wrapper = self._wrapper(root)
        env = base_env(root, fake_dsh=wrapper)
        env.update({
            "DSH_REVIEW_PREFLIGHT": "0",
            "DSH_LIVE_SCALING": "0",
            "DSH_AUTO_SKILLS": "off",
            "DSH_QUALITY_CONVERGENCE": "observe",
            "LIVE_FIRST_TASK": str(root / "first-task.txt"),
            "LIVE_RESUME_TASK": str(root / "resume-task.txt"),
        })
        args = {"prompt": "Build a tiny note.", "workspace": str(workspace),
                "sandbox": "danger", "quality": True,
                "independent_review": True, "quality_rounds": 1,
                "subagents": 1, "max_seconds": 30, "job_max_seconds": 120}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            _text, first, _path = harness.run_harness(args, cfg)
        self.assertEqual(first.get("review_status"), "error")
        partial = review_progress.ReviewProgress.load(workspace)
        self.assertIsNotNone(partial)
        self.assertEqual(partial.get("status"), "interrupted")
        areas = {item["key"]: item["status"] for item in partial["areas"]}
        self.assertEqual(areas.get("deliverables"), "checked")
        defects = {item["text"]: item for item in partial["defects"]}
        self.assertIn("Missing QR code on page 2", defects)
        self.assertEqual(defects["Missing QR code on page 2"]["evidence"],
                         "page 2 is blank")
        self.assertEqual(defects["Missing QR code on page 2"]["severity"],
                         "blocking")
        self.assertTrue(any(item["command"] == "python3 -m unittest"
                            for item in partial["tests_rerun"]))
        self.assertTrue((root / "first-task.txt").is_file())

        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(config_path=root / "config.json", env=env)
            _text2, second, _path2 = harness.resume_harness(
                {"resume_run_id": first["id"]}, cfg)
        resume_task = (root / "resume-task.txt").read_text(encoding="utf-8")
        resume_report = [item for item in second.get("review_rounds") or []
                         if item.get("kind") == "review"]
        self.assertEqual(len(resume_report), 1, resume_report)
        self.assertEqual(resume_report[0].get("status"), "ok")
        self.assertIn("Resumed review checkpoint", resume_task)
        self.assertIn("Missing QR code on page 2", resume_task)
        remaining = resume_task.split("Remaining areas to check:", 1)[1].split(
            "Already checked", 1)[0]
        self.assertNotIn("Deliverable files and artifacts", remaining)
        self.assertIn("Already checked (do not redo): `Deliverable files and "
                      "artifacts`", resume_task)
        merged_text = (workspace / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("Missing QR code on page 2", merged_text)
        self.assertIn("Final quality score: 9.0/10", merged_text)
        self.assertIn("carried over from interrupted review", merged_text)
        self.assertEqual(resume_report[0].get("blocking_defects"), 1)
        self.assertEqual((second.get("review_partial") or {}).get("status"),
                         "completed")


if __name__ == "__main__":
    unittest.main()
