# CrewForeman [![CI](https://github.com/StanyMwamba/crewforeman-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/StanyMwamba/crewforeman-mcp/actions/workflows/ci.yml)

CrewForeman 3.15.0 is a local stdio MCP server. It runs Harness on your machine, sizes a team, builds in waves, has an independent reviewer score the result, and saves a record of every run.

## Roles
| Role | Who | What it does |
|---|---|---|
| Builder | Harness, any model (default DeepSeek) | Sizes the team, builds in waves, revises on notes. |
| Independent reviewer | Configurable, any vendor (e.g. Kimi) | Scores 0–10 and lists blocking defects. |
| Client / coordinator | Your MCP client (e.g. Claude) | Sends the brief and reads the result. |
| MiMo builder | mimo, xiaomi/mimo-v2.6-pro | Builds and revises, like the DeepSeek builder. |
| MiMo reviewer | mimo, xiaomi/mimo-v2.6-pro | Scores 0–10 from a second vendor. |
| MiMo crosscheck crew | mimo, xiaomi/mimo-v2.6-pro | Cross-checks parts in crosscheck mode. |
| MiMo backup | mimo | Takes over when DeepSeek is quarantined or failing. |

**Using a reviewer from a different vendor means a model never grades its own work.**
Use a reviewer from any vendor - see [docs/REVIEWER.md](docs/REVIEWER.md). The lead sizes the team and merges the best candidates - see [docs/TEAMS.md](docs/TEAMS.md).
Opt-in `mode="crosscheck"` builds independent parts with one crew and cross-checks each part with another; the connector scores the gate - see [docs/CROSSCHECK.md](docs/CROSSCHECK.md). Jobs without a write set can run in parallel when the inferred sets are disjoint and confident - see [docs/PARALLEL.md](docs/PARALLEL.md).

## Method
CrewForeman creates one work item for each brief. If the same work is already running, it returns that run. Otherwise it sizes a team and plans waves, enforcing a deterministic gate (secrets, lint, tests) before any cross-vendor review.
```mermaid
flowchart LR
    A[Request<br/>idempotency_key] --> B{Admit + work_id<br/>active duplicate?}
    B -- yes --> R[Return existing run]
    B -- no --> D[PLAN.json + waves<br/>loop guard]
    D --> G1{Pre-review gate<br/>secrets · lint · tests}
    G1 -- fail --> G[Revise<br/>quality_rounds]
    G1 -- pass --> E[Cross-vendor review<br/>round 2+: delta only]
    E --> F{Score ≥ 8.5<br/>0 blocking?}
    F -- no --> G --> D
    F -- yes --> H[Journal + result<br/>MCP task completes]
```
## Outcomes
```mermaid
flowchart LR
    A[Team builds<br/>+ loop guard] -- done --> G{Gate:<br/>secrets · lint · tests}
    A -- stalls --> X[Rescue team<br/>finishes the work] --> G
    G -- cap hit --> GF([gate_failed])
    G -- pass --> R{Cross-vendor review<br/>score ≥ 8.5?}
    R -- yes --> OK([Accepted])
    R -- no --> T{Tries left?}
    T -- yes --> F[Fix with<br/>reviewer notes] --> G
    T -- no --> N([Returned<br/>below gate])
    classDef good fill:#e6f4ea,stroke:#1a7f37,color:#0f3d1e;
    classDef warn fill:#fff4e5,stroke:#9a6700,color:#4d3300;
    classDef step fill:#f6f8fa,stroke:#57606a,color:#1f2328;
    classDef rescue fill:#ddf4ff,stroke:#0969da,color:#0a3069;
    class OK good; class N,GF warn; class A,G,R,T,F step; class X rescue;
```
Runs update `result.json` and are saved locally. Terminal states update the MCP journal. See [docs/STATUS.md](docs/STATUS.md).

## Convergence
Recorded runs, not a benchmark. One representative job, round by round in its accepted run.

| Round | What the reviewer checked | Review length | Score | Blocking defects |
|---:|---|---|---:|---:|
| 1 | Full review | Long | 7.5 | 1 |
| 2 | Flagged defects + new changes | Shorter | 8.5 | 0 |

