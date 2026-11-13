# Operations: configuration, safety, budgets, traces and retention (v3.14.3)

This page is the detailed reference behind the short README. Everything here
is implemented by `deepseek_mcp/` and exercised by the test suite with fakes;
no live paid API or real Harness process is needed.

## Builder practices (3.9.5)

Every builder and revision prompt carries a fixed `builder-practices` block
(v1.0, at most 350 words) assembled by `deepseek_mcp/builder_practices.py`.
Implementation work receives the full **code** variant; documentation and
research work receives a lighter variant. The block is independent of skill
detection and applies to every build. The text is versioned, so a change in
the practices is visible in run history, and the report below compares scores
by that version.

It is on by default. Set the boolean `builder_practices` config key to `false`,
or export `DSH_BUILDER_PRACTICES=false`, to turn injection off. The run record
stores the active set and version under `builder_practices`, with the
variant, word count, character count and an estimated token cost, alongside
`first_review_score` and `review_scores`, so runs with and without the block
can be compared.

Measure first-review against final score, grouped by practice-set version:

```bash
python3 scripts/report_scores.py --runs ~/DeepSeek-MCP/runs
```

The table reports the first review and final score per practice-set version.
Add `--json` for machine-readable output. Older runs without the dedicated
fields are grouped from `review_rounds`, so existing history still appears.

## Configuration and paths

The server reads one optional JSON config file. Precedence is: explicit
`--config PATH`, then `DEEPSEEK_MCP_CONFIG`, then the per-OS default below.
Environment variables use the `DSH_*` / `DEEPSEEK_*` names documented in
`config.example.json` and override file values.

| OS | Config file | Runs / jobs / state default |
| --- | --- | --- |
| macOS | `~/.config/deepseek-harness-mcp/config.json` | `~/DeepSeek-MCP/{runs,jobs,state}` |
| Linux | `$XDG_CONFIG_HOME/deepseek-harness-mcp/config.json` (default `~/.config/...`) | `~/DeepSeek-MCP/{runs,jobs,state}` |
| Windows | `%APPDATA%\deepseek-harness-mcp\config.json` | `%LOCALAPPDATA%\DeepSeek-MCP` |

On Linux, runs, jobs and state intentionally use the historical
`~/DeepSeek-MCP` location rather than `$XDG_DATA_HOME` / `$XDG_STATE_HOME`, so
existing run histories keep working; every path stays overridable through the
environment variables in `config.example.json`.

### One root for all mutable state: `state_root` (P3.4a)

`CREWFOREMAN_STATE_DIR` (environment) and `state_root` (JSON config key)
relocate the four connector-owned mutable roots under one directory. They are
useful for per-test isolation, read-only sandboxes, and side-by-side instances.

When a root is set, these defaults are derived from it:

| Config key | Derived value | Built-in default when unset |
| --- | --- | --- |
| `state_dir` | `<root>/state` | `~/DeepSeek-MCP/state` |
| `log_dir` | `<root>/runs` | `~/DeepSeek-MCP/runs` |
| `jobs_dir` | `<root>/jobs` | `~/DeepSeek-MCP/jobs` |
| `pid_file` | `<root>/keep_awake.pid` | `~/DeepSeek-MCP/keep_awake.pid` |

`dsh_sessions_dir` is deliberately **not** derived. Harness session logs live
under dsh's own home (`~/.dsh/sessions` by default), which dsh keys off
`DSH_HOME`; the connector does not pass `DSH_HOME` to builder/worker children.
Deriving the read path from CrewForeman's root would therefore make token and
cost accounting and child-activity progress silently scan an empty directory.
It stays `~/.dsh/sessions` unless the config file sets it or
`DSH_SESSIONS_DIR` / `DEEPSEEK_DSH_SESSIONS_DIR` is exported. Set one of those
explicitly only if your dsh also runs with a matching `DSH_HOME`; otherwise
leave it alone.

`skills_dir` and `harness_workspace` are also **not** derived; set them
explicitly if they must move too. A leading `~` in the root is expanded. An
empty or unset value means "use the historical defaults", which are
byte-identical to v3.9.2.

Precedence, lowest to highest:

