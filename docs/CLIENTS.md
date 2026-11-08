# Connecting MCP clients

`crewforeman-mcp` 3.12.0 is a local **stdio** MCP server. The client starts the
process and speaks newline-delimited JSON-RPC 2.0 over stdin/stdout; there is no
network listener.

Two placeholders appear in the snippets:

- `/path/to/crewforeman-mcp` — absolute path to the installed console script.
  Replace it everywhere. If the command is on your `PATH`, the bare
  `crewforeman-mcp` also works; on Windows the script may be
  `crewforeman-mcp.exe`.
- `YOUR_DEEPSEEK_API_KEY` — your own `DEEPSEEK_API_KEY` value. Prefer an
  environment variable or your OS keychain; never commit a real key to a
  shared config file.

The server reads `DEEPSEEK_API_KEY` from its environment. An optional
`--config PATH` argument selects a JSON config file; put it in the client's
argument list, for example `["--config", "/path/to/config.json"]`.

Before configuring a client, check the command itself:

```bash
/path/to/crewforeman-mcp --version
```

It prints `crewforeman-mcp 3.12.0`. If that fails, fix the install first
(see [INSTALL.md](INSTALL.md)); the client snippets below cannot repair a
missing executable.

## Native command

Version 3.12.0 installs the `crewforeman-mcp` command and the shorter alias
`deepseek-mcp`. A client that accepts the common `mcpServers` shape can use
this minimal entry:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Existing configurations that use the `deepseek` server name, the
`deepseek-harness-mcp` command or `python3 -m deepseek_mcp` keep working; the
import module and entry-point aliases are unchanged. Clients differ in the
wrapper key (`mcpServers` or `servers`) and in where the file lives.

## Cursor

**Config.** Project-specific: `.cursor/mcp.json` in the repository. Global:
`~/.cursor/mcp.json` in your home directory. Cursor merges both, and the
project entry wins when the same server name appears in both. Cursor documents
the home-relative path for all platforms; it does not publish separate
macOS/Windows/Linux paths.

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "type": "stdio",
      "command": "/path/to/crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

**Confirm.** Open **Customize > MCPs**. `crewforeman-mcp` should be listed with
its toggle on, and its tools should appear in the tools list at the top of the
chat. For startup errors, open **Output > MCP Logs** and look for connection
errors.

**Pitfall.** Cursor requires `command` to be on the PATH it sees or to be a
full path. A bare `crewforeman-mcp` fails when the console script's directory
is not on that PATH; the absolute `/path/to/crewforeman-mcp` form avoids it.

