# Reviewer

CrewForeman can use an independent reviewer from a different vendor. The reviewer scores the result from 0 to 10 and lists blocking defects. It is read-only by default and runs in a private home, so it does not share the builder's session state.

## Reviewer wrapper

No separate reviewer model setting ships. The reviewer runs the same `dsh_bin` (`DSH_BIN`) as the builder. CrewForeman gives each review run a private `DSH_HOME` under `<state_dir>/reviewer-homes/`.

Point `DSH_BIN` at a wrapper that checks `DSH_HOME` (the private review home). On a review run, the wrapper can load a patch that selects another vendor's model. On other runs, it calls `dsh` unchanged. `dsh_provider` and `dsh_model` are shared, so changing them changes the builder too.

A working example is the operator wrapper `dsh-reviewer-kimi.sh` plus `reviewer-kimi.patch.yml`. Set the `dsh` path and patch path inside the script, then point `DSH_BIN` at it. These files are operator examples, not package files or package settings.

The reviewer path is used for the pre-flight READY ping and for the paid review. `review_preflight` defaults to `true`, and `review_preflight_timeout` defaults to 45 seconds.

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
