# Known limitations and failure modes (3.12.0)

Only limitations that remain in this version are listed here.

| Area | Limitation | Behaviour / workaround |
| --- | --- | --- |
| Packaging | `pip install .` uses a small in-tree PEP 517 backend so it works offline; that backend does not implement editable installs (PEP 660). | Install into a venv with `pip install .`, or run `python -m deepseek_mcp` from the repository root for a source-linked development loop. |
| Sandbox | Only macOS has an OS-level Seatbelt profile (`sandbox-exec`). Linux and Windows rely on the Harness permission mode and the workspace boundary. | `sandbox_support()` reports `seatbelt` or `none`; the run metadata records which was used. Review the workspace before accepting output. |
| Windows | Windows behaviour is exercised with fakes and the CI matrix; it has not had the same manual field use as macOS. | Process-tree termination uses `taskkill /T /F`; paths use `%APPDATA%`/`%LOCALAPPDATA%`. Report a failing case with the run id. |
| Resume | Resume can only continue from a checkpoint boundary. Work done by a DSH process killed mid-tool-call is repeated. | Skeleton and checkpoints are written before each phase; finished revisions are reused. |
| Live scaling | The candidate score is a deterministic heuristic, not a model review. It can misjudge unusually formatted output. | Threshold, max extra waves and the whole-job budget are configurable; set `live_scaling=false` to disable. |
| Reviewer | The reviewer is a fresh Harness process; `reviewer_model`/`reviewers` can put it on another vendor, but a legacy `dsh_bin` wrapper that does not set a vendor label is fail-closed and pauses once. | Configure `reviewers` (see `docs/REVIEWER.md`); the same-vendor question is explicit and labelled. |
| Mixed models | A child model override inside the parent `workflow` cannot be enforced or killed mid-flight by the connector. `MODEL_AUDIT.json` evidence is cross-checked, but unsupported overrides degrade to `unverified`, never `verified`. | Use `model_plan_enforcement="strict"` to stop rather than degrade; pin the whole-job cost ceiling. |
| Model prices | A model with no `model_prices`/`reviewer_prices` row and no built-in price has unknown cost. | Record the price row; the run keeps `cost_known=false` and never fabricates a number. |
| Backup builder | One switch per run, never recursive; a backup cannot start after the failed builder wrote files. | Inspect `run["backup_builder"]`; the header names the class and model. |
| Traces | Export is optional and best effort. A broken OTLP/Langfuse endpoint is logged; it never fails the run. | Traces and timelines remain on disk under the state directory. |
| Retention | Cleanup is off by default (`retention_days=0`) and never removes the newest `keep_runs`/`keep_jobs`. | Use `deepseek_maintenance` with `dry_run` first. |
| Browser | `take_screenshot` needs an installed Chrome/Edge or an OS screenshot helper and, on macOS, Screen Recording permission. | Returns `unsupported` with the missing dependency; it does not install anything. |
| Transport | The server is local stdio only. There is no network listener, authentication layer or multi-user mode. | Run one server process per client; use the client's own access control. |
| Keys | The server reads `DEEPSEEK_API_KEY` from its environment and never writes it to run records. | Configure the key in the MCP client's `env` block, not in prompts. |
