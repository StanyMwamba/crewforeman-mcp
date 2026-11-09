#!/usr/bin/env python3
"""Report whether this host has a real, usable OS write boundary.

Output (--json): {"os", "mechanism", "abi", "usable", "detail"}.
Exit 1 with --require when the primitive is absent or unusable.
"""
import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile


def _macos():
    exe = shutil.which("sandbox-exec")
    if not exe:
        return {"os": "darwin", "mechanism": "seatbelt", "abi": None,
                "usable": False, "detail": "sandbox-exec not found"}
    scratch = tempfile.mkdtemp(prefix="dsh-probe-")
    canary = os.path.join(os.path.dirname(scratch),
                          "dsh-probe-canary-%d" % os.getpid())
    try:
        if os.path.exists(canary):
            os.unlink(canary)
        code = "import sys;open(sys.argv[1],'w').write('x')"
        try:
            control = subprocess.run(
                [exe, "-p", "(version 1)(allow default)", sys.executable,
                 "-c", "pass"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            if control.returncode != 0:
                return {"os": "darwin", "mechanism": "seatbelt", "abi": None,
                        "usable": False,
                        "detail": "sandbox-exec cannot run here (nested? rc=%s)"
                                  % control.returncode}
            proc = subprocess.run(
                [exe, "-p", "(version 1)(allow default)(deny file-write*)",
                 sys.executable, "-c", code, canary],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except Exception as exc:  # noqa: BLE001 - probe reports, never raises
            return {"os": "darwin", "mechanism": "seatbelt", "abi": None,
                    "usable": False, "detail": "sandbox-exec failed: %s" % exc}
        denied = (not os.path.exists(canary)) and proc.returncode != 0
        detail = ("canary denied" if denied else
                  "canary write succeeded (rc=%s)" % proc.returncode)
        return {"os": "darwin", "mechanism": "seatbelt", "abi": None,
                "usable": denied, "detail": detail}
    finally:
        if os.path.exists(canary):
            try:
                os.unlink(canary)
            except OSError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)


def _linux():
    libc = ctypes.CDLL(None, use_errno=True)
    abi = -1
    err = 0
    try:
        ctypes.set_errno(0)
        abi = int(libc.syscall(444, 0, 0, 1))  # LANDLOCK_CREATE_RULESET_VERSION
        err = ctypes.get_errno()
    except Exception as exc:  # noqa: BLE001
        return {"os": "linux", "mechanism": "landlock", "abi": None,
                "usable": False, "detail": "landlock syscall unavailable: %s" % exc}
    if abi < 0:
        return {"os": "linux", "mechanism": "landlock", "abi": None,
                "usable": False, "detail": "landlock unavailable (errno=%s)" % err}
    usable = abi >= 3
    return {"os": "linux", "mechanism": "landlock", "abi": abi,
            "usable": usable,
            "detail": "abi=%d%s" % (abi, "" if usable else " (abi_lt_3)")}


def _windows():
    if platform.system() != "Windows":
        return {"os": "win32", "mechanism": "windows-write-restricted",
                "abi": None, "usable": False, "detail": "not Windows"}
    try:
        from deepseek_mcp import sandbox_windows
        status = sandbox_windows.probe_status()
        return {
            "os": "win32", "mechanism": "windows-write-restricted",
            "abi": None, "usable": bool(status.get("ok")),
            "detail": "write_restricted=%s;logon_sid=%s;admin=%s;%s"
                      % (status.get("write_restricted"),
                         status.get("logon_sid"),
                         status.get("admin"),
                         status.get("reason") or "ok"),
        }
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        return {"os": "win32", "mechanism": "windows-write-restricted",
                "abi": None, "usable": False,
                "detail": "token probe failed: %s" % exc}

def probe():
    if sys.platform == "darwin":
        return _macos()
    if sys.platform.startswith("linux"):
        return _linux()
    if sys.platform == "win32":
        return _windows()
    return {"os": sys.platform, "mechanism": "none", "abi": None,
            "usable": False, "detail": "unsupported platform"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = probe()
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("%(os)s %(mechanism)s usable=%(usable)s %(detail)s" % result)
    return 0 if (result["usable"] or not args.require) else 1


if __name__ == "__main__":
    sys.exit(main())
