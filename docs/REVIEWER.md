# Reviewer

CrewForeman can use an independent reviewer from a different vendor. The reviewer scores the result from 0 to 10 and lists blocking defects. It is read-only by default and runs in a private home, so it does not share the builder's session state.

## Provider keys and the credentials file

A provider entry names its key variable; it never stores the key:

```json
{"name": "anthropic", "api_key_env": "ANTHROPIC_API_KEY", ...}
```

The connector resolves that name in this order:

1. the process environment (an explicit export always wins);
2. the Harness credentials file, `~/.dsh/.credentials.yaml` by default. Set
   `credentials_file` in the config, or `CREWFOREMAN_CREDENTIALS_FILE` /
   `DSH_CREDENTIALS_FILE` in the environment, to use another path. The legacy
   flat `NAME: value` form and the nested `records` / `refs` layouts are read.

A file that is readable by group or others is skipped with a warning; make it
owner-only with `chmod 600`. The value is copied only into the child `dsh`
process environment under that name. Overlays, run records, logs, events and
errors carry the variable name only, never the value.

## Reviewer routing (new in 3.12.0)

The connector, not a hand-edited wrapper, chooses the reviewer. `reviewer_model`
and `reviewer_provider` select a model through a generated provider/model
overlay; the key stays in the environment under the provider's `api_key_env`
name. A per-job `reviewer_model` / `reviewer_provider` argument overrides the
config for that job only.

For more than one candidate, set `reviewers` to an ordered list:

```json
{"reviewers": [
  {"name": "opus", "vendor": "Anthropic", "provider": "anthropic",
   "model": "claude-opus-4-1", "enabled": true},
  {"name": "kimi", "vendor": "Kimi (Moonshot)", "provider": "moonshot",
   "model": "kimi-k3.1-pro", "api_key_env": "MOONSHOT_API_KEY"}
]}
```

`dsh_bin` and `patch` are operator-only fields for a legacy wrapper; they are
never accepted as tool arguments. A missing binary or unreadable patch is a
candidate error, not a crash. `reviewer_max_preflights` (default 8) caps how
many configured candidates are pinged.

Selection runs after the deterministic gate and before any paid review. The
connector resolves the vendor, runs the READY ping in configured order, and
uses the first cross-vendor candidate that answers `READY`. Credit and auth
failures move to the next candidate immediately; 5xx and dropped streams get
at most two attempts. The chosen candidate's exact header is
`Reviewed by: <vendor>/<model>`.

## Legacy reviewer wrapper

Existing `dsh_bin` wrapper setups (for example `dsh-reviewer-kimi.sh` and
`dsh-reviewer-sonnet.sh`) keep working unchanged as the legacy path. When
`reviewers` is empty, CrewForeman synthesizes the main candidate from
`dsh_bin` + `reviewer_vendor`/`reviewer_model`, and a second candidate from
`backup_dsh_bin`. The vendor comes from the explicit label, then the wrapper
basename (`kimi` -> Kimi (Moonshot), `sonnet` -> Anthropic). A legacy wrapper
loads its own key; no connector overlay is required for it.

The reviewer path is used for the pre-flight READY ping and for the paid
review. `review_preflight` defaults to `true`, and `review_preflight_timeout`
defaults to 45 seconds. `review_preflight=false` disables only the redundant
round preflight; selection still performs its own READY ping for `reviewers`
candidates.

## Same-vendor rule and the one question

A reviewer must come from a different vendor than every builder model that may
run for the job: the parent, every slice, every competing candidate (including
losers), and `backup_builder_model` when configured. Unknown is fail-closed,
never treated as different.

If no cross-vendor candidate passes preflight, the job pauses with exactly one
`needs_input` question. The options list the same-vendor models that answered
READY, labelled `<vendor>/<model> (same vendor)`, then
`Do not review; return the build as unreviewed`. A same-vendor candidate is
started only after the user picks it, and the header then says exactly
`same-vendor review (chosen by user)`. `reviewer_same_vendor_policy="never"`
removes same-vendor options; it never silently chooses one.

Resume the paused job with `resume_deepseek` and the `option` shown by
`deepseek_job_status`. The connector re-preflights the chosen candidate and
runs the paid review; it does not rerun the builder. Declining returns
`ok_unreviewed` with `review_declined_by_user=true` and no invented score.

## MiMo as reviewer (new in 3.15.0)

MiMo Code (`mimo`, v0.1.17) is an independent-reviewer candidate like any
other vendor. Set `reviewer_provider: "mimo"` with
`reviewer_model: "xiaomi/mimo-v2.6-pro"` (or add a `reviewers[]` entry with
`"vendor": "Xiaomi (MiMo)"`), and the connector runs the `READY` preflight
ping and the paid review through the MiMo adapter. Against DeepSeek
builders the MiMo reviewer is cross-vendor, so the same-vendor question
does not arise; against MiMo builders pick a DeepSeek or other-vendor
reviewer instead, and the same fail-closed rule applies.

A MiMo reviewer runs under the `read-only` per-run `mimocode.jsonc`
preset: reads, `glob`/`grep`/`list` and non-mutating `bash` are allowed
while `edit` and mutating commands are denied, so the reviewer cannot
grade its own writes. Auth failures name the remedy
(`mimo providers login` for the `xiaomi` provider, or the provider key
variable); limit and network failures carry the same vendor-correct text
as builder runs. MiMo is also accepted as the backup reviewer target when
the primary reviewer's service fails, and the quota-error attribution
names `Xiaomi (MiMo)`, never DeepSeek.

