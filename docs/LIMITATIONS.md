# Known limitations and failure modes (3.11.0)

Only limitations that remain in this version are listed here.

| Area | Limitation | Behaviour / workaround |
| --- | --- | --- |
| Packaging | `pip install .` uses a small in-tree PEP 517 backend so it works offline; that backend does not implement editable installs (PEP 660). | Install into a venv with `pip install .`, or run `python -m deepseek_mcp` from the repository root for a source-linked development loop. |
| Sandbox | Only macOS has an OS-level Seatbelt profile (`sandbox-exec`). Linux and Windows rely on the Harness permission mode and the workspace boundary. | `sandbox_support()` reports `seatbelt` or `none`; the run metadata records which was used. Review the workspace before accepting output. |
| Windows | Windows behaviour is exercised with fakes and the CI matrix; it has not had the same manual field use as macOS. | Process-tree termination uses `taskkill /T /F`; paths use `%APPDATA%`/`%LOCALAPPDATA%`. Report a failing case with the run id. |
| Resume | Resume can only continue from a checkpoint boundary. Work done by a DSH process killed mid-tool-call is repeated. | Skeleton and checkpoints are written before each phase; finished revisions are reused. |
| Live scaling | The candidate score is a deterministic heuristic, not a model review. It can misjudge unusually formatted output. | Threshold, max extra waves and the whole-job budget are configurable; set `live_scaling=false` to disable. |
| Reviewer | By default the reviewer is a fresh Harness process that uses the same provider and model family as the builder. A separately configured reviewer wrapper can use another vendor (see `docs/REVIEWER.md`). | Treat the default `REVIEW.md` as independent-in-process, not independent-in-model. |
| Traces | Export is optional and best effort. A broken OTLP/Langfuse endpoint is logged; it never fails the run. | Traces and timelines remain on disk under the state directory. |
| Retention | Cleanup is off by default (`retention_days=0`) and never removes the newest `keep_runs`/`keep_jobs`. | Use `deepseek_maintenance` with `dry_run` first. |
| Browser | `take_screenshot` needs an installed Chrome/Edge or an OS screenshot helper and, on macOS, Screen Recording permission. | Returns `unsupported` with the missing dependency; it does not install anything. |
| Transport | The server is local stdio only. There is no network listener, authentication layer or multi-user mode. | Run one server process per client; use the client's own access control. |
| Keys | The server reads `DEEPSEEK_API_KEY` from its environment and never writes it to run records. | Configure the key in the MCP client's `env` block, not in prompts. |
