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

## Mixed models and competing candidates (new in 3.12.0)

`builder_model` / `builder_provider` set the parent builder. Empty values
inherit `dsh_model` / `dsh_provider`, so the default stays DeepSeek. A per-job
`builder_model` / `builder_provider` argument wins for that job only. The
connector writes a workspace-local 0600 provider/model overlay, passes it as
`--patch`, keeps the key in the environment, and deletes the overlay in a
`finally` block. No overlay is generated for the default target, so the 3.11.0
argv is byte-identical.

`slice_models` maps a slice name to `"provider/model"` or
`{provider, model}`; the connector validates every entry, caps the map at
`builder_model_max_slices` (8), and injects a deterministic `## Model plan`
section. Each child agent sets `provider` and `model` explicitly. The builder
writes `MODEL_AUDIT.json`; the connector cross-checks requested pairs, observed
models and existing files. A missing or ignored audit is `unverified`, never
`verified`; `model_plan_enforcement="warn"` marks the run degraded and
`strict` stops it before review.

`model_candidates` maps a slice to 2..N model specs. Each candidate is a child
on that model and writes `candidates/<slice>--<tag>.<ext>`. The connector
scores the files with the existing candidate scorer, picks the highest score
(lower known cost, then configured order on a tie), copies the winner over the
slice's first `Writes:` path from `SLICES.md`, and records
`model_competition`. The cap is `model_candidates_max` (3 extra candidates per
job). A competition without a whole-job cost ceiling records
`budget_warning="no_whole_job_ceiling"`; the connector cannot kill one child
inside the parent workflow, so `model_plan.midflight_kill=false` is recorded.

## Backup builder model (new in 3.12.0)

`backup_builder_model` (empty disables) names a second builder model. It uses
the same `dsh_bin` and the normal model overlay; no separate binary or wrapper
is needed. `backup_builder_provider` is optional, and
`backup_builder_max_cost_usd` (default 1.0, `0` disables) is an independent
ceiling; the whole-job ceiling can only make it smaller.

The switch table is deliberately narrow: credit/budget and auth switch
immediately; service unavailable (5xx) and dropped streams switch only after
`backup_builder_min_attempts` (2) and a RetryPolicy allowance; stalls,
timeouts, cancellations, low or missing scores and gate/review/revision
outcomes never switch. A builder that already wrote files is skipped with
`reason="side_effects"` because rerunning would mix partial artifacts. The
backup is never recursive and never switches twice.

The result header says
`| backup builder: <vendor>/<model> (main failed: <reason class>)`, and
`run["backup_builder"]` records status, class, redacted main error, attempts,
ceiling and settlement. The switch is not sticky: later waves and revisions
use the configured main builder again. The backup builder's vendor joins the
reviewer's builder-vendor set because it may have built the job.

## Pipelined builds (new in 3.11.0)

A job may declare a `write_set` (repo-relative prefixes or globs) or a `slice`
name from `SLICES.md`. When the queue is full and a running base job is in its
review phase, one disjoint job may start in its own git worktree, with its own
patch and its own reviewer. Unknown or absent write-sets overlap everything, so
a job that does not declare one stays serial. Plan widening defers further
starts until that job is done.

The connector never merges patches before install. In FIFO order it proves the
whole accepted set applies with `git apply --check` in a throwaway worktree;
any conflict applies nothing to the canonical tree and reports
`install_conflict`. A manual patch at the head is a drain point: later patches
stay pending until an operator installs or discards it. The lead still owns the
shared files; pipelining is a scheduling feature, not a second integrator.

The deterministic gate runs before the first reviewer call. Failure revises the
builder without spending a reviewer call, and after `gate_max_revisions` the
run ends `gate_failed` rather than pretending the work was reviewed. Failure
notes from previous runs of the same task type are advisory: they add a short
pre-flight list to the next builder prompt and never change the score.

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
| `builder_model` | "" | Parent builder model; empty inherits `dsh_model` |
| `builder_provider` | "" | Parent builder provider; empty inherits `dsh_provider` |
| `slice_models` | {} | Per-slice model map, capped at 8 |
| `model_candidates` (tool) | {} | Competing candidates per slice, capped at 3 extras |
| `model_plan_enforcement` | warn | `warn` degrades, `strict` stops on an unverified plan |
| `backup_builder_model` | "" | Backup builder model; empty disables |
| `backup_builder_max_cost_usd` | 1.0 | Backup phase's own ceiling; 0 disables |
| `reviewers` | [] | Ordered reviewer candidates; empty keeps the legacy wrapper |
| `reviewer_same_vendor_policy` | ask | `ask` offers same-vendor choices; `never` removes them |

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
