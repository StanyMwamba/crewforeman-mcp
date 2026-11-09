"""Tiny in-tree, stdlib-only PEP 517 build backend.

Stock offline Python 3.9 / pip 21.2.4 creates an isolated build environment
where setuptools is not installed.  ``pyproject.toml`` therefore declares
``requires = []`` and ``build-backend = "build_backend"`` with
``backend-path = ["."]``; this module supplies the four hooks pip needs.

The backend reads its identity from ``pyproject.toml`` (a deliberately tiny
regex parser) and refuses to build if the declared version does not match
``deepseek_mcp.config.SERVER_VERSION``.
"""

import base64
import gzip
import hashlib
import os
import re
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYPROJECT_PATH = ROOT / "pyproject.toml"
CONFIG_PATH = ROOT / "deepseek_mcp" / "config.py"


def _read_text(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _section(text, name):
    pattern = r"(?ms)^\[" + re.escape(name) + r"\][^\n]*\n(.*?)(?=^\[|\Z)"
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def _scalar(section_text, key, default=None):
    pattern = r'(?m)^\s*' + re.escape(key) + r'\s*=\s*"([^"]*)"'
    match = re.search(pattern, section_text)
    return match.group(1) if match else default


def _load_pyproject():
    text = _read_text(PYPROJECT_PATH)
    project = _section(text, "project")
    classifiers = []
    match = re.search(r"(?ms)^\s*classifiers\s*=\s*\[(.*?)\]", project)
    if match:
        classifiers = re.findall(r'"([^"]*)"', match.group(1))
    license_text = "MIT"
    match = re.search(
        r'(?m)^\s*license\s*=\s*\{\s*text\s*=\s*"([^"]*)"', project
    )
    if match:
        license_text = match.group(1)
    data = {
        "name": _scalar(project, "name"),
        "version": _scalar(project, "version"),
        "summary": _scalar(project, "description", ""),
        "requires-python": _scalar(project, "requires-python", ">=3.9"),
        "license": license_text,
        "classifiers": classifiers,
    }
    if not data["name"] or not data["version"]:
        raise RuntimeError(
            "pyproject.toml [project] must declare name and version"
        )
    return data


def _config_version():
    match = re.search(
        r'(?m)^SERVER_VERSION\s*=\s*"([^"]+)"', _read_text(CONFIG_PATH)
    )
    if not match:
        raise RuntimeError(
            "cannot find SERVER_VERSION in deepseek_mcp/config.py"
        )
    return match.group(1)


PYPROJECT = _load_pyproject()
NAME = PYPROJECT["name"]
VERSION = PYPROJECT["version"]
SUMMARY = PYPROJECT["summary"]
REQUIRES_PYTHON = PYPROJECT["requires-python"]
LICENSE = PYPROJECT["license"]
CLASSIFIERS = PYPROJECT["classifiers"]
CONFIG_VERSION = _config_version()

if VERSION != CONFIG_VERSION:
    raise RuntimeError(
        "version mismatch: pyproject.toml has %r but "
        "deepseek_mcp/config.py SERVER_VERSION is %r"
        % (VERSION, CONFIG_VERSION)
    )

DIST_NAME = re.sub(r"[-.]+", "_", NAME)
DIST_INFO = "%s-%s.dist-info" % (DIST_NAME, VERSION)
WHEEL_NAME = "%s-%s-py3-none-any.whl" % (DIST_NAME, VERSION)
SDIST_NAME = "%s-%s.tar.gz" % (DIST_NAME, VERSION)
DATA_PREFIX = (
    "%s-%s.data/data/share/crewforeman-mcp/skills" % (DIST_NAME, VERSION)
)

ENTRY_POINTS = (
    "[console_scripts]\n"
    "crewforeman-mcp = deepseek_mcp.server:main\n"
    "deepseek-harness-mcp = deepseek_mcp.server:main\n"
    "deepseek-mcp = deepseek_mcp.server:main\n"
)

WHEEL_METADATA = (
    "Wheel-Version: 1.0\n"
    "Generator: build_backend (crewforeman-mcp)\n"
    "Root-Is-Purelib: true\n"
    "Tag: py3-none-any\n"
)

TOP_LEVEL_FILES = (
    "pyproject.toml",
    "build_backend.py",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "config.example.json",
)
TREE_DIRS = (
    "deepseek_mcp",
    "skills",
    "docs",
    "tests",
    "scripts",
    "bench",
    ".github",
)
EXCLUDED_PARTS = {
    ".git",
    ".worklogs",
    "runs",
    "state",
    "build",
    "dist",
    "__pycache__",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log", ".egg-info")


def _metadata_text():
    lines = [
        "Metadata-Version: 2.1",
        "Name: %s" % NAME,
        "Version: %s" % VERSION,
        "Summary: %s" % SUMMARY,
        "License: %s" % LICENSE,
        "Requires-Python: %s" % REQUIRES_PYTHON,
    ]
    lines.extend("Classifier: %s" % classifier for classifier in CLASSIFIERS)
    return "\n".join(lines) + "\n"


def _wheel_payload():
    files = {}
    package_dir = ROOT / "deepseek_mcp"
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if "__pycache__" in relative.split("/"):
            continue
        files[relative] = path.read_bytes()
    skills_dir = ROOT / "skills"
    for path in sorted(skills_dir.glob("*.md")):
        files["%s/%s" % (DATA_PREFIX, path.name)] = path.read_bytes()
    files["%s/METADATA" % DIST_INFO] = _metadata_text().encode("utf-8")
    files["%s/WHEEL" % DIST_INFO] = WHEEL_METADATA.encode("utf-8")
    files["%s/entry_points.txt" % DIST_INFO] = ENTRY_POINTS.encode("utf-8")
    files["%s/LICENSE" % DIST_INFO] = (ROOT / "LICENSE").read_bytes()
    return files


def _record_bytes(files):
    lines = []
    for name in sorted(files):
        data = files[name]
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()
        ).rstrip(b"=").decode("ascii")
        lines.append("%s,sha256=%s,%d" % (name, digest, len(data)))
    lines.append("%s/RECORD,," % DIST_INFO)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _zip_write(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    dist_info = Path(metadata_directory) / DIST_INFO
    dist_info.mkdir(parents=True, exist_ok=True)
    files = _wheel_payload()
    for name in sorted(files):
        if not name.startswith(DIST_INFO + "/"):
            continue
        target = dist_info / name[len(DIST_INFO) + 1:]
        target.write_bytes(files[name])
    return DIST_INFO


def build_wheel(wheel_directory, config_settings=None,
                metadata_directory=None):
    files = _wheel_payload()
    files["%s/RECORD" % DIST_INFO] = _record_bytes(files)
    target = Path(wheel_directory) / WHEEL_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            _zip_write(archive, name, files[name])
    return WHEEL_NAME


def _is_excluded(path):
    parts = path.parts
    for part in parts:
        if part in EXCLUDED_PARTS or part.startswith("wave"):
            return True
    if parts and parts[-1].endswith(EXCLUDED_SUFFIXES):
        return True
    return False


def _sdist_paths():
    paths = []
    for name in TOP_LEVEL_FILES:
        path = ROOT / name
        if path.is_file():
            paths.append(path)
    for tree in TREE_DIRS:
        base = ROOT / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            if _is_excluded(relative):
                continue
            paths.append(path)
    return sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix())


def _sdist_mode(path, data):
    """Canonical tar mode for one source file.

    A Windows checkout has no POSIX execute bits, so ``st_mode`` alone would
    publish ``scripts/clean_install.sh`` as 0644 and break the sdist contract.
    A shebang is the portable executable intent; keep the stat bit as the
    primary signal on POSIX.
    """
    executable = bool(path.stat().st_mode & 0o111)
    if not executable and data.startswith(b"#!"):
        executable = True
    return 0o755 if executable else 0o644


def build_sdist(sdist_directory, config_settings=None):
    target = Path(sdist_directory) / SDIST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = "%s-%s" % (DIST_NAME, VERSION)
    import io

    entries = []
    for path in _sdist_paths():
        relative = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        entries.append((relative, data, _sdist_mode(path, data)))
    # A source distribution carries PKG-INFO at its root; pip and older
    # setuptools read it directly before building the wheel.
    entries.append(("PKG-INFO", _metadata_text().encode("utf-8"), 0o644))
    with open(str(target), "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0,
                           filename="") as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for relative, data, mode in entries:
                    info = tarfile.TarInfo("%s/%s" % (prefix, relative))
                    info.size = len(data)
                    info.mode = mode
                    info.mtime = 1700000000
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
    return SDIST_NAME
