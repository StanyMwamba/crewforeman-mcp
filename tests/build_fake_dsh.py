#!/usr/bin/env python3
"""Compile the test-only fake dsh launcher.  A missing compiler is fatal."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "fake_dsh_launcher.c"
ROOT = HERE.parent


def _c_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _write_header(directory, script):
    header = Path(directory) / "fake_dsh_config.h"
    header.write_text(
        "#define FAKE_DSH_PYTHON \"%s\"\n#define FAKE_DSH_SCRIPT \"%s\"\n"
        % (_c_escape(sys.executable), _c_escape(Path(script).resolve())),
        encoding="utf-8")
    return header


def _windows_compiler():
    roots = [os.environ.get("ProgramFiles(x86)"),
             os.environ.get("ProgramFiles"), r"C:\Program Files (x86)"]
    vswhere = None
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if candidate.exists():
            vswhere = candidate
            break
    if vswhere is None:
        raise RuntimeError("vswhere.exe not found; MSVC is required to build fake dsh")
    out = subprocess.run(
        [str(vswhere), "-latest", "-products", "*",
         "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-property", "installationPath"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    install = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    vcvars = Path(install) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.exists():
        raise RuntimeError("vcvars64.bat not found under %r" % install)
    return vcvars


def _posix_compiler():
    cc = os.environ.get("CC") or shutil.which("cc")
    if not cc:
        raise RuntimeError("no C compiler (cc) found; it is required for fake dsh")
    return cc


def build_c(source, out=None):
    """Compile a plain C source (no generated include); returns the binary."""
    source = Path(source).resolve()
    if out is None:
        out = source.with_suffix(".exe" if os.name == "nt" else ".bin")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cf-c-build-") as work:
        if os.name == "nt":
            vcvars = _windows_compiler()
            bat = Path(work) / "build.bat"
            bat.write_text(
                '@echo off\r\ncall "%s" >nul || exit /b 1\r\n'
                'cl /nologo /O2 /Fe:"%s" "%s" || exit /b 1\r\n'
                % (vcvars, out, source), encoding="utf-8")
            proc = subprocess.run(["cmd", "/c", str(bat)],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        else:
            proc = subprocess.run(
                [_posix_compiler(), "-O2", "-o", str(out), str(source)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError("C build failed:\n%s" % proc.stdout)
    return out


def build_launcher(script, out=None):
    """Compile the launcher that execs *script*; returns the launcher Path."""
    script = Path(script).resolve()
    if out is None:
        out = script.with_suffix(".exe" if os.name == "nt" else ".bin")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fake-dsh-build-") as include:
        _write_header(include, script)
        if os.name == "nt":
            vcvars = _windows_compiler()
            bat = Path(include) / "build.bat"
            bat.write_text(
                '@echo off\r\ncall "%s" >nul || exit /b 1\r\n'
                'cl /nologo /O2 /I"%s" /Fe:"%s" "%s" || exit /b 1\r\n'
                % (vcvars, include, out, SOURCE), encoding="utf-8")
            proc = subprocess.run(["cmd", "/c", str(bat)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True)
        else:
            proc = subprocess.run(
                [_posix_compiler(), "-O2", "-I", include, "-o", str(out),
                 str(SOURCE)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError("fake dsh launcher build failed:\n%s" % proc.stdout)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    script = Path(argv[0]) if argv else ROOT / "artifacts" / "fake-dsh" / "fake_dsh.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    if not script.exists():
        script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    launcher = build_launcher(script)
    print(launcher)
    return 0


if __name__ == "__main__":
    sys.exit(main())
