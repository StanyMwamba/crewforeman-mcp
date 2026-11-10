# Installing crewforeman-mcp 3.14.1

`crewforeman-mcp` is a Python 3.9+ package with **zero runtime dependencies**.
It ships a local MCP stdio server and five skill checklists (`code`, `data`,
`document`, `poster`, `web`) installed as data files under
`share/crewforeman-mcp/skills`.

3.13.0 adds the Linux and Windows OS sandboxes and the `os_sandbox` modes. The package name, console
scripts, skills layout and zero-dependency install contract are unchanged, so
there is no migration step. The 3.12.1 change is key lookup: a provider key is
resolved by the **name** of its variable, first from the connector's process
environment and then from the Harness credentials file. Existing `dsh_bin`
wrapper setups keep working.

## Requirements

- Python 3.9 or newer; the stock 3.9 `venv` is enough.
- Zero runtime dependencies. The in-tree, stdlib-only PEP 517 backend declares
  `requires = []`, so there is no network, setuptools, compiler or Visual C++
  build-tools requirement.
- `git` 2.5 or newer is optional. Pipelined jobs use it when the workspace is a
  git work tree; without git the connector falls back to serial jobs.
- Editable installs are unsupported: the in-tree backend does not implement
  `pip install -e .`. Install into a virtual environment instead.
- The five skill files are data, not Python packages. A source checkout run as
  `python -m deepseek_mcp` keeps using `./skills`.

## Install

Run these from the repository root (the directory containing `pyproject.toml`).

| Goal | One command |
| --- | --- |
| Run without installing (uv) | `uvx crewforeman-mcp` |
| Isolated user install (pipx) | `pipx install .` |
| Install into the active interpreter | `python -m pip install .` |

`uvx crewforeman-mcp` resolves the package from the configured index;
`uvx --from . crewforeman-mcp` runs a local checkout before it is published.
Pin this release for a reproducible install with
`uvx crewforeman-mcp==3.14.1`. After a `pipx` or `pip` install, both
`crewforeman-mcp` and the shorter alias `deepseek-mcp` are on `PATH`.

### macOS

```sh
# Option A: no persistent install
uvx crewforeman-mcp --help

# Option B: isolated user-level install
brew install pipx
pipx ensurepath
pipx install .

# Option C: virtual environment
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
crewforeman-mcp --help
```

Skills are data files under `<venv>/share/crewforeman-mcp/skills`; for a pipx
install they live inside the pipx environment for `crewforeman-mcp`.

### Linux

```sh
# Option A: ephemeral run
uvx crewforeman-mcp

# Option B: isolated user install
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install .

# Option C: virtual environment
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
crewforeman-mcp --help
```

On distributions with PEP 668 ("externally managed environment"), use `pipx`
or a venv; a bare `python3 -m pip install .` may refuse with
`error: externally-managed-environment`.

The Linux file-write boundary is Landlock, applied by an unprivileged launcher.
It needs Ubuntu 24.04 or newer with kernel 6.2+ (Landlock ABI 3) and enabled
Landlock support. There is no extra install step, no root and no `sudo`: the
launcher restricts itself and then executes `dsh`. Kernel ABI 1-2 runs in
degraded mode and does not claim truncate protection; `os_sandbox=require`
refuses to start instead. Network is not restricted. CI proves this boundary
on `ubuntu-24.04`: the workflow pins that runner because `ubuntu-latest` is
Ubuntu 26.04, where `actions/setup-python` has no Python 3.9 build.


### Windows

Use PowerShell; `py -3` selects the launcher's Python 3.

```powershell
uvx crewforeman-mcp
py -3 -m pip install --user pipx
py -3 -m pipx ensurepath
pipx install .
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
crewforeman-mcp --help
```

Skills install under `<venv>\share\crewforeman-mcp\skills`; pipx environments
live under `%LOCALAPPDATA%\pipx\venvs`. Every module is pure Python, so no
compiler or Visual C++ build tools are needed.

