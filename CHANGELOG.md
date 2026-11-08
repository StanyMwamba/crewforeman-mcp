# Changelog

All notable changes to CrewForeman are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## 3.12.0

Configurable builders and reviewers, mixed-model teams, per-model cost and
score learning, a backup builder, and a stricter reviewer-vendor rule. The
deterministic gate and PLAN contract learn two exceptions.

### Added

- **Builder model routing (R1).** `builder_model` / `builder_provider` and the
  per-job tool arguments select the builder through a workspace-local 0600
  provider/model overlay. Keys stay in the environment (`apiKeyEnv` names
  only), the overlay is deleted in a `finally` block, and the empty/default
  target keeps the 3.11.0 argv byte-identical. Validation rejects injection
  characters, unknown providers and unlisted models before a paid call.
- **Reviewer model routing (R1b).** `reviewer_model` / `reviewer_provider` and
  the per-job arguments choose the reviewer the same way. Existing
  `dsh-reviewer-kimi.sh` / `dsh-reviewer-sonnet.sh` wrappers remain the legacy
  path, including `backup_dsh_bin`.
- **Mixed models and competing candidates (R2).** `slice_models` assigns a
  model per slice (cap 8), `model_candidates` runs 2..N candidates per slice on
  different models, and the existing candidate scorer picks the winner. The
  builder writes `MODEL_AUDIT.json`; the connector records `verified`,
  `partial`, `mismatch`, `unverified` or `ignored` and never claims an
  unobserved model ran. `model_plan_enforcement` is `warn` or `strict`.
- **Per-model cost and score (R3).** `run["model_usage"]` aggregates tokens,
  estimated cost, `cost_known`, aggregate scores, wins/losses and an
  `__other__` fold. Unpriced models keep `null`, never a fabricated number.
  Team sizing gains a model axis (`model_hint`); `failure_notes` moves to
  schema v2 with a sibling, idempotent `model_stats` map and a bounded note.
- **Backup builder (R4).** `backup_builder_model` takes over only for
  credit/budget, auth, 5xx-after-retries or repeated stream drops, under its
  own `backup_builder_max_cost_usd` ceiling. It never switches after writes,
  never recurses, and the exact header records
  `backup builder: <vendor>/<model> (main failed: <class>)`.
- **Reviewer vendor rule (R5).** `reviewers` is an ordered candidate list. The
  first cross-vendor candidate whose READY preflight passes wins; otherwise the
  job pauses with ONE `needs_input` question listing available same-vendor
  models. A same-vendor review starts only when the user chooses it and carries
  the exact label `same-vendor review (chosen by user)`. Declining returns
  `ok_unreviewed` without inventing a score.
- **Gate and PLAN scope (R6).** The generic secret rule now flags literal
  key-shaped values only; ordinary identifiers assigned to calls, variables or
  `int(...)` are not findings. A diff with no product change skips lint/tests
  but still scans secrets; patch/log/cache artifacts and five known split
  fixtures are handled by path; a no-baseline job classifies its outputs.
  `PLAN.json` is `optional` for a tiny single-file job instead of `invalid`.

### Documentation

- TEAMS, REVIEWER, OPERATIONS, CLIENTS, STATUS, LIMITATIONS and
  `config.example.json` document the new keys, records and status. README
  keeps its 3.11.0 shape apart from the version, the Builder role and three
  Guarantees lines.

## 3.11.0

P3.5: a deterministic pre-review gate, written predictions, learned failure
notes and write-set-aware pipelined jobs.

### Added

- **Deterministic gate (F1).** Before any paid reviewer call, the connector
  scans the builder's added/changed files for secrets, detects and runs lint,
  and runs the project test suite on the exact post-builder tree. Checks are
  ordered secrets → lint → tests with per-check budgets, signed evidence and
  skip-safe detection. A hard failure starts a gate revision without a
  reviewer process; after `gate_max_revisions` the run ends `gate_failed` with
  `review_status="skipped"` and `quality_score=None`. A gate crash fails
  closed (`error`, `stop_reason="gate_error"`), never open.
- **Written predictions (F2).** The builder is asked to write `PLAN.json`
  before its first edit. The connector normalises paths and globs, compares
  the declaration with the actual diff (unexpected/missing files and tests,
  risk hits, stale/late/invalid plans) and records bounded evidence.
  `plan_contract="warn"` is the default; `"block"` adds one blocking defect so
  even a 10/10 review must revise; `"off"` disables the feature.
- **Learning from failures (F3).** A bounded, decaying per-task-type store
  records reviewer blocking-defect categories after each round and injects at
  most one short "common failures" note into the next builder or revision
  prompt. Only fixed buckets and opaque ids are stored; non-bucket labels are
  rejected or hashed and never rendered.
- **Pipelined jobs (F4).** A job may declare a `write_set` (or a `slice` from
  `SLICES.md`). While a base job is in its review phase and capacity is full,
  one disjoint queued job may start in its own detached git worktree, with its
  own patch and reviewer. Accepted patches are installed FIFO only after a
  combined `git apply --check` in a throwaway worktree; a conflict applies
  nothing and is reported on the job and in the result header. Limits:
  `pipeline_max_extra_jobs=1`, a 2× hard ceiling, cost headroom or a finite
  per-job budget (unknown cost fails closed), and a bounded patch backlog.

### Changed

- Reviewer failure attribution now names the reviewer that actually ran. A
  credit/quota/auth/429 failure from a non-DeepSeek backup names its vendor
  and model and says the DeepSeek balance is unaffected; the
  "DeepSeek account balance is empty" sentence is reserved for a DeepSeek
  reviewer. Main-reviewer attribution is unchanged.
- Status vocabulary gains failure-shaped `gate_failed`; jobs refuse to close a
  `gate_failed` run as done. `docs/STATUS.md` documents the precedence and the
  `install_conflict` terminal detail.
- Jobs expose `write_set`, `pipeline_slot`, `base_commit` and `install_status`
  in the structured status; the tool accepts `write_set` and `slice`.

### Fixed

- A backup-reviewer quota error is no longer reported as a DeepSeek balance
  problem, so operators do not top up the wrong account.
- A pipeline install conflict no longer leaves a job looking accepted; the
  canonical tree is left untouched and the job reports `install_conflict`.

### Tests

- Focused suites for the gate, plan contract, failure notes, pipeline core,
  queue transport, vendor attribution, status vocabulary and integration
  seams; release/docs assertions updated to 3.11.0. The full stdlib `unittest`
  suite is the release gate.

## 3.10.0

Part A: reviewer resilience, resumable reviews and explicit job deadlines.

### Added

- Optional backup reviewer (`backup_dsh_bin`, default empty = unchanged), with
  optional `backup_reviewer_vendor` / `backup_reviewer_model` identity labels.
  The connector switches to the backup only on non-quality failures of the
  main reviewer: credit/budget exhaustion (429 with budget/quota wording or
  insufficient balance), auth/invalid key, service unavailable/5xx after the
  bounded retries, and repeated stream drops after the bounded retry. Low
  scores, blocking defects and missing-score reprompts are quality outcomes of
  a successful reviewer and never trigger a switch.
- A backup review is labelled in the run record, the journal and the result
  header with `Reviewed by: backup (<vendor/model>) — main failed: <reason
  class>`. The backup is not sticky: the next job starts with the main
  reviewer again.
- Bounded retry for stream drops caused by a closed handle: `flush on a closed
  handle`, `STREAM_CLOSED` and `ECONNRESET` are treated as transient and
  retried with backoff up to a hard attempt cap. A repeated drop after the cap
  becomes a switch class for the backup reviewer.

### Changed

- Interrupted reviews are resumable. A review that actually started and was cut
  off is reconstructed from its partial checkpoint and re-run on resume rather
  than being consumed as a completed round.
- An explicit caller `job_max_seconds` is honoured even when it exceeds the
  built-in 7200-second default. An explicitly configured lower value still
  caps it, and resume does not re-clamp a saved envelope by the default. The
  per-process `stall_cap_hard_max_seconds` ceiling is unchanged.

### Fixed

- A review actually cut off by the whole-job deadline gets one fresh review
  window on resume; a review that never started behind an already-expired
  deadline still refuses to start (R5b).
- If both the main and backup reviewers fail, the run records a clear reviewer
  error and `quality_score` stays `None`; no score is invented.

### Tests

- Release version assertions updated to 3.10.0, with focused coverage for
  backup switching, bounded stream-drop retries, resumable interrupted
  reviews and explicit `job_max_seconds`.

### Part B

- Review scope: the independent reviewer separates findings about the change
  under review from pre-existing issues in untouched code. Pre-existing findings
  go under `## Follow-ups (pre-existing, non-blocking)` and do not make the run
  fail; every follow-up must start with exactly one tag, `[security]`,
  `[data-loss]` or `[minor]`. `[security]` and `[data-loss]` stay blocking and
  are counted in `Blocking defects: N`; `[minor]` is never blocking regardless
  of wording. An untagged follow-up containing a security or data-loss keyword
  is treated as blocking (fail safe), so a double negation cannot hide a real
  defect. Blocking follow-ups surface as `[blocking: security]` or
  `[blocking: data-loss]`. The follow-up list is kept in the run record, a short
  list appears in the result, and the missing-score re-prompt path is unchanged.