**Skills that learn:** CrewForeman saves tasks that fit no existing skill. When one recurs, it drafts a checklist, reviewed against the 8.5/10 bar, and promotes it only after five uses with a median within 0.3 of baseline and blocking-rate rise ≤0.05. `auto_skills` = `auto` | `propose` | `off` (see [docs/skills.md](docs/skills.md)).

## Install
Python 3.9+; separately installed Harness (`dsh`) required.
```sh
python3 -m pip install .
crewforeman-mcp --help
```
Add to client configuration:
```json
{"mcpServers": {"crewforeman-mcp": {"command": "crewforeman-mcp",
  "env": {"DEEPSEEK_API_KEY": "...", "DSH_BIN": "/path/to/dsh"}}}}
```

## MiMo Code worker
MiMo Code (`mimo`, v0.1.17) is the second-vendor worker. Check `mimo --version`, then run `mimo providers login` with a pay-as-you-go sk- key or a Token Plan tp- key and its own base URL. Build, review and crosscheck default to `xiaomi/mimo-v2.6-pro`; probes use `xiaomi/mimo-v2.6-flash`. Pick MiMo per job with `builder_provider`/`builder_model`, `reviewer_provider`/`reviewer_model` or `crosscheck_crew_provider`/`crosscheck_crew_model`; `mimo_bin` or `MIMO_BIN` names the binary.
Concurrency detects `workflow.maxConcurrentAgents`, else min(16, 2 x cores), uses the max, and backs off on 429 or limit errors. Permissions are allow rules passed in `MIMOCODE_CONFIG_CONTENT`: never `--yolo`, and skip-permissions only via the explicit `mimo_skip_permissions` opt-in. Serve/attach shares one server under a per-server password in `MIMOCODE_SERVER_PASSWORD`. Usage and cost are recorded per vendor, vendor-reported cost first; CI runs a fake `mimo` only.
Example: DeepSeek keeps building and MiMo reviews, so the reviewer is a different vendor.
```json
{"reviewer_provider": "mimo",
 "mimo_bin": "mimo", "mimo_default_model": "xiaomi/mimo-v2.6-pro"}
```
## Guarantees
- Local JSON-RPC; no hosted service or network listener.
- Whole-job budget covers time and estimated cost for all phases.
- Blocking review defects provide an ordered fix list.
- If the reviewer's service fails, a backup reviewer from another vendor takes over, and a quota error names that reviewer's vendor and model, never DeepSeek.
- Cross-vendor review; backup builder on auth/stream drop.
- Pre-review gate runs secrets, lint, tests; revision loops for failures.
- `PLAN.json` diff verification; deterministic loop guard catches no-change/ping-pong.
- Bounded failure notes decay into the next builder prompt; a disjoint queued job with a declared write-set installs its patch only after `git apply --check` passes.
- Every Harness run is saved as Markdown and JSON with token usage, estimated cost and the review trail.
## Limits
- **macOS:** Seatbelt; writes outside the workspace are denied, while reads and network are unrestricted.
- **Linux:** Landlock ABI 3+; `UNSANDBOXED` when no boundary, `require` refuses, ABI 1-2 degraded.
- **Windows:** write-restricted token plus workspace/temp ACE; `delete_outside=unverified` unless proven.
- **Reviewer & data:** Model score is judgement, not proof of correctness; run records and job files are plaintext, with no user authentication or encryption at rest.

## Documentation
| Page | Contents | Page | Contents |
|---|---|---|---|
| [Install](docs/INSTALL.md) | Setup | [Teams](docs/TEAMS.md) | Agent team |
| [Reviewer](docs/REVIEWER.md) | Vendor choice | [Operations](docs/OPERATIONS.md) | Budgets |
| [Skills](docs/skills.md) | Registry | [Status](docs/STATUS.md) | Vocabulary |
| [Clients](docs/CLIENTS.md) | Examples | [Limitations](docs/LIMITATIONS.md) | Modes |

## License
MIT. See [LICENSE](LICENSE).
