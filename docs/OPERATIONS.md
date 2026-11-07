# Operations: configuration, safety, budgets, traces and retention (v3.9.0)

This page is the detailed reference behind the short README. Everything here
is implemented by `deepseek_mcp/` and exercised by the test suite with fakes;
no live paid API or real Harness process is needed.

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
  suite on the exact reviewed tree (auto-detects `tests/` with `test_*.py`, or
  use `review_test_command`; `review_test_suite=""` disables it). The reviewer
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

`review_context_max_chars` bounds the diff text (default 60000).
`review_test_timeout_seconds` (default 300) is the connector test hard timeout.
The baseline copy is bounded (256 KiB per file, 400 files); larger or binary
files are recorded in the manifest with an `excluded` reason and listed as
changed/added/deleted with a metadata-only body.

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

On Linux and Windows the operating-system sandbox layer is not available;
the Harness permission preset and the workspace boundary still apply, and the
run record says which layer was used. Treat untrusted work as a container/VM
job. The child environment is scrubbed: only `DEEPSEEK_API_KEY` and the key
variables named by configured providers survive, and keys are never written to
run records or checkpoints. No API key is stored in a run record, checkpoint or
trace.

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
| `allowed_workspace_roots` | `[]` | Optional allow-list for run/resume workspace roots. |
| `authority_ttl_seconds` | `604800` | Envelope expiry; a resume reports expiry drift and refreshes it. |

A rescue finisher is deliberately short (at most 8 minutes and 12 tool calls)
and only runs when the builder left usable output files. It is not a way to
extend a job past its deadline.

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
| OS sandbox | Seatbelt | Harness preset only | Harness preset only |
| Restart helper | `osascript` + `open -a` | `pkill` + command/`xdg-open` | `taskkill` + `start` |

Every helper returns a clear `unsupported: ...` sentence instead of raising
when the platform or the application cannot be handled.

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

There are no separate run/trace directory overrides and no base-directory key:
the run and job roots come from `log_dir`/`jobs_dir`, and the trace/checkpoint
roots are derived from `state_dir`. `~` is expanded, and a relative path is
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
