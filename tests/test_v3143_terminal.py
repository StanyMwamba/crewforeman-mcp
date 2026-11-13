"""FIX 1 terminal-truth regressions (3.14.3).

Covers the two field failures:

* 77fa54 paused on "which reviewer should score this job?" after the reviewer
  pre-flight was killed by the whole-job deadline, and the detached job wrapper
  then refused a terminal done status ("review phase is still in flight").
* 84d3c6 / 0671ed saw a closed-handle teardown line take over the phase error
  even though the stop reason was a time limit.

Windows-compatible: no shell, no network, tempfile only.
"""

import unittest

from deepseek_mcp import harness, jobs, status


def _run(**overrides):
    run = {
        "id": "20261112-132614-77fa54",
        "status": "ok_unreviewed",
        "phase": "review-1_paused",
        "review_status": "paused",
        "review_in_flight": False,
        "independent_review": True,
        "review_rounds": [],
        "review_error": ('Anthropic reviewer failed: session "s": flush on a '
                         "closed handle; time limit reached"),
    }
    run.update(overrides)
    return run


class TerminalGateTests(unittest.TestCase):
    def test_paused_review_is_not_in_flight(self):
        gate = jobs._review_gate_state_from_run(_run())
        self.assertFalse(gate[0], gate)
        self.assertTrue(gate[6], "a paused review is a settled outcome")
        self.assertIsNone(jobs._review_gate_error(gate))

    def test_paused_run_does_not_error_the_job(self):
        # The job wrapper uses the same gate before recording the validated
        # question; a paused review must reach that question path instead of
        # being turned into status=error.
        self.assertIsNone(
            jobs._review_gate_error(jobs._review_gate_state_from_run(_run())))

    def test_stale_in_flight_flag_on_accepted_run_is_ignored(self):
        run = _run(
            status="ok", phase="review-1_done", review_status="accepted",
            review_in_flight=True,
            review_rounds=[{"kind": "review", "round": 1, "score": 9.0}],
            review_error=None,
        )
        gate = jobs._review_gate_state_from_run(run)
        self.assertFalse(gate[0])
        self.assertTrue(gate[2])
        self.assertIsNone(jobs._review_gate_error(gate))

    def test_recorded_review_cannot_be_refused_as_missing(self):
        run = _run(
            status="ok", phase="review-2_done", review_status="accepted",
            review_in_flight=False,
            review_rounds=[{"kind": "review", "round": 1, "score": 9.0},
                           {"kind": "review", "round": 2, "score": 9.2}],
            review_error=None,
        )
        self.assertIsNone(
            jobs._review_gate_error(jobs._review_gate_state_from_run(run)))

    def test_reviewer_error_counts_as_settled_not_missing(self):
        run = _run(status="ok_unreviewed", phase="review-1_preflight",
                   review_status="error", review_in_flight=False)
        gate = jobs._review_gate_state_from_run(run)
        self.assertFalse(gate[0])
        self.assertTrue(gate[6])
        self.assertIsNone(jobs._review_gate_error(gate))

    def test_genuinely_running_review_still_refuses(self):
        run = _run(status="ok", phase="review-2", review_status="running",
                   review_in_flight=True, review_error=None)
        gate = jobs._review_gate_state_from_run(run)
        self.assertTrue(gate[0])
        self.assertIn("still in flight", jobs._review_gate_error(gate))

    def test_gate_failed_still_refuses(self):
        run = _run(status="gate_failed", phase="gate",
                   review_status="skipped", review_in_flight=False)
        message = jobs._review_gate_error(jobs._review_gate_state_from_run(run))
        self.assertIn("deterministic gate failed", message)

    def test_genuine_run_error_still_propagates(self):
        run = _run(status="error", phase="builder", review_status="disabled",
                   independent_review=False, review_in_flight=False,
                   review_error=None)
        message = jobs._review_gate_error(jobs._review_gate_state_from_run(run))
        self.assertIn("ended with status 'error'", message)

    def test_final_status_is_unreviewed_when_reviewer_failed(self):
        final, detail, passed = status.final_status(
            builder_status="ok", review_status="error", score=None,
            review_recorded=True)
        self.assertEqual(final, "ok_unreviewed")
        self.assertFalse(passed)
        self.assertIn("not verified", detail)

    def test_attempted_review_failure_without_round_is_unreviewed(self):
        # 77fa54 shape: no review round could be recorded because the
        # pre-flight was killed by the whole-job deadline.
        final, detail, passed = status.final_status(
            builder_status="ok", review_status="error", score=None,
            review_recorded=False)
        self.assertEqual(final, "ok_unreviewed", detail)
        self.assertFalse(passed)
        run = _run(status="ok_unreviewed", phase="review-1_preflight",
                   review_rounds=[], review_status="error",
                   review_in_flight=False)
        gate = jobs._review_gate_state_from_run(run)
        self.assertFalse(gate[0])
        self.assertIsNone(jobs._review_gate_error(gate))

    def test_selection_infra_failure_is_classified(self):
        infra = {"status": "paused", "candidates": [
            {"state": "preflight_failed", "class": "stalled",
             "attempts": 2},
            {"state": "preflight_failed", "class": "stream dropped",
             "attempts": 1},
        ]}
        self.assertTrue(harness._selection_infrastructure_failure(infra))
        auth = {"status": "paused", "candidates": [
            {"state": "preflight_failed", "class": "auth", "attempts": 1},
        ]}
        self.assertFalse(harness._selection_infrastructure_failure(auth))
        config_only = {"status": "paused", "candidates": [
            {"state": "config_error", "class": "config", "attempts": 0},
        ]}
        self.assertFalse(
            harness._selection_infrastructure_failure(config_only))

    def test_crosscheck_below_threshold_cannot_close_done(self):
        run = _run(status="crosscheck_below_threshold", phase="crosscheck",
                   review_status="skipped", review_rounds=[],
                   independent_review=True, review_error=None)
        message = jobs._review_gate_error(jobs._review_gate_state_from_run(run))
        self.assertIn("crosscheck_below_threshold", message)

    def test_real_time_limit_selection_is_infrastructure(self):
        from deepseek_mcp import reviewer_vendor
        candidate = {"name": "kimi", "vendor": "Kimi (Moonshot)",
                     "model": "", "enabled": True, "error": None,
                     "dsh_bin": "/bin/echo", "patch": "",
                     "api_key_env": "", "legacy_wrapper": False}

        def ping(_candidate):
            return {"status": "error", "stop_reason": "time_limit",
                    "timed_out": True, "error": "time limit reached",
                    "answer": None}

        selected = reviewer_vendor.select_candidate(
            [candidate], ["DeepSeek"], ping)
        self.assertEqual(selected["candidates"][0]["class"], "stalled")
        self.assertTrue(harness._selection_infrastructure_failure(selected))


