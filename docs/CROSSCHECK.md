# Crosscheck mode (3.14.3)

`crosscheck` is opt-in. A job enters it with:

```json
{"prompt": "...", "mode": "crosscheck"}
```

or by setting config `"default_mode": "crosscheck"`. With neither, every job
keeps the existing behaviour byte-for-byte. Tool arguments override config:
`crosscheck_threshold`, `crosscheck_crews`, `crosscheck_crew_size`,
`crosscheck_max_rounds`, `audit_review`, `audit_sample_rate`,
`audit_spot_requirements` and `audit_spot_fixes`.

## Crews and parts

Defaults are 2 crews x 4 agents (8 subagents). `crosscheck_crews` and
`crosscheck_crew_size` change the default team; an explicit `subagents` value
is split evenly across the requested crews. Each crew is a logical group of
Harness subagents in the same build.

The lead writes `PLAN.json` first. The connector reads the top-level `parts`
list (also accepts `slices` or `tasks`) and validates:

- at least two parts, each with a non-empty repo-relative `write_set`;
- write sets are pairwise disjoint (a directory covers its children);
- `depends_on` references exist and contain no cycle.

Independent parts build at once; a dependent part builds after its dependency
level. The connector assigns each part a builder crew and a **different**
checker crew by rotation, so no crew ever checks a part it built. A rescued
part keeps that rule.

```json
{"version": 1, "write_set": ["deepseek_mcp/a.py", "tests/test_a.py"],
 "parts": [
   {"id": "p1", "title": "module", "write_set": ["deepseek_mcp/a.py"]},
   {"id": "p2", "title": "tests", "write_set": ["tests/test_a.py"],
    "depends_on": ["p1"]}]}
```

## Build and cross-check waves

Wave 1: every builder crew builds its parts. Wave 2: each crew reviews only
parts it did not build. Half of each crew are **breakers** (edge cases, wrong
inputs, error paths, failing tests); half are **spec checkers** (every brief
requirement point by point). A finding records:

- `severity`: `BLOCKING` or `minor`;
- `lens`: `breaker` or `spec`;
- `file`, `line` and the evidence command plus output;
- `found_by_crew`, then `status` (`open`, `fixed`, `justified`,
  `false_alarm`), the builder's fix and the finding crew's confirmation.

The original builder fixes. The finding crew re-runs the evidence and
confirms. Rounds stop at `crosscheck_max_rounds` (default 3, including the
initial build/check round).

## The connector-owned score

The crew's prose and self-score are ignored. The connector reads
`CROSSCHECK.json`, checks the parts against `PLAN.json`, and runs the test
suite through the deterministic gate. Crew pairing is connector-owned:
builders/checkers reported by the artifact are ignored, and each part is
assigned by rotation. The score is five equally weighted conditions, 2.0
points each:

| # | Condition | Fraction |
|---|---|---|
| 1 | Connector-run tests | 1.0 only when the gate test check is `ok`; 0.5 for a bounded compile smoke when no test suite is detected; else 0.0 |
| 2 | Every brief requirement checked | connector `R1..Rn` ids covered by the artifact, divided by the connector's checklist size; each row needs a real paired part, evidence, and its assigned checker (never the builder or rescuer) |
| 3 | Zero open BLOCKING | 1.0 only when every real BLOCKING finding is explicitly `fixed`; unknown, `justified`, `wontfix` and unconfirmed states stay open |
| 4 | Every minor fixed or justified | fixed or justified minors divided by all real minors; a justification needs at least 20 characters and acceptance by the finding crew or the part's paired checker |
| 5 | Every fix confirmed by the finding crew | confirmed fixes divided by fixed findings; the fix must be recorded by the part's builder (or a successful rescuer), never the finder, and the finder (the part's paired checker) confirms with evidence |

```
raw   = 2.0 * (tests + requirements + blocking + minors + fixes)
score = 10.0 when every fraction is 1.0, else min(round(raw, 2), 9.99)
```

A compile-only smoke is not a test suite: it earns 0.5 of the tests
condition, so a job with no tests can never reach 10.0. A missing or empty
part status is not built; only an explicit `built`/`done` status (or a
successful rescue that reconciles the part) earns the build condition.

A part that is not built (`planned`, `pending`, `stalled`, `failed`,
`blocked` or `error`) caps the score at 6.0; an unresolved rescue caps it at
8.0. Those caps keep a partially built task below a passing threshold, and
10.0 requires every part to be built (or completed by a successful rescue),
not merely every evidence sheet to be green. The build contract also prints
the connector's own brief checklist (`R1..Rn`); requirement rows must use
those exact ids, because coverage is measured against the connector list, not
against ids the crew invents.