- 6a Project map: multi-agent workspaces keep a bounded `PROJECT_MAP.md`; the
  connector stores a private fingerprinted per-project record under its state
  directory and offers the map to the next builder as reuse (verify only what you
  change) or refresh (stale). Summaries never carry the map body or workspace
  path.
- 6b Slice overlap: a connector-side `SLICES.md` parser reports write-set
  overlaps between slices in the same wave in the run record, result header and
  metrics. Warnings only; the run is never blocked.
- 6c Waste report: `WASTE_REPORT.md` is parsed into launched, used and discarded
  counts plus a bounded waste ratio (explicit or derived) in the run record; a
  ratio is only used when a launched count is present. `choose_agents` uses the
  historical mean waste ratio: at or above 0.35 it reduces the automatic start by
  up to 2 agents, never below the floor and never for an explicit `subagents`
  count.
- 6d Docs: `docs/TEAMS.md` describes the project map, slice overlap warnings and
  waste-aware sizing, and the README gains one Guarantees line. Version stays
  3.10.0.

### Part C

- `docs/CLIENTS.md` now carries editor setup guides for Cursor, VS Code with
  GitHub Copilot, Claude Desktop, Claude Code and the OpenAI Codex CLI, each
  with the config file location, a copy-paste `mcpServers` entry and a
  verification step. The snippets were checked against the vendors' official
  MCP documentation (linked in the guide) and against this server's stdio
  transport. The guide also shows how to start the server manually before
  wiring a client.

## 3.9.5

### Added

- Fixed `builder-practices` block (v1.0, at most 350 words) injected into
  builder and revision prompts. A full **code** variant is used for
  implementation work and a lighter variant for documentation and research
  tasks. It applies to every build and does not depend on skill detection.
- `deepseek_mcp/builder_practices.py`, which owns the versioned practice text
  and selects the variant.
- Run records now include `builder_practices` (set, version, variant,
  enabled, words, characters and estimated tokens), `first_review_score`, and
  `review_scores`, making the first review and the final score available side
  by side.
- `scripts/report_scores.py`: first-review versus final score grouped by
  practice-set version. Run
  `python3 scripts/report_scores.py --runs <log_dir>` for a table, or add
  `--json` for machine-readable output.

### Changed

- Builder and revision prompt assembly in `deepseek_mcp/quality.py` appends
  the practices block when enabled.
- New `builder_practices` config key (boolean, default `true`) and
  `DSH_BUILDER_PRACTICES` environment alias. Set `builder_practices` to
  `false` (or export `DSH_BUILDER_PRACTICES=false`) to disable injection.
- Version bumped from 3.9.4 to 3.9.5 in package metadata, runtime/serverInfo,
  observability, the smoke client, the README's version line (line 3), and
  docs headings.
- Documentation: new `docs/CREDITS.md`, a "Builder practices (3.9.5)" section
  in `docs/OPERATIONS.md`, and this changelog entry.

### Tests

- New focused tests: `tests/test_v395_practices_core.py`,
  `tests/test_v395_prompt_wiring.py`, `tests/test_v395_run_record.py`, and
  `tests/test_v395_score_report.py`.
- Existing release tests were updated for the 3.9.5 version.

## 3.9.4

### Added

- One score re-prompt per completed review that has no parseable 0-10 score.
  The connector retries in the same reviewer session when a session id exists;
  when the session is gone or errored, it makes one short fresh reviewer call
  carrying the report text. The attempt is recorded as `score_reprompt`
  (method, status, seconds, tokens in/cache/out/total, cost, parsed
  score/blocking) and as a `score_reprompt` journal event. Its usage is added
  to `reviewer_executions`, the review round and `reviewer_usage`.
- `score_missing` on the review round and run record: an unscored review is
  represented as missing rather than as `0.0`.
- Regression coverage in `tests/test_v394_review_score.py` for parser
  tolerance, the same-session re-prompt and one-shot fresh fallback, token
  roll-up, `score_missing` gating/resume, legacy synthetic-defect cleanup and
  the unscored revision brief. The tests use the fake reviewer and need no
  network access.

### Changed

- Score parsing is context-aware: a number is accepted only from a whole score
  label, so unrelated text such as `checked 3/10 files`,
  `9.0/10 pt`, `Test coverage score: 10/10` or `Code quality score: 9/10`
  is no longer read as a score. A parenthetical qualifier is dropped only when
  it explicitly names the overall score (`## Score (overall)`,
  `(out of 10)`); category forms such as `## Score (test coverage)` and
  `| Quality (code quality) | 9/10 |` are rejected instead of being collapsed
  to `Score`. `Final quality score: X/10`,
  bold/table forms and a final standalone `N/10` still parse; target and
  visual-impact lines do not win, and a bare closing value counts only under an
  overall-score or summary heading (not `## Tests`/`## Fix list`). Quoted or
  blockquoted scores are ignored. A review with no score statement yields
  `score=None`; no number is fabricated. The fresh score-re-prompt fallback
  derives the score the report's findings support instead of answering
  `unknown`.
- A re-prompted score is reparsed through the normal review parser, so the
  creative visual-impact cap and the report's section-derived blocking count
  still apply. A reply that omits `Blocking defects: N` keeps the
  report-derived count rather than recording a fabricated zero.
- A score-missing round with no real blocking defects skips the revision cycle
  and moves to the next review round. If no round remains, the run ends as
  `ok_below_gate` with `quality_score` null and `review_passed` false, and the
  warning names the unscored review instead of printing a `None/10` score.

### Fixed

- Removed the `score = 0.0` coercion and the synthetic blocking defect
  ("Independent reviewer did not report a 0-10 score.") added when a review
  completed without a score. The reviewer's real defects and real
  `blocking_defects` count are preserved. A 3.9.3 partial/report that still
  contains that connector-invented bullet is filtered on merge/parse, so it
  cannot keep a workspace permanently blocked.
- The quality gate no longer accepts a null score, and an unscored review with
  no real defects no longer buys a revision for a defect that does not exist.
- Resume treats a score-missing round that recorded no revision as a consumed
  review round, not as a pending revision, so the next round re-reviews. A
  score-missing round that does have real blocking defects still resumes its
  missing revision.

## 3.9.3

### Added

- State root: `CREWFOREMAN_STATE_DIR` (environment) and `state_root` (JSON
  config key) relocate the four connector-owned roots `state_dir`, `log_dir`,
  `jobs_dir` and `pid_file` under one directory (`<root>/state`,
  `<root>/runs`, `<root>/jobs`, `<root>/keep_awake.pid`). Unset means the
  historical 3.9.2 defaults, byte for byte; explicit config-file values and
  per-key `DSH_*`/`DEEPSEEK_*` aliases still win. `dsh_sessions_dir` is **not**
  derived (see Fixed). `skills_dir` and `harness_workspace` are not derived.
  See `docs/OPERATIONS.md`.
- Sandbox-safe test suite (P3.4a): `tests/isolation.py` (standard library
  only) gives every test a fresh `case-*` directory under a workspace-local
  scratch base: an existing `TMPDIR`/`TMP`/`TEMP` whose basename is
  `.dsh-tmp-*` first, then `<repo>/.dsh-tmp-tests`, then `<repo>/.tmp`.
  `repo.parent` is never a candidate. `tests/__init__.py` installs the
  per-test hook for `python3 -m unittest discover -s tests`, and `load_tests`
  covers package discovery; `tests/conftest.py` is only an optional pytest
  adapter, not a dependency. The helper points the state root,
  `DSH_SESSIONS_DIR`, `DEEPSEEK_MCP_CONFIG`, `TMPDIR`/`TMP`/`TEMP` and
  `PYTHONPYCACHEPREFIX` inside the case and clears inherited
  `DEEPSEEK_MCP_LOG_DIR`/`_JOBS_DIR`/`_PID_FILE` and
  `DEEPSEEK_DSH_SESSIONS_DIR` for the duration (restoring them afterwards),
  because those aliases outrank the derived roots. Runs, jobs, sessions,
  traces, checkpoints, queue/locks, journals, skills, restart/render status
  and tempfiles stay in the workspace; the directory is removed after the
  test. `HOME`/`USERPROFILE`/`XDG_*` are deliberately untouched because
  `api.redact_secrets()` rewrites a personal home prefix to `~` in
  machine-readable trace paths, and redirecting `HOME` would break the resume
  round-trip.
- If no workspace scratch root is writable (read-only review sandbox), the
  isolation hook records one **counted** skip per test
  (`result.startTest` + `result.addSkip` with an `environment-only:` reason +
  `result.stopTest`) instead of letting `SkipTest` escape before `startTest`.
  The run completes as `OK (skipped=N)` and never aborts with a traceback.
  `tests/sandbox_guard.py` detects an outer Seatbelt/Harness sandbox through
  `safety.seatbelt_usable()` for a future test that genuinely needs a real
  nested `sandbox-exec`; no current test needs it.
