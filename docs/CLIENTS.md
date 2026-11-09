# Connecting MCP clients

`crewforeman-mcp` 3.13.1 is a local **stdio** MCP server. The client starts the
process and speaks newline-delimited JSON-RPC 2.0 over stdin/stdout; there is no
network listener.

Placeholders in the snippets:

- `/path/to/crewforeman-mcp` - absolute path to the installed console script.
  Replace it everywhere. A bare `crewforeman-mcp` works when the command is on
  `PATH`; on Windows the script may be `crewforeman-mcp.exe`.
- `YOUR_DEEPSEEK_API_KEY` - the default DeepSeek key placeholder.
- `YOUR_ANTHROPIC_API_KEY`, `YOUR_MOONSHOT_API_KEY` (or `YOUR_KIMI_API_KEY`) -
  optional, only for those vendors.

3.13.1 looks each key up by variable **name**: first the connector's process
environment is checked, then the Harness credentials file
(`~/.dsh/.credentials.yaml` by default). If your jobs run through the Harness,
its credentials file normally already holds the keys, so the `env` entries in
the snippets can be omitted. Otherwise put the variables in the `env` block or
export them in the shell that starts the client. Never commit a real key to a
shared config file. Model selection is covered in [INSTALL.md](INSTALL.md).

## Native command

3.13.1 installs the `crewforeman-mcp` console script and the shorter alias
`deepseek-mcp`. Check the command before configuring a client:

```bash
/path/to/crewforeman-mcp --version
```

It prints `crewforeman-mcp 3.13.1`. If that fails, fix the install first
(see [INSTALL.md](INSTALL.md)); a client snippet cannot repair a missing
executable. A client that accepts the common `mcpServers` shape can use this
minimal entry:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
    }
  }
}
```

Existing entries that use the `deepseek` server name, the
`deepseek-harness-mcp` command or `python3 -m deepseek_mcp` keep working.
Clients differ in the wrapper key (`mcpServers` or `servers`) and in where the
file lives.

## Cursor

**Config.** Project: `.cursor/mcp.json` in the repository. Global:
`~/.cursor/mcp.json` (the same home-relative file on macOS, Linux and Windows;
Cursor publishes no OS-specific variants). Both files are merged, and the
project entry wins when the same server name appears in both. `type: "stdio"`
is required for a local server.

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "type": "stdio",
      "command": "/path/to/crewforeman-mcp",
      "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
    }
  }
}
```

**Env.** Put `DEEPSEEK_API_KEY` in the `env` object; add `ANTHROPIC_API_KEY` or
`MOONSHOT_API_KEY` only if those vendors are used. Harness users can omit these
entries because 3.13.1 reads the names from the Harness credentials file.

**Confirm.** Open **Customize** in the sidebar and check the `crewforeman-mcp`
toggle; its tools appear under **Available Tools**. Startup errors are in
**Output > MCP Logs** (`Cmd+Shift+U`).

**Pitfall.** `command` must be on the PATH Cursor sees or be a full path; a bare
`crewforeman-mcp` fails when the console-script directory is not on that PATH.

Official docs: [Cursor MCP reference](https://cursor.com/docs/mcp) and
[Cursor MCP help](https://cursor.com/help/customization/mcp).

## VS Code with GitHub Copilot

**Config.** Workspace: `.vscode/mcp.json`, with a top-level `servers` object
(the same relative path on macOS, Linux and Windows). User profile: run
**MCP: Open User Configuration** from the Command Palette; the vendor publishes
no fixed filesystem path for that file, so open it through the command rather
than constructing one. `type: "stdio"` is required.

```json
{
  "servers": {
    "crewforeman-mcp": {
      "type": "stdio",
      "command": "/path/to/crewforeman-mcp",
      "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
    }
  }
}
```

**Env.** Put `DEEPSEEK_API_KEY` in the `env` object; add `ANTHROPIC_API_KEY` or
`MOONSHOT_API_KEY` only if those vendors are used. Harness users can omit these
entries because 3.13.1 reads the names from the Harness credentials file.

**Confirm.** Run **MCP: List Servers**: the server appears with start, restart
and **Show Output** actions. **MCP: Show Installed Servers** confirms it is
installed; then ask the agent to call one tool and expand the returned output.

**Pitfall.** `.vscode/mcp.json` requires `servers`; a portable block pasted with
`mcpServers` is not read as a server list, so nothing appears in the list.
Copilot sessions read the separate portable `.mcp.json` / `mcp-config.json`
format with `mcpServers`; the two keys are not interchangeable.

Official docs: [MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

## Claude Desktop

**Config.** The file is `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json` (or under
  `$XDG_CONFIG_HOME` when it is set)
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Open it from **Settings > Developer > Edit Config**; Claude Desktop creates the
file if it does not exist. Use a top-level `mcpServers` object.

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
    }
  }
}
```

**Env.** Put `DEEPSEEK_API_KEY` in the `env` object; add `ANTHROPIC_API_KEY` or
`MOONSHOT_API_KEY` only if those vendors are used. Harness users can omit these
entries because 3.13.1 reads the names from the Harness credentials file.

**Confirm.** Fully quit and reopen Claude Desktop, then click the **+** button
at the bottom of the chat box and open **Connectors** (or **Manage
connectors**) to select `crewforeman-mcp` and inspect its tools. Newer builds
also list local servers under **Settings > Desktop app > Extensions > Local MCP
servers**.

**Pitfall.** Closing the window does not reload the configuration; Claude
Desktop reads the file at startup. Quit the application completely from its own
Quit/Exit command (on macOS, `Cmd+Q` also works), then reopen it.

Official docs: [MCP local-server quickstart](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers)
and [Claude Desktop local MCP servers](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).

## Claude Code

**Config.** Project scope: `.mcp.json` at the project root; commit it to share
the server with the team. Local scope (the default for `claude mcp add`) and
user scope live in `~/.claude.json`: macOS `~/.claude.json`, Linux
`~/.claude.json`, Windows `%USERPROFILE%\.claude.json`. Add `--scope project`
to write `.mcp.json`, or `--scope user` for every project.

```bash
claude mcp add --env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY \
  --transport stdio crewforeman-mcp \
  -- /path/to/crewforeman-mcp
