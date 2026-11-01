#!/usr/bin/env python3
"""Thin entry point for the DeepSeek MCP server.

The original launch command keeps working:

    /usr/bin/python3 /path/to/deepseek_mcp.py

Optional flags:
    --tools all|mac     expose all tools (default) or only macOS tools
    --config PATH       config file (default ~/.config/deepseek-harness-mcp/config.json)
    --job SPEC          internal: run one detached background job

Python 3.9, standard library only.
"""

import sys

from deepseek_mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