## Resumable reviews

A review writes `REVIEW.partial.md` and `REVIEW.partial.json` while it runs. The reviewer emits structured `@@REVIEW_PROGRESS@@` checkpoint lines, and the connector parses and saves them, along with completed areas and test reruns. After an interruption, `resume_deepseek` resumes from that checkpoint: the reviewer checks the remaining areas, and carried findings are merged under their original blocking or non-blocking heading.

See [OPERATIONS.md](OPERATIONS.md) for configuration and retention, and [TEAMS.md](TEAMS.md) for where review sits in the run.

## Review scope and follow-up tags

Only defects caused by the change under review can block. A pre-existing
problem in code the change does not touch is a follow-up. List follow-ups under
the exact heading `## Follow-ups (pre-existing, non-blocking)`, and give every
follow-up exactly one tag as the first thing on its line:

| Tag | Meaning | Blocking |
|---|---|---|
| `[security]` | security defect | yes |
| `[data-loss]` | data-loss defect | yes |
| `[minor]` | anything else, including docs typos and benign mentions | no |

For example, `[minor] Old typo in authentication documentation` is non-blocking
even though it contains the word "authentication". The same sentence with no
tag is blocking under the fail-safe rule below.

Tags are recognised only at the start of a line: leading whitespace is allowed,
matching is case-insensitive, and `data_loss` / `data loss` inside the brackets
is accepted. A tag in the middle of a line is not a tag. The parser removes the
leading scope tag from the stored follow-up text and records the kind as
`security`, `data-loss` or `None` (`[minor]` yields `None`, `blocking=False`).

Untagged follow-ups fail safe. If an untagged line contains any security or
data-loss keyword, it is blocking; security wins when both kinds appear.
Negation is not considered: `Legacy migration cannot avoid data loss` is
blocking, and so is `Legacy endpoint cannot avoid exposing user credentials`.
Tag a benign mention `[minor]` instead of relying on how the sentence reads.
The connector counts blocking follow-ups in `Blocking defects: N` and surfaces
them with the exact marker `[blocking: security]` or `[blocking: data-loss]` in
the parsed defect and fix lists. An untagged follow-up with no security or
data-loss keyword is non-blocking.

Do not repeat follow-ups under `## Blocking defects` or
`## Non-blocking defects`; list each pre-existing issue once, under the
follow-up heading. Old-format reviews without a follow-up section are unchanged.

## Backup reviewer

`backup_dsh_bin` (default `""`) points at a second Harness executable or
wrapper for a reviewer from another vendor. Empty means unchanged: the main
`dsh_bin` reviewer is the only reviewer. `backup_reviewer_vendor` and
`backup_reviewer_model` are optional labels recorded with the backup review;
when they are empty, the connector derives the vendor from the wrapper
filename (for example `sonnet` -> Anthropic, `kimi` -> Kimi (Moonshot)) and
the model from the reviewer execution (falling back to `dsh_model`).

The backup is used only after the main reviewer has failed for a **non-quality**
reason:

- credit or budget exhaustion (HTTP 429 with budget/quota wording, or an
  insufficient-balance error);
- authentication or invalid-key failure;
- service unavailable / HTTP 5xx after the bounded retries; or
- repeated stream drops after the bounded retry (for example `flush on a
  closed handle`, `STREAM_CLOSED`, `ECONNRESET`).

The reviewer pre-flight uses the same bounded retry as the paid reviewer before
the backup is tried: a 5xx or dropped-stream failure gets a second main attempt
with exponential backoff. Auth and credit failures are not retried; they switch
immediately because another attempt cannot fix them. The backup pre-flight must
answer `READY` before the paid round runs.

A low score, blocking defects or a missing score are quality outcomes of a
successful reviewer process; they never cause a vendor switch. The connector
re-runs the same review round with the backup and records the banner:

`Reviewed by: backup (<vendor/model>) — main failed: <reason class>`

The switch is **not sticky**: it applies only to that round, and the next job
starts with the main reviewer again. If the main reviewer's pre-flight fails
with a switch class, the backup must pass its own pre-flight before the paid
review runs. If both reviewers fail, the run fails with a clear reviewer error
and `quality_score` is `None`; no score is invented.

## Vendor attribution (new in 3.11.0)

The failure message names the reviewer that actually ran, not the main
reviewer's vendor. With a DeepSeek main reviewer and a Kimi backup, a Kimi
quota error says `Kimi (Moonshot)/<model> reviewer quota is exhausted ...; the
DeepSeek balance is unaffected.` The sentence `DeepSeek account balance is
empty` is reserved for a reviewer whose identity is DeepSeek. Auth errors name
the vendor key variable when it is known (`MOONSHOT_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`); a bare 429 names
the vendor and asks the operator to wait and retry; any other failure gets a
generic attributed sentence. Raw provider text is not copied into the
user-facing sentence, so it cannot echo a secret.

The label comes from `backup_reviewer_vendor` / `backup_reviewer_model` when
set; otherwise the wrapper basename and the execution model are fallbacks, and
an unidentified vendor is labelled `unknown`, never DeepSeek. This is a
message-only change: it does not alter admission, retry, score or status.
