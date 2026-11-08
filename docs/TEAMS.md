# How the agent team works

This page describes the team the lead can use. Sizes and limits below are the shipped defaults and the code paths that enforce them.

## The lead

The lead is the main builder session. It reads the brief, chooses the parts, reads a compact summary of each wave, merges the best candidate, runs the checks, and drives revisions. The lead reads wave summaries, not every draft.

## Team size

`choose_agents` classifies the task as simple, code, creative or research and estimates small, medium or large from the prompt length and the number of deliverables. A simple single-step question gets one agent and no helpers.

For real work the starting count comes from this table:

| Task type | Small | Medium | Large |
|---|---:|---:|---:|
| Code | 4 | 4 | 8 |
| Creative | 6 | 8 | 12 |
| Research | 6 | 8 | 12 |

The automatic start is capped by `subagent_soft_cap` (default 32) and `default_subagents` (default 16). Ordinary history nudges move the start by at most two agents in either direction. A strong historical bucket with a smaller count is matched directly, so that reduction can be larger; a larger count needs two comparable target-clearing runs before the start is raised by at most two. An explicit `subagents` count is kept exactly, including 0, up to `max_explicit_subagents` (default 32). Counts above that are refused before a paid process starts.

## Waves

A wave is a group of agents working at the same time. `_wave_sizes` fills each wave up to the detected Harness child concurrency, so a team of 10 at concurrency 4 runs as 4 + 4 + 2. It never starts more children than Harness allows. `max_harness_jobs` (default 4) separately caps concurrent Harness jobs and queues the rest in order.

## Splitting the work and choosing candidates

Each subagent takes a different approach or a separate part of the task and writes a candidate file first. A wave saves its results to `wave-<n>-results.md`. A sub-leader then writes `wave-<n>-summary.md` with the best candidate path, strengths, defects, discarded approaches and a seed for the next wave. The lead compares those summaries and combines the best work into one final answer. Later waves are seeded with the best candidate and its defect list, not started from scratch.

## Live scaling

When a wave's candidates score below `live_scaling_threshold` (default 7.0), CrewForeman can add a new wave with different approaches. It adds at most `live_scaling_max_extra_waves` waves (default 2), needs at least 120 seconds left, and, when a job budget is set, needs at least 10 percent of it unspent. An unknown cost is treated as over budget. A strong wave adds nothing.

## Rescue team

If the builder stalls or hits its time limit but has left usable output files, a short rescue finisher can complete the work. It gets at most 8 minutes and 12 tool calls and never extends the whole-job deadline. It is reported as a rescue, not as a normal completion.

## Revision loop

After the build, the independent reviewer scores the result and lists blocking defects. The target is 8.5/10 with zero blocking defects. `quality_rounds` (default 3) caps the revisions. Each later review re-checks the flagged defects and the new changes, using test results the connector signed itself. The reviewer is read-only by default; `allow_writing_reviewer` is an operator-only escape hatch.

`quality_convergence` defaults to `observe`, which records a would-stop convergence decision but keeps the normal loop. `auto` stops the loop and asks one question when the score stops improving.

### Review scope and follow-up tags

Pre-existing issues in untouched code go under `## Follow-ups (pre-existing,
non-blocking)`. Every follow-up line starts with exactly one tag: `[security]`
or `[data-loss]` (blocking) or `[minor]` (non-blocking regardless of wording).
An untagged follow-up containing a security or data-loss keyword is treated as
blocking (fail safe), so a double negation cannot hide a real finding. See
[REVIEWER.md](REVIEWER.md) for the parser mapping and examples.

## Resumable reviews and pre-flight (new in 3.9.2)

A review checkpoints its completed areas, findings and test reruns to `REVIEW.partial.md` and `REVIEW.partial.json`. If the connector stops, a resume checks only the remaining areas and merges carried findings under their original severity. Before a paid review, a short READY ping through the same reviewer path checks credit, auth and reachability. `review_preflight` defaults to `true`; `review_preflight_timeout` defaults to 45 seconds.

