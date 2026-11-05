# Operations: configuration, safety, budgets, traces and retention (v3.8.0)

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