The Windows file-write boundary uses a write-restricted token with an ACE on
the workspace and temp directory. Run the connector and builders as a standard
user; this is the tested path. CI targets the GitHub-hosted `windows-2025` x64
runner, whose account is an Administrators member. The first real run
(42629362611) failed in the sandbox probe before the suite
(`AttributeError`, `logon_sid=None`, `admin=False`, `usable=false`); 3.13.3
fixed the probe and run 42648360100 proved it (`usable=true`,
`write_restricted=true` on both Pythons), but that run's suite was red and its
membership query failed (`WinError 1309`). 3.13.9 was green in run 42697229851, so 3.14.1
makes no new Windows CI boundary claim: the next GitHub run is the only Windows
proof. The probe records `admin` (token elevation) and `admin_member` (account
group) separately. The standard-user path is not proven by CI; run the Windows
11 VM script (WINDOWS_TEST.md in the VM test package) as a standard user to
prove it, and make no no-admin claim without that run. Delete/rename outside
remains `delete_outside=unverified` unless a boundary run proves it. UNC
workspaces are
unsupported (the ACL boundary is local-path only), and network/UNC writes are
not blocked. `os_sandbox=require` refuses to start when the token or ACL cannot
be created.


### Fresh-venv verification

Run this from a checkout. `PIP_NO_INDEX=1` proves the install needs no network
or build dependency; stay outside the checkout when importing, so the source
tree cannot shadow the installed package.

```sh
python3 -m venv /tmp/dsh-verify
. /tmp/dsh-verify/bin/activate          # Windows: .\dsh-verify\Scripts\activate
PIP_NO_INDEX=1 python -m pip install --no-cache-dir .
cd /tmp
python -c "import deepseek_mcp, deepseek_mcp.server; print(deepseek_mcp.__version__)"
crewforeman-mcp --help
deepseek-mcp --help
python -m deepseek_mcp --help
python -m pip show -f crewforeman-mcp | grep crewforeman-mcp/skills
```

Expected: the version prints `3.14.1`, each `--help` exits 0, and the file
listing shows the five `.md` skill files. On Windows replace the last line with
`python -m pip show -f crewforeman-mcp | Select-String crewforeman-mcp/skills`.
If the files exist but a run says skills are missing, set `DSH_SKILLS_DIR` to
that directory (see [OPERATIONS.md](OPERATIONS.md)).

### Uninstall

```sh
python -m pip uninstall crewforeman-mcp
pipx uninstall crewforeman-mcp
uv cache clean crewforeman-mcp
```

Uninstalling removes the console scripts, package files and the data files
under `share/crewforeman-mcp/skills`. A source checkout is untouched. Delete
`DSH_SKILLS_DIR` if you set it by hand.

## Keys

3.14.1 looks a provider key up by its variable **name**: first the connector's
process environment is checked, then the Harness credentials file
(`~/.dsh/.credentials.yaml` by default). `config.json` lists variable names,
never values.

Override the path with the `credentials_file` config key, or with
`CREWFOREMAN_CREDENTIALS_FILE` / `DSH_CREDENTIALS_FILE` in the environment; the
built-in default is `~/.dsh/.credentials.yaml`. Keep the file owner-only
(`chmod 600`): a file readable by group or others is skipped with a warning.
The reader accepts the flat `NAME: value` form and the nested `records` /
`refs` layouts.

```json
{"name": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}
```

That fragment is what a `providers[]` entry carries for the key; it is not a
key value and is safe to commit. The connector never writes a key to a run
record, checkpoint or trace, and the generated provider/model overlay carries
`apiKeyEnv` (a name) only.

- `DEEPSEEK_API_KEY` - the default DeepSeek provider; needed for DeepSeek
  builders or reviewers.
- `ANTHROPIC_API_KEY` - Anthropic (Claude) builders or reviewers.
- `MOONSHOT_API_KEY` - Kimi/Moonshot; some setups name it `KIMI_API_KEY`.
  Use whichever name your provider entry puts in `api_key_env`.
- `OPENAI_API_KEY` - OpenAI models.