## Multi-agent efficiency (new in 3.10.0)

### Project map

When a workspace has a `PROJECT_MAP.md`, the connector stores a private
per-project record under the connector state directory, keyed by
a hash of the workspace path (`project-maps/<hash>.json`, owner-only
permissions); it holds the bounded map body and the fingerprint and is never
copied into the repository, a run record or a result. The fingerprint is the git
HEAD plus the working-tree status when git is available, otherwise the tracked
file list and mtimes. The map text is bounded at 12,000 characters.

Before the next builder starts, the connector compares the saved fingerprint with
the current one and offers the map in one of three states: `new` (map present, no
saved record), `reuse` (fingerprints match, with "Reuse this map; verify only what
you change."), or `stale` (fingerprints differ, with "This map is stale; refresh
PROJECT_MAP.md before you finish."). The run record keeps only the compact
summary - state, presence, bytes, version and a short fingerprint - never the
workspace path or the map body.

### Slice overlap warnings

Before a wave, the builder writes `SLICES.md`: each wave, then each slice, then a
`Writes:` line naming the files that slice may touch. The connector parses the
file and compares write-sets inside the same wave. Two slices in one wave that
share a file produce an overlap warning. The warning is deliberate: warn, not
block. It appears in the run record, in the result header and in the run metrics,
so the lead can reassign the file or accept the overlap. A file used by slices in
different waves is not an overlap.

### Waste report and team sizing

At the end of a multi-agent run the builder writes `WASTE_REPORT.md` with
`Agents launched`, `Agents used` and `Agents discarded` (a compact
`N launched / M used / K discarded` line also parses). The connector stores the
counts and the waste ratio in the run record. An explicit `Waste ratio:` is
clamped to 0..1 and still requires a launched count; otherwise the ratio is
discarded divided by launched, with discarded derived from launched minus used.
No launched count (or zero launched) means no ratio.

Team sizing reads the mean waste ratio from comparable history. When it is at or
above `WASTE_NUDGE_THRESHOLD` (0.35), the automatic start is reduced by up to
`WASTE_NUDGE_STEP` (2 agents), bounded by the configured floor, and the sizing
reason names the nudge. A lower or absent ratio changes nothing, and an explicit
`subagents` count is never nudged.

## Settings

| Setting | Default | Effect |
|---|---:|---|
| `default_subagents` | 16 | Ceiling for the automatic start |
| `subagent_soft_cap` | 32 | Soft cap on automatic sizing |
| `max_explicit_subagents` | 32 | Hard ceiling on a caller count |
| `max_harness_jobs` | 4 | Concurrent Harness jobs; 0 disables the cap |
| `quality_max_rounds` | 3 | Default revision limit |
| `quality_convergence` | observe | `auto` asks instead of looping |
| `live_scaling` | true | Enable extra waves for weak candidates |
| `live_scaling_threshold` | 7.0 | Weak-candidate score |
| `live_scaling_max_extra_waves` | 2 | Extra-wave limit |
| `review_preflight` | true | READY ping before a paid review |
| `review_preflight_timeout` | 45 | Pre-flight timeout in seconds |
| `independent_review` | true | Independent review for real work |

The score target is 8.5/10. A reviewer is a model: its score is judgement, not proof.

## Constants

| Constant | Value | Effect |
|---|---:|---|
| `project_map.MAX_MAP_CHARS` | 12000 | Bounded map offered to the next builder |
| `project_map.MAX_FILES` | 2000 | File-list fingerprint bound |
| `slices.MAX_TEXT` | 20000 | Bounded slice plan read per wave |
| `waste_report.MAX_TEXT` | 20000 | Bounded waste report read at the end |
| `WASTE_NUDGE_THRESHOLD` | 0.35 | Mean waste ratio that nudges the start down |
| `WASTE_NUDGE_STEP` | 2 | Agents removed by one waste nudge |