class ShutdownNoiseTests(unittest.TestCase):
    def test_closed_handle_line_is_stripped_after_a_time_limit(self):
        raw = ('progress line\n'
               'dsh: session "session-1": flush on a closed handle\n'
               'tail\n')
        cleaned = harness._strip_shutdown_noise(raw, "timeout")
        self.assertNotIn("flush on a closed handle", cleaned)
        self.assertIn("progress line", cleaned)
        self.assertIn("tail", cleaned)

    def test_closed_handle_line_is_kept_without_a_stop_reason(self):
        raw = 'dsh: session "session-1": flush on a closed handle\n'
        self.assertEqual(harness._strip_shutdown_noise(raw, None), raw)

    def test_preflight_time_limit_is_infrastructure(self):
        execution = {"status": "error", "stop_reason": "time_limit",
                     "timed_out": True,
                     "error": 'session "s": flush on a closed handle; '
                              "time limit reached"}
        self.assertTrue(harness._preflight_infrastructure_failure(execution))

    def test_preflight_auth_failure_keeps_the_existing_pause(self):
        execution = {"status": "error", "stop_reason": "error",
                     "error": "401 invalid api key"}
        self.assertFalse(harness._preflight_infrastructure_failure(execution))

    def test_preflight_closed_handle_without_deadline_is_infrastructure(self):
        execution = {"status": "error", "stop_reason": "error",
                     "error": 'session "s": flush on a closed handle'}
        self.assertTrue(harness._preflight_infrastructure_failure(execution))


class SandboxWarningOnErrorPathTests(unittest.TestCase):
    """P2-2: an unknown job id must not hide the os_sandbox=off warning."""

    def test_unknown_run_still_names_the_opt_out(self):
        import tempfile
        from deepseek_mcp import config
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config({"os_sandbox": "off", "jobs_dir": tmp})
            with self.assertRaises(RuntimeError) as ctx:
                jobs.tool_status(
                    {"run_id": "20260101-000000-abc"}, cfg)
        text = str(ctx.exception)
        self.assertIn("OS sandbox disabled by config os_sandbox=off", text)
        self.assertIn("No background job", text)


if __name__ == "__main__":
    unittest.main()
