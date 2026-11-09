# Known limitations and failure modes (3.13.6)

Only limitations that remain in this version are listed here.

## Platform boundaries (3.13.6)

Every sentence below is tied to the boundary evidence for that OS. If a gate is
red, or a claim has not been run yet, the claim is narrowed, never widened.

- **macOS:** CrewForeman runs builders under a Seatbelt profile when
  `sandbox-exec` passes the probe. The boundary suite proves that creating,
  modifying, truncating, renaming and deleting files outside the workspace is
  refused, and that `..` and resolved-symlink escapes are refused. Reads are not
  restricted, and network access is not restricted.
- **Linux:** CrewForeman runs builders under a Landlock ruleset applied by an
  unprivileged launcher when the kernel reports Landlock ABI 3 or newer. The
  same file-write, rename and delete claims apply, and the boundary suite runs
  on `ubuntu-24.04` (CI pins that runner because `ubuntu-latest` is Ubuntu
  26.04, where `actions/setup-python` has no Python 3.9 build). ABI 1-2 hosts
  run in degraded mode and say so; the run does
  not claim the truncate protection it cannot provide. Network access is not
  restricted.
- **Windows:** CrewForeman runs builders with a write-restricted token and an
  ACE for the restricting SID on the workspace and its temp directory. The
  first real `windows-2025` run (GitHub run 42629362611, 3.13.2) failed in the
  sandbox probe with `AttributeError`, `logon_sid=None`, `admin=False` and
  `usable=false`; CI stopped with "no enforced OS sandbox on this host" before
  the suite or the boundary. 3.13.3 fixed the probe; run 42648360100 then
  proved it (`usable=true`, `write_restricted=true` on both Pythons) but the
  suite was red and the membership query failed (`WinError 1309`). 3.13.6
  addresses the 3.13.3 suite failures; the next GitHub run is the only
  Windows evidence. The hosted runner account is an Administrators member, but the failed probe's
  `admin=False` was produced before a reliable elevation check and is not
  evidence that the token is unelevated; `admin` (process-token elevation) and
  `admin_member` (account group) are recorded separately and membership is not
  claimed while the query errors. Until the next run is green no Windows
  boundary is claimed.
  If that run reaches the boundary, the suite requires create, modify, append
  and truncate denials outside the workspace; delete and rename outside are
  recorded and stay `delete_outside=unverified` unless the run proves them. The
  boundary child is a minimal native helper: Python 3.15.2 itself exits
  `0xC0000142` at DLL init under the write-restricted token on windows-2025
  (CPython treats that status as a desktop-heap startup failure), so the test
  exercises the ACL policy with a native process. This does not prove that a
  Python child starts under the token; the product's real child is `dsh`, and
  its startup under the restricted token is still unproven until a run shows
  it. A `0xC0000142` from any spawned child is recorded as unverified, never as
  a denial. The
  standard-user path is not proven by CI: the user's Windows 11 arm64 VM run
  (WINDOWS_TEST.md in the VM test package) remains the only standard-user
  evidence, and it must be re-run after this change. No no-admin claim is made.
  Network and UNC writes are not blocked.
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
| Path matching | Plan, write-set and record matching folds case on Windows only. macOS is matched case-sensitively: APFS can be formatted case-sensitive, so the host-wide "every Mac ignores case" assumption would merge `A.py` and `a.py` into one plan entry on such a volume. | Declare each file with its on-disk spelling; on a case-insensitive Mac volume a differently-cased declaration is not treated as the same file. |
| Sandbox | The OS boundary is a file-write boundary only, and it differs per platform: macOS Seatbelt, Linux Landlock at ABI 3 or newer, Windows write-restricted token. ABI 1-2 Linux and hosts without a usable wrapper run degraded or `UNSANDBOXED`, never as an enforced boundary. | Read the run's `os_sandbox` object and header, and the per-OS limits below. Review the workspace before accepting output. |
| Windows | The first real `windows-2025` run (42629362611) failed in the sandbox probe before the suite or boundary ran (`AttributeError`, `logon_sid=None`, `admin=False`, `usable=false`). 3.13.3 fixed the probe; run 42648360100 proved it (`usable=true`, `write_restricted=true` on both Pythons) but the suite was red (82 failures + 141 errors on 3.9; 83 + 139 on 3.15) and `admin_member` errored (`WinError 1309`). 3.13.6 contains the suite fixes; the next GitHub run is the required Windows evidence for the suite and boundary. The runner account is an Administrators member, but the failed probe's elevation value is not evidence; `admin` (token elevation) and `admin_member` (account group) are recorded separately. When the next run reaches the boundary, create/modify/append/truncate denials outside the workspace are required; delete/rename outside is recorded and stays `delete_outside=unverified` unless the run proves it. The token blocks file writes only; network and UNC writes are not blocked. The standard-user path is still established only by the user's Windows 11 VM run. | Process-tree termination uses `taskkill /T /F`; paths use `%APPDATA%`/`%LOCALAPPDATA%`. Run as a standard user; no no-admin claim is made. Report a failing case with the run id. |
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