Official docs: [Cursor MCP reference](https://cursor.com/docs/mcp) and
[Cursor MCP help](https://cursor.com/help/customization/mcp).

## VS Code with GitHub Copilot

**Config.** Workspace: `.vscode/mcp.json`. User profile: run **MCP: Open User
Configuration** from the Command Palette to open the profile `mcp.json`. The MCP
documentation does not give a fixed filesystem path for that file, so open it
through the command rather than constructing one. The VS Code format uses a
top-level `servers` object:

```json
{
  "servers": {
    "crewforeman-mcp": {
      "type": "stdio",
      "command": "/path/to/crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Copilot agent sessions read the portable format directly: put the same entry
under `mcpServers` in `.mcp.json` at the workspace root, or in
`$COPILOT_HOME/mcp-config.json` (default `~/.copilot/mcp-config.json`). VS Code
forwards normal `servers` entries to a Copilot session, but not entries that
require interactive `${input:...}` values. The two top-level keys are not
interchangeable.

**Confirm.** Run **MCP: List Servers** from the Command Palette: the server
should be listed with start, restart and **Show Output** actions. **Chat: Open
Customizations > MCP Servers** shows its enabled state. Then ask the agent to
call one tool and expand the tool call to check the returned output.

**Pitfall.** `.vscode/mcp.json` requires `servers`; a portable block pasted
with `mcpServers` is not read as a server list, so the server never appears in
the list. Rename the key when moving a snippet between the two formats.

Official docs: [Add MCP servers in VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
and the [MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

## Claude Desktop

**Config.** The JSON file is `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`, or
  `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json` when
  `XDG_CONFIG_HOME` is set.

Open it from **Settings > Developer > Edit Config**; Claude Desktop creates it
if it does not exist. Use a top-level `mcpServers` object:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

A stdio entry needs only `command` and the optional `env`; do not add a `url`
or `type`. The optional config file goes in `args`: `["--config",
"/path/to/config.json"]`.

**Confirm.** Fully quit and reopen Claude Desktop. Click the **+** button at
the bottom of the chat box, open **Connectors > Manage connectors**, and select
`crewforeman-mcp` to inspect its tools. Newer builds also list local servers
under **Settings > Desktop app > Extensions > Local MCP servers**.

**Pitfall.** Closing the window does not reload the configuration; Claude
Desktop reads `claude_desktop_config.json` at startup. Quit the application
completely from its own Quit/Exit command (on macOS, `Cmd+Q` also works), then
reopen it.

One-click `.mcpb` Desktop Extensions are the other install route; this section
covers the manual JSON developer configuration, which remains supported.
Official docs: [MCP local-server quickstart](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers)
and [Claude Desktop local MCP servers](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).

## Claude Code

**Config.** Project scope: `.mcp.json` at the project root; commit it to share
the server with the team. Local scope (the default for `claude mcp add`) and
user scope both live in `~/.claude.json`: local entries are stored under the
current project's path, user entries at the top level.

Add the connector over stdio:

```bash
claude mcp add --transport stdio crewforeman-mcp \
  --env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY \
  -- /path/to/crewforeman-mcp
```

Add `--scope project` to write `.mcp.json`, or `--scope user` to make the
server available in every project. The `--` is required: Claude Code's own
options go before it, and everything after it is passed to the server.

For a team-shared `.mcp.json`, use:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Claude Code reads an entry with no `type` as stdio, so `type` is not needed.

**Confirm.** `claude mcp list` prints a health status per server (`Connected`
when healthy); `claude mcp get crewforeman-mcp` shows its scope, command and
environment. Inside a session, `/mcp` shows each server's status and tools.

**Pitfall.** A project-scoped `.mcp.json` server shows `Pending approval (run
claude to approve)` in `claude mcp list` and `claude mcp get` until you start
`claude` in that folder, accept the workspace-trust dialog and approve it.
Repository-committed approvals are ignored in an untrusted folder, so the
entry can look configured while it never connects.

Official docs: [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## OpenAI Codex CLI

**Config.** User-wide: `~/.codex/config.toml`. Project-scoped:
`.codex/config.toml`, read only in projects Codex trusts. Add a server table:

```toml
[mcp_servers.crewforeman-mcp]
command = "/path/to/crewforeman-mcp"

[mcp_servers.crewforeman-mcp.env]
DEEPSEEK_API_KEY = "YOUR_DEEPSEEK_API_KEY"
```

The same entry can be added from the CLI:

```bash
codex mcp add crewforeman-mcp \
  --env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY \
  -- /path/to/crewforeman-mcp
```

`command` starts the local stdio server and `env` sets its environment. The
optional `args`, `env_vars` and `cwd` fields are documented; use `args =
["--config", "/path/to/config.json"]` only if you rely on the connector's
optional JSON config file. `env_vars` forwards named variables from Codex's
own environment instead of setting literal values.

**Confirm.** `codex mcp list` shows the configured server. In the `codex` TUI,
`/mcp` lists active MCP servers. A successful connector tool call is the final
proof that the command and key work.

**Pitfall.** A project-scoped `.codex/config.toml` is ignored unless the
project is trusted, so a correctly written server can be absent from
`codex mcp list` and `/mcp`. Put the table in `~/.codex/config.toml` for a
user-wide setup.

Official docs: [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp) and the
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Other clients

Cline and Gemini CLI accept the same `mcpServers` shape (Cline reads its MCP
settings file; Gemini CLI reads `settings.json` and also has a `gemini mcp add`
command). Use the generic entry below and check the client's own documentation
for the current file location and menu names, which change between releases.

## Generic stdio JSON

For any client that accepts a command plus arguments:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Equivalent command line:

```bash
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY /path/to/crewforeman-mcp
```

## Verify without a client

The bundled smoke test speaks the protocol directly, so it isolates
configuration problems from client problems:

```bash
python3 scripts/smoke_mcp.py --command /path/to/crewforeman-mcp
python3 scripts/smoke_mcp.py --timeout 30 --config /path/to/config.json
```

It sends `initialize` (protocol `2025-11-25`), `notifications/initialized`,
`tools/list` and `tools/call` for `list_skills`, prints one `PASS` line per
check and exits non-zero on the first failure. Use
`--command python3 -m deepseek_mcp` (or `--command ... -- --config ...`) to
test a different interpreter or entry point.

## Restarting a desktop client

`clients.restart_client_app` can quit and reopen a named application:
`osascript` + `open -a` on macOS, `taskkill` + `start` on Windows, and
exact-name `pkill -x`/`pgrep -x` + command lookup/`xdg-open` on Linux. The
detached helper owns the close, reopen and verification so the reopen
survives the client exiting. On macOS the helper runs the authoritative
`osascript -e 'application "Claude" is running'` and a corroborating
`process_check` of `pgrep -a -x <App>` in the same attempt, and records both
results in the status JSON. `osascript` alone decides whether the attempt is
`reopened`; `pgrep` never overrides a failed or false `osascript` result.
`-a` includes ancestors, so a connector started from inside Claude's process
tree can see Claude where plain `pgrep -x Claude` cannot. Ancestor PIDs are
excluded from the pgrep corroboration, and if the ancestor walk itself fails
(for example `/bin/ps` is denied by the sandbox) the check is recorded as
inconclusive (`process_check_ancestry_known: false`) rather than treating an
empty ancestor set as fresh evidence. On Linux, verification also
checks the reopened process's executable path, so a same-named helper or unrelated
process cannot make the restart look successful. The call validates the
application name (max 60 characters) and wait time (0–60 seconds) and
returns an `unsupported: ...` sentence instead of raising when the
platform or application cannot be handled. Browser-based clients may need a page reload
even after the process restarts.

If the standalone `restart_claude.sh` reports "not running" while Claude is
open, its `is_running()` test uses plain `pgrep -x`; change that one line to
`pgrep -a -x "$PROC_NAME"` (the script is outside this package and is not
edited by the connector).


## Model routing environment (new in 3.12.0)

The builder and reviewer can be selected without editing a wrapper. Keep keys
in the server environment and name them in the client config:

```json
{
  "env": {
    "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY": "YOUR_ANTHROPIC_API_KEY",
    "DSH_BUILDER_MODEL": "claude-sonnet-5-5",
    "DSH_BUILDER_PROVIDER": "anthropic",
    "DSH_REVIEWER_MODEL": "kimi-k3.1-pro",
    "DSH_REVIEWER_PROVIDER": "moonshot"
  }
}
```

`DSH_BUILDER_MODEL`/`DSH_BUILDER_PROVIDER` and
`DSH_REVIEWER_MODEL`/`DSH_REVIEWER_PROVIDER` are optional. The default remains
DeepSeek. A per-call `builder_model`, `builder_provider`, `reviewer_model` or
`reviewer_provider` overrides the environment for that job only. Do not put a
key value in `config.json`; the connector stores env-var names only.
