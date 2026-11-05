# Changelog

All notable changes to CrewForeman are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