- 34 new tests over the 3.9.2 baseline: 18 from the original 3.9.3 build
  plus 16 from the fix round (3 for state-root/session precedence, 2 for the
  B1 fake-HOME session regression, 3 for isolation/read-only counted skips,
  6 for macOS restart verification including the fail-closed
  ancestor-unknown regression, and 2 for the detached-caffeinate handle).
  Full stdlib suite: **1042 tests** (1008 historical pytest baseline), zero
  failures, zero errors, zero skips in a writable workspace-write Harness
  job, with the system Python, no `danger-full-access`, no nested sandbox and
  no installed test dependency.

### Fixed

- Harness session accounting: `state_root` no longer derives
  `dsh_sessions_dir`. dsh decides its session home from `DSH_HOME`, which the
  connector does not pass to builder/worker children, so deriving the read
  path made token/cost accounting and child activity scan an empty directory.
  It stays `~/.dsh/sessions` unless the config file or
  `DSH_SESSIONS_DIR`/`DEEPSEEK_DSH_SESSIONS_DIR` explicitly sets it. Per-test
  isolation redirects `DSH_SESSIONS_DIR` at the case so the suite still keeps
  sessions inside the workspace.
- Read-only/scratch roots: an unwritable workspace now yields counted
  environment-only skips instead of a traceback that aborted the run; the
  first temp candidate is no longer `repo.parent` (which would write into the
  live `~/DeepSeek-MCP`), and `.dsh-tmp-*` is git-ignored.
- macOS restart process check: plain `pgrep -x Claude` can miss the running
  app because `pgrep` excludes its own ancestors, and a connector started from
  inside Claude's process tree is a descendant of the app. The helper
  **executes** `["pgrep", "-a", "-x", "Claude"]` in the same attempt as the
  authoritative `osascript -e 'application "Claude" is running'` and records
  both results in `restart-status.json`. `osascript` remains authoritative for
  `reopened`; pgrep is corroboration only and never overrides a failed or
  false `osascript` result. Ancestor PIDs are excluded from the corroboration,
  and an incomplete ancestor walk (for example `/bin/ps` denied) is recorded
  as inconclusive and cannot confirm a reopen. `pgrep -a -x` is corroboration
  and diagnosis only; it is not a fix and never makes `reopened` true. The
  out-of-scope `restart_claude.sh` still relies on `is_running()`.
- ResourceWarning noise: the identified intentionally detached children — the
  macOS `caffeinate` child in `jobs.keep_awake()`, the fake-dsh `tree`
  grandchild, the fake-dsh grandchild/opener scripts in the v3.2 stall/final
  pipe tests and `tests/test_p32_restart.py` — are marked as reaped on their
  `Popen` handles. The last residual was attributed to the real detached job
  worker started by `tests/test_v360_cancel.py`; that test now captures and
  reaps the handle after cancellation. The final full-suite logs contain zero
  `ResourceWarning: subprocess` lines.
- Documentation: `docs/OPERATIONS.md` documents the four-root state root, the
  explicit session override, the counted read-only skip behaviour and the
  executed macOS process check; the retention sentence no longer claims there
  is no base-directory key. `docs/CLIENTS.md` and `CHANGELOG.md` match the
  executed check.

### Changed

- Version 3.9.3 (`deepseek_mcp/config.py`, `deepseek_mcp/observability.py`,
  `pyproject.toml`, README and current-version docs).

## 3.9.2

### Added

- Detached client-restart helper (`deepseek_mcp/restart_helper.py`):
  `restart_client_app` now closes the client, schedules a helper that
  survives the client exit (POSIX `start_new_session`, Windows
  `DETACHED_PROCESS`), waits, reopens, verifies with `osascript`/exact
  `pgrep -x`/`tasklist`, retries a few times, and records JSON status at
  `restart-status.json` (`restart_status_path`, `restart_helper_attempts`,
  `restart_helper_retry_seconds`). No version bump.
- Release-safety guidance in `docs/OPERATIONS.md`: the builder never pushes,
  tags or publishes; a separate post-acceptance step pushes exactly the
  reviewed commit after `review_status=accepted` and `review_passed=true`,
  checking HEAD, clean status and the review report. The optional future flag
  `forbid_network_side_effects_until_accepted` is documented as not yet
  implemented because DSH has no reliable same-job git/network interception
  point.
- P3.2 resumable reviews: the connector writes `REVIEW.partial.md` and
  `REVIEW.partial.json` with areas checked, findings/evidence and tests rerun,
  refreshed by a heartbeat while the reviewer runs. The reviewer emits
  structured `@@REVIEW_PROGRESS@@` checkpoint lines that the connector parses
  and persists live, so a hard kill keeps the findings and completed areas.
  An interrupted review is resumed from that checkpoint: the new reviewer
  checks only the remaining areas, and carried findings are merged into one
  `REVIEW.md` under their original blocking/non-blocking headings.
- P3.2 reviewer pre-flight: a near-free `READY` ping through the same reviewer
  path runs before the paid review. No credit/quota, auth failure or
  unreachable reviewer stops the review with a vendor-correct remedy
  (`review_preflight`, `review_preflight_timeout`, `DSH_REVIEW_PREFLIGHT`).
- P3.2 reviewer usage visibility: per-review-round input, cache-read, output,
  total tokens and (when `reviewer_prices` are configured) cost are shown in
  the run `.json` and `.md`, the job result text and structured job status;
  the pre-flight reviewer call is folded into the same round/aggregate counts.

### Fixed

- Test portability in root/read-only Linux sandboxes: `tests/__init__.py` no
  longer creates nonexistent explicit `TMPDIR`/`TMP`/`TEMP` candidates; it
  probes only existing explicit directories and still creates the deliberate
  repository/workspace/cache fallbacks.  New `tests.tempdir()` and
  `tests.make_tempdir()` helpers return a verified writable directory or raise
  `unittest.SkipTest` with an environment-only reason.  The retry, health and
  tempdir tests now use those helpers and a regular-file blocker instead of
  `/no/such/...`, which uid 0 could create.  See `docs/OPERATIONS.md`.
- Linux restart safety: the default helper now matches the app's exact
  process name (`pkill -x`/`pgrep -x`) instead of the full command line, so
  its own `--app-name` argv cannot kill or satisfy verification. Reopened
  processes are checked against the expected executable path.
- Resume severity: defect extraction now keeps `## Blocking defects` and
  `## Non-blocking defects` distinct, and carried findings merge back under
  their own heading instead of being promoted to blocking. A carried finding
  that the resumed report repeats under the other heading is now moved back to
  the checkpoint's original section (evidence preserved) instead of being
  skipped; the connector matches carried findings by normalised text and
  severity group, and on a resumed review treats the merged report's
  blocking/non-blocking sections as authoritative over the trailing
  `Blocking defects: N` summary, so a stale count cannot hide or promote a
  moved finding. Relocation is decided from the actual Markdown heading, so
  the prompt-rendered inline `[blocking]`/`[non-blocking]` tag cannot suppress
  moving a repeat that sits under the wrong heading. This closes both the
  false-accept and false-promotion cases.
  See `tests/test_p32_review_resume.py` and
  `tests/test_p32_resume_acceptance.py`.
- Restart executable identity: `_same_executable` no longer accepts a bare
  basename match. It requires both paths to exist and be the same real path or
  the same inode (`os.path.samefile`), so a different executable with the same
  name cannot satisfy restart verification. Symlink resolution is retained;
  see `tests/test_p32_restart.py`.

## 3.9.1

### Added

- P3.1 queue dependencies: `harness_deepseek` accepts optional `urgent: true`
  and `depends_on: <run_id>`. An urgent build is ordered after P0 resume and
  before ordinary builds. A dependent job waits in `queued` state without
  holding a lease or running slot, and its dependency is expedited ahead of
  ordinary queued builds. Unknown, failed, cancelled and cyclic dependencies
  are rejected before a worker starts; a dependency that fails while the
  dependent is waiting fails that job instead of deadlocking the queue.
  `deepseek_job_status` exposes `urgent`, `depends_on`, `waiting_on`,
  `dependency_status` and the urgent-aware `priority_label`.
- P3.1 idle-slot safety net: the dispatcher periodically checks (file reads
  only, no model call) whether every occupied slot is idle/waiting while work
  is queued. If so it admits exactly the next eligible queued job, records an
  `admission_safety_net` queue event (mirrored to the P1 journal when
  possible), and never exceeds the fixed `2 * capacity` hard ceiling.
- Review-cost wiring: R1 now auto-detects a project in a direct child
  directory (for example `<workspace>/repo/tests`) and runs the signed suite
  from that project root; R2 prioritises focused/source files and caps each
  diff body so generated artifacts cannot consume the whole context; the
  reviewer prompt is evidence-aware (trust a clean signed suite, do not rerun
  the whole suite). A job can no longer close as `done` while an independent
  review phase is in flight or was never recorded.
