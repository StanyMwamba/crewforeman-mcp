# Automatic parallel jobs (3.14.2)

Parallelism is automatic, not declared. When a Harness job is submitted
without `write_set`, the connector infers one from the brief, `PLAN.json` and
`SLICES.md`. It runs the job in parallel only when the inferred set is
confident and disjoint from every running job in the same repo. Otherwise the
job stays serial, exactly as before.

## Inference

`deepseek_mcp/writesets.py` performs a cheap, deterministic pass. There is no
model call and no git subprocess in the inference path.

Signals, in precedence order:

1. `PLAN.json` top-level `write_set` (confidence 0.93), then part/slice write
   sets or `files_expected` (0.90).
2. `SLICES.md` `Writes:` lines (0.88).
3. Explicit brief lines such as `Files:`, `Write set:`, `Touching:`,
   `Modify:`, `Create:` or `Change:` (0.90).
4. Backticked paths in the brief (0.90).
5. Bare paths containing a slash or a known file extension (0.78).

Agreement between two or more independent sources adds 0.03. A brief that
asks for the whole repo (`entire repo`, `all files`, `everywhere`) is marked
wide, loses 0.20, and always stays serial. More than 40 inferred entries also
loses 0.10. Unknown or prose-only paths are not confident enough to parallel.

Only repo-relative forward-slash paths are stored. Absolute paths, `..`
escapes, `.git`, URLs, `PLAN.json`, `SLICES.md` and `CROSSCHECK.json` are
dropped. On Windows, comparisons use a case-folded key; git pathspecs are
always passed after `--`.

## Admission rule

For a queued job in a workspace that already has a running pipeline job:

| Situation | Result |
|---|---|
| `auto_parallel=false` | serial, reason `auto_parallel_disabled` |
| no inferred/declared write set | serial, reason `no_write_set` |
| confidence below `auto_parallel_min_confidence` (default 0.85) | serial, reason `low_confidence` |
| wide brief | serial, reason `wide_task` |
| inferred set overlaps a running job in the same repo | serial, reason `write_set_overlap` |
| a running peer has no/low-confidence set | serial, reason `peer_unknown_write_set` or `peer_low_confidence` |
| disjoint and confident | parallel, reason `disjoint_confident` |

Different workspaces are independent: the overlap test only compares jobs
with the same canonical workspace. A job that is not eligible for the
pipeline still blocks parallel builders, and an ineligible active job keeps
the safe serial path.

Each parallel job runs in its own detached git worktree based on the queue
base commit. The canonical tree is never written by the builder.

## Diff containment and re-integration

When a parallel job finishes, the connector captures its patch and compares
the actual changed files with the inferred write set.

- The patch is installed only when every changed file is inside the set and
  `git apply --check` (or the normal queue proof) succeeds.
- A file outside the set marks the patch `serial_reintegration` with the stray
  file list. It is not merged blindly. The connector re-runs that job's
  integration serially on the current canonical tree: a clean `git apply`
  first, then a three-way apply as a rebase. The resolution
  (`apply_clean` or `rebase_3way`) and the reason are recorded.
- If neither applies, the patch is retained as `manual`, the worktree is
  retained, and the run reports the conflict; the canonical tree is unchanged.
- A failed `git apply --check` follows the same serial path.

The run's `pipeline` block records the inferred write set, confidence, source,
parallel/serial decision and reason, peers, stray files, integration
resolution and any conflict.

## Configuration

| Key | Default | Meaning |
|---|---:|---|
| `auto_parallel` | `true` | Infer a set when none is declared and allow disjoint parallel jobs |
| `auto_parallel_min_confidence` | `0.85` | Minimum confidence for parallel eligibility |
| `pipeline_enabled` | `true` | Worktree/patch machinery must be enabled |
| `pipeline_isolation` | `worktree` | Only a worktree is supported |
| `pipeline_install` | `auto` | `auto`, `manual` or `off` |
| `pipeline_min_remaining_usd` | `1.0` | Budget headroom before an extra slot is used |
| `pipeline_max_pending_patches` | `8` | Bounded patch backlog |

The tool arguments `auto_parallel` and `auto_parallel_min_confidence`
override the config for one job. The effective values are frozen into the
job's `pipeline` block (`auto_parallel`, `auto_parallel_min_confidence`) and
the admission queue item, so a per-job argument still wins after the job is
queued or the live config changes. `write_set` and SLICES precedence are
unchanged; an explicit declaration always wins over inference. A per-job
`auto_parallel=false` keeps the job serial, but it still starts when no
same-repository peer is running.

## Troubleshooting

- Job stayed serial: inspect `pipeline.unavailable_reason` or
  `pipeline.parallel_reason` in the run record.
- Confidence too low: name the files or directories in the brief, or add a
  `PLAN.json` `write_set`; prose-only sets are deliberately serial.
- Overlap: split the two jobs or declare narrower, disjoint prefixes.
- Install conflict: read `pipeline.integration` and `pipeline.install`; the
  worktree is retained when the patch could not be re-integrated.
- Do not run two jobs in the same non-git workspace in parallel; a git
  worktree is required for isolation.