1. built-in defaults (the table's right column);
2. paths derived from a non-empty state root;
3. explicit values in the config file — `state_root` relocates only keys the
   file did not set itself, so `{"state_root": "/srv/cf", "log_dir":
   "/var/log/cf"}` keeps that one custom `log_dir`;
4. explicit environment aliases — `DSH_STATE_DIR` /
   `DEEPSEEK_MCP_STATE_DIR` (state only), `DEEPSEEK_MCP_LOG_DIR`,
   `DEEPSEEK_MCP_JOBS_DIR`, `DEEPSEEK_MCP_PID_FILE`, `DSH_SESSIONS_DIR` /
   `DEEPSEEK_DSH_SESSIONS_DIR`. These always win over derived paths and file
   values.

`CREWFOREMAN_STATE_DIR` is an environment variable, so it beats the config
file's `state_root` for the derived paths. If both `DSH_STATE_DIR` and
`CREWFOREMAN_STATE_DIR` are set, `DSH_STATE_DIR` wins for `state_dir` only;
the other three roots still come from `CREWFOREMAN_STATE_DIR`.

```bash
# Environment for one process tree (no file edit):
CREWFOREMAN_STATE_DIR=/srv/crewforeman python deepseek_mcp.py

# Equivalent config file entry:
# {"state_root": "/srv/crewforeman"}
```

Paths are expanded and stripped exactly like the existing path keys
(`log_dir`, `jobs_dir`, ...), and Python 3.9+ with the standard library only
is still required.

`DSH_BIN` overrides Harness discovery. When it is unset the server checks the
per-OS application/runtime locations, then `PATH`. `DSH_SKILLS_DIR` points at
the skill checklists when they are not next to the package.

### Work identity, deduplication and the run journal (v3.8.0)

`harness_deepseek` now has a durable work item separate from the run id. The
work id is a SHA-256 fingerprint of the canonical request (prompt, workspace
and file paths, sandbox/reviewer policy, quality and budget options, skill);
`wait_seconds`, `title` and connector-internal fields are excluded. State lives
under `<state_dir>/work/<work_id>/` (`work.json`, `attempts.jsonl`, a
`events.jsonl` journal, `result.json` and an O_EXCL `.lock`).

* `dedupe_active` (default `true`, `DSH_DEDUPE_ACTIVE`) returns an existing
  active run for an identical fingerprint instead of buying a second pipeline.
  Set it to `false` to allow identical concurrent runs; an explicit
  `idempotency_key` always selects its own work item and can safely retry even
  after the first run finished.
* `task_completion` (default `"job"`, `DSH_TASK_COMPLETION`) keeps an MCP task
  `working` until its detached job reaches a terminal journal state. Set it to
  `"request"` for the old complete-on-handler-return behaviour during a client
  migration window.
* `deepseek_job_status` accepts `events_since`; with a cursor it returns the
  new events plus `next_seq`, `stage`, `attention`, one `question`, `options`,
  `next_action`, `terminal` and the durable `result`. Without it, the response
  is the unchanged text. A pending answerable question is accepted only from
  one fenced ```` ```attention ```` JSON block (`question`, `options`, optional
  `evidence`). Review and failure follow-ups are deterministic status
  projections, never questions inferred from prose.

Journal files rotate at 1 MB (one previous segment is retained), so a client
cursor older than the retained window receives the retained events rather than
an error. Terminal work is never collapsed into a cached result: a later
identical request starts a new attempt under the same work id, including one
selected by an explicit idempotency key, and the attempt history stays auditable.

### Admission control (P3-1)

`harness_deepseek` and `resume_deepseek` jobs pass through
`deepseek_mcp/admission.py`. `ask_deepseek`, `consult_deepseek` and
`swarm_deepseek` keep their pre-P3 direct-start behaviour, so the Harness cap
never queues unrelated tools. The operator cap is `max_harness_jobs` (default
**4**, `DSH_MAX_HARNESS_JOBS`); `0` means unlimited. A positive value is clamped to the Harness child capacity detected
by `concurrency.detect`, so `max_harness_jobs=64` cannot exceed the installed
Harness limit. Raising the cap above the detected limit only produces a run
record note; the effective value is the lower one.

Queue and lease state lives under `<state_dir>/queue/`:

* `items/<run_id>.json` is the durable queue record (`priority`, `created`,
  `spec_path`, `fencing_token`, `status`).
* `leases/<run_id>.json` is the live lease (`pid`, `host`, `heartbeat`,
  `lease_until`, `fencing_token`). A worker refreshes it from
  `jobs.write_status` (throttled); the dispatcher reclaims a record when its
  pid is dead or its lease expired after three missed heartbeat intervals.
  Reclaiming bumps the fencing token, so a late heartbeat or an old worker
  cannot re-take the slot.
* Per-work lock files fail closed: if the lock cannot be acquired, admission
  raises `AdmissionError` and the job is marked failed; the connector never
  falls back to a direct spawn, so a broken queue cannot start an unbounded
  job. A queue-wide `queue.lock` wraps the capacity check plus claim in one
  interprocess critical section, so two server processes (or two controller
  instances) admitting different run ids cannot both take the last slot. The
  queue assumes a local filesystem; network-share lock semantics are not
  guaranteed. Queue/lock failures are recorded in the job status and the P1
  work item as a sanitized error.

A P0 record is created for `resume_deepseek` (or a call carrying
`resume_run_id`); every new build is P1. With `reserve_resume_slot` on (the
default), a queued P0 keeps one slot free: a P1 may not take the last free
slot while a resume waits. P0 may use the reserved slot. A **running** job is
never suspended or cancelled to make room. Set `reserve_resume_slot=false` for
a single-operator install that prefers plain priority/FIFO.

`serve()` starts one lazy dispatcher thread; it starts the oldest eligible
record, re-checks after every claim, and exits when the queue is empty. If the
server exits, queued records stay on disk and the next server start drains
them; already-running jobs remain detached and continue. `deepseek_job_status`
adds `queue_position` (1-based among pending records, 0 once running),
`priority`, `priority_label`, `holder` (worker pid) and `lease_until`.

### Queue dependencies, urgent builds and the safety net (P3.1)

`harness_deepseek` has two optional additive parameters:

* `urgent` (boolean) orders a queued build after every P0 resume record and
  ahead of ordinary queued builds. It is a flag, not a new numeric priority:
  old queue records keep `priority: 1` / `P1 new build` and are unchanged.
  Running jobs are never suspended to make room.
* `depends_on` (run id string) makes the job wait until that run reaches a
  successful terminal state. While waiting the record stays `queued`, has
  `waiting_on` set, holds no lease and therefore no running slot. The
  dependency record is expedited ahead of ordinary queued builds so the
  dependent job cannot starve it. A `status: done` record with
  `progress: needs_input` is still waiting, not successful; this holds even
  when the queue record itself was already released as `done`, because the
  `needs_input` hold takes precedence over terminal success. The prompt text is
  never inspected for run ids; only the explicit parameter creates a
  dependency.
* Equal effective order is FIFO: a new urgent submission cannot self-claim
  ahead of an older queued urgent or dependency record of the same tier.
  `reserve_resume_slot=false` keeps only its documented exception for an
  ordinary non-urgent P1 build taking the last slot from a waiting P0; urgent
  and dependency jobs never use that bypass.

Dependency validation happens before a worker starts. The queue record is
consulted first, then `<jobs_dir>/<run_id>.status.json`, then the saved run
record `<log_dir>/<run_id>.json` when the job status file was already cleaned:

* an unknown run id is rejected with `Unknown dependency run_id ...`;
* a dependency whose status is `error`/`failed`/`cancelled` is rejected with
  `Dependency run_id ... finished with error: ...`;
* a dependency cycle (including a self-dependency) is rejected with the chain,
  e.g. `Dependency cycle detected: a -> b -> a`.

If a dependency fails while a dependent job is already waiting, the dependent
is failed with a dependency error instead of waiting forever. The dependency
that is queued at the admission cap is admitted first when a slot frees; the
dependent never takes a slot while it is still waiting. A dependency that
already finished successfully lets the job start normally.

`deepseek_job_status` (plain and `events_since` structured/status file) adds
`urgent`, `depends_on`, `waiting_on`, `dependency_status`
(`ready`/`waiting`/`unknown`/`error`) and the urgent-aware `priority_label`
(`P0 resume`, `P1 urgent`, `P1 new build`). Old run records without these
fields read as ordinary non-urgent builds with `waiting_on: null`.

The dispatcher also runs a cheap safety net on its normal tick. It performs
only local file reads and no model call. When the queue is non-empty, every
occupied slot reports idle/waiting (job status `queued`/`waiting`/`idle`/
`needs_input`/terminal, or a progress word such as `waiting`), and the active
count is at or above the effective capacity (`max_harness_jobs` clamped to the
detected Harness child limit), it admits exactly the next eligible queued job. Every such admission appends an `admission_safety_net` event to
`<state_dir>/queue/events.jsonl` (and mirrors it into the P1 journal when the
run has a work id). The safety net may exceed the soft capacity only up to the
fixed hard ceiling `2 * effective capacity`; the ceiling is checked under the
queue-wide lock before the claim, so repeated ticks cannot run away. It never
claims dependency-waiting records. If a status file is missing or unreadable,
the slot is treated as busy (fail closed). Queued records survive a restart;
`serve()` restarts the dispatcher and the next tick re-evaluates dependencies
and the safety net.

### Review convergence and cost controls (P3-2, R1-R3)

`quality_convergence` is `observe` (default) or `auto`
(`DSH_QUALITY_CONVERGENCE`). Review rounds are parsed into defect signatures
(SHA-256 of normalized kind + file anchor + evidence type) and persisted with
`first_seen_round`. If the same blocking signature repeats in **K=2**
consecutive rounds, `auto` stops before buying another revision and emits one
attention question with options `resume_with_scope`, `accept_below_gate` and
`stop`; `observe` records `quality_convergence.action=observe_stop` and keeps
the normal loop so the operator can see the would-stop decision first. A
question is emitted at most once per work item. The stop is durable: the work
item is held and `result.json` is written as `needs_input` before any terminal
event, and `_finalize_work()` folds the same question into the terminal save,
so the MCP task reports `input_required` with the question/options instead of
completing. When every blocking finding is cosmetic and the score is within
tolerance of the 8.5 gate, the loop stops after the first revision instead of
polishing rounds indefinitely.

A stop is not a dead end: `resume_deepseek run_id=<id> option="resume_with_scope"
answer="<narrower scope>"` reopens exactly one revision at the stopped review
round. The scope note is passed to that revision prompt, the two historical
signature rounds do not stop that manual cycle before the revision runs, and
the following review applies normal convergence again. `option="accept_below_gate"`
accepts the current result below the gate, and `option="stop"` ends the work;
both resolve the pending question, record `attention_resolution`, write a
terminal `ok_below_gate` result and finish the work item so the MCP task
completes instead of staying `input_required`.

The connector reduces reviewer cost without lowering the gate:

* **R1 (signed test evidence):** before each review the connector runs the full
  suite on the exact reviewed tree (auto-detects `tests/` with `test_*.py` at
  the workspace root or in a direct child project such as `repo/`, preferring
  `repo`; or use `review_test_command`; `review_test_suite=""` disables it).
  The signed payload records the detected `project_root` and the command runs
  with that root as its cwd. The reviewer
  receives an HMAC-signed result with tree SHA-256, test/failure/error counts,
  output SHA-256 and a bounded output tail. It trusts that evidence for the
  named tree, reruns only new or defect-related tests, and may rerun everything
  when suspicious. A tree hash that changes during the run invalidates the
  evidence and is flagged. The HMAC key lives only in
  `<state_dir>/review-evidence/keys/connector.key` (0600); the evidence names
  `key_id=connector`, and `review_context.verify_evidence()` reloads that key
  to verify the exact signed payload (tampering fails).
* **R2 (diff first):** a bounded baseline is captured before the builder. The
  reviewer starts from the unified diff and surrounding context instead of
  reading the whole repository; the prompt says so explicitly.
* **R3 (targeted re-review):** round 1 is a full review. The baseline is
  recaptured immediately before each revision writes, so the next round
  receives exactly that revision's new diff (not an empty diff and not the
  cumulative diff since the original builder) plus the previously flagged
  blocking defects and only the changed files' immediate context.

The job wrapper also consults the durable run record after the handler
returns. A requested review that is still in flight, was expected but never
recorded, or belongs to a run whose terminal status is `error` closes the job
as `error`, never `done/finished`. A superseded attempt applies the same gate
to the canonical run. A recorded reviewer failure that produced
`ok_unreviewed` is a successful job and is unchanged.

`review_context_max_chars` bounds the diff text (default 60000). Within that
budget, deliverable files named by the connector and core source files are
shown before generated patches/logs/docs, and each file body is capped
(8000 chars) so one artifact cannot consume the context. The prompt is
evidence-aware: a clean signed suite says do not rerun the whole suite; a
skipped suite says run only the relevant tests and never claim a pass.

`review_test_timeout_seconds` (default 300) is the connector test hard timeout.
`model_prices` (default `{}`) is the generic per-model table of USD per
million tokens used for builders and as the fallback for reviewers:
`{"kimi-k3.1-pro": {"input": 0.0, "cache_read": 0.0, "output": 0.0}}`.
`reviewer_prices` (default `{}`) is an optional reviewer-only override with the
same shape; it is never applied to a builder. Reviewer cost resolves in the
order `reviewer_prices` -> `model_prices` -> the built-in DeepSeek
`HARNESS_PRICES`. Partial or unknown rows keep cost unknown and are never
zero-filled. With the empty defaults the reviewer's input/cached/output tokens
are still recorded in each review round and in `reviewer_usage`. Both transports
use the same tables: headless session logs resolve each observed model, and the
SDK transport prices its reviewer usage through them.

### Incremental review checkpoints and resumable reviews (P3.2)

While the independent reviewer runs, the connector writes two workspace files:
`REVIEW.partial.json` (schema v1) and a human-readable `REVIEW.partial.md`.
They are rewritten atomically by a heartbeat (default every 5 seconds) and
record the review round, elapsed time, coarse areas (brief, deliverables,
correctness, tests, safety) with `pending`/`in_progress`/`checked` state, any
findings with evidence, and connector test reruns. Neither file is a delivered
artifact; both are excluded from `deliverable_files` and the reviewer write
guard allows the connector to keep them current.

If the reviewer is interrupted (credit/quota error, crash, timeout, restart,
stream drop), the partial is left as `interrupted`; it is never deleted or
promoted to `completed`. `resume_deepseek run_id=<id>` finds the newest partial
from the run record, the checkpoint, or `REVIEW.partial.json` (matching the
run/attempt identity and round), gives it to the new reviewer, and asks that
reviewer to check only the areas still marked `pending`/`in_progress` while
re-verifying the carried findings. The new report and the carried findings are
merged into one `REVIEW.md`; a carried blocking defect still counts toward the
acceptance gate, so a high new score cannot silently drop it.

A paused reviewer-vendor question is durable too. `resume_deepseek` with
`option` or `answer` re-enters the review, runs the chosen reviewer and records
the same-vendor label when the user chose one; the attention block is recovered
from checkpoint metadata or phase data when the run JSON is a synthetic or
early skeleton.

### Reviewer pre-flight (P3.2)

Before each paid review round, `review_preflight` (default `true`,
`DSH_REVIEW_PREFLIGHT`; `review_preflight_timeout` default 45 s) sends a tiny
`READY` prompt through the same reviewer path, reviewer policy and private
`DSH_HOME` wrapper. It does not use tools and costs a few tokens. If the
reviewer cannot answer because the account has no credit/quota, the key is
rejected, or the endpoint is unreachable, the paid review is **not started**.
The run records `reviewer_preflight` and a vendor-correct `review_error`, for
example: Kimi/Moonshot quota -> top up at `platform.moonshot.cn` (`.ai`
outside mainland China); DeepSeek balance -> `platform.deepseek.com`. Set
`review_preflight=false` (or `DSH_REVIEW_PREFLIGHT=0`) only when the operator
has another way to know the reviewer is healthy.

Reviewer token usage is written to each `review_rounds` entry
(`tokens_in`, `cache_read_tokens`, `tokens_out`, `total_tokens`, `usage`,
`models`) and aggregated in `reviewer_usage`. The run `.json` keeps the
structured fields, the run `.md` shows one usage line per round, the job result
text shows the same, and `deepseek_job_status run_id=<id> events_since=0`
returns `reviewer_usage` plus a compact `review_rounds` view. Cost is included
when `reviewer_prices` or `model_prices` resolves every observed model, with the
built-in DeepSeek `HARNESS_PRICES` as the final fallback; otherwise the record
says `cost unknown (no configured model price)`.

### Review score re-prompt and `score_missing`

A completed review with no parseable 0-10 score now gets exactly one re-prompt
in the same reviewer session, carrying the report text. If no session id exists,
or the same-session call fails because the session is gone or errored, the
connector falls back once to a short fresh call that derives the score the
report's findings support (it is not told to answer `unknown`). The attempt is
a paid reviewer-phase call: the round records `score_reprompt` (method, status,
seconds, tokens in/cache/out/total, cost and the parsed score/blocking), emits
a `score_reprompt` journal event, and adds its usage to `reviewer_executions`
and `reviewer_usage`, so token and cost totals stay honest. The parsed two
lines are appended to the connector-owned `REVIEW.md` and the augmented report
is reparsed, so the creative visual-impact cap and the report's blocking count
still apply. A reply that omits `Blocking defects: N` keeps the report-derived
count instead of a fabricated zero.

Score labels only discard a parenthetical qualifier when it explicitly names
the overall score (`(overall)`, `(out of 10)`). Category qualifiers such as
`## Score (test coverage)` or `| Quality (code quality) | 9/10 |` are unrelated
metrics and leave the review unscored, so they trigger the same re-prompt
instead of satisfying the acceptance gate.

`score_missing` marks a round/run whose review still has no score. It is not a
blocking defect: the reviewer's real defects and real blocking count are kept
unchanged. The gate never accepts a null score, and a score-missing round with
no real blocking defects skips the revision and moves to the next review round.
If no round remains, the run ends as `ok_below_gate` with `quality_score` null
and `review_passed` false.

### Detached client restart status (P3.2)

`restart_client_app` writes a small JSON status file before returning
(`restart_status_path`, default `<state_dir>/restart-status.json`) and starts a
detached helper in a new session. The helper owns the close/wait/reopen loop:
it closes the app, waits `wait_seconds`, opens it, verifies it is running, and
retries `restart_helper_attempts` times (default 3) with
`restart_helper_retry_seconds` (default 2). Because it is detached, it survives
the client exit that the restart itself may trigger. Read the status file after
the app comes back: `state` is `scheduled`, `waiting`, `reopened` or `failed`,
with `attempts`, `attempts_detail`, timestamps, `helper_pid`, and `error`.
`restart_client_app` returns the status path; no secret is written there.

On macOS the helper runs `osascript -e 'application "Claude" is running'`
(authoritative for the app bundle) and the corroborating process check
`["pgrep", "-a", "-x", "Claude"]` in the same attempt, and records both in the
same JSON (`process_check_returncode`, `process_check_output`,
`process_check_ok`, `process_check_ancestry_known`, `verified_by`).
`verified_by` is `osascript`, or `both` when pgrep also corroborates; a failed
or false `osascript` keeps the attempt `failed` even if pgrep sees a process.
`-a` aids diagnosis only: `pgrep` excludes its own process and all of its **ancestors**, so
plain `pgrep -x Claude` can report nothing while Claude is running when the
connector itself was started from inside Claude's process tree. Ancestor PIDs
are excluded from the corroboration so an old Claude process cannot by itself
prove that a reopened app is running. If the ancestor walk fails (for example
`/bin/ps` is denied in the Harness sandbox), `process_check_ok` is false and
`process_check_ancestry_known` is false; pgrep alone never confirms a reopen.

The baseline copy is bounded (256 KiB per file, 400 files); larger or binary
files are recorded in the manifest with an `excluded` reason and listed as
changed/added/deleted with a metadata-only body.

## Gates, plans, failure notes and pipelining (3.11.0)

These four features are connector-wide. All keys are in `config.example.json`
and in `DEFAULTS`; every key has a `DSH_*` and a `DEEPSEEK_*` environment
alias. Missing keys are not inferred from the environment by the feature
modules.

### F1 deterministic gate

The gate runs on the exact post-builder tree, before the first paid reviewer
call, only when an independent review is requested. Order is fixed:
secrets -> lint -> tests. A failed secret scan skips the tests (never execute a
tree with a leaked credential); lint and tests still produce evidence when it
is safe. Evidence is signed and bounded; `output_tail` is capped and redacted.

| Key | Default | Meaning |
| --- | --- | --- |
| `gate_enabled` | `True` | Master switch; false records a skip. |
| `gate_tests` | `auto` | `auto`, `off`, or a command string/list. |
| `gate_test_command` | `""` | Explicit command; a missing executable fails the gate. |
| `gate_lint` | `auto` | `auto`, `off`, or a command string/list. |
| `gate_lint_command` | `""` | Explicit command; a missing executable fails the gate. |
| `gate_secret_scan` | `True` | Built-in scan; no external command replaces it. |
| `gate_secret_patterns` | `[]` | Additional regexes. They add findings, never suppress them. |
| `gate_secret_scan_budget_seconds` | `120.0` | Per-scan wall clock; expiry is reported as `timed_out`, not raised. `0` means an immediate `timed_out`, not unbounded. |
| `gate_timeout_seconds` | `300.0` | Total gate wall clock. |
| `gate_max_revisions` | `2` | Gate-funded revisions before `gate_failed`. |
| `gate_max_output_chars` | `4000` | Per-check output tail cap. |

The scan skips tool and vendor trees at any depth (`.uv-python`, `.venv`,
`venv`, `node_modules`, `.tox`, `site-packages`, `.eggs`, `.nox`, `.pixi`) in
both the changed-file path and a whole-tree scan. When a whole-tree scan is
needed (a fresh or synthetic run with no baseline snapshot and no explicit
output paths), it enumerates
tracked plus untracked-not-ignored files with `git ls-files` under a hard
subprocess timeout and falls back to a bounded walk on any error. An expired budget reports `timed_out` and
`truncated` and never hangs the gate; `scan_tree_report` exposes those flags, while the
backward-compatible `scan_tree` returns findings only.

A failure starts a gate revision with the same cost, health and checkpoint path
as a review revision, but no reviewer process. After the cap the run ends
`gate_failed` (`review_status="skipped"`, `quality_score=None`). A gate
exception fails closed with `error` / `stop_reason="gate_error"`. When no tests
and no lint are detected the verdict is `skipped` and the reviewer proceeds.

### F2 written predictions

The builder is told to write `PLAN.json` before the first edit. After the
build the connector compares declared files/tests/risks and `write_set` with the
actual diff.

| Key | Default | Meaning |
| --- | --- | --- |
| `plan_contract` | `warn` | `warn`, `block`, `off`; unknown values fall back to warn. |
| `plan_file` | `PLAN.json` | Workspace-relative path; absolute/`..` falls back to the default. |
| `plan_max_files` | `200` | Cap on declared files plus tests. |
| `plan_max_risks` | `20` | Cap on normalised risks. |

`warn` records mismatches and adds a `plan:` note to the result header without
changing score or blocking. `block` adds one blocking defect after the reviewer
parse, so a 10/10 review must still revise; a fixed plan clears it. `PLAN.json`
is not a deliverable and is excluded from review context.

### F3 failure notes

After a completed review round with blocking defects, the connector maps each
defect through the fixed `quality.defect_kind` buckets and records it per task
type. The next builder or revision prompt of that type may receive at most one
short "common failures" section.

| Key | Default | Meaning |
| --- | --- | --- |
| `failure_notes` | `on` | `on`/`off`; off disables reads, writes and file creation. |
| `failure_notes_max` | `3` | Maximum labels in one note. |
| `failure_notes_half_life_days` | `30.0` | Decay half-life; minimum 0.1. |
| `failure_notes_min_occurrences` | `2` | Minimum lifetime count before injection. |
| `failure_notes_max_store` | `200` | Global cap; highest weight is kept. |
| `failure_notes_store` | `""` | Override path; empty means `<state_dir>/failure_notes.json`. |

Only bucket labels and opaque run ids are stored. A non-bucket label is
rejected if it looks like a path, email, URL or long prose; a passing label is
stored as `sig:<hash>` and never rendered. A corrupt store is quarantined and
the run continues; notes never change score, retries or pass/fail.

### F4 pipelined jobs

A job is pipeline-eligible only when it declares a non-empty `write_set` (tool
argument) or a `slice` resolved from `SLICES.md`, `pipeline_enabled` is true,
isolation is `worktree`, the canonical workspace is a git work tree (git >=
2.5) and its dirty paths are only previously installed patches or recorded
deliverables. Non-declaring jobs keep the serial behaviour.

| Key | Default | Meaning |
| --- | --- | --- |
| `pipeline_enabled` | `True` | Admission switch: false stops new eligible/extra starts; already isolated jobs still finish and install. |
| `pipeline_max_extra_jobs` | `1` | Extra jobs beyond capacity. |
| `pipeline_require_disjoint` | `True` | Enforce write-set disjointness; unknown overlaps everything. |
| `pipeline_isolation` | `worktree` | Only `worktree` is implemented; anything else disables the feature. |
| `pipeline_worktree_root` | `""` | Empty means `<state_dir>/worktrees`. |
| `pipeline_patch_dir` | `""` | Empty means `<state_dir>/patches`. |
| `pipeline_install` | `auto` | `auto`, `manual` (keep the patch), `off` (skip). |
| `pipeline_min_remaining_usd` | `1.0` | Daily-budget headroom floor for an extra start. |
| `pipeline_max_pending_patches` | `8` | Backlog cap before extras are refused. |
| `pipeline_queue_scan` | `head` | `head` (FIFO, predictable) or `first_fit`. |
| `pipeline_patch_retention_days` | `3` | Days to keep a worktree/patch after manual or conflict. |

While a base job is in its review phase and capacity is full, one extra job may
start if its write-set does not overlap any active job. Each job has its own
worktree, patch and reviewer. Accepted patches are installed FIFO only after
`git apply --check` succeeds for the whole set in a throwaway worktree; a
conflict applies nothing and sets `stop_reason="install_conflict"`. A manual
patch at the FIFO head is a drain point: later patches stay pending. Set
`pipeline_install="manual"` to stop canonical installs while inspecting
patches; set `pipeline_enabled=false` to stop new starts.

## Loop guard (3.14.2)

Builder and finisher phases feed raw Harness events and the workspace digest
to a deterministic detector (`deepseek_mcp/loop_guard.py`). It counts four
signals: repeated failing command plus error, edit ping-pong on the same
file/hunk, tool calls with no workspace change, and repeated test-failure
signatures. Thresholds are `loop_guard_repeat_command`,
`loop_guard_edit_pingpong`, `loop_guard_nochange_calls` and
`loop_guard_same_test_failure`; `loop_guard_max_actions` caps the ladder and
`loop_guard_tokens_per_call` sets the estimate basis.

`loop_guard_enabled` defaults to true with conservative thresholds. A nudge
writes `LOOP_GUARD_NUDGE.md`, emits a progress line, stops the looping attempt
and restarts the builder once with the nudge and the repeated evidence in its
task when budget allows (a headless child has no live message channel). A
re-plan runs after a second loop: it starts the existing rescue finisher with a
re-plan instruction that names the evidence. If that finisher also loops, its
nudge or re-plan collapses to a hard stop that skips the review and records
`status="stopped"`. Escalation requires another threshold of new evidence, so
poll timing alone cannot stop a run. With `loop_guard_confirm=true`, one
bounded `deepseek-flash` call may confirm any scheduled nudge, re-plan or
stop; a veto keeps the child running and is recorded as `vetoed`, and a
missing key or an error keeps the deterministic decision. The run record gets
`loop_guard` and
`metrics.loops_caught` / `metrics.loop_guard_tokens_saved_estimate`, and
`WASTE_REPORT.md` gets a short `## Loop guard (v3.14.1)` block. Savings are an
estimate, never a billing claim.

## Safety and sandboxing

Builders default to `workspace-write`; the independent reviewer defaults to
`read-only` and cannot widen its own policy. `danger-full-access` is accepted
only as an explicit argument, never from a config default. Permission is
passed to Harness through the documented `DSH_PERMISSION_MODE` deployment
override; the headless profile does not accept `--permission-mode`,
`--approval-policy` or `--add-dir`, so `add_dirs` is recorded as a limitation
instead of being passed as a flag.

On macOS the connector wraps the child in a generated Seatbelt profile
(`sandbox-exec`) when a probe proves it is usable. The profile denies writes
by default and allows only the workspace (write mode), `REVIEW.md`,
`.browser-evidence`, a workspace-local `TMPDIR` and the run's private DSH
home. The read-only reviewer gets no write grant for the operator's `~/.dsh`
at all; its own home lives under the state directory, outside the
builder-writable workspace. Credential documents are denied after every grant.
This is a file-write boundary, not a network fence or a process fence.

### OS sandbox modes and config keys (3.14.1)

`os_sandbox` is `auto` (default), `off` or `require` (`DSH_OS_SANDBOX`; legacy
`true`/`false` normalizes to `auto`/`off`). `auto` uses the best available
boundary and records `degraded=true` plus an `UNSANDBOXED` header line when none
is usable. `require` refuses to start a builder with
`stop_reason="sandbox_unavailable"`; it never falls back to a prompt-only
boundary. `off` records `enforcement=none` and does not claim a boundary.

| Key | Default | Values / meaning | Env alias |
| --- | --- | --- | --- |
| `os_sandbox` | `auto` | `auto`, `off`, `require` | `DSH_OS_SANDBOX` |
| `linux_sandbox` | `auto` | `auto`, `landlock`, `bwrap`, `off` | `DEEPSEEK_MCP_LINUX_SANDBOX` |
| `linux_min_abi` | `3` | Below this, no `enforced` claim; ABI 1-2 is degraded. | `DEEPSEEK_MCP_LINUX_MIN_ABI` |
| `windows_sandbox` | `auto` | `auto`, `restricted`, `appcontainer`, `off`; AppContainer is not provisioned in 3.14.1 and fails closed as unavailable | `DEEPSEEK_MCP_WINDOWS_SANDBOX` |
| `sandbox_extra_writes` | `[]` | Absolute roots; canonicalized, deduped, at most 32. | `DEEPSEEK_MCP_SANDBOX_EXTRA_WRITES` (JSON) |
| `sandbox_probe_ttl` | `300.0` | Probe cache seconds; `0` re-probes. | `DEEPSEEK_MCP_SANDBOX_PROBE_TTL` |
| `sandbox_canary` | `auto` | `auto` or `off`; off is recorded. | `DEEPSEEK_MCP_SANDBOX_CANARY` |

The run record carries `os_sandbox` (`schema`, `kind`, `enforcement`, `abi`,
`probe`, `canary`, `tested`, `degraded`, `blocked`, `reason`, `temp_roots`,
`profile_sha256`, `requested`, `platform`, `writes_outside`, `delete_outside`,
`network`) and a one-line `sandbox_header`, for example
`boundary=landlock(abi=8) enforced; writes=deny-outside; delete=deny-outside;
network=unrestricted`. `writes_outside` and `delete_outside` are suite claims
(`denied`, `unverified`, `not_claimed`), never guesses. `enforcement=enforced`
requires both `probe.ok` and `canary.ok`; `require` blocks when either fails.

### Sandbox record and defaults (3.14.3)

A builder with sandbox `workspace-write` gets the OS boundary whenever the
platform can provide one, including when `os_sandbox` is unset (`auto`).
`danger-full-access` is never derived from a default: only an explicit
per-job request is honoured, and the record then reads
`boundary=none(explicit danger-full-access opt-in); writes=unbounded;
delete=unbounded; network=unrestricted` instead of the ambiguous
`boundary=none(policy_opt_out)`.

`boundary=none(policy_opt_out)` now means exactly one thing: the operator set
`os_sandbox: "off"` (or `DSH_OS_SANDBOX=off`/`false`), or the platform key
(`linux_sandbox` / `windows_sandbox`) to `"off"`. In that mode
`deepseek_job_status` prepends one line:

```
WARNING: OS sandbox disabled by config os_sandbox=off; file writes are unbounded
```

and the structured status carries the same text as `sandbox_warning`. The
recommended setting is `"os_sandbox": "auto"` (or `"require"` to fail closed);
a per-job `sandbox: "danger-full-access"` stays an explicit, recorded opt-in.

On macOS the wrapper is `sandbox-exec` with the generated Seatbelt profile. On
Linux the launcher applies a Landlock ruleset to itself and then `exec`s `dsh`
(ABI 3+ handles truncate; reads and network stay unrestricted). On Windows the
launcher creates a write-restricted token, grants an ACE for the restricting SID
on the workspace and temp roots, and starts `dsh` in a kill-on-close Job Object;
network and UNC writes are not blocked, and delete/rename outside is reported as
`delete_outside=unverified` unless a Windows boundary run proves it. The first
real `windows-2025` run (42629362611, 3.13.2) failed in the sandbox probe before
the suite or boundary ran (`AttributeError`, `logon_sid=None`, `admin=False`,
`usable=false`). 3.13.3 fixed the wrong-DLL bindings; run 42648360100 then
proved the probe (`usable=true`, `write_restricted=true` on py3.9 and py3.15)
but the suite was red and `admin_member` could not be measured (`WinError
1309`). 3.13.9 was green in run 42697229851; 3.14.3 needs its own green Windows run as
the required proof of the suite and the boundary. The runner account is an Administrators member,
but that is not the same as an elevated process token; the probe records
`admin` (token elevation) and `admin_member` (account group) separately. This
is a file-write boundary, not a container, VM, network firewall or malware
sandbox.

The child environment is scrubbed: only `DEEPSEEK_API_KEY` and the key
variables named by configured providers survive, and keys are never written to
run records or checkpoints. No API key is stored in a run record, checkpoint or
trace.

### Restart per OS (3.14.1)

Restart is best-effort and OS-specific; the status file records `state`,
`verified_by`, `error` and the resolved opener. macOS closes/reopens with
`osascript`/`open -a` and verifies with `osascript` (pgrep corroborates only;
Automation denial `-1743` is classified). Windows uses `taskkill`/`start` and
verifies with `tasklist`/`QueryFullProcessImageNameW`; `DETACHED_PROCESS` with
`CREATE_BREAKAWAY_FROM_JOB` is best-effort. Linux uses exact `pkill -x`/
`pgrep -x` plus `/proc/<pid>/exe`, tries the beta client's varying desktop names
(`claude`, `claude-desktop`, `claude-app`) and falls back to
`gtk-launch`/`xdg-open`; use the explicit command override when resolution
fails. The restart helper is deliberately unsandboxed and is recorded as such.

### Provider key lookup

A provider's `api_key_env` name is resolved in this order: the process
environment first, then the Harness credentials file
(`~/.dsh/.credentials.yaml` by default; override with `credentials_file`,
`CREWFOREMAN_CREDENTIALS_FILE` or `DSH_CREDENTIALS_FILE`). The reader accepts
the wrappers' flat `NAME: value` form and the nested `records` / `refs`
layouts. A credentials file readable by group or others is skipped with a
warning, so keep it owner-only (`chmod 600`). The resolved value is injected
only into the child `dsh` environment under that variable name; overlays, run
records, logs, events, checkpoints and errors carry the name only. Example
provider entry (variable names only, no value):

```json
{"name": "anthropic", "base_url": "https://api.anthropic.com",
 "api": "anthropic-messages", "api_key_env": "ANTHROPIC_API_KEY",
 "models": ["claude-opus-4-1"]}
```


## Release safety: the builder never pushes, tags or publishes

**The builder agent must never run `git push`, `git tag`, `gh release`, a
package publish, or any other network side effect.** A build/review job runs
with no network side effects: it produces a commit and an independent review,
and then stops. Release is a separate, human-approved step.

Run the build/review job first. Confirm the run record reports
`review_status=accepted` and `review_passed=true`, then run a **separate
post-acceptance step** from the checkout. That step must push exactly the
reviewed commit, never "whatever HEAD happens to be":

```sh
set -eu
REVIEWED_SHA="${REVIEWED_SHA:?set to the exact commit the reviewer accepted}"
RUN_RECORD="${RUN_RECORD:?set to the run record, e.g. ~/DeepSeek-MCP/runs/<run_id>.json}"
test -f "$RUN_RECORD"
test "$(git rev-parse HEAD)" = "$REVIEWED_SHA"   # HEAD unchanged since review
test -z "$(git status --porcelain)"              # clean working tree
python3 - "$RUN_RECORD" <<'CHECK'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
status = report.get("review_status") or report.get("status")
assert status == "accepted", status
assert report.get("review_passed") is True, report.get("review_passed")
CHECK
git push origin "$REVIEWED_SHA:refs/heads/main"
# Tag/publish only the exact reviewed commit, never a moving branch:
git tag -a "vX.Y.Z" "$REVIEWED_SHA" -m "Reviewed release vX.Y.Z"
git push origin "refs/tags/vX.Y.Z"
```

Before the push, check that HEAD is the commit that was reviewed, the working
tree is clean, and the review report path exists with
`review_status=accepted` and `review_passed=true`. If any check fails, stop.
The reviewer must be independent; a builder self-report is not acceptance.

A same-job guard is **not implemented**. DSH runs connector commands in a
child sandbox that has no reliable git or network interception point: the
sandbox is a file-write boundary, not a network fence, and wrapping child
commands would be bypassable by another child process. The guard therefore
remains an operator/release-process responsibility. The optional future flag
`forbid_network_side_effects_until_accepted` (default `false`, not implemented
in 3.9.x) is reserved for a future release that can enforce this at the
sandbox or proxy layer.

## Authority envelope (P2-1)

Every `harness_deepseek` call opens one **direct** authority envelope. The run
record and checkpoint metadata carry it: `basis` (`direct|continuation`),
`origin`, `policy_digest`, `sandbox`, `reviewer_policy`,
`reviewer_override`, `workspace_root`, `job_max_seconds`,
`job_max_cost_usd`, `max_agents`, `created` and `expires`. P1-2 (prompt bundle
hashing) is not implemented, so the envelope deliberately has **no**
`prompt_bundle_hash` field.

`resume_deepseek`, rescue, live-scaling, reviewer, revision and auto-skill
phases are continuations. A continuation can only narrow sandbox, reviewer,
budget, agent count or workspace root; widening raises a typed
`WideningError`. `danger-full-access` is never inherited (it downgrades to
`workspace-write`), including by rescue, live-scaling, revision, reviewer and
auto-skill. A resume revalidates the recorded envelope against the current
config, writes `policy_drift` into the run record and uses the stricter
intersection. Deserialization is fail-closed: a non-integral/future version,
non-finite or non-positive budget, malformed timestamp, non-boolean flag,
negative agent count, relative workspace root or danger reviewer invalidates
the whole envelope. The recorded expiry is checked before any lease refresh, so
an expired grant produces an `expired` drift entry. A pre-authority 3.8.0
record gets a conservative legacy envelope (builder sandbox capped by current
config, reviewer read-only, budgets and agent count clamped). A record that
advertises `authority_version` but has a missing, corrupt or newer envelope
fails closed.

Auto-skill agents are read-only continuations and execute only inside the
recorded `workspace_root`: the connector creates scratch space at
`<workspace_root>/.deepseek/skill-build/<cluster>/<stamp>` and verifies every
actual execution workspace with the same containment check before invoking an
agent. If that directory cannot be created inside the root, auto-skill fails
closed instead of executing in a scratch path outside the grant.

The reviewer is read-only by default. A caller-supplied `reviewer_sandbox`
other than `read-only` is refused. If an operator genuinely needs a writing
reviewer, set `allow_writing_reviewer=true`; the run then records
`reviewer_override` and the writing reviewer gets the same workspace-diff
retry guard as a builder. `allowed_workspace_roots` (a list, empty by default)
bounds a new run's workspace and a resume's recorded root; requests outside it
are refused before any DSH process starts.

## Retry and component health (P2-2)

`deepseek_mcp/health.py` classifies failures as `rate_limit`, `network`,
`provider_5xx`, `auth`, `no_balance`, `budget`, `deadline`, `stall`, `prepare`,
`reviewer`, `cancelled` or `unknown`, and parses `Retry-After` seconds or HTTP
dates. Retry attempts are phase-specific: builder 2, reviewer 2, revision 1,
rescue 1. Backoff is exponential with jitter, capped at
`retry_backoff_cap_seconds` (60); a provider `Retry-After` is clamped to the
same cap and receives the same jitter. Auth, no balance, prepare, cancel,
deadline and budget never retry. A retry is also refused when the whole-job deadline or
cost ceiling is crossed, or when a workspace snapshot shows that the previous
writing attempt changed files outside `REVIEW.md`/evidence. A failed reviewer
is retried on its own; the paid builder is never restarted for it.

Health lives in `<state_dir>/health.json`, written under an O_EXCL lock with an
atomic replace. Each component records consecutive failures, last class,
retry-after, last success and quarantine. `error` appears only at three
consecutive failures; any success resets the entry. A quarantined component
blocks new automatic phases and emits a structured `progress` journal event
(`kind=attention`, `attention=health_quarantine`); completed run records remain
readable. Inspect with `deepseek_maintenance action=health` (or the status
section) and clear with `action=clear_health` (`component` optional). A corrupt
`health.json` is treated as quarantined until cleared; `clear_health` resets a
corrupt board to a clean state. `health_gate=false`
disables the gate but keeps recording.

## Cost reservations (P2-3)

`CostLedger` (in `deepseek_mcp/budget.py`) reserves a conservative estimate
before each paid phase and settles the actual cost from session logs after it.
Admission checks `committed + reserved + estimate` against
`job_max_cost_usd` and, when set, against `daily_limit_usd` including active
reservations. A daily preflight also counts reservations, so two concurrent
jobs cannot jointly pass the same limit. Unknown cost fails closed whenever a
ceiling exists; there is no `allow_unknown_cost` option. Each run record and
the structured `deepseek_job_status` payload carry
`{limit, estimated, reserved, committed, available, cost_known}`. Reservations
left by a dead worker are reclaimed after `cost_reservation_ttl_seconds`
(default 900).

Estimates come from the local price table, not from billing. Admission is
strict by default: `committed + reserved + estimate` must fit the job ceiling
for the first and every later paid phase. `allow_first_phase_overrun` is an
opt-in compatibility switch (default `false`); when enabled it records
`admission_overrun` and still reserves the oversized estimate. Live mid-phase
enforcement projects `prior committed spend + live usage + the remaining
reserve`, so a retry, reviewer, rescue, revision or live-scaling wave is judged
against the whole job, not just its own estimate. When the child emits a priced
usage event over that projection, the process is stopped at the event boundary,
the checkpoint is marked interrupted, and the run records `budget_exceeded`.
This also applies to the `sdk` and `auto` transports: `SdkClient` consults a
synchronous observer on every `session.event` usage notification, signals the
SDK process group and aborts the turn at that boundary, preserving the observed
usage for settlement. If an SDK client returns only final usage without a
notification, the aggregate is checked once after completion. Transports with
no observable live usage fall back to reserve-at-admission and settle-at-exit. Settlement is idempotent by reservation/operation id: a repeated
settle returns the existing committed row instead of adding a second one, and a
post-TTL settle uses one deterministic fallback row.

## Watchdog and budgets

Two timers protect a Harness process:

Each Harness process runs in its own process group so a stop can terminate
the whole child tree without signalling the MCP server itself.

* `stall_seconds` (default `480`) stops a process that emits no progress for
  that long. Child-session growth and changed workspace files count as
  progress, so a silent `workflow` barrier or a long browser/render call is not
  mistaken for a stall.
* `stall_max_seconds` (default `1800`) is a wall-clock cap for one process. It
  is **progress-gated**: when the cap is reached but progress arrived within
  `stall_cap_grace_seconds` (default 120), the deadline is extended by
  `stall_cap_extension_seconds` (default 300) instead of killing the work.
  Extensions stop at the hard ceiling `stall_cap_hard_max_seconds` (default
  `7200`): at that point the cap fires even if progress is recent, so a phase
  can never run past it. Set the hard ceiling to `0` to disable only the
  ceiling. The cap is scaled per phase by `cap_reviewer_multiplier` (1.5),
  `cap_revision_multiplier` (1.25) and `cap_finisher_multiplier` (0.5). Set a
  multiplier to 0 to disable the cap for that phase; `stall_seconds=0`
  disables only the silence timer.

The unconditional bound is the whole-job budget, which spans builder,
live-scaling waves, reviewer, revisions, rescue and retries:

| Key | Default | Meaning |
| --- | --- | --- |
| `job_max_seconds` | `7200` | Wall-clock deadline for the whole call. `0`/`null` disables it. |
| `job_max_cost_usd` | unset | Estimated-cost ceiling for the whole call. Unknown cost cannot be proven under budget, so further phases stop and the run records it. |
| `daily_limit_usd` | unset | Daily ceiling; admission adds active reservations to settled spend. |
| `cost_reservation_ttl_seconds` | `900` | Lease after which a dead worker's reservation is reclaimed. |
| `allow_first_phase_overrun` | `false` | Opt-in 3.8.0 compatibility: when `true`, a fresh run's first paid phase may start above an unusually small ceiling; the overrun is reserved and recorded as `admission_overrun`. The default is strict admission for every phase. |
| `retry_backoff_base_seconds` / `retry_backoff_cap_seconds` | `1.0` / `60.0` | Exponential retry base and hard cap. |
| `health_gate` | `true` | Block new automatic phases for a quarantined component. |
| `allow_writing_reviewer` | `false` | Operator-only escape hatch; records `reviewer_override`. |
| `backup_dsh_bin` | `""` | Optional alternate `dsh` executable or wrapper for a backup reviewer from another vendor. Empty keeps the single-reviewer behaviour. |
| `backup_reviewer_vendor` | `""` | Optional vendor label used in the backup banner and records (for example `kimi`). |
| `backup_reviewer_model` | `""` | Optional model label used in the backup banner and records. |
| `allowed_workspace_roots` | `[]` | Optional allow-list for run/resume workspace roots. |
| `authority_ttl_seconds` | `604800` | Envelope expiry; a resume reports expiry drift and refreshes it. |

A rescue finisher is deliberately short (at most 8 minutes and 12 tool calls)
and only runs when the builder left usable output files. It is not a way to
extend a job past its deadline.

Transient stream drops (`flush on a closed handle`, `STREAM_CLOSED`,
`ECONNRESET`) are classified as network failures. Builder and reviewer calls
retry them with exponential backoff (`retry_backoff_base_seconds` /
`retry_backoff_cap_seconds`) up to the phase attempt cap; a reviewer that is
still dropping after its bounded retries is a backup-reviewer switch class.
The reviewer pre-flight retries a transient 5xx/stream failure once before the
backup reviewer is considered; auth and credit failures switch immediately.

A review that was actually cut off by the whole-job deadline (its `review-N` phase is `failed`, `interrupted` or `running`, or `REVIEW.partial.json` exists) gets **one fresh review window** on resume. A review that never started behind an already-expired deadline still refuses to start. The retry and the resumed reviewer draw from the new window, not the old whole-job clock.

## Transport

`dsh_transport` selects `headless` (the stable default; `--profile headless
--json`), `sdk` (`--profile sdk` JSON-RPC) or `auto` (SDK first, headless
fallback with the reason recorded on the run). The SDK child is wrapped by the
same permission and Seatbelt policy, receives the scrubbed environment and
honours cancellation.

## Browser and rendering

Before a visual run the connector probes headless Chrome/Edge once, with a
bounded command. Each browser command uses a hard timeout (`timeout 60`,
`gtimeout 60`, or a Perl alarm on stock macOS), a fresh workspace-local
`--user-data-dir`/`HOME`/`TMPDIR`, and at most two Chrome attempts. When no
browser works the task text says `render unavailable: <reason>` and the
builder ships source plus the limitation instead of retrying forever.
Screenshot capture uses `screencapture` on macOS, an installed screenshot
tool on Linux and PowerShell on Windows; otherwise it returns `unsupported`.

## Observability

`trace` (default on) writes one JSONL file per Harness run under
`<state_dir>/traces/<run-id>.jsonl` plus a timeline JSON. Each event has a UTC
timestamp, run id, phase, event name and safe fields; strings are redacted.
The run record contains a compact observability summary and the same timeline.

Optional export is off unless configured:

* `otlp_endpoint` — OTLP/HTTP JSON collector (`http://host:4318` or a full
  `/v1/traces` URL).
* `langfuse_host` — Langfuse host; credentials are read only from
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`.

Export is best effort and never fails a run. A broken endpoint is recorded in
the trace summary and the local files remain.

## Platform matrix

| Concern | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Process group | `start_new_session` + `killpg` | same | `CREATE_NEW_PROCESS_GROUP`, `taskkill /T /F` |
| DSH discovery | application runtime, Homebrew, `PATH` | `/usr/local/bin`, `~/.local/bin`, `PATH` | `%LOCALAPPDATA%` programs, `PATH` |
| Chrome/Edge | `/Applications/...`, `PATH` | `/usr/bin`, `/snap/bin`, `PATH` | `%PROGRAMFILES%`, `%LOCALAPPDATA%`, `PATH` |
| Temp | `TMPDIR` | `TMPDIR` or `/tmp` | `%TEMP%` |
| Permissions | `0600`/`0700` | `0600`/`0700` | inherited ACLs (no POSIX mode) |
| Keep awake | `caffeinate` | `systemd-inhibit` | `SetThreadExecutionState` |
| OS sandbox | Seatbelt | Landlock (ABI 3+) or bwrap | write-restricted token |
| Restart helper | `osascript` verify (authoritative) + executed `pgrep -a -x <App>` process check + `open -a` | exact `pkill -x`/`pgrep -x` + command/`xdg-open` | `taskkill` + `start` |

Every helper returns a clear `unsupported: ...` sentence instead of raising
when the platform or the application cannot be handled.

## Continuous integration (macOS, Linux and Windows)

`.github/workflows/ci.yml` is a thin wrapper: it checks out, installs one matrix
Python and runs the per-OS entry script. The matrix is `macos-latest`,
`ubuntu-24.04` and `windows-2025` on Python 3.9 (the supported floor) and 3.15
(the newest stable CPython line, 3.15.2, released 2026-10-25;
`actions/setup-python` resolves the newest 3.15.x patch).
The Linux runner is pinned to `ubuntu-24.04` because `ubuntu-latest` now
resolves to Ubuntu 26.04, where `actions/setup-python` has no Python 3.9 build
(`The version 3.9 with architecture x64 was not found for Ubuntu 26.04`). The
Linux Landlock boundary remains proven on `ubuntu-24.04`; do not move the
Linux leg back to `ubuntu-latest` until that image provides Python 3.9.

A macOS job can be cancelled while still queued. In run 42648360100 the
`macos-latest` py3.9 job waited 901 s with `steps: []` and `runner_name: ""`,
then finished `cancelled`: it never reached the 25-minute `timeout-minutes`,
and no newer run existed to trigger `concurrency.cancel-in-progress`. That is
hosted-runner capacity, not this workflow's matrix or timeout. Re-run that job
(`gh run rerun <run-id> --job <job-id>`); do not raise the timeout or serialise
the matrix for it.

`scripts/ci.sh` is the single source of the checks on macOS and Linux and is
runnable locally with the same command CI uses (`bash scripts/ci.sh`, with
`PYTHON` overriding the interpreter). The Windows job is one step,
`pwsh -NoProfile -File scripts/ci.ps1`: a native PowerShell mirror of
`scripts/ci.sh` that does not go through Git Bash, so Windows paths are not
translated and exit codes surface directly. It runs the same checks: hygiene,
package install, the test-only fake `dsh`, the sandbox probe, the full suite
with `unittest discover -s tests -q`, and `tests.test_sandbox_boundary` with
`CF_REQUIRE_REAL_SANDBOX=1`, so a skipped real boundary is a failure. The
GitHub-hosted `windows-2025` runner is x64 and UTC. Its account is an
Administrators member, but 3.13.2's first real run failed in the probe before
it could establish elevation; the probe now records `admin` (token elevation)
and `admin_member` (account group) separately, and the next run is the only
Windows proof.

The gate stays timezone-independent: authority timestamps are persisted as UTC
ISO 8601 (`+00:00`), and the suite passes with `TZ=UTC` (what GitHub runners
use) and under any local zone. Both actions are pinned by commit SHA,
permissions are `contents: read`, and the workflow uses no secrets and no
`pull_request_target`.

The admin-runner caveat is stated rather than hidden: the first
`windows-2025` run (42629362611) failed in the probe, so its `admin=False` was
not a measured elevation result. Run 42648360100 proved the probe on both
Pythons but was red in the suite, and its Administrators-membership query
failed (`WinError 1309`), so `admin_member` is not claimed either. 3.13.9 was green on both Windows
Pythons in run 42697229851 (suite and boundary) and records token elevation
and group membership separately; 3.14.3 needs its own green Windows run as the
required proof for this release. Until that run is green, no
create/modify/append/truncate denial is claimed, delete/rename outside stays
`delete_outside=unverified`, and CI makes no no-admin claim. The standard-user
path remains established only by the user's Windows 11 arm64 VM run.

## Retention and permissions

`retention.py` removes expired run artifacts, job artifacts and traces while
always preserving the newest configured runs/jobs. `budget.py` enforces the
per-job wall-clock and cost limits, and `status.py` produces the terminal
status recorded in reports.

### Retention configuration

`cleanup(cfg, now=None, dry_run=False)` accepts a config dict or any object
whose attributes (or `.get(name, default)` method) provide the keys, such as
the `Config` object returned by `load_config`; a `SimpleNamespace` also works.
The retention knobs are optional (`retention_days`/`keep_runs`/`keep_jobs` fall
back to `0`/`50`/`50` inside `cleanup`); a directory must be named for it to be
scanned. Pass the loaded `Config` object, which always carries the
`config.DEFAULTS` below, or set the directory keys explicitly. A missing,
`None` or empty `log_dir`/`jobs_dir`/`state_dir` leaves that root unscanned and
absent from `roots`; it is never turned into the literal `None` or the current
directory.

| Key | `config.DEFAULTS` | Meaning |
| --- | --- | --- |
| `retention_days` | `0` | Age in days after which artifacts are eligible for deletion. `0` or `None` (the default) disables age-based deletion, so cleanup is off unless you opt in. |
| `keep_runs` | `50` | Newest run groups always kept, even when older than the window. |
| `keep_jobs` | `50` | Newest job groups always kept. A group is all files sharing the prefix before the run suffix (`.json`, `.md`) or the job suffix (`.job.json`, `.status.json`, `.log`). |
| `log_dir` | `~/DeepSeek-MCP/runs` | Directory scanned for `*.json` / `*.md` run files. |
| `jobs_dir` | `~/DeepSeek-MCP/jobs` | Directory scanned for flat job files (`*.job.json`, `*.status.json`, `*.log`). |
| `state_dir` | `~/DeepSeek-MCP/state` | Traces live in `<state_dir>/traces` (`*.jsonl`/`*.json`) and checkpoints in `<state_dir>/checkpoints` (`*.json`); both are cleaned as part of the same run. |

There are no separate per-artifact overrides: the run and job roots come from
`log_dir`/`jobs_dir`, and the trace/checkpoint roots are derived from
`state_dir`. `state_root` (or `CREWFOREMAN_STATE_DIR`) relocates all of those
defaults at once; explicit values still win per key (see "One root for all
mutable state" above). `~` is expanded, and a relative path is
resolved against the process working directory. `now` is a timestamp override
for tests; it defaults to the current time.

Example using `SimpleNamespace`:

```python
from types import SimpleNamespace
from deepseek_mcp.retention import cleanup

cfg = SimpleNamespace(
    retention_days=14,
    keep_runs=10,
    keep_jobs=10,
    log_dir="/srv/deepseek-harness/state/runs",
    jobs_dir="/srv/deepseek-harness/state/jobs",
    state_dir="/srv/deepseek-harness/state",
)
preview = cleanup(cfg, dry_run=True)
print(preview)
# {'deleted': 12, 'kept': 48, 'bytes': 910233, 'errors': [], 'dry_run': True,
#  'roots': {'runs': '/srv/deepseek-harness/state/runs',
#            'jobs': '/srv/deepseek-harness/state/jobs',
#            'traces': '/srv/deepseek-harness/state/traces',
#            'checkpoints': '/srv/deepseek-harness/state/checkpoints'}}
result = cleanup(cfg)
print(result["deleted"], result["bytes"], result["errors"])
```

Return value: `deleted` and `kept` are file counts; `bytes` is the total size of
deleted (or, in a dry run, deletable) files; `errors` is a list of non-fatal
per-path messages; `dry_run` echoes the flag; `roots` maps each configured
`runs`, `jobs`, `traces` and `checkpoints` root to the absolute directory that
was scanned, and omits a root whose key is absent or empty. Empty job
directories left behind are removed outside dry runs when a jobs root is
configured, and are not counted.

### Safety rules

* Paths resolving to the filesystem root, the user's home directory, or an
  ancestor of home are refused with `ValueError` (message
  `refusing unsafe <label> path: <path>`) before anything is scanned or
  deleted; there is no custom exception class. Run with `dry_run=True` first in
  new deployments.
* Only regular files are deleted; symlinks are skipped. Missing directories are
  treated as empty, not errors. Absent, `None` or empty
  `log_dir`/`jobs_dir`/`state_dir` values are skipped entirely, so cleanup
  never scans the literal `None` directory or the process working directory.
* A file whose metadata cannot be read is skipped and left in place without an
  `errors` entry; an unreadable directory is reported in `errors` and left in
  place.
* A zero/absent `retention_days` means nothing is age-eligible, so cleanup
  deletes nothing; it is idempotent and safe to schedule.

### Permissions

`secure_tree(path)` applies owner-only permissions recursively on POSIX and
returns a `(directories, files)` tuple: directories `0700`, files `0600`.
Symlinks are skipped and unreadable entries are left unchanged. An absent,
`None` or empty path is a no-op and returns `(0, 0)`: it never walks the
process working directory implicitly. On Windows it is a no-op and returns
`(0, 0)`; ACLs must be managed with `icacls` or equivalent instead.

```python
from deepseek_mcp.retention import secure_tree
secure_tree("/srv/deepseek-harness/state")
```

Run this after creating state directories (and again after restoring a backup)
so run logs containing prompts, diffs, or provider responses are not readable
by other local users. Keep the harness process's umask restrictive as well
(`umask 077`).

### Scheduling

A daily cron/systemd timer is enough; retention is idempotent. Always use a
lock so two cleanups cannot race, and alert on a non-empty `errors` list rather
than on `deleted == 0`. For cost control, persist `JobBudget.snapshot()` when a
run ends. Its fields are `max_seconds`, `max_cost_usd`, `elapsed_seconds`,
`remaining_seconds`, `cost_usd`, `cost_known`, `time_exceeded`,
`cost_exceeded` and `stop_reason`. With a cost ceiling configured, an unknown
cost (`cost_known: false`, `cost_usd: null`) yields `cost_exceeded: true`: it
cannot be proven under budget, so further paid phases stop, exactly as
`budget.py` and the whole-job budget section above say. Unknown cost is never
treated as free.

### Windows note

`secure_tree` does nothing on Windows (`os.name == "nt"`): inherited ACLs stay
as configured by the service account. Retention itself is cross-platform; path
refusal also applies on Windows, and long paths should be avoided in state
directories.

### Test temp directories in root/read-only sandboxes

The test package probes `DSH_TEST_TMPDIR`, `DSH_TMPDIR`, `TMPDIR`, `TMP` and
`TEMP` only when the path already exists; it never creates an explicit
`TMPDIR`.  This matters under uid 0, where a path such as `/no/such` would
otherwise be created and mask the sandbox misconfiguration.  If no explicit
candidate is usable, it creates and verifies a fallback under
`<repo>/.dsh-tmp-tests`, `<repo>/.tmp` (and `$PWD/.tmp` when `$PWD` is inside
the repo) before trying the user cache, tmpfs and the system temp root.
`repo.parent` is never a fallback.  Set `TMPDIR` (or `DSH_TEST_TMPDIR`) to an
existing writable directory to choose the location.  `tests.tempdir()` returns
a verified directory and raises `unittest.SkipTest` with an environment-only
reason when nothing is writable; `tests.make_tempdir()` creates a fresh child
directory.  In tests, prefer a regular-file blocker (`regular-file/tmp`) over
`/no/such/...`, because root can create arbitrary absolute paths while
`ENOTDIR` under a file fails for every uid.

### Running the full suite inside a Harness sandbox (P3.4a)

From the repository root inside the Harness job workspace, run the standard
library runner (pytest is neither required nor installed by this project):

```bash
python3 -m unittest discover -s tests -q
```

That command is enough under `sandbox workspace-write`; it needs no
`danger-full-access`, no `sandbox-exec` wrapper and no
`run_suite_noseatbelt.py`. `tests/isolation.py` is standard-library-only and
gives every test a fresh `case-*` directory under a workspace-local scratch
base: an existing `TMPDIR`/`TMP`/`TEMP` whose basename is `.dsh-tmp-*`
(the Harness grant) first, then `<repo>/.dsh-tmp-tests`, then `<repo>/.tmp`.
The base is never the checkout's parent (`repo.parent`). `DSH_TEST_TMPDIR` /
`DSH_TMPDIR` are explicit test-run overrides and may point anywhere writable.
`tests/__init__.py` installs that per-test hook when the test package is
imported (the `unittest discover -s tests` layout) and also exposes it through
`load_tests` for package discovery; `tests/conftest.py` is only an optional
pytest adapter, not a dependency.

Inside a case the helper points `CREWFOREMAN_STATE_DIR`,
`DSH_STATE_DIR`/`DEEPSEEK_MCP_STATE_DIR`, `DSH_SESSIONS_DIR`,
`DSH_SKILLS_DIR`/`DEEPSEEK_SKILLS_DIR`,
`DSH_WORKSPACE`/`DEEPSEEK_WORKSPACE`, `DEEPSEEK_MCP_CONFIG`,
`TMPDIR`/`TMP`/`TEMP` and `PYTHONPYCACHEPREFIX` at the case. It also removes
inherited per-key aliases (`DEEPSEEK_MCP_LOG_DIR`, `DEEPSEEK_MCP_JOBS_DIR`,
`DEEPSEEK_MCP_PID_FILE`, `DEEPSEEK_DSH_SESSIONS_DIR`) for the duration,
because those outrank the derived roots; the previous values are restored
after the test. `DSH_SESSIONS_DIR` is redirected rather than removed because
session accounting is no longer derived from the state root (B1). Runs, jobs,
sessions, traces, checkpoints, queue/locks, journals, skills, restart/render
status and `tempfile` output therefore land inside the workspace, and the case
directory is deleted after the test.

`HOME`/`USERPROFILE`/`XDG_*` are deliberately left alone:
`api.redact_secrets()` rewrites `/Users/<name>` and `/home/<name>` to `~`, so
redirecting `HOME` would make machine-readable trace/resume paths re-expand
under the wrong home (nine resume tests depend on that round-trip).

Explicit `TMPDIR` (or `DSH_TEST_TMPDIR`) still chooses the base for
`tests.tempdir()`; the per-test case root is the workspace-local
`tests.workspace_temp_root()` so an unwritable or outside-workspace `TMPDIR`
cannot push state out of the sandbox. If no workspace scratch root is
writable (for example a read-only review sandbox), `tests/isolation.py` records
one **counted** skip per test (`result.startTest` + `result.addSkip` with an
`environment-only:` reason + `result.stopTest`) instead of letting `SkipTest`
escape before `startTest`. The run therefore completes as
`OK (skipped=N)`; it never aborts with a traceback.
`tests/sandbox_guard.py` detects an outer Seatbelt/Harness sandbox through
`safety.seatbelt_usable()` and lets a future test that genuinely needs a real
nested sandbox skip with an `environment-only` reason instead of failing with
EPERM. At 3.9.3 no test needs that skip: every sandbox-policy test mocks
`seatbelt_usable` and `DSH_OS_SANDBOX=0`, so an ordinary writable run reports
zero skips.

Baseline: 1008 tests at 3.9.2 (historical pytest run, kept only for the
comparison). The 3.9.3 P3.4a work adds 34 tests over that baseline: 18 from
the original build plus 16 from the fix round (3 state-root/session
precedence, 2 B1 session regression, 3 isolation/read-only skip, 6 macOS
restart verification including the fail-closed ancestor-unknown regression,
and 2 detached-caffeinate handle tests). Final stdlib-discovered total:
**1042 tests**.

## Durable work

- `harness_deepseek` accepts `idempotency_key`. Retries with the same key share one work item. After that work ends, a new request starts a new attempt. `dedupe_active` (default `true`, `DSH_DEDUPE_ACTIVE`) merges identical requests only while they are active.
- Work files live under `<state_dir>/work/<work_id>/`: `attempts.jsonl`, `events.jsonl` and `result.json`.
- To follow a run, call `deepseek_job_status` with `run_id` and `events_since=0`, then pass the returned `next_seq` back as `events_since`. The reply adds new events, `stage`, `attention`, any pending `question` and `options`, `next_action`, `terminal` and `result`. An older `events_since` value receives retained events, not an error.
- `task_completion="job"` (default) keeps the MCP task `working` until a final journal state. A pending question in one fenced `attention` JSON block (`question`, `options`, optional `evidence`) makes it `input_required`. `task_completion="request"` completes when the call returns.

## Models, backup builder and gate scope (3.12.0)

All new keys are additive; empty values reproduce 3.11.0. Configuration
precedence remains defaults < config file < environment < per-job tool
argument. This table is the operator-facing summary of the registry in
`deepseek_mcp/config.py`.

| Key | Default | Meaning |
|---|---:|---|
| `builder_model` | `""` | Parent builder model; empty inherits `dsh_model` |
| `builder_provider` | `""` | Parent builder provider; empty inherits `dsh_provider` |
| `builder_allow_unlisted_models` | `false` | Allow a model absent from `providers[].models` |
| `builder_model_max_slices` | `8` | Cap on `slice_models` entries |
| `slice_models` | `{}` | Default slice -> model map; a tool argument wins |
| `model_candidates_max` | `3` | Extra competing candidates per job |
| `model_plan_enforcement` | `warn` | `warn` degrades; `strict` stops before review |
| `model_prices` | `{}` | Generic USD/M-token rows for builders and reviewer fallback: `{input, cache_read, output}` |
| `credentials_file` | `~/.dsh/.credentials.yaml` | Provider-key file read after the process environment |
| `reviewer_prices` | `{}` | Optional reviewer-only override, never applied to builders |
| `model_usage_max_models` | `8` | Named rows before the `__other__` fold |
| `history_model_min_runs` | `2` | Floor before a model can be `best_model` |
| `history_model_max` | `6` | Model history rows kept in sizing history |
| `failure_notes_models` | `on` | Feed per-model learning into `failure_notes` |
| `failure_notes_max_models` | `6` | Model rows per task type |
| `failure_notes_model_min_runs` | `2` | Model note floor |
| `backup_builder_model` | `""` | Backup builder model; empty disables |
| `backup_builder_provider` | `""` | Optional provider; empty derives |
| `backup_builder_max_cost_usd` | `1.0` | Independent backup ceiling; `0` disables |
| `backup_builder_min_attempts` | `2` | Attempts before a 5xx/stream switch |
| `reviewers` | `[]` | Ordered reviewer candidates (see REVIEWER.md) |
| `reviewer_provider` | `""` | Reviewer provider for `reviewer_model` |
| `reviewer_model` | `""` | Reviewer model; empty keeps the legacy wrapper |
| `reviewer_vendor` | `""` | Legacy reviewer vendor label |
| `reviewer_max_preflights` | `8` | Candidate preflight cap |
| `reviewer_same_vendor_policy` | `ask` | `ask` offers consent; `never` removes it |
| `gate_no_product_mode` | `skip` | `skip` a no-product diff; `off` restores 3.11.0 |
| `gate_noproduct_globs` | artifact list | Paths that never require lint/tests |
| `gate_secret_skip_globs` | patch/log list | Paths never secret-scanned |
| `gate_split_fixture_paths` | 5 fixture paths | Known fake-credential fixtures |
| `plan_optional_max_files` | `1` | Tiny-job PLAN threshold; `0` disables |

Environment aliases exist for the common ones: `DSH_BUILDER_MODEL`,
`DSH_BUILDER_PROVIDER`, `DSH_BACKUP_BUILDER_MODEL`,
`DSH_BACKUP_BUILDER_PROVIDER`, `DSH_BACKUP_BUILDER_MAX_COST_USD`,
`DSH_REVIEWER_VENDOR`, `DSH_REVIEWER_MODEL`, `DSH_REVIEWER_PROVIDER`,
`DSH_REVIEWER_MAX_PREFLIGHTS`, `DSH_GATE_NO_PRODUCT_MODE` and
`DSH_PLAN_OPTIONAL_MAX_FILES`. Invalid values for the new enums fall back to
the documented default rather than aborting startup.

### Cost and case records

`run["model_usage"]` is rebuilt at terminal serialization. It holds named
`<provider>/<model>` rows with roles, executions, token buckets, estimated
cost, `cost_known`, cost source, aggregate scores (no score lists), wins and
losses, an `__other__` fold, `unpriced_models`, `unattributed_scores` and
`totals`. If any contributing model is unpriced, that row and the total are
`null`; the connector never zero-fills, proxies the parent price or splits a
total by token share. `model_prices` is the generic table for builders and the
reviewer fallback; `reviewer_prices` optionally overrides reviewer/revision
pricing and is never applied to a builder. Resolution is `reviewer_prices` ->
`model_prices` -> built-in DeepSeek `HARNESS_PRICES`; partial or unknown rows
stay `null`, never `0`. Unknown-cost text reads `no configured model price`.

`MODEL_AUDIT.json` is required when a model plan is supplied. The connector
cross-checks requested pairs, observed model ids and existing files and records
`model_plan.status` as `verified`, `partial`, `mismatch`, `unverified` or
`ignored`. A missing audit is never verified. `model_plan_enforcement="warn"`
sets `run["degraded"]=true` and adds `MODEL_PLAN_NOT_EXECUTED`; `strict` stops
the run before review.

`run["backup_builder"]` records `used`, `status`, vendor, model, reason class,
redacted `main_error`, attempts, ceiling, settlement and the header line. The
binary path is never stored. `run["builder_vendors"]` is the sorted canonical
vendor set used by the reviewer rule.

### Gate scope and PLAN exceptions

The gate classifies every changed path as `product`, `artifact`,
`split_fixture` or `plan`. With `gate_no_product_mode="skip"` (default), a diff
with no product change runs the secret scan and skips lint/tests with
`skipped_reason="no_product_code_change"`; a real secret still fails and blocks
review. Artifacts such as `logs/**`, `*.patch`, `*.diff`, `PLAN.json`,
`REVIEW*.md`, report files, caches, media and docs never require lint/tests.
The five known split fixtures are skipped by the secret scan but still count as
a test change, so a fixture-only edit runs the suite. `gate_no_product_mode="off"`
restores 3.11.0 exactly.

With no pre-builder baseline, the gate classifies `output_files` and
`deliverable_files` and records `baseline_missing`. A job with no product output
takes the same skipped branch.

`plan_optional_max_files` (default 1, `0` disables) turns an absent or invalid
`PLAN.json` into `status="optional"`, `blocking=false`,
`note="plan: optional (tiny job)"` only when 1..N product files changed.
Reasons are `tiny_job_absent_plan`, `tiny_job_invalid_plan` and
`tiny_job_plan_not_in_snapshot`. A valid plan is always compared; a real
mismatch still blocks in `block` mode.

### New status

`needs_input` is a non-terminal pause: one validated question is waiting for a
client answer. It is held by the work journal, is not returned by
`final_status`, and is resolved with `resume_deepseek` plus the `option` shown
by `deepseek_job_status`. Declining a reviewer question ends `ok_unreviewed`
with `review_declined_by_user=true` and `quality_score=null`.

## 3.14.3 reliability options

### `job_max_seconds`

`job_max_seconds` bounds one whole job across the builder, gate revisions,
the independent reviewer and the rescue/finisher phase. An absent key uses the
built-in 7200 s default; an explicit `null` or `0` means **unlimited** in every
phase and is recorded as `job_max_seconds: 0` with
`job_max_seconds_display: "unlimited"`; the internal 7200 s stall ceiling is
not re-applied to a phase that has no deadline. A negative tool argument or configured
value is rejected with a clear error.
An explicit `0` is never re-capped, not even when the operator configured a
positive `job_max_seconds` ceiling: the ceiling caps positive per-job requests
only. Remove the key (or set it to `null`) if you also want the operator
default to be unlimited. Progress-gated stall detection (`stall_seconds`,
`stall_max_seconds`) is unchanged and still protects against a silent child.

### `depends_on_policy`

Controls what a queued job does when the run named by `depends_on` ends in a
non-success terminal state.

| Value | Meaning |
| --- | --- |
| `success` (default) | Start only when the dependency finished successfully. A dependency error produces a message that names the dependency, its terminal status and the remedy, and the job is parked, not silently dropped. |
| `finished` | Start once the dependency reaches any terminal state, including `error`, `no_balance`, `deadline_exceeded` and `cancelled`. |

Tool argument `depends_on_policy`, config key `depends_on_policy` (an invalid
value is rejected at submission), env aliases `DSH_DEPENDS_ON_POLICY` and
`DEEPSEEK_DEPENDS_ON_POLICY`. Resuming an errored dependency revives the
dependents parked on the old attempt: they are re-pointed at the resumed run
id and re-queued, so a transient dependency failure does not leave them dead.

### `git_identity` and `git_identity_exceptions`

Every commit, amend or rebase the connector creates is authored and committed
with the configured identity. Default:

```json
"git_identity": {"name": "crewforeman-mcp", "email": "noreply@example.com"}
```

The helper sets it repo-locally before touching the index and also passes
`-c user.name` / `-c user.email`, so a global git identity cannot leak in,
including through a rebase. Before a push, the privacy gate scans both the
content of the pushed commits and their **metadata** - the author and
committer name+email, plus email addresses in commit messages and
`Co-authored-by` / `Signed-off-by` trailers, for every commit in
`base..head` - and blocks the push when any identity is outside the allowlist (the configured `git_identity`
name and address and the default `crewforeman-mcp` / `noreply@example.com`).
Exception entries are remotes, never emails; an email-shaped exception does
not widen the global allowlist. An exception remote is exempt from the
metadata check but its added content is still scanned.

`git_identity_max_scan_commits` (default 10000) bounds one gate run. A larger
range fails closed with `too_many_commits` instead of scanning part of the
history; raise it only for a repository that really has that much history.

`git_identity_exceptions` lists remotes that keep their own repo-local
identity, for example a personal fork such as `"StanyMwamba/Cypraea"`.
Matching is **exact**, not a substring: an entry must be `owner/repo` (a
GitHub.com shorthand only) or a full URL (with or without `.git`). A
substring match would let an entry such as `owner/repo` exempt an unrelated
`evil.example/fork-of-owner/repo` push target, and a bare remote name is a
local label that can be re-pointed at another URL, so bare names such as
`origin` are ignored. On GitHub Enterprise, GitLab or any other host, use the
full URL. An exception is
resolved from a configured remote name; a direct-URL `git push` does not
borrow it. No personal address is built in anywhere.

```bash
python -m deepseek_mcp.git_identity --repo . --base origin/main --head HEAD \
    --remote origin        # exit 1 blocks the push
```

The connector installs the gate automatically the first time it creates a
commit or rebase in a clone, so later `git push` runs the gate even if
`core.hooksPath` was never configured. An operator's existing `pre-push` hook
is preserved as `pre-push.local` and runs first; the gate never discards it.
The hook embeds the connector package path, so it works even when the clone
is not the connector checkout. To install the checked-in hook by hand
instead:

```bash
git config core.hooksPath scripts/hooks
```

Connector-controlled release tooling calls
`git_identity.push_with_gate(cfg, repo, remote, refspec, base=...)` (wrapped
by `scripts/ship_push.py`) and never a bare `git push`: it runs the same gate
and returns before the push on any violation. That helper is the hard local
gate; the Git hook is cooperative and, like every Git hook, can be skipped
with `git push --no-verify`. GitHub Actions therefore runs the same CLI gate
over the pushed range before the test matrix, so a bypassed local hook still
cannot land an off-allowlist commit or content on `main`. Release/publish
tooling must call `push_with_gate` rather than a bare `git push`.
`scripts/hooks/pre-push` runs the same gate for each pushed ref, using the
remote's advertised sha as the base (or the merge-base for a new branch). It
scans metadata and content and exits non-zero on any violation.