- Reviewer accounting: review rounds and the run record now carry the
  reviewer's own input/cached/output tokens and a `reviewer_usage` aggregate
  when readable Kimi/DSH session logs exist. A new `reviewer_prices` table
  (default: `{}`, no prices shipped) makes reviewer cost explicit when the
  operator supplies per-million-token rates.

### Changed

- Queue ordering is additive: records without `urgent`/`depends_on` keep their
  pre-P3.1 order, and `PRIORITY_RESUME` / `PRIORITY_BUILD` numeric values are
  unchanged. The 20 MCP tool names and the package version are unchanged.

### Fixed

- A dependency whose queue record is already `done` but whose job status is
  `done/needs_input` now keeps the dependent `queued` with no lease; the
  `needs_input` hold takes precedence over terminal success. A later
  `finished` progress releases it.
- A job whose independent-review run ended with a durable `status="error"`
  (for example a reviewer child session still writing after exit) now closes
  as `error`, not `done/finished`. The superseded-attempt path honours the
  canonical run's review gate too, while a recorded reviewer failure
  (`ok_unreviewed`) remains a successful job.
- SDK-transport reviewer usage is priced through the configured
  `reviewer_prices` table (built-in rates remain the fallback), matching the
  headless session-log accounting.

## 3.9.0

### Added

- P3-1 bounded, fair admission: `deepseek_mcp/admission.py` adds a persistent
  queue under `<state_dir>/queue/`, one lease per running job
  (`pid`, `host`, `heartbeat`, `lease_until`, `fencing_token`), a lazy
  dispatcher started by `serve()`, P0 resume priority and `reserve_resume_slot`
  (default on). `max_harness_jobs` defaults to **4**; `0` means unlimited. A
  positive cap is clamped to the detected Harness child capacity. Queued work
  survives a server restart; running workers keep their detached process and
  heartbeat their lease from `jobs.write_status`. Dead-pid and three-missed-
  heartbeat leases are reclaimed, and a late heartbeat with an old fencing
  token is ignored. `deepseek_job_status` reports `queue_position`, `priority`,
  `priority_label`, `holder` and `lease_until`.
- P3-2 review convergence: `defect_signature()` hashes the normalized defect
  kind, file anchor and evidence type. Signatures and `first_seen_round` are
  persisted in `review_rounds` and checkpoints. When the same blocking
  signature repeats in `K=2` consecutive review rounds, `quality_convergence`
  can stop auto-revision and emit exactly one attention question with options
  `resume_with_scope`, `accept_below_gate`, `stop`. Cosmetic-only blocking
  findings stop after one revision when the score is within tolerance.
  `quality_convergence` is `observe|auto` and defaults to **observe**.
- Reviewer cost reductions: the connector now captures a workspace baseline,
  runs the full test suite on the exact reviewed tree before each review and
  passes the reviewer an HMAC-signed result (tree hash, counts, output digest).
  Review round 1 is a full review; later rounds start from the connector diff
  and the previously flagged defects. Reviewers are told to trust the signed
  evidence for that exact tree, rerun only new/defect-related tests, and may
  rerun everything when suspicious.
- `scripts/smoke_live.py`: opt-in, paid end-to-end smoke (tiny builder +
  independent reviewer + version check). Requires `--yes`; documented in
  `docs/INSTALL.md`.

### Changed

- Admission is scoped to `harness_deepseek` and `resume_deepseek`; ask,
  consult and swarm jobs keep their previous direct-start behaviour.
- The review baseline is captured immediately before each revision (and once
  before the builder), so a later round's diff is exactly the preceding
  revision's changes instead of an empty diff. Binary/oversized files are
  recorded in the baseline manifest (with an `excluded` reason) and reported
  as metadata-only changed/added/deleted entries.
- New config keys: `max_harness_jobs` (4), `reserve_resume_slot` (true),
  `admission_heartbeat_seconds` (30), `admission_lease_seconds` (90),
  `quality_convergence` (`observe`), `review_test_suite` (`auto`),
  `review_test_command` (empty), `review_test_timeout_seconds` (300),
  `review_context_max_chars` (60000). Environment aliases use the documented
  `DSH_*` / `DEEPSEEK_*` pattern.
- `docs/OPERATIONS.md` documents admission, convergence and review-evidence
  operation; `docs/INSTALL.md` documents the live smoke.

### Fixed

- The MCP `initialize` instructions header now derives its version from
  `SERVER_VERSION`, and the four release tests derive the same prefix from the
  constant, so the header cannot drift from `serverInfo.version` again.
- Admission reclaim re-reads and revalidates the lease under the per-work
  lock (identity, fencing token, expiry and PID) before queueing an item, so
  a heartbeat that renews the lease between the stale expiry check and the
  lock can no longer be fenced and duplicated by a second worker.
- `accept_below_gate` and `stop` now resolve the pending convergence question:
  the attention payload is cleared, the chosen option is recorded, a terminal
  `ok_below_gate` result is written, the work item is finished and the MCP
  task completes instead of remaining `input_required`.
- A terminal job reports `queue_position: 0` even when its older status file
  still carries a non-zero position, and `jobs.run_job()` restores the
  process-wide worker globals in `finally` so an in-process caller cannot
  leave progress/heartbeat writes aimed at a stale status file.
- A queued MCP task now receives its `job_id` before `start_job` returns, so
  it stays `working` while the job is queued and completes only from the
  terminal job/journal state instead of the queue marker.
- Admission state/lock failures fail closed: the job and its work item are
  marked failed with a sanitized error and no worker is spawned, preserving
  `max_harness_jobs` when the queue is unavailable.
- `resume_with_scope` is a working convergence remedy. A manual resume reopens
  one revision/review cycle, passes the scope note to the revision and lets
  the historical repeated signature pass that one decision.
- Connector evidence uses a persistent HMAC key (bytes) in connector state,
  referenced by `key_id`; `verify_evidence()` verifies the exact payload and
  rejects tampering.
- Provider/reviewer errors now name the actual vendor and cause. A Kimi
  (Moonshot) HTTP 429 `exceeded_current_quota_error` or insufficient-balance
  failure reports the Kimi/Moonshot remedy instead of claiming the DeepSeek
  account balance is empty; DeepSeek, rate-limit and authentication cases keep
  their own remedies. The failed execution's provider/model/model-list fields
  have strict precedence over configured `dsh_*` hints, so conflicting config
  cannot rename the vendor; configuration is only a fallback and free error
  text is used last.
- A convergence stop stays pending through terminal finalization:
  `_finalize_work()` writes the durable result as `needs_input`, holds the work
  item and does not append a terminal `result` event, so the MCP task is
  `input_required` with exactly one question/options payload.
- The admission capacity check and claim share a queue-wide interprocess
  `queue.lock`, so independent controllers or server processes admitting
  different run ids cannot exceed `max_harness_jobs` by both observing the
  last free slot.

## 3.8.1 — P2 authority, health and cost controls

### Added

- P2-1 authority envelope: every `harness_deepseek` run records a direct
  authority basis, resolved sandbox, forced read-only reviewer policy,
  workspace root, whole-job time/cost bounds and agent cap. Resume, rescue,
  live-scaling, reviewer, revision and automatic-skill phases are
  continuations that may only narrow the envelope; a resume under stricter
  config records `policy_drift` and uses the intersection. `reviewer_sandbox`
  values other than read-only are refused unless the operator sets
  `allow_writing_reviewer` (then `reviewer_override` is recorded). Optional
  `allowed_workspace_roots` bounds both new and resumed workspaces. Ancient
  3.8.0 records get a conservative legacy envelope; a record that advertises
  authority but cannot supply a valid one fails closed. P1-2 is not
  implemented, so no `prompt_bundle_hash` is recorded or claimed.
- P2-2 retry and health state machine: `deepseek_mcp/health.py` classifies
  provider/network failures, parses `Retry-After`, applies phase-specific
  attempts (builder 2, reviewer 2, revision 1, rescue 1) with capped
  exponential backoff, and hard-stops auth/no-balance/prepare/cancel/deadline/
  budget. A writing-phase retry is refused when a workspace snapshot shows
  changes outside allowed evidence. A locked `state_dir/health.json` tracks
  per-component consecutive failures, retry timestamps and quarantine;
  three consecutive failures quarantine a component and block new automatic
  phases until `deepseek_maintenance action=clear_health`. `action=health`
  and the status sections expose the board; completed runs stay readable.
- P2-3 cost reservation ledger: `CostLedger` reserves a conservative phase
  estimate at admission, checks `committed + reserved + estimate` against the
  job ceiling and the daily limit (including active reservations), settles the
  actual cost per reservation, reclaims expired leases from dead workers, and
  records `{limit, estimated, reserved, committed, available, cost_known}` in
  the run record and structured job status. Settlement is idempotent by
  reservation/operation id, so a repeated settle cannot double-count. Unknown
  committed or active reservation cost blocks a paid phase whenever a ceiling
  exists; there is no `allow_unknown_cost` bypass. Live scaling waves reserve
  and settle like every other paid phase. When a child actually emits priced
  usage, a whole-job guard (prior committed + live usage + remaining reserve)
  stops it at that event boundary and the run records `budget_exceeded`. The
  `sdk` and `auto` transports observe `session.event` usage synchronously,
  signal the SDK process group and abort the turn at the boundary while keeping
  the observed usage for settlement; a final-usage fallback covers a client
  that returns usage without a notification. Transports with no observable
  live usage stay reserve-at-admission / settle-at-exit.