```

The `--` is required: Claude Code's own options go before it, and everything
after it is passed to the server. Keep at least one other option between
`--env` and the server name; otherwise the CLI reads the name as another
`KEY=value` pair. For a team-shared `.mcp.json`:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
    }
  }
}
```

**Env.** Put `DEEPSEEK_API_KEY` in the `env` object (or pass `--env`); add
`ANTHROPIC_API_KEY` or `MOONSHOT_API_KEY` only if those vendors are used.
Harness users can omit these entries because 3.13.1 reads the names from the
Harness credentials file. Claude Code also expands `${VAR}` in `.mcp.json`, so
`${DEEPSEEK_API_KEY}` is an alternative to the literal placeholder.

**Confirm.** `claude mcp list` prints a health status per server (`Connected`
when healthy); `claude mcp get crewforeman-mcp` shows its scope, command and
environment. Inside a session, `/mcp` shows each server's status and tools.

**Pitfall.** A project-scoped `.mcp.json` server shows
`Pending approval (run claude to approve)` in `claude mcp list` and
`claude mcp get` until you start `claude` in that folder, accept the
workspace-trust dialog and approve it. Committed approvals are ignored in an
untrusted folder, so the entry can look configured while it never connects.

Official docs: [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## OpenAI Codex CLI

**Config.** User-wide: `~/.codex/config.toml` (on Windows,
`%USERPROFILE%\.codex\config.toml`). Project-scoped: `.codex/config.toml`,
read only in projects Codex trusts. Each server is a
`[mcp_servers.<name>]` table; a stdio entry uses `command` and has no `type`
key.

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

**Env.** Put `DEEPSEEK_API_KEY` in the `.env` table (or pass `--env`); add
`ANTHROPIC_API_KEY` or `MOONSHOT_API_KEY` only if those vendors are used.
Harness users can omit these entries because 3.13.1 reads the names from the
Harness credentials file. `env_vars = ["DEEPSEEK_API_KEY"]` forwards a variable
from Codex's own environment instead of writing a literal value.

**Confirm.** `codex mcp list` shows the configured server; `codex mcp --help`
lists the MCP subcommands. In the `codex` TUI, `/mcp` lists active MCP servers.
A successful connector tool call is the final proof.

**Pitfall.** A project-scoped `.codex/config.toml` is ignored unless the
project is trusted, so a correctly written table can be absent from
`codex mcp list` and `/mcp`. Put the table in `~/.codex/config.toml` for a
user-wide setup.

Official docs: [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp) and the
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Generic stdio JSON

For any client that accepts a command plus arguments, use the `mcpServers`
shape and check the client's own documentation for its file location and menu
names, which change between releases:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "/path/to/crewforeman-mcp",
      "env": {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
    }
  }
}
```

Equivalent command line:

```bash
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY /path/to/crewforeman-mcp
```

## Verify without a client

The bundled smoke test speaks the protocol directly, so it separates
configuration problems from client problems:

```bash
python3 scripts/smoke_mcp.py --command /path/to/crewforeman-mcp
python3 scripts/smoke_mcp.py --timeout 30 --config /path/to/config.json
```

It sends `initialize` (protocol `2025-11-25`), `notifications/initialized`,
`tools/list` and `tools/call` for `list_skills`, prints one `PASS` line per
check and exits non-zero on the first failure.