Add only the variables for vendors you actually use. If you run jobs through
the Harness, its credentials file normally already holds them, so you can
omit these keys from the editor's MCP `env` block. Otherwise set them in that
block (see [CLIENTS.md](CLIENTS.md)) or export them in the shell that starts
the client:

```sh
export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
export ANTHROPIC_API_KEY="YOUR_ANTHROPIC_API_KEY"
```

**Never put a key value in `config.json`.** Use the variable name only. If a
provider entry names `api_key_env` and neither the environment nor the
credentials file defines it, the job fails with
`builder_provider_key_missing` before a paid call.

Legacy `dsh_bin` wrapper setups keep working unchanged: the wrapper loads its
own key, so no provider entry or connector-side key is needed for it.

## Choose models

Builders and the reviewer can each use any model that a configured provider
lists. Precedence, highest first: per-job `harness_deepseek` arguments, then
the `builder_*` / `reviewer_*` config keys, then `dsh_provider`/`dsh_model`
and the built-in default (`deepseek-official` / `deepseek-flash`). Empty values
inherit the next level.

| Key | Scope | Meaning |
| --- | --- | --- |
| `builder_model` | config, per-job | Parent builder model; empty inherits `dsh_model`. |
| `builder_provider` | config, per-job | Parent builder provider; empty inherits `dsh_provider`. |
| `reviewer_model` | config, per-job | Reviewer model; empty keeps the legacy wrapper path. |
| `reviewer_provider` | config, per-job | Provider for `reviewer_model`. |
| `slice_models` | config, per-job | Slice name to `"provider/model"` or `{"provider", "model"}`, capped by `builder_model_max_slices` (default 8). |
| `model_candidates` | per-job only | Slice name to 2..N model specs; at most 3 extra candidates per job. |
| `backup_builder_model` | config | Second builder model; empty disables. |
| `backup_builder_provider` | config | Optional provider for the backup builder; empty derives it. |
| `backup_builder_max_cost_usd` | config | Independent backup ceiling; default `1.0`, `0` disables. |
| `providers[]` | config | Provider entries with `name`, `vendor`, `base_url`, `api`, `api_key_env`, `models`. |

A model must appear in its provider's `models` list unless
`builder_allow_unlisted_models` is `true`. If the provider is omitted, it is
inferred only when exactly one configured provider lists that model; otherwise
set the provider explicitly. When `backup_builder_model` is set, it takes over
only for credit/budget, authentication, 5xx-after-retries or repeated stream
drops; it never switches after the main builder has written files, and it never
switches twice.

Example (a): default DeepSeek builders with an Anthropic reviewer.

```json
{
  "providers": [
    {
      "name": "anthropic",
      "vendor": "Anthropic",
      "base_url": "https://api.anthropic.com",
      "api": "anthropic-messages",
      "api_key_env": "ANTHROPIC_API_KEY",
      "models": ["claude-opus-4-1"]
    }
  ],
  "reviewer_provider": "anthropic",
  "reviewer_model": "claude-opus-4-1"
}
```

The builder keys are absent, so the parent builder stays on the default
DeepSeek target. `claude-opus-4-1` is an example model id: list a model that
your provider actually serves. Use the endpoint and API type your Harness
build expects for Anthropic; the values above are the ones the repository's
model-policy tests use.

Example (b): a per-job override passed as `harness_deepseek` arguments, not as
`config.json`.

```json
{
  "builder_provider": "anthropic",
  "builder_model": "claude-opus-4-1",
  "reviewer_provider": "deepseek-official",
  "reviewer_model": "deepseek-flash"
}
```

This one job builds on Anthropic and reviews on DeepSeek, using the Anthropic
provider entry from example (a); the config and later jobs are unchanged. The
same arguments object also accepts `slice_models` (a slice name to
`"provider/model"` or `{"provider", "model"}`) and `model_candidates` (a slice
name to 2..N model specs). Both trigger the model-plan path, so the builder
must write `MODEL_AUDIT.json`; a missing or ignored audit is `unverified`,
never `verified`.

## Reviewer from another vendor