### Changed

- `deepseek_maintenance` accepts `health` and `clear_health` actions (and an
  optional `component`), keeping the 20 MCP tool names and existing response
  prefixes. `deepseek_maintenance action=status` appends a health line.
- Daily preflight now counts active cost reservations, so concurrent jobs
  cannot both pass the same daily check.
- New config keys: `allow_writing_reviewer` (false),
  `allowed_workspace_roots` (empty), `authority_ttl_seconds` (604800),
  `health_gate` (true), `retry_backoff_base_seconds` (1.0),
  `retry_backoff_cap_seconds` (60.0), `cost_reservation_ttl_seconds` (900) and
  `allow_first_phase_overrun` (false), with `DSH_*`/`DEEPSEEK_*` environment
  equivalents.
- Admission is strict by default, including the first paid phase; the old
  first-phase overrun is opt-in and recorded as `admission_overrun`.
- Authoritative envelope deserialization is fail-closed (integral version,
  finite numeric budgets, ISO timestamps, real booleans, absolute workspace
  root) and an expired grant records `policy_drift` instead of silently
  refreshing before the check. `Retry-After` obeys the 60 s cap and jitter.
- Automatic-skill agents now execute only inside the recorded authority
  workspace: scratch dirs are created under
  `<workspace_root>/.deepseek/skill-build/...`, verified with the same
  containment check before every agent call, and refused if they cannot be
  created inside the grant.
- Settlement identity is decisive: a named operation that settles after its
  reservation was released (TTL reclaim/restart) gets its own deterministic
  committed row instead of borrowing another same-phase row, and stale-row
  cleanup is scoped to that operation.
- Every budget stop leaves the running checkpoint phase `interrupted`
  (builder live stop, reviewer admission/live stop, rescue, revision and
  live-wave), and already-quarantined rescue/revision entries emit structured
  health attention. `check_live()` fails closed on unknown active reservations.
- Test `tests/__init__.py` honours `TMPDIR` (then `TMP`/`TEMP`) and falls back
  to a verified writable `.tmp` under the repository/workspace, so a read-only
  reviewer with no usable system temp directory can run the suite.
- README now explains the three roles (builder, independent reviewer,
  client/coordinator) and how to route review phases to a different vendor
  through the shipped `DSH_BIN` hook; it does not claim a reviewer-only model
  setting that the tree does not have.

### Tests

- 796 tests pass, including `tests/test_p2_authority.py`,
  `tests/test_p2_health.py`, `tests/test_p2_cost.py` and
  `tests/test_p2_review_regressions.py` (49 fail-first regressions).

## 3.8.0 — durable work identity and run journal

### Added

- Durable work identity for `harness_deepseek`: canonical request fingerprints produce filesystem-backed work ids, an optional stable `idempotency_key`, and append-only attempt history under `<state_dir>/work/<work_id>/`.
- A durable run journal for each work item, per-attempt events, and an atomic current `result.json`: cursor-based `deepseek_job_status` replies when `events_since` is supplied include `next_seq`, `stage`, `attention`, one pending question and options, `next_action`, `terminal` and the durable result.
- Structured attention from one validated fenced `attention` JSON block; a pending question holds the work open for `needs_input`/`input_required` instead of recording a false terminal state.

### Changed

- `dedupe_active` (default `true`, `DSH_DEDUPE_ACTIVE`) now suppresses a duplicate spawn only while identical work is active. Terminal work is not collapsed into a prior result, and an explicit `idempotency_key` gives retries a stable work item with a new attempt after the prior attempt ends.
- MCP tasks with `task_completion="job"` now follow the detached job and journal and complete only at a terminal state for the current attempt; `task_completion="request"` restores completion when the handler returns.
- `deepseek_job_status` keeps its existing text response when `events_since` is omitted, and older job files without work ids remain readable. The P1 changes are filesystem-backed and local, with no new service or database.
- Test coverage: 679 tests pass, including P1 work-identity, journal, task-bridge and integration suites.

## 3.7.0 — automatic skill building and the CrewForeman rename

- Distribution name, console command and MCP server name are now `crewforeman-mcp` (brand: CrewForeman). The import module stays `deepseek_mcp` and the old `deepseek-harness-mcp`/`deepseek-mcp` console scripts remain aliases.
- Skills are now a file-backed registry with the five built-ins as seeds.
  `detect_skill` reports a confidence and returns unmatched below the
  threshold instead of forcing the nearest checklist.
- Unmatched runs are captured as privacy-safe token signatures with their
  outcome and review evidence; similar runs cluster (3 by default, 2 when a
  severe defect repeats).
- A Harness author drafts new skills and a separate fresh reviewer scores them;
  only >=8.5 with zero blocking defects is saved, as a candidate.
- Candidates are injected, promoted after non-worse runs, demoted/retired when
  they hurt, versioned for rollback, and near-duplicates merge into the
  existing skill. Repeated lessons refresh existing checklists, built-ins
  included.
- `auto_skills` = auto | propose | off; new `list_skills`/`read_skill` options
  and `approve_skill`/`reject_skill`/`retire_skill`/`rollback_skill` tools.
- Every automatic change is recorded in the run record, the trace and
  `skills/.capture/decisions.jsonl`.

## 3.6.0

### Added

- One-command offline install: a stdlib-only in-tree PEP 517 backend
  (`build_backend.py`) plus a `setup.py`/`setup.cfg` legacy fallback. Plain
  `pip install .` succeeds with `PIP_NO_INDEX=1` on the stock Python 3.9 venv,
  with no setuptools in the isolated build environment. The wheel installs the
  `deepseek-harness-mcp` and `deepseek-mcp` console scripts and the five skill
  checklists under `share/deepseek-harness-mcp/skills`.
- Structured observability: a per-run JSONL trace, a timeline JSON, a summary
  in the run record, and optional OTLP/HTTP or Langfuse-compatible export.
  Export is disabled by default and a failing endpoint never fails a run.
- Generic MCP client support: neutral tool and server-instruction wording,
  nine ready-made snippets (Claude Desktop/Claude Code, Cursor, VS Code,
  Windsurf/Cline, Codex CLI, Gemini CLI and a generic stdio block) and
  `docs/CLIENTS.md`.
- `scripts/smoke_mcp.py`, a client-independent stdio smoke test that performs
  `initialize`, `tools/list` and a real `tools/call`.
- `restart_client_app(app_name)`, a generic restart helper with a graceful
  `unsupported` answer per OS; `restart_claude_app` remains a legacy alias.
- Cross-platform layer: per-OS config, data and temp paths, POSIX process
  groups versus Windows `taskkill /T /F`, Chrome/Edge discovery, keep-awake,
  screenshot and restart commands, sandbox fallback, and owner-only
  permissions. The CI matrix runs Ubuntu, macOS and Windows on Python 3.9-3.12.
- Whole-job `JobBudget` (`job_max_seconds`, `job_max_cost_usd`) spanning the
  builder, live-scaling waves, reviewer, revisions, rescue and retries, with
  the distinct statuses `deadline_exceeded` and `budget_exceeded`.
- Live scaling: the first builder wave is scored against the brief, and a weak
  wave gets at most a configured number of extra waves with new approaches
  while the whole-job time and cost budget allow it. The decision, score,
  approaches and reason are recorded in the run.
- Review-aware final status (`ok`, `ok_unreviewed`, `ok_below_gate`,
  `ok_rescued`) while the old status fields remain for existing consumers.
- A configurable hard maximum for explicit `subagents`, validated before the
  first round, and `swarm_deepseek` limit validation before the first HTTP
  request.
- Optional retention cleanup with a dry run, and owner-only 0600/0700
  permissions for configuration, runs, jobs and state on POSIX.
- v3.6.0 integration suites for resume of a killed run (including `REVIEW.md`
  restoration), request cancellation, the progress-gated watchdog, mocked
  macOS/Linux/Windows platform logic and end-to-end live scaling.

### Changed

- The watchdog wall-clock cap is progress-gated and phase-scaled. Recent
  child-session or workspace-file activity extends it by
  `stall_cap_extension_seconds` up to the new hard ceiling
  `stall_cap_hard_max_seconds` (default 7200 s). A silent phase still stops at
  `stall_seconds`, and a non-positive phase multiplier now disables that
  phase's cap as documented instead of clamping it to 60 s.
- A run skeleton and checkpoint are written before the first DSH process.
  Resume reuses finished builder, review and revision phases; the accepted
  reviewer report text is stored in the checkpoint and restored through the
  normal secret redaction.
