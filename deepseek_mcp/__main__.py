"""Allow ``python -m deepseek_mcp`` as an alternative to the entry script."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