A reviewer must be a different vendor from every builder model that may run for
the job: the parent, every slice, every competing candidate (including the
losers), and `backup_builder_model` when it is set. An unknown vendor is
fail-closed and is never treated as different.

1. Declare the vendor in `providers[]` (the entry names the key variable; it
   never contains the key):

```json
{
  "providers": [
    {
      "name": "anthropic",
      "vendor": "Anthropic",
      "base_url": "https://api.anthropic.com",
      "api": "anthropic-messages",
      "api_key_env": "ANTHROPIC_API_KEY",
      "models": ["claude-opus-4-1"]
    }
  ]
}
```

2. Point the reviewer at it, or use an ordered `reviewers[]` list:

```json
{
  "reviewer_provider": "anthropic",
  "reviewer_model": "claude-opus-4-1"
}
```

```json
{
  "reviewers": [
    {
      "name": "opus",
      "vendor": "Anthropic",
      "provider": "anthropic",
      "model": "claude-opus-4-1",
      "api_key_env": "ANTHROPIC_API_KEY"
    }
  ]
}
```

Each `reviewers[]` entry takes `name`, `vendor`, `provider`, `model`,
`api_key_env` (optional; the provider entry's name is used when omitted) and
`enabled`. `dsh_bin` and `patch` are operator-only legacy fields and are never
accepted as tool arguments. `reviewer_max_preflights` (default 8) caps how many
candidates are pinged.

The connector pings candidates in order and uses the first cross-vendor one
that answers `READY`. If none does, the job pauses with exactly one
`needs_input` question. Its options list the same-vendor models that answered
as `<vendor>/<model> (same vendor)`, then
`Do not review; return the build as unreviewed`. A same-vendor reviewer starts
only after you pick it; its header then says exactly
`same-vendor review (chosen by user)`. Set `reviewer_same_vendor_policy` to
`"never"` to remove the same-vendor options; the connector never chooses one
silently. Declining returns `ok_unreviewed` with no invented score. Resume the
paused job with `resume_deepseek` and the `option` reported by
`deepseek_job_status`.

When `reviewers` is empty, the legacy path still applies: `dsh_bin` is the
reviewer wrapper, `reviewer_vendor`/`reviewer_model` label it, and
`backup_dsh_bin` is the second candidate. A non-empty `reviewers` list makes
those legacy fields ignored, so move an existing wrapper into a `reviewers[]`
entry (or keep `reviewers` empty) when migrating.

## Troubleshooting

- `crewforeman-mcp: not found` - the scripts directory is not on `PATH`. Use
  `python -m deepseek_mcp` inside the venv, or run `pipx ensurepath` and open a
  new shell.
- `externally-managed-environment` - use `pipx` or a venv, not a bare pip
  install into the system interpreter.
- Wrong version - check `python -m pip show crewforeman-mcp` and clear a stale
  uvx cache with `uv cache clean crewforeman-mcp`.
- `builder_provider_key_missing` - `api_key_env` is unset in the connector
  process and not found in the Harness credentials file. Export it or add it to
  the MCP `env` block; Harness users normally already have it in the
  credentials file under that name.
- `builder_model_unlisted` - add the model to `providers[].models`, or set
  `builder_allow_unlisted_models` to `true` only for a trusted model.
- Reviewer preflight fails - check credit, auth and the key variable named in
  the error. A cross-vendor reviewer must differ from every builder model,
  including slices and the backup builder.

`--help` is safe: it never reads a key and never starts the server.

## Live end-to-end smoke

`scripts/smoke_live.py` is an **opt-in paid run**. It starts one real Harness
builder and its independent reviewer with a tiny prompt, then checks the
builder status, the reviewer score and the running version. It makes no network
call without `--yes`.

```bash
python3 scripts/smoke_live.py --yes --json
```

Useful options: `--workspace DIR`, `--config PATH`, `--timeout SECONDS`
(default 300). Without `--yes` the script exits 2 and prints the cost warning.
The JSON summary has `ok`, `version`, `builder_status`, `quality_score`,
`review_status`, `workspace` and `error`. This costs real credit; run it
manually after install or upgrade, never in CI.
