#!/bin/sh
# Offline clean-install verification for crewforeman-mcp.
#
# Usage: scripts/clean_install.sh /absolute/temp/dir
#
# Everything is written under the caller-supplied temp directory; the source
# tree is only read.  Runs with the stock Python 3.9 venv and PIP_NO_INDEX=1,
# so it proves plain `pip install .` works with no network and no setuptools
# in the PEP 517 build environment.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /absolute/temp/dir" >&2
    exit 2
fi

V=$1
case "$V" in
    /*) ;;
    *) echo "temp dir must be absolute: $V" >&2; exit 2 ;;
esac

mkdir -p "$V"
V=$(cd "$V" && pwd -P)
HERE=$(cd "$(dirname "$0")/.." && pwd -P)

rm -rf "$V/venv" "$V/sdist" "$V/scratch" "$V/check"
mkdir -p "$V/sdist" "$V/scratch"

echo "== python and pip =="
python3 --version
python3 -m pip --version

echo "== create venv =="
python3 -m venv "$V/venv"

export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

echo "== offline pip install . =="
"$V/venv/bin/python" -m pip install --no-cache-dir "$HERE"

echo "== installed version =="
"$V/venv/bin/python" -c 'import deepseek_mcp; print("deepseek_mcp", deepseek_mcp.__version__)'

echo "== console scripts =="
"$V/venv/bin/crewforeman-mcp" --help
"$V/venv/bin/deepseek-harness-mcp" --help
"$V/venv/bin/deepseek-harness-mcp" --version
"$V/venv/bin/deepseek-mcp" --version

echo "== full suite against the installed copy =="
# Run the import from $V, not from the caller's dev tree.  With cwd=dev, the
# empty sys.path[0] makes ``import deepseek_mcp`` resolve to the *source* tree,
# so the "installed copy" proof would silently test the working tree instead of
# the wheel that pip just installed.  The assertion below makes that impossible.
(cd "$V" && "$V/venv/bin/python" - "$V/check" <<'PY'
import pathlib
import shutil
import sys

import deepseek_mcp

destination = pathlib.Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
source = pathlib.Path(deepseek_mcp.__file__).resolve().parent
venv = pathlib.Path(sys.prefix).resolve()
if venv not in source.parents:
    raise SystemExit(
        "refusing to copy a non-installed package: %s (venv %s)" % (source, venv))
shutil.copytree(
    source, destination / "deepseek_mcp",
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
)
print("copied installed package from %s" % source)
PY
)
for item in tests docs skills scripts bench; do
    cp -R "$HERE/$item" "$V/check/"
done
for item in deepseek_mcp.py README.md CHANGELOG.md LICENSE config.example.json \
            pyproject.toml build_backend.py setup.py setup.cfg MANIFEST.in; do
    cp "$HERE/$item" "$V/check/"
done
(
    cd "$V/check"
    "$V/venv/bin/python" -c 'import deepseek_mcp; print("test import:", deepseek_mcp.__file__)'
    "$V/venv/bin/python" -m unittest discover -s tests
)

echo "== build sdist through the in-tree backend =="
(cd "$HERE" && "$V/venv/bin/python" -c \
    'import build_backend, sys; print(build_backend.build_sdist(sys.argv[1]))' \
    "$V/sdist")

SDIST=$(find "$V/sdist" -maxdepth 1 -name '*.tar.gz' -print | head -n 1)
if [ -z "$SDIST" ]; then
    echo "no sdist produced" >&2
    exit 1
fi
echo "sdist: $(basename "$SDIST")"

echo "== extract sdist into scratch =="
tar -xzf "$SDIST" -C "$V/scratch"

echo "== sdist top-level entries =="
find "$V/scratch" -mindepth 1 -maxdepth 1 -print

echo "== unexpected scratch/build dirs (must be empty) =="
STRAY=$(find "$V/scratch" -type d \( -name build -o -name dist \
    -o -name '*.egg-info' -o -name __pycache__ -o -name 'wave*' \) -print)
if [ -n "$STRAY" ]; then
    echo "$STRAY"
    echo "unexpected scratch directories in sdist" >&2
    exit 1
fi

TOPLEVEL=$(find "$V/scratch" -mindepth 1 -maxdepth 1 -type d -print | wc -l | tr -d ' ')
if [ "$TOPLEVEL" -ne 1 ]; then
    echo "expected exactly one top-level sdist directory, found $TOPLEVEL" >&2
    exit 1
fi

echo "OK: offline clean install, entry points, and sdist verified under $V"
