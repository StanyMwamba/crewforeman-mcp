# Installing crewforeman-mcp 3.9.4

`crewforeman-mcp` is a Python 3.9+ package with **zero runtime
dependencies**. It ships a local MCP stdio server and five skill checklists
(`code`, `data`, `document`, `poster`, `web`) that are installed as data files
under `share/crewforeman-mcp/skills`.

There are three supported one-command forms. Run them from the repository
root (the directory containing `pyproject.toml`).

| Goal | One command |
| --- | --- |
| Run without installing (uv) | `uvx crewforeman-mcp` |
| Isolated user install (pipx) | `pipx install .` |
| Install into the active interpreter | `python -m pip install .` |

`uvx crewforeman-mcp` resolves the package from the configured index;
use `uvx --from . crewforeman-mcp` to run a local checkout before it is
published. After a `pipx` or `pip` install, the console scripts
`crewforeman-mcp` and the shorter alias `deepseek-mcp` are both on
`PATH` and can be launched with `--help`.

The project builds with a small in-tree, stdlib-only PEP 517 backend
(`build_backend.py`), declared in `pyproject.toml` with `requires = []`.
`pip install .` therefore needs no network, no setuptools and no build
dependencies, and it works with the stock Python 3.9 venv. `setup.py` and
`setup.cfg` remain as a legacy fallback for `--no-build-isolation` callers.
Editable installs (`pip install -e .`) are not supported by the in-tree
backend; install into a venv instead.

## macOS

Python 3.9 or newer is required (the system `python3` on recent macOS is
fine). Prefer an isolated install:

```sh
# Option A: no persistent install, fetch and run
uvx crewforeman-mcp --help

# Option B: isolated user-level install
brew install pipx
pipx ensurepath
pipx install .

# Option C: virtual environment, no changes outside the venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
crewforeman-mcp --help
```

Skills are data files under the environment prefix:
`<venv>/share/crewforeman-mcp/skills`. For a pipx install that is
`~/.local/share/pipx/venvs/crewforeman-mcp/share/crewforeman-mcp/skills`.
The source checkout keeps using `./skills` when run with
`python -m deepseek_mcp`.

## Linux

```sh
# Option A: ephemeral run with uv
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

On distributions with PEP 668 ("externally managed environment"), use
`pipx` or a venv; a bare `python3 -m pip install .` may refuse with
`error: externally-managed-environment`.

## Windows

Use PowerShell; `py -3` selects the launcher's Python 3.

```powershell
# Option A: ephemeral run with uv
uvx crewforeman-mcp

# Option B: isolated user install
py -3 -m pip install --user pipx
py -3 -m pipx ensurepath
pipx install .

# Option C: virtual environment
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
crewforeman-mcp --help
```

Skills install to `<venv>\share\crewforeman-mcp\skills`; for pipx the
environment lives under `%LOCALAPPDATA%\pipx\venvs\crewforeman-mcp`. Long paths are not an
issue; no compiler or Visual C++ build tools are needed because every module
is pure Python.

## Fresh-venv verification recipe

Run this from a checkout; pip builds and installs without touching the
network. Keep the current directory outside the checkout when importing the
installed package, so the source tree cannot shadow it:

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

The repository also ships `scripts/clean_install.sh <absolute-temp-dir>`,
which performs the offline install, checks both console scripts, copies the
installed package into a scratch tree (refusing to proceed unless the
import came from the venv) and runs the full test suite against that copy,
then builds and inspects the sdist. It removes its own `venv/`, `sdist/`,
`scratch/` and `check/` trees first, so it can be re-run in the same directory.

On Windows replace the last line with
`python -m pip show -f crewforeman-mcp | Select-String crewforeman-mcp/skills`.
Expected results: the version prints `3.9.4`, each `--help` exits 0, and the
file listing shows the five `.md` skill data files under
`share/crewforeman-mcp/skills`. If the files are present but a run says
skills are missing, set `DSH_SKILLS_DIR` to that directory (see docs/OPERATIONS.md).

## Uninstall

```sh
# pip / venv
python -m pip uninstall crewforeman-mcp

# pipx
pipx uninstall crewforeman-mcp

# uvx caches are disposable; clear one package's cache with
uv cache clean crewforeman-mcp
```

Uninstalling removes the console scripts and package files. Data files under
`share/crewforeman-mcp/skills` are removed by pip/pipx; a source checkout
is left untouched. Delete `DSH_SKILLS_DIR` if you set it by hand.

## Requirements and troubleshooting

- Python 3.9+; no third-party packages.
- `--help` is safe and never reads the API key or starts the server.
- Environment variables (`DEEPSEEK_API_KEY`, `DSH_*`) are documented in the
  repository README; none are needed to install or run `--help`.
- If `crewforeman-mcp` is "not found", the scripts directory is not on
  `PATH`: use `python -m deepseek_mcp` inside the venv, or run
  `pipx ensurepath` and open a new shell.

## Live end-to-end smoke

`scripts/smoke_live.py` is an **opt-in paid run**. It starts one real Harness
builder and its independent reviewer with a tiny prompt, then verifies that the
builder finished `ok`, the reviewer returned a `quality_score`, the review
status is `accepted`/`stopped`, and the running version matches the package
version. It is never part of the normal test suite and makes no network call
without `--yes`.

```bash
# from the source checkout, with the operator's real config/key
python3 scripts/smoke_live.py --yes --json
```

Useful options: `--workspace DIR` (otherwise a fresh directory under `TMPDIR`,
with a workspace fallback), `--config PATH`, and `--timeout SECONDS` (default
300). Without `--yes` the script exits 2 and prints the cost warning. The JSON
summary has `ok`, `version`, `builder_status`, `quality_score`,
`review_status`, `workspace` and `error`; exit code 0 means every check passed.
This costs real credit, so run it manually after install or upgrade, never in
CI. The same evidence is summarised in `docs/OPERATIONS.md`.
