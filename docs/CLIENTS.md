# Connecting MCP clients

The server is a standard **stdio** MCP server. Any client that launches a
subprocess and speaks newline-delimited JSON-RPC 2.0 can use it. There is no
network listener and no vendor lock-in.

Two values appear in every snippet:

- `python3` — replace it with the interpreter where this package is installed
  (Windows often uses `py`; a virtual environment may need its full path).
- `YOUR_DEEPSEEK_API_KEY` — placeholder for your own `DEEPSEEK_API_KEY`
  value. Prefer an environment variable or your OS keychain; never commit a
  real key to a shared config file.

The server reads `DEEPSEEK_API_KEY` from its environment. An optional
`--config PATH` argument selects a JSON config file. If your client supports
environment variables, setting `DEEPSEEK_API_KEY` there is enough; otherwise
add it to the `env` object shown below.

## Native command

Version 3.9.0 installs the `crewforeman-mcp` command and registers the MCP
server name `crewforeman-mcp`. Use this form for a new configuration:

```json
{
  "mcpServers": {
    "crewforeman-mcp": {
      "command": "crewforeman-mcp",
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Existing configurations that use the `deepseek` key, the legacy
`deepseek-harness-mcp` command or `python3 -m deepseek_mcp` keep working; the
import module and entry-point aliases are unchanged. The remaining examples
use the legacy form so an existing setup can be compared side by side.

## Claude Desktop

Config file: `claude_desktop_config.json` in the Claude Desktop application
support directory for your OS.

```json
{
  "mcpServers": {
    "deepseek": {
      "command": "python3",
      "args": ["-m", "deepseek_mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Quit Claude Desktop completely and reopen it. Then ask: “Use the deepseek
tools to list my skills.”

## Claude Code

```bash
claude mcp add deepseek \
  --env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY \
  -- python3 -m deepseek_mcp
```

Check it with `claude mcp list`. To include an explicit config file, append
`--config /path/to/config.json` after `deepseek_mcp`.

## Cursor

Project config: `.cursor/mcp.json`; global config: the equivalent Cursor
config directory.

```json
{
  "mcpServers": {
    "deepseek": {
      "command": "python3",
      "args": ["-m", "deepseek_mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Reload the window (or restart Cursor) and confirm the server shows a green
status in the MCP panel.

## VS Code

Workspace config: `.vscode/mcp.json`. VS Code uses a `servers` object and a
`type` field.

```json
{
  "servers": {
    "deepseek": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "deepseek_mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Run **MCP: List Servers** from the command palette, or start the server from
the MCP panel.

## Windsurf and Cline

Both use the familiar `mcpServers` shape. Use Windsurf's
`mcp_config.json` or Cline's `cline_mcp_settings.json` in the client's
application-support/config directory.

```json
{
  "mcpServers": {
    "deepseek": {
      "command": "python3",
      "args": ["-m", "deepseek_mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Restart the client or use its MCP refresh action after saving.

## Codex CLI

Config file: `~/.codex/config.toml` (TOML).

```toml
[mcp_servers.deepseek]
command = "python3"
args = ["-m", "deepseek_mcp"]
env = { DEEPSEEK_API_KEY = "YOUR_DEEPSEEK_API_KEY" }
```

Run `codex mcp list` to confirm registration.

## Gemini CLI

Register the server from a shell:

```bash
gemini mcp add \
  --env DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY \
  deepseek python3 -m deepseek_mcp
```

If you prefer the settings file, the same `mcpServers` JSON object shown for
Claude Desktop is accepted in Gemini CLI's settings file. Run
`gemini mcp list` to confirm.

## Generic stdio JSON

For any client that accepts a command plus arguments:

```json
{
  "mcpServers": {
    "deepseek": {
      "command": "python3",
      "args": ["-m", "deepseek_mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"
      }
    }
  }
}
```

Equivalent command line:

```bash
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY python3 -m deepseek_mcp
```

## Verify without a client

The bundled smoke test speaks the protocol directly, so it isolates
configuration problems from client problems:

```bash
python3 scripts/smoke_mcp.py
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
`pkill` + command lookup/`xdg-open` on Linux. It validates the application
name (max 60 characters) and wait time (0–60 seconds) and returns an
`unsupported: ...` sentence instead of raising when the platform or
application cannot be handled. Browser-based clients may need a page reload
even after the process restarts.
