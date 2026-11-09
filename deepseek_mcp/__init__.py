"""DeepSeek MCP server package (3.13.7).

Public entry point: ``deepseek_mcp.py`` in the repository root, or
``python -m deepseek_mcp``. Standard library only.
"""

from .config import SERVER_NAME, SERVER_VERSION

__all__ = ["SERVER_NAME", "SERVER_VERSION"]
__version__ = SERVER_VERSION
