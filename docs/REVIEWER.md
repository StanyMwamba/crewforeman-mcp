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
