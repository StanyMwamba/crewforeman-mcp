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

## Resumable reviews and pre-flight (new in 3.9.2)

A review checkpoints its completed areas, findings and test reruns to `REVIEW.partial.md` and `REVIEW.partial.json`. If the connector stops, a resume checks only the remaining areas and merges carried findings under their original severity. Before a paid review, a short READY ping through the same reviewer path checks credit, auth and reachability. `review_preflight` defaults to `true`; `review_preflight_timeout` defaults to 45 seconds.

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