- `notifications/cancelled` reaches normal (non-task) `tools/call` Harness
  runs, not only task jobs.
- The `harness_deepseek` schema documents `sandbox`, `reviewer_sandbox`,
  `approval`, `transport` and `add_dirs`.
- Version constants, package metadata, README, installation docs and release
  tests are 3.6.0.

### Fixed

- Retention cleanup skips a missing, `None` or empty
  `log_dir`/`jobs_dir`/`state_dir` instead of treating it as the literal
  `None` directory or the current directory, and `secure_tree("")` is a no-op.
  A partial or empty config can no longer delete or chmod unrelated files;
  `docs/OPERATIONS.md` documents the skip rule, the real `errors` behaviour
  and the `SimpleNamespace` input contract.
- The `harness_deepseek` schema now documents `live_scaling_max_extra_waves`
  with its real default (2, not 1), and `tests/test_v360_docs.py` ties the
  advertised value to `config.DEFAULTS`.
- `scripts/clean_install.sh` removes an existing `check/` tree before copying,
  so it can be re-run in the same temp directory.
- Expand `~` in an explicit config file path (`--config`, `DEEPSEEK_MCP_CONFIG`, and background-job `config_path` specs) so those workers honor the user config instead of silently using defaults. Background job specs now persist the resolved absolute config path (while still redacting the arguments), so a worker with a different `HOME` loads the same file.
- The absolute watchdog cap no longer kills a builder, reviewer or revision
  that is demonstrably progressing (the v3.5.0 incident at 900.2/900.3 s).
- `job_max_cost_usd` is checked before the independent reviewer and before the
  finisher as well as before a revision, so an exceeded whole-job budget stops
  all further paid phases and reports `budget_exceeded`.
- The single transient builder retry is now gated by the same whole-job cost
  budget: when the first attempt already reached the ceiling (or its cost is
  unknown), the retry is skipped, the run records
  `builder_retry.status: "skipped"` and ends `budget_exceeded` instead of making
  one more paid DSH call.
