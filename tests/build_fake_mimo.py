#!/usr/bin/env python3
"""Compile the test-only fake mimo launcher.

Thin wrapper over ``tests/build_fake_dsh.py``: the same tiny C launcher works
for any Python script (paths are baked in at build time), so this module just
re-exposes it under the fake-mimo name and keeps a compatible CLI::

    python3 tests/build_fake_mimo.py [script]

A missing C compiler is fatal (same contract as the fake dsh build).
"""
import sys
from pathlib import Path

try:
    from . import build_fake_dsh
except ImportError:  # tests run as top-level modules
    import build_fake_dsh

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Shared launcher source; FAKE_DSH_PYTHON / FAKE_DSH_SCRIPT are baked per build.
SOURCE = build_fake_dsh.SOURCE


def build_launcher(script, out=None):
    """Compile the launcher that execs *script*; returns the launcher Path."""
    return build_fake_dsh.build_launcher(script, out=out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    script = Path(argv[0]) if argv else ROOT / "artifacts" / "fake-mimo" / "fake_mimo.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    if not script.exists():
        script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    launcher = build_launcher(script)
    print(launcher)
    return 0


if __name__ == "__main__":
    sys.exit(main())
