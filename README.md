# CrewForeman

CrewForeman 3.13.6 is a local stdio MCP server. It runs Harness on your machine, sizes a team, builds in waves, has an independent reviewer score the result, and saves a record of every run.

## Roles
| Role | Who | What it does |
|---|---|---|
| Builder | Harness, any model (default DeepSeek) | Sizes the team, builds in waves, revises on notes. |
| Independent reviewer | Configurable, any vendor (e.g. Kimi) | Scores 0–10 and lists blocking defects. |
| Client / coordinator | Your MCP client (e.g. Claude) | Sends the brief and reads the result. |

**Using a reviewer from a different vendor means a model never grades its own work.**
Use a reviewer from any vendor - see [docs/REVIEWER.md](docs/REVIEWER.md).

The lead sizes the team and merges the best candidates - see [docs/TEAMS.md](docs/TEAMS.md).

Runs survive restarts and can be resumed - see [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Method
CrewForeman creates one work item for each brief. If the same work is already running, it returns that run. Otherwise it sizes a team and plans waves, up to the number of helper agents Harness allows in parallel. A reviewer scores the result from 0 to 10 and lists defects that block acceptance. `quality_rounds` caps the revisions.
```mermaid
flowchart LR
    A[Request<br/>optional idempotency_key] --> B{Admit + work_id<br/>active duplicate?}
    B -- yes --> R[Return existing run]
    B -- no --> D[New attempt<br/>classify + bounded waves]
    D --> E[Independent review]
    E --> F{Pass?}
    F -- no --> G[Revise<br/>quality_rounds] --> E
    F -- yes --> H[Journal + result<br/>MCP task completes]
```
## Outcomes
```mermaid
flowchart LR
    A[Team builds] -- done --> R{Review<br/>score ≥ 8.5?}
    A -- stalls --> X[Rescue team<br/>finishes the work]
    X --> R
    R -- yes --> OK([Accepted])
    R -- no --> T{Tries left?}
    T -- yes --> F[Fix with<br/>reviewer notes]
    F --> R
    T -- no --> N([Returned<br/>with notes])

    classDef good fill:#e6f4ea,stroke:#1a7f37,color:#0f3d1e;
    classDef warn fill:#fff4e5,stroke:#9a6700,color:#4d3300;
    classDef step fill:#f6f8fa,stroke:#57606a,color:#1f2328;
    classDef rescue fill:#ddf4ff,stroke:#0969da,color:#0a3069;
    class OK good;
    class N warn;
    class A,R,T,F step;
    class X rescue;
```
A run passes only at 8.5/10 or higher with no blocking defect. `quality_rounds` sets how many tries it gets. If the team stalls, a rescue team finishes the work. Time limits can stop a run. Accepted and returned outcomes update the journal and `result.json`. The MCP task completes only at a final state. See [docs/STATUS.md](docs/STATUS.md) for the status vocabulary.

**Skills that learn:** CrewForeman saves tasks that fit no existing skill. When a task recurs, it drafts a new checklist and has the reviewer check it against the 8.5/10 bar. A new skill is promoted only after five uses and at least three scored runs. Its median score must stay within 0.3 of the baseline. Its blocking-defect rate may rise by at most 0.05. It may lose at most one comparison against the baseline. `auto_skills` = `auto` | `propose` | `off` (see [docs/skills.md](docs/skills.md)).
## Convergence
Recorded runs, not a benchmark. One representative job (P3.2), round by round in its accepted run. Round 1 is a full review; round 2 re-checks what was flagged and the new changes.

| Round | What the reviewer checked | Review length | Score | Blocking defects |
|---:|---|---|---:|---:|
| 1 | Full review | Long | 7.5 | 1 |
| 2 | Flagged defects + new changes | Shorter | 8.5 | 0 |

Later rounds re-check only what was flagged, using test results the connector signed itself.

## Install
Python 3.9 or newer; no runtime dependencies. The separately installed Harness (`dsh`) is required. Set `DSH_BIN` when it is not at the platform default.
```sh
python3 -m pip install .
crewforeman-mcp --help
```
Add the server to the client's MCP configuration:
```json
{"mcpServers": {"crewforeman-mcp": {"command": "crewforeman-mcp",
  "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY",
  "DSH_BIN": "/absolute/path/to/dsh"}}}}
```
Call `harness_deepseek` with a prompt and a workspace. A long run may return a job id to poll with `deepseek_job_status`.
## Guarantees
- It runs locally over standard input/output (stdio) using JSON-RPC; there is no hosted service or network listener.
- Team size and exact waves follow the Harness child limit. One whole-job budget covers time and estimated cost for every phase.
- Every Harness run is saved as Markdown and JSON with token usage, estimated cost and the review trail. Resume a killed run with `resume_deepseek`.
- Blocking review defects come with an ordered fix list before the next review.
- If the reviewer's service fails, a backup reviewer from another vendor takes over, and a quota error names that reviewer's vendor and model, never DeepSeek.
- Slices that share a file in the same wave are reported as overlap warnings, and waste-aware sizing nudges a high-discard team smaller.
- Before any paid review, a deterministic gate runs a secret scan, lint and tests; a failure is revised without a reviewer call and ends `gate_failed` when its cap is exhausted.
- The builder writes `PLAN.json` before the first edit; the connector compares it with the actual diff and warns by default or blocks on mismatches.
- Bounded, decaying failure notes for each task type are added to the next builder prompt; only fixed defect categories and opaque ids are stored.
- A disjoint queued job (declared write-set) may start during another job's review in its own worktree; patches are installed only after `git apply --check` proves the whole set applies.
- The builder model is configurable per job and per slice, teams may mix models and run competing candidates, and the default stays DeepSeek.
- Cost and score are tracked per model for team sizing and failure notes; a backup builder may take over under its own cost ceiling after credit, auth, 5xx or repeated stream-drop failures.
- A reviewer must come from a different vendor than every builder; if none is available the job asks once and labels a user-chosen same-vendor review, and the gate skips lint and tests when no product code changed while tiny single-file jobs may omit PLAN.json.
## Limits
- **macOS:** Seatbelt; the boundary suite proves create, modify, truncate, rename and delete outside the workspace, plus symlink and `..` escapes, are refused; reads and network are unrestricted.
- **Linux:** Landlock ABI 3+; the same file-write claim is proven on `ubuntu-24.04`, ABI 1-2 is degraded, `os_sandbox=require` refuses with no boundary and `auto` prints `UNSANDBOXED`; network is unrestricted.
- **Windows:** write-restricted token plus workspace/temp ACE; the suite requires create, modify and truncate denial; delete/rename is claimed only if the boundary job proves it, else `delete_outside=unverified`; network/UNC writes are not blocked and no no-admin claim is made.
- **Reviewer:** the reviewer is a model; its score is judgement, not proof of correctness.
## Documentation
| Page | Contents |
|---|---|
| [Install](docs/INSTALL.md) | Requirements, install and client setup |
| [Reviewer](docs/REVIEWER.md) | Using a reviewer from another vendor |
| [Teams](docs/TEAMS.md) | How the agent team works |
| [Operations](docs/OPERATIONS.md) | Configuration, safety, budgets, traces, retention |
| [Skills](docs/skills.md) | Registry, detection, lifecycle, automatic creation |
| [Status](docs/STATUS.md) | Run status vocabulary and precedence |
| [Clients](docs/CLIENTS.md) | Client configuration examples |
| [Limitations](docs/LIMITATIONS.md) | Known limitations and failure modes |

## License
MIT. See [LICENSE](LICENSE).