- Live scaling now receives the `cost_known` flag. With `job_max_cost_usd` set
  and no priceable session log, an extra wave is refused ("cannot prove the
  remaining budget") instead of treating the unknown cost as `$0`; without a
  cost ceiling the existing wave behaviour is unchanged.
- `docs/OPERATIONS.md` now documents the shipped retention/budget API: defaults
  `retention_days=0`, `keep_runs=50`, `keep_jobs=50`, roots
  `log_dir`/`jobs_dir`/`state_dir`, `ValueError` for unsafe paths, the real
  `cost_usd`/`cost_known` snapshot keys, and "unknown cost stops further paid
  phases". `tests/test_v360_docs.py` asserts those values statically.
- Resuming a run whose accepted review leaves nothing to execute now finalizes
  the saved run instead of preserving `status: "running"`, and the accepted
  `REVIEW.md` is still restored (redacted like other run text). A resume
  also carries the wall-clock already spent and re-checks the whole-job
  cost/deadline bounds before
  accepting a durable review or starting another paid reviewer, so a kill in
  the gap between the reviewer checkpoint and the post-review gate cannot
  produce a false `ok`.
- A SIGKILL of a fake reviewer mid-phase is covered by an integration test that
  resumes from the durable checkpoint without re-running the finished builder.
- The sdist includes `bench/`, `PKG-INFO` and the `ROOT_CAUSE` document, and
  preserves the executable bit on `scripts/clean_install.sh`; the clean-install
  check copies `bench/` so the installed-copy suite matches the dev suite. It
  now runs the copy step from the venv and asserts the imported package lives
  under that venv, so the installed-copy proof cannot silently test the source
  tree.
- Checkpoint metadata and task argument summaries redact operator home paths
  and key material recursively.
- The reviewer sandbox no longer grants the operator `~/.dsh/sessions` when a
  private reviewer home outside the workspace is used.
- Tests written for the 3.5.0 surface were migrated to the 3.6.0 contract
  instead of being skipped or deleted.

## 3.5.0

### Added

- Permission presets (`read-only`, `workspace-write`, `danger`) resolved by
  `safety.resolve`, with reviewer read-only and builder workspace-write defaults
  and explicit-only danger opt-in. DSH receives the documented
  `DSH_PERMISSION_MODE` override.
- macOS Seatbelt wrapper (`safety.wrap_command`, `sandbox-exec`) generated from
  a per-run profile: writes allowed only in the workspace (write mode),
  `REVIEW.md`, `.browser-evidence`, the run TMPDIR, `~/.dsh` runtime dirs and a
  workspace-local reviewer home; the managed credential document is denied
  explicitly. The wrapper degrades to an unwrapped run with a recorded reason
  when unavailable or nested.
- Scrubbed child environment: only `DEEPSEEK_API_KEY` and the key env vars
  named by configured providers survive; config paths and `*_BASE_URL` values
  are removed.
- Documented `dsh --profile sdk` JSON-RPC transport (`transport.SdkClient`) with
  bounded deadlines, tolerant session-event parsing, usage normalization,
  `session/resume`, process-group cleanup, a forwarded cancel check and
  automatic fallback to headless (`dsh_transport: auto`). The SDK child is
  wrapped by the same sandbox policy as the headless path.
- MCP task augmentation: `tasks/list|get|result|cancel`,
  `notifications/tasks/status`, persisted task state with TTL, and
  `notifications/progress` frames carrying the negotiated `progressToken`.
  `deepseek_job_cancel` and `tasks/cancel` stop the job and the Harness process
  group.
- Durable per-phase checkpoints under `state_dir/checkpoints/<run-id>/`, atomic
  manifests with secrets/home paths redacted, and `resume_deepseek` /
  `harness_deepseek(resume_run_id=...)` to reuse finished builder and accepted
  review phases and to continue from a saved DSH session.
- Optional OpenAI-compatible `providers` config (env-var names only, no keys in
  files), priority-ordered `provider_fallback` over transient failures, and
  `consult_deepseek` for independent cross-model answers without a judge.
- Browser evidence (console errors, failed requests, DOM/snapshot check) under
  `.browser-evidence/` when Chrome is available, plus run-record fields for
  transport, sandbox wrapping, checkpoint and provider decisions.
- `config.example.json` and README now document the v3.5 keys, 15-tool surface,
  permission/SDK/checkpoint/provider/browser-evidence behaviour and the
  reviewer report flow.

### Changed

- The independent reviewer no longer needs to write `REVIEW.md`: a read-only
  DSH process returns the complete report and the connector persists it after
  the write guard, redacting key material and operator home paths. Reports
  written by an explicitly wider reviewer policy are still preserved.
- `harness_deepseek` can resume from a checkpoint instead of restarting, and
  records `os_sandbox.wrapped`/`reason`, `transport` and checkpoint status.
- Version constants, README title, package version and release tests are 3.5.0.

### Fixed

- Progress notifications were constructed with the two-argument `notify`
  function, so every emission raised and was silently suppressed; the emitter
  now receives a one-frame adapter and Harness text progress (not only mappings)
  reaches the client.
- `tasks/list` required a `taskId` before dispatch; it is now handled first and
  works without one.
- `_materialize_review` wrote the reviewer answer verbatim; `sk-...` keys and
  operator home paths (under `/Users` or `/home`) are now redacted before
  `REVIEW.md` is written.
- The SDK transport ignored `cancel_check` and the OS sandbox; it now wraps the
  child with `safety.wrap_command`, creates a workspace TMPDIR, polls the cancel
  check while waiting and maps cancellation to a terminal `cancelled` result.
- The README claimed 12 tools and described the reviewer as writing `REVIEW.md`
  and the run as having no OS-level sandbox; it now documents the 15 registered
  tools and the actual connector-written report/Seatbelt behaviour.

## 3.4.0

### Added

- Child-aware progress/watchdog: growth of this run's child session logs under
  the encoded workspace session directory counts as progress while a Harness
  process is silent. Child ids come from the run's own `subagent/catalog`
  events, so a concurrent run in the same workspace cannot refresh the watch.
  A configurable absolute cap (`stall_max_seconds`, default 900 s) stops a child
  that appends forever. The run records honest child-activity events, the last
  observed active count and whether the cap fired. A healthy multi-child wave
  is no longer killed by the parent-only 480 s stall (live evidence from the
  first wave of the v3.3.0 work).
- Workspace-local TMPDIR for every Harness process, created under the workspace
  and removed afterwards; HOME is unchanged because the Harness needs `~/.dsh`.
- Render-provider health probe: headless Chrome discovery from operator
  configuration (`DSH_CHROME_BIN`), PATH or standard system app paths, one
  bounded `--version` plus `about:blank --dump-dom` probe with a fresh
  workspace-local profile/HOME/TMPDIR, a cached result, and injection of the
  exact working command or a `render unavailable: <reason>` fallback. The probe
  never searches the model-writable workspace for an executable.
- Creative visual-impact contract: 2-3 genuinely distinct directions, a named
  focal point/palette/type/image device, a separate visual-impact score
  (~30% weight), a 7.5 cap for accurate-but-plain results, and recorded
  strongest/weakest visual elements. Revisions must change composition, type
  or colour rather than only copy.
- `child_progress`, `stall_max_seconds`, `render_probe`, `render_probe_timeout`,
  `chrome_bin` and `transient_retry` configuration, with matching environment
  variables.
- A bounded retry for a builder that fails fast, writes no output, and reports a
  transient network/rate-limit error; both attempts are counted in cost.

### Changed

- Sizing counts declared deliverables only (a Deliverables/Outputs section,
  explicit output file names, or numbered page/section requirements), not fact
  bullets or deliverable keywords. The live VIP-ad brief now records 3
  deliverables, not 10.
- History may match a strong smaller bucket from one comparable run; upward
  moves remain capped at +2 and require at least two runs in the larger bucket.
- Child-activity events, the watchdog cap, retry details, the render-probe
  result and the sizing explanation are stored in the run JSON and Markdown
  record.
- The MCP `initialize` instructions and `harness_deepseek` description/schema
  now advertise v3.4, the child-aware and absolute-capped watchdog,
  workspace TMPDIR, render probe, minimal browser environment and bounded
  retry. Version constants and README are 3.4.0.

### Fixed

- Healthy multi-child waves killed by a watchdog that observed only the parent
  process.
- A child or concurrent run that kept writing logs could previously refresh the
  watchdog forever; the per-run child-id filter and absolute 900 s cap bound it.
- Chrome/zygote grandchildren could outlive a stall or time-limit stop; the
  Harness now starts in its own session and the whole process group is
  signalled.
- Chrome `NSTemporaryDirectory`/ProcessSingleton failures caused by an
  unwritable default TMPDIR inside the sandbox.
- An empty `state_dir` wrote the render cache into the current working
  directory; it now disables the cache.
- Over-sized agent waves caused by counting facts and keyword hits as
  deliverables.
- A question with a numbered deliverable ("What is an API? 3 pages") was
  misclassified as simple and received no agents.
- Automatic revision escalation could exceed `default_subagents` or
  `subagent_soft_cap`; it is now capped by the smallest ceiling.
- Creative review that scored accuracy/readability but ignored visual impact,
  allowing an 8.8/10 ad that the user rejected as visually plain. The cap now
  requires a positive plainness assertion or defect bullet, so "not visually
  plain" no longer triggers a paid revision.
- Child-aware progress watched the wrong session directory for workspaces
  containing spaces, non-ASCII characters, `~`, separator runs or long paths.
  The encoder is now a single shared port of the installed Harness
  `projectKey` (UTF-16 `~XXXX` escapes, 251-character cap), so a growing child
  log is found instead of raising a false stall.
- `stall_max_seconds` was ignored when `stall_seconds` was `null`/`0` because
  the watch reported itself disabled and never started its monitor thread.
  The absolute cap now enables the watch on its own.
- The render probe gave up on the first discovered browser and the hint always
  said `--headless=new`, so a broken desktop Chrome could hide a working
  `chrome-headless-shell` or a successful legacy `--headless` run. The probe
  now tries up to four trusted candidates in a deadline-bounded loop, records
  the binary and headless flag that actually worked, and emits that exact flag.
- The README config-file example omitted `stall_max_seconds`, `child_progress`,
  `render_probe`, `render_probe_timeout`, `chrome_bin` and `transient_retry`;
  it now round-trips to the documented defaults and a release test enforces it.

### Security

- Retries are limited to one attempt and only for fast, output-free, transient
  failures; never on stalls, time limits, auth or quota/balance. The render
  probe installs nothing, uses a workspace-local profile and a hard timeout,
  runs the browser with a minimal environment that carries no connector API
  keys, never executes a workspace-supplied binary implicitly, and redacts the
  home path from prompts. Watchdog stops signal only this Harness process group
  and never the connector's own group.

## 3.3.0

### Fixed

- The first `workflow` wave no longer relies on a child exiting on its own.
  The v3.3.0 first-wave evidence: 5/8 candidates, 0 PNG, 8 `agent-start`, 0
  `agent-end`, a parent-only 480 s stall, and two children still holding
  Chrome jobs when the process tree was killed. The builder task now requires
  a hard shell timeout for every headless Chrome/browser/server/network
  command (`timeout 60`, `gtimeout 60`, or a Perl alarm on stock macOS), a
  fresh workspace-local `--user-data-dir`/`HOME`/`TMPDIR`, leftover-process
  cleanup, at most two Chrome attempts, and a `render unavailable: <reason>`
  fallback instead of a retry loop. Builders must write their candidate file
  first and return within 6 minutes / 12 tool calls; a wave must return within
  7 minutes and render only the chosen candidate.
- The rescue finisher is now strict: at most 12 tool calls and 8 minutes, no
  redesign, no new browser profiles, no new audits, one existing candidate or
  the most complete one, at most three minimal fixes, time-boxed commands,
  `test -s`/size verification, and an immediate final answer. If no candidate
  exists it writes the simplest correct deliverable directly. This fixes the
  observed 74-step / 83-call / 600 s finisher that wrote `flyer.png` at
  22:37:54 but was killed before returning, so the run stayed `error`.
- The poster and web skills now carry the same headless-Chrome rules, and the
  quality-loop render step is time-boxed with a fresh profile.
- The MCP `initialize` instructions now report v3.3, and the
  `harness_deepseek` description and `subagents` schema describe the dynamic
  0/4/6/8/12 sizing instead of the removed "16+ / rounded up / soft-capped at
  32" contract.
- The execution-safety block and the 6-minute / 12-tool-call early-write
  budget also apply to 0- and 1-agent builder tasks, not only multi-agent
  waves.
- Short definitional technical questions ("What is an API?", "What is HTML?",
  "What is a CSV file?") now classify as simple/0 agents. History no longer
  raises a start when the best bucket's mean utility misses the 8.5 target,
  `history_stats` documents its exact-count buckets, the quality-off finisher
  step numbering is contiguous (1-7), and `FINISHER_MAX_SECONDS` is 480 s to
  match the task text's 8-minute cap.

### Added

- Dynamic agent selection when the caller does not pass `subagents`: the task
  is classified as simple (0 agents), code (start 4), creative/design (start
  6-12 by size) or research/data (start 6-12 by size), with a size estimate
  from prompt length, bullets and deliverable count. `default_subagents` is
  now a ceiling for automatic starts; explicit `subagents` still wins exactly.
- History learning from past run records: per task type and starting count,
  mean official/self score, cost, duration and stall/rescue/error rates are
  combined into a stall-penalised utility. The start is nudged by at most 2
  agents and the reason is stored as `agent_choice_reason` (also
  `agent_history` and `agent_choice`).
- Escalation on a failed independent review: the next revision may use more
  agents or a different approach, recorded in `agent_escalations` and the
  revision entry. A first round at >= 8.5 with zero blocking defects still
  stops early. Every proposed wave is capped at the detected concurrency.
- Recurring reviewer findings: when the same defect kind appears in at least
  two runs of a task type, one sanitised, deduplicated lesson is appended to
  that type's skill file and recorded in `lessons_added`.
- New unit tests for the root-cause evidence, execution-safety task text, the
  lean finisher, classification/size/history/escalation, recurring lessons and
  the fake-Harness integration paths.

### Changed

- Version 3.3.0 in the package, MCP `serverInfo`, README and changelog.
- `default_subagents` remains 16 in `config.example.json`, but it is now the
  ceiling for automatic classification starts rather than the starting count;
  `0` still disables automatic subagents.

## 3.2.0

- Stall watchdog: `stall_seconds` (default 480, `null` or `0` disables) stops a
  Harness process that emits no progress event for that long (terminate, 2 s
  grace, then kill) and records `stalled: true`, the stalled step and elapsed
  silence in the run. The existing `max_seconds` timer and the watchdog share
  one first-writer-wins stop reason, so they cannot double-kill or mislabel a
  run.
- Rescue instead of discard: when a builder stops by stall or time limit and
  left non-empty output files, a short single-agent finisher run (0 subagents,
  same workspace, its own `min(600, remaining)`-style limit) uses the existing
  candidates to produce and check the final deliverables. A successful rescue
  continues into the normal independent review and ends as `ok_rescued`, or
  `ok_unreviewed` if review then fails, with a `[WARNING]` line in the result
  text. Finisher failure or no usable files keeps `error`; `no_balance` still
  wins and never rescues.
- One `workflow` call per wave: the builder task and quality-loop instructions
  now require a separate `workflow`/`parallel()` call for each wave, saving
  `wave-<n>-results.md` before the next wave, and continuing with already
  finished waves if one hangs instead of putting every wave in one call.
- `log_dir` fallback fixed and reported: paths are whitespace-stripped and
  `~`-expanded at the point of use, the write probe is no longer a hidden
  dotfile (which Google Drive File Provider can reject), and a genuine
  permission/OS error is kept instead of being swallowed. A fallback is logged,
  stored as `log_fallback_reason` in the run JSON/Markdown, and shown as a
  `[WARNING]` in the result text.
- `stall_seconds` added to `config.example.json` and config loading
  (`DSH_STALL_SECONDS` / `DEEPSEEK_STALL_SECONDS`); version bumped to 3.2.0.
- New unit tests for stall detection (fake clock and fake process), rescue and
  its no-file/no-balance/finisher-failure paths, per-wave task text, `log_dir`
  with spaces and an unwritable primary, and release/config metadata.

## 3.1.1

- Task text states the concurrency limit first: waves of at most C, never more than C agents in one parallel() block (an oversized parallel() hung Harness).
- Independent review/revision failure no longer discards the builder's work: status `ok_unreviewed` with a clear warning.
- Empty DeepSeek balance (Insufficient Balance / QUOTA / HTTP 402) gives a clear top-up message and status `no_balance`; no review rounds are started.
- `config.example.json` with a $2/day spend alert; `bench/live_smoke.py`.

## [Unreleased]

_No unreleased changes yet._

## [3.1.0] - 2026-10-31

### Added

- Automatic Harness concurrency detection. Reads `maxActiveSubagents` (default
  8) from the installed `dsh-subagent` docs and `maxConcurrentAgents` from the
  PTC engine docs, takes the smaller value, and caches it in
  `<state_dir>/harness-concurrency.json` with a timestamp and app version.
  Re-detects after 7 days, on app-version change, or after an agent-limit
  error. Adds `max_concurrent_children` and `state_dir` settings, plus an
  optional cheap workflow probe and fallback 8.
- Independent verification owned by the connector. After the builder run, a
  separate fresh single-agent Harness process reviews the original brief, skill
  checklist and deliverable files, writes `REVIEW.md`, and returns the official
  `quality_score`; the builder's result is kept as `self_score`. Blocking
  defects or a score below 8.5 trigger a revision run and another review,
  recorded per round with score, defects, time and cost. New
  `independent_review` option (default true for real work).
- Append-only daily spend ledger at `<state_dir>/spend-ledger.jsonl`, written
  on every run save. `spend_today` reads only today's rows and deduplicates by
  run id, with a rebuild from run records when the ledger is missing.
- Pipelined learning waves with sub-leader summaries and next-wave seeding in
  the Harness task text, including explicit overlap guidance for the detected
  concurrency.
- Unit tests for concurrency detection/cache/probe, independent review and
  revision stop conditions, lesson dedupe/cap/injection, and the spend ledger.

### Changed

- `default_subagents` is now 16 (was 12) and `subagent_soft_cap` is 32 (was
  24). Automatic counts are rounded up to full waves of the detected
  concurrency; explicit caller counts remain exact.
- The quality loop now uses `quality_rounds` for independent review/revision
  rounds (default 3) and runs a builder self-check first.
- Lessons are appended only for real signal: fixed independent-review defects,
  failed runs or user ratings. Near-identical lessons are deduplicated, each
  skill file keeps at most 30 active lessons, older overflow is condensed into
  at most 10 consolidated bullets, and only the 8 most relevant/recent lessons
  are injected into a task.
- Daily spend now comes from the ledger instead of scanning every run record.

### Fixed

- The Harness task no longer hard-codes "8 children"; wave planning and task
  text use the detected concurrency.
- A reviewer failure is surfaced as a run error instead of being silently
  replaced by the builder's self-score.
- `quality_rounds=0` remains clamped to one review round.

## [3.0.0] - 2026-10-31

### Added

- Package layout: `deepseek_mcp/` with `server.py`, `api.py`, `store.py`,
  `jobs.py`, `harness.py`, `quality.py`, `skills.py`, `spend.py`, `mac.py` and
  `config.py`, plus the thin `deepseek_mcp.py` entry point and
  `python -m deepseek_mcp` support.
- Portable configuration through `~/.config/deepseek-harness-mcp/config.json`
  (override with `DEEPSEEK_MCP_CONFIG`) and environment variables for every
  setting. Precedence is defaults, then config file, then environment, then
  explicit tool arguments.
- Adaptive subagents for Harness: no subagents for simple questions, 12 or
  more for real work, and further scaling with the workload up to a configurable
  soft cap (default 24). Explicit `subagents` values still win. The chosen count,
  reason and workload signals are saved in the run and written into the task.
- Mandatory quality loop for real work: the lead restates the brief as a
  checklist, generates independent candidates, runs automatic checks, and uses a
  separate reviewer that inspects rendered output with `read_image`. The
  reviewer scores quality from 0 to 10 against a target of 8.5 or better, with
  configurable revision rounds and a final `QUALITY_REPORT.md`.
- The quality loop instructs automatic checks for common deliverables,
  including headless Chrome rendering, overflow and overlap detection, minimum
  print font sizes, QR-code decoding, build, tests and lint for code, and
  completeness checks for documents.
- Skill library in `skills/` for poster, document, web, code and data work. The
  task type is detected automatically and the matching checklist is injected
  into the Harness task. A distilled lesson is appended after every Harness run,
  and `rate_deepseek_run` adds explicit user feedback to the matching file.
- New tools: `rate_deepseek_run`, `list_skills` and `read_skill`.
- Harness session-log cost parsing and accounting: finds recent session logs, decompresses
  them with the `zstd` binary when available, and sums uncached input,
  cache-read and output tokens per assistant message. Both `deepseek-flash` and
  `deepseek-v4-pro` are priced. Unknown or unreadable cost is reported as
  unknown instead of being guessed.
- Daily alerts and estimated-spend tracking with optional `daily_alert_usd` and
  `daily_limit_usd`. Alerts are appended to tool results, and a hard limit
  blocks new paid calls only when explicitly configured.
- `bench/run_benchmark.py` with three fixed tasks (poster, letter and CLI) and
  Markdown result output, plus a dry-run mode that starts no server and makes no
  paid calls.
- `tests/` using the standard-library `unittest` runner, a fake DeepSeek HTTP
  server and a fake `dsh` CLI. GitHub Actions runs the suite on macOS and Ubuntu
  with Python 3.9 and 3.12.
- Public-release documentation: `README.md`, this `CHANGELOG.md`
  and the project `LICENSE`.

### Changed

- Backward compatibility with v2.4 tools: every existing tool name is preserved
  (`ask_deepseek`, `swarm_deepseek`, `harness_deepseek`, `deepseek_job_status`,
  `list_deepseek_runs`, `read_deepseek_run`, `keep_mac_awake`, `mac_screenshot`
  and `restart_claude_app`), so existing MCP launch configurations keep working.
- Legacy v2 environment variable names are still accepted alongside the new
  `DSH_*` and `DEEPSEEK_MCP_*` names, so existing configurations do not need to
  be edited before upgrading.
- `swarm_deepseek` is marked **experimental** in its tool description while
  remaining fully functional. `harness_deepseek` is the recommended path for
  real deliverables.
- The default run directory is the generic `~/DeepSeek-MCP/runs`; the previous
  Google-Drive-specific default has been removed.
- Run records include skill, subagent and quality fields where applicable.
- Detached jobs and background workers load the same configuration file as the
  server.

### Fixed

- Harness runs no longer lose token and cost data when the CLI does not print
  usage itself; session logs are parsed after the run.
- Duplicate usage entries in session logs are deduplicated by message ID or
  sequence number, preventing double billing in estimates.
- Configuration, jobs, logs and skills are no longer read from import-time
  globals, so environment and config-file changes are picked up by each call.
- Harness stderr is capped and included in the saved run for debugging.
- A missing final event is reported as a run error while the run record is still
  saved, so failures remain inspectable.
- Unknown Harness models and missing `zstd` produce an explicit unknown-cost
  result instead of a misleading zero; unknown direct-API models now show
  `n/a` instead of `$0.000000`.
- Built-in `~/...` paths are expanded, and detached workers load the exact
  config file selected by the parent server.
- A failed Harness launch or a silent child exceeding `max_seconds` still saves
  an error run; a stale `QUALITY_REPORT.md` is moved aside before a new run.
- Final scores with a nearby target/threshold are parsed correctly, and passed
  checklist items are no longer mistaken for defects.
- Concurrent run saves and job status writes use unique temporary files; a
  malformed job spec or invalid run id cannot escape the jobs directory.

### Security

- API keys are read only from the environment at call time and are never
  returned, logged, or written to run records, job specifications or skill
  lessons.
- Skill lessons, run records, job specs and job status results are redacted
  before storage: email addresses, bearer tokens, key-like secrets and personal
  home-directory paths are removed or replaced.
- `keep_mac_awake` verifies that a PID belongs to `caffeinate` before stopping
  it, so a stale PID file cannot kill an unrelated process.
- The server keeps all run, job and workspace data in configured folders and
  does not read or modify Claude app configuration.
- Tool descriptions warn against sending confidential or personal data.
- Run identifiers are validated before file lookup, and run files are written
  atomically.

## [2.4.0]

- Previous release: single agent, adaptive swarm with judge, background jobs,
  run history, the Harness tool, keep-awake, and macOS helper tools.