`crosscheck_threshold` defaults to **10.0** and is validated to the inclusive
range 0.0-10.0. A malformed value stops the job before any paid call. Lower
thresholds are meaningful because conditions 2, 4 and 5 are fractional.

If the score is still below the threshold after `crosscheck_max_rounds`, the
run stops with `status="crosscheck_below_threshold"`. It does not run the
audit and does not loop. `CROSSCHECK.json` and the run record carry the unmet
condition list.

## Rescue

The existing finisher still completes a builder run that stalls, stops early,
hits a time limit, loops, loses provider balance or dies on a transient network
error (the last two in crosscheck mode). The finisher works on the whole
workspace: the connector narrows the record to an explicit `part_id` or
`failed_crew`, otherwise to the parts that are not built yet, and reconciles a
completed part to `built`. An explicit `part_id` that matches no part is
recorded as `scope="unknown_part"` and fails closed; it never widens to every
part. Only a rescue with no explicit target and no failed crew may record
`scope="all"` and mark the parts it completed. Each row records the failed
crew (when known), reason, scope and rescue crew, and only a successful rescue
marks the part `rescued`; a failed one caps the score at 8.0. A crew
that did not build or rescue the part must still cross-check it; without that evidence
condition 2 keeps the score below 10.

## Per-job record

The run stores `crosscheck`:

- `parts` with write sets, dependency order, builder/checker crews and status;
- `pairing`, `requirements`, `findings`, `rounds` and `rescues`;
- `summary`: raw/unique/real/false-alarm and breaker/spec counts;
- `tests`, `score`, `conditions`, `status`, `seconds`, `usage` and tokens.

The same record is written atomically to `CROSSCHECK.json`.

## Audit

After a passing crew gate, the normally configured reviewer runs once. It
gets the diff since job start, the crew evidence and the connector test
result. It re-runs `audit_spot_requirements` (default 3) requirement evidence
commands and `audit_spot_fixes` (default 2) confirmed-fix commands, scans the
diff, and reports its own score, blocking list and any crew claim that did not
hold. See [REVIEWER.md](REVIEWER.md) and [PARALLEL.md](PARALLEL.md) for the
adjacent behaviours.

Risk detection is used only to (1) never sample the audit out and (2) record
`risky: yes/no + why`. The reviewer model is never switched because a file is
risky. Risk globs come from `risk_paths` (the name used in the 3.14.2 brief)
and `audit_risk_globs`; both lists are merged with the built-in defaults.
`audit_sample_rate` defaults to 1.0; `0.2` audits one in five low-risk jobs. A
failed crew claim forces full audits for the next five crosscheck jobs. On the
full audit, every seeded spot id must come back with `pass`/`fail` and the
command output that was re-run; a missing id, malformed list or failed spot is
blocking, and an empty audit section is `not_reported`, never a pass. The
heading-only fallback must satisfy the same spot contract. An audit blocking
list gets one crew fix round and a focused re-audit of only those items (the
focused round is not required to repeat the original spot sample).

A resumed crosscheck job replays the frozen `mode`/`crosscheck_*`/`audit_*`
settings and `risk_paths`; a saved crosscheck run without that config fails
closed instead of silently restarting in standard mode.

## Reproduction gate (3.14.3)

Findings are evidence, not opinions. In crosscheck mode every finding must
carry a `repro` object:

```json
{"repro": {"command": "python3 -m unittest tests.test_x.TestY.test_z",
           "output": "FAIL: assertion ..."}}
```

or, for a specification finding that cannot be executed, the exact source it
quotes:

```json
{"repro": {"file": "deepseek_mcp/safety.py", "line": 412,
           "quote": "if mode == \"off\":"}}
```

Before a finding is counted, the **connector** re-runs the reproduction once
in the workspace. Identical rows (same id and same `repro`) share one run; the
same id with different evidence is verified separately, so a decoy cannot hide
a real finding. The command is split into an argv and is never run through
a shell, with a scrubbed environment (`PATH`, `HOME`/desktop, `TMPDIR`/`TEMP`,
locale and the platform's process variables survive; API keys do not), a hard
10 s cap, and a process-group kill on timeout. The
observed output must contain the claimed output; an explicit `exit_code`
pins the status when the finder cares about it. A spec quote is checked
against the file at the recorded line. Findings that do not reproduce are
dropped, marked `filtered_by: "connector_repro"` with `repro_status` `failed`
or `unverified`, and counted as `false_alarm_filtered`; a dropped finding can
never be an open BLOCKING item. The per-job record reports `raw_findings`,
`unique`, `real`, `false_alarm`, `false_alarm_filtered`, `unverified` and the
breaker/spec split, so a noisy round is visible as numbers rather than prose.
