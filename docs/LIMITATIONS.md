# Known limitations and failure modes (3.13.0)

Only limitations that remain in this version are listed here.

## Platform boundaries (3.13.0)

Every sentence below is tied to the boundary run for that OS. If a gate is red,
the claim is narrowed, never widened.

- **macOS:** CrewForeman runs builders under a Seatbelt profile when
  `sandbox-exec` passes the probe. The boundary suite proves that creating,
  modifying, truncating, renaming and deleting files outside the workspace is
  refused, and that `..` and resolved-symlink escapes are refused. Reads are not
  restricted, and network access is not restricted.
- **Linux:** CrewForeman runs builders under a Landlock ruleset applied by an
  unprivileged launcher when the kernel reports Landlock ABI 3 or newer. The
  same file-write, rename and delete claims apply, and the boundary suite runs
  on `ubuntu-24.04`. ABI 1-2 hosts run in degraded mode and say so; the run does
  not claim the truncate protection it cannot provide. Network access is not
  restricted.
- **Windows:** CrewForeman runs builders with a write-restricted token and an
  ACE for the restricting SID on the workspace and its temp directory.
  The boundary suite proves that creating, modifying and truncating files
  outside the workspace is refused. Deleting or renaming outside is claimed only
  if a Windows boundary run proves it; otherwise the run header reports
  `delete_outside=unverified`. Network and UNC writes are not blocked. Windows
  CI is PENDING the user's non-admin VM run; no Windows runner elevation is
  recorded, and this text makes no no-admin claim.
- **When no boundary is available:** `os_sandbox=require` refuses to start the
  builder. `os_sandbox=auto` starts it only with `degraded=true`, and the run
  header prints `UNSANDBOXED`. A run record never reports `enforcement=enforced`
  unless the probe and the canary both passed.
- **Scope:** this is a file-write boundary, not a container, VM, network
  firewall or malware sandbox. It does not protect against kernel or sandbox
  exploits, a compromised `dsh` install, a different same-user process, or
  secret exfiltration over the network. Run records are plaintext.
- **Reviewer:** the reviewer is a model. Its score is judgement, not proof of
  correctness.
- **Restart:** restarting Claude Desktop is best-effort and OS-specific. macOS
  uses Automation (`osascript`), Windows uses `taskkill`/`start`, and Linux is
  best-effort against the beta client whose desktop-entry name varies; use the
  explicit command override when resolution fails.

| Area | Limitation | Behaviour / workaround |
| --- | --- | --- |
| Packaging | `pip install .` uses a small in-tree PEP 517 backend so it works offline; that backend does not implement editable installs (PEP 660). | Install into a venv with `pip install .`, or run `python -m deepseek_mcp` from the repository root for a source-linked development loop. |
| Sandbox | The OS boundary is a file-write boundary only, and it differs per platform: macOS Seatbelt, Linux Landlock at ABI 3 or newer, Windows write-restricted token. ABI 1-2 Linux and hosts without a usable wrapper run degraded or `UNSANDBOXED`, never as an enforced boundary. | Read the run's `os_sandbox` object and header, and the per-OS limits below. Review the workspace before accepting output. |
| Windows | Windows behaviour is exercised with fakes and the Windows unit suites; it has not had the same manual field use as macOS, and no Windows CI job runs yet. The write-restricted token blocks file writes only; network and UNC writes are not blocked. Delete/rename outside is claimed only when a Windows boundary run proves it (CI is macOS/Linux; Windows pending), otherwise `delete_outside=unverified`. | Process-tree termination uses `taskkill /T /F`; paths use `%APPDATA%`/`%LOCALAPPDATA%`. Run as a standard user; no no-admin claim is made. Report a failing case with the run id. |
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
