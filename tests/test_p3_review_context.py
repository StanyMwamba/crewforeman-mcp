import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

from deepseek_mcp import quality, review_context as rc


def _tmpdir():
    base = os.environ.get("TMPDIR") or os.getcwd()
    try:
        return tempfile.mkdtemp(prefix="p3-review-", dir=base)
    except OSError:
        return tempfile.mkdtemp(prefix="p3-review-", dir=os.getcwd())


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class ReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tree_digest_stable_ignores_and_changes(self):
        _write(self.tmp, "a.txt", "hello")
        _write(self.tmp, "REVIEW.md", "ignored")
        _write(self.tmp, ".dsh-tmp-abc", "ignored")
        _write(self.tmp, "state/blob.txt", "ignored")
        _write(self.tmp, "__pycache__/x.pyc", "ignored")
        first = rc.tree_digest(self.tmp)
        self.assertEqual(first["files"], 1)
        self.assertEqual(first, rc.tree_digest(self.tmp))
        _write(self.tmp, "a.txt", "hello2")
        second = rc.tree_digest(self.tmp)
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_signature_roundtrip_and_tamper(self):
        payload = {"tree": "abc", "tests": 3}
        # No explicit secret must generate bytes, not a hex str (hmac.new).
        self.assertEqual(len(rc.sign_payload(payload)), 64)
        sig = rc.sign_payload(payload, "sekret")
        self.assertTrue(rc.verify_signature(payload, sig, "sekret"))
        self.assertFalse(rc.verify_signature(payload, sig, "wrong"))
        self.assertFalse(rc.verify_signature({"tree": "xyz"}, sig, "sekret"))
        self.assertFalse(rc.verify_signature(payload, "0" * 64, "sekret"))

    def test_suite_ok_signed_and_counts(self):
        _write(self.tmp, "tests/test_ok.py",
               "import unittest\nclass T(unittest.TestCase):\n"
               "    def test_a(self): self.assertTrue(True)\n")
        result = rc.run_test_suite(self.tmp, timeout=60, secret="s3cret")
        self.assertEqual(result["status"], "ok", result["output_tail"])
        self.assertEqual(result["tests"], 1)
        self.assertTrue(rc.verify_signature(result, result["signature"], "s3cret"))
        tampered = dict(result)
        tampered["tests"] = 99
        self.assertFalse(rc.verify_signature(tampered, result["signature"],
                                             "s3cret"))
        self.assertEqual(result["failures"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertTrue(result["signed"])
        self.assertTrue(rc.verify_signature(
            {k: v for k, v in result.items() if k != "signature"},
            result["signature"], "s3cret"))
        self.assertNotIn("s3cret", json.dumps(result))
        self.assertEqual(result["tree_sha256"], result["tree_sha256_after"])

    def test_suite_failure_counts(self):
        _write(self.tmp, "tests/test_bad.py",
               "import unittest\nclass T(unittest.TestCase):\n"
               "    def test_a(self): self.fail('boom')\n")
        result = rc.run_test_suite(self.tmp, timeout=60)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tests"], 1)
        self.assertGreaterEqual(result["failures"], 1)
        self.assertIsNotNone(result["signature"])

    def test_suite_timeout_does_not_hang(self):
        result = rc.run_test_suite(
            self.tmp, command=[sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.5)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "error")
        self.assertLess(result["seconds"], 5)

    def test_auto_detect_skips_without_tests(self):
        result = rc.run_test_suite(self.tmp)
        self.assertEqual(result["status"], "skipped")
        self.assertIsNotNone(result["signature"])

    def test_baseline_and_diff_added_changed_bounded(self):
        _write(self.tmp, "pkg/mod.py", "".join("line %d\n" % i for i in range(10)))
        baseline = rc.capture_baseline(self.tmp, os.path.join(self.tmp, ".baseline"))
        self.assertTrue(baseline["files"])
        _write(self.tmp, "pkg/mod.py", "".join("line %d\n" % i for i in range(10))
               + "changed tail\n")
        _write(self.tmp, "pkg/new.py", "brand new\n")
        diff = rc.diff_context(self.tmp, baseline, max_chars=3000)
        self.assertIn("changed tail", diff["text"])
        self.assertIn("brand new", diff["text"])
        self.assertIn("line 8", diff["text"])  # 3 lines of context
        self.assertIn("pkg/new.py", diff["files"])
        self.assertEqual(diff["stats"]["added"], 1)
        self.assertEqual(diff["stats"]["changed"], 1)
        self.assertLessEqual(len(diff["text"]), 3100)

    def test_baseline_manifest_round_trip_for_resume(self):
        _write(self.tmp, "pkg/mod.py", "before\n")
        root = os.path.join(self.tmp, ".baseline")
        baseline = rc.capture_baseline(self.tmp, root)
        loaded = rc.load_baseline(root)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["sha256"], baseline["sha256"])
        self.assertEqual([item["path"] for item in loaded["files"]],
                         [item["path"] for item in baseline["files"]])
        _write(self.tmp, "pkg/mod.py", "after\n")
        diff = rc.diff_context(self.tmp, loaded, max_chars=2000)
        self.assertIn("-before", diff["text"])
        self.assertIn("+after", diff["text"])

    def test_focus_text_and_round1_prompt(self):
        _write(self.tmp, "a.py", "x = 1\n")
        baseline = rc.capture_baseline(self.tmp, os.path.join(self.tmp, ".baseline"))
        _write(self.tmp, "a.py", "x = 2\n")
        focus = rc.review_focus_text(baseline, self.tmp, ["off by one in a.py"])
        self.assertIn("off by one in a.py", focus)
        self.assertIn("do not read the whole repo", focus)
        self.assertIn("targeted", focus.lower())
        self.assertIn("x = 2", focus)
        round1 = quality.build_review_task("brief", ["a.py"], round_no=1,
                                           review_context="diff: x = 2")
        self.assertNotIn("targeted", round1.lower())
        round2 = quality.build_review_task("brief", ["a.py"], round_no=2,
                                           prior_defects=["off by one in a.py"],
                                           review_context=focus)
        self.assertIn("off by one in a.py", round2)
        self.assertIn("targeted", round2.lower())

    def test_baseline_manifest_records_binary_and_oversized_metadata(self):
        _write(self.tmp, "bin.dat", "old\n")
        with open(os.path.join(self.tmp, "bin.dat"), "wb") as handle:
            handle.write(b"\x00old binary")
        with open(os.path.join(self.tmp, "big.txt"), "w", encoding="utf-8") as handle:
            handle.write("x" * 300)
        baseline = rc.capture_baseline(self.tmp,
                                       os.path.join(self.tmp, ".baseline"),
                                       max_file_bytes=128)
        entries = {item["path"]: item for item in baseline["files"]}
        self.assertEqual(entries["bin.dat"].get("excluded"), "binary")
        self.assertEqual(entries["big.txt"].get("excluded"), "oversized")
        with open(os.path.join(self.tmp, "bin.dat"), "wb") as handle:
            handle.write(b"\x00new binary")
        with open(os.path.join(self.tmp, "big.txt"), "w", encoding="utf-8") as handle:
            handle.write("y" * 300)
        with open(os.path.join(self.tmp, "new.bin"), "wb") as handle:
            handle.write(b"\x00new file")
        diff = rc.diff_context(self.tmp, baseline, max_chars=4000,
                               max_file_bytes=128)
        self.assertIn("bin.dat", diff["files"])
        self.assertIn("big.txt", diff["files"])
        self.assertIn("new.bin", diff["files"])
        self.assertIn("binary file changed; no body", diff["text"])
        self.assertIn("oversized file changed; no body", diff["text"])
        self.assertIn("binary file added; no body", diff["text"])
        self.assertEqual(diff["stats"], {"added": 1, "changed": 2, "deleted": 0})

    def test_evidence_key_reference_roundtrip(self):
        evidence = {"tree": "abc", "tests": 3, "key_id": "connector"}
        key_id, key = rc.ensure_evidence_key(self.tmp)
        self.assertEqual(key_id, "connector")
        evidence["key_id"] = key_id
        evidence["signature"] = rc.sign_payload(evidence, key)
        self.assertTrue(rc.verify_evidence(evidence, self.tmp))
        tampered = dict(evidence)
        tampered["tests"] = 4
        self.assertFalse(rc.verify_evidence(tampered, self.tmp))
        self.assertEqual(stat.S_IMODE(
            os.stat(os.path.join(self.tmp, "review-evidence", "keys",
                                 "connector.key")).st_mode) & 0o777, 0o600)

    def test_test_evidence_block(self):
        block = rc.format_test_evidence({
            "status": "ok", "command": ["python", "-m", "unittest"],
            "tree_sha256": "a" * 64, "tests": 2, "failures": 0, "errors": 0,
            "output_digest": "b" * 64, "output_tail": "OK"})
        self.assertIn("Signed test evidence", block)
        self.assertIn("a" * 64, block)
        self.assertIn("rerun everything", block.lower())


if __name__ == "__main__":
    unittest.main()
