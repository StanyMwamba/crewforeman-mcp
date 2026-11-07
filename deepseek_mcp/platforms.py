"""Cross-platform process, path and helper primitives.

Everything here is a small, injectable function so the test suite can exercise
macOS, Linux and Windows behaviour on any host. Callers pass an explicit
``system`` only in tests; production uses the current platform.

Python 3.9 compatible; standard library only.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = "Windows"
MACOS = "Darwin"
LINUX = "Linux"


def normalize_system(system=None):
    text = str(system or "").strip()
    if text in (WINDOWS, MACOS, LINUX):
        return text
    low = text.lower()
    if low in ("windows", "win32", "nt"):
        return WINDOWS
    if low in ("darwin", "macos", "mac"):
        return MACOS
    if low in ("linux", "posix"):
        return LINUX
    if os.name == "nt":
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    return LINUX


def home_dir(env=None):
    env = os.environ if env is None else env
    raw = env.get("USERPROFILE") or env.get("HOME")
    return str(raw or os.path.expanduser("~"))


def default_paths(system=None, home=None, env=None):
    """Return private default locations for config, runs, jobs and state."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    home = home or home_dir(env)
    if system == WINDOWS:
        config_root = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local_root = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return {
            "config_dir": os.path.join(config_root, "deepseek-harness-mcp"),
            "data_dir": os.path.join(local_root, "DeepSeek-MCP"),
            "temp_dir": env.get("TEMP") or os.path.join(local_root, "Temp"),
        }
    if system == LINUX:
        config_root = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        state_root = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        data_root = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        return {
            "config_dir": os.path.join(config_root, "deepseek-harness-mcp"),
            "data_dir": os.path.join(data_root, "DeepSeek-MCP"),
            "state_dir": os.path.join(state_root, "DeepSeek-MCP"),
            "temp_dir": env.get("TMPDIR") or "/tmp",
        }
    # macOS keeps the historical locations for backward compatibility.
    return {
        "config_dir": os.path.join(home, ".config", "deepseek-harness-mcp"),
        "data_dir": os.path.join(home, "DeepSeek-MCP"),
        "temp_dir": env.get("TMPDIR") or "/tmp",
    }


def _existing(paths):
    result = []
    for raw in paths:
        try:
            text = os.path.expandvars(os.path.expanduser(str(raw)))
            if text and Path(text).is_file():
                result.append(text)
        except (TypeError, ValueError, OSError):
            continue
    return result


def chrome_candidates(system=None, env=None):
    """Trusted Chrome/Edge paths for the platform, in preference order."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    names = ("chrome-headless-shell", "chromium", "chromium-browser",
             "google-chrome", "google-chrome-stable")
    paths = []
    if system == WINDOWS:
        roots = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"),
                 env.get("LOCALAPPDATA")]
        for root in [item for item in roots if item]:
            paths.extend([
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            ])
    elif system == MACOS:
        paths.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])
    else:
        paths.extend([
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ])
    for name in names:
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            paths.insert(0, found)
    return _existing(paths)


def dsh_candidates(system=None, env=None):
    """Common DeepSeek Harness CLI locations; DSH_BIN still wins."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    paths = []
    if system == MACOS:
        paths.append("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
    elif system == WINDOWS:
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        paths.extend([
            os.path.join(local, "Programs", "dsh", "dsh.exe"),
            os.path.join(local, "DeepSeek", "Harness", "dsh.exe"),
        ])
    paths.extend([
        os.path.join(home, ".local", "bin", "dsh"),
        "/usr/local/bin/dsh",
        "/opt/homebrew/bin/dsh",
    ])
    found = shutil.which("dsh", path=env.get("PATH"))
    if found:
        paths.insert(0, found)
    return _existing(paths)


def popen_kwargs(system=None):
    """Detach a child so it survives the MCP client reconnecting."""
    system = normalize_system(system)
    if system == WINDOWS:
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def pid_alive(pid, system=None):
    """Safe liveness check (Windows os.kill(pid, 0) can terminate)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if normalize_system(system) == WINDOWS:
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _pid_alive(pid):
    return pid_alive(pid)


def terminate_tree(pid, system=None, runner=None, timeout=5.0):
    """Terminate one process and its children; never signal our own group.

    POSIX: SIGTERM to the child's own process group (when it leads one), wait,
    then SIGKILL. Windows: ``taskkill /PID <pid> /T /F``. Returns a small dict
    for the run record; never raises.
    """
    system = normalize_system(system)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"terminated": False, "reason": "invalid pid"}
    if pid <= 0 or pid == os.getpid():
        return {"terminated": False, "reason": "refused unsafe pid"}
    runner = runner or subprocess.run
    if system == WINDOWS:
        try:
            proc = runner(["taskkill", "/PID", str(pid), "/T", "/F"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)
            code = getattr(proc, "returncode", 1)
            return {"terminated": code == 0, "method": "taskkill", "exit_code": code}
        except Exception as exc:
            return {"terminated": False, "method": "taskkill", "reason": str(exc)}
    pgid = None
    try:
        pgid = os.getpgid(pid)
    except (OSError, ValueError):
        pgid = None
    # Only signal a group when the target leads it. Signalling the server's own
    # group would kill the MCP client process.
    use_group = pgid is not None and pgid == pid and pgid != os.getpgid(0)
    try:
        if use_group:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError) as exc:
        return {"terminated": False, "method": "killpg" if use_group else "kill",
                "reason": str(exc)}
    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            if use_group:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    return {"terminated": True, "method": "killpg" if use_group else "kill"}


def keep_awake_command(system=None, runner=None):
    """Return an argv that keeps the host awake, or ``None``.

    Windows is handled in-process with ``SetThreadExecutionState`` (see
    mac.py), so it returns ``None`` here with a marker.
    """
    system = normalize_system(system)
    if system == MACOS:
        return ["/usr/bin/caffeinate", "-i", "-s", "-m"]
    if system == LINUX:
        if shutil.which("systemd-inhibit"):
            return ["systemd-inhibit", "--what=idle:sleep", "--why=DeepSeek MCP",
                    "--mode=block", "sleep", "infinity"]
        return None
    return None


def screenshot_command(system=None, path="", env=None):
    """Return an argv that captures ``path``, or ``None`` when unsupported."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    if system == MACOS:
        binary = "/usr/sbin/screencapture"
        return [binary, "-x", "-t", "png", str(path)] if Path(binary).is_file() else None
    if system == WINDOWS:
        escaped = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
            "$bmp.Save('%s',[System.Drawing.Imaging.ImageFormat]::Png)" % escaped
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    for name, prefix in (("gnome-screenshot", ["-f"]), ("scrot", []),
                         ("grim", []), ("spectacle", ["-b", "-o"])):
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return [found] + prefix + [str(path)]
    return None


def restart_client_command(system=None, app_name="Claude"):
    """Return a platform command that restarts ``app_name``, or ``None``.

    The caller runs quit, waits, then runs open. Keeping the pieces separate
    makes the helper testable and avoids a shell string with the app name.
    """
    system = normalize_system(system)
    name = str(app_name or "").strip()
    if not name:
        return None
    if system == MACOS:
        return {"quit": ["osascript", "-e", 'tell application "%s" to quit' % name],
                "open": ["open", "-a", name]}
    if system == WINDOWS:
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        return {"quit": ["taskkill", "/IM", exe, "/F"],
                "open": ["cmd", "/c", "start", "", name]}
    # Match the exact process name, never the full command line: the
    # detached restart helper's own argv contains the app name and a
    # ``-f`` match would terminate the helper instead of the app.
    process_name = name.lower().replace(" ", "-")[:15]
    return {"quit": ["pkill", "-x", process_name],
            "open": ["gtk-launch", name.lower().replace(" ", "-")]}


def sandbox_support(system=None):
    """Describe the OS sandbox layer available to this connector."""
    system = normalize_system(system)
    if system == MACOS:
        usable = Path("/usr/bin/sandbox-exec").is_file()
        return {"kind": "seatbelt", "available": usable,
                "reason": None if usable else "sandbox-exec not found"}
    if system == WINDOWS:
        return {"kind": "none", "available": False,
                "reason": "no built-in file-write wrapper; use a container/VM "
                          "or Windows Sandbox for untrusted work"}
    return {"kind": "none", "available": False,
            "reason": "no built-in file-write wrapper; use a container/VM "
                      "(bubblewrap/nsjail) for untrusted work"}


def private_permissions(path, system=None, is_dir=False):
    """0700 for directories, 0600 for files on POSIX; no-op on Windows."""
    if normalize_system(system) == WINDOWS:
        return False
    try:
        os.chmod(str(path), 0o700 if is_dir else 0o600)
        return True
    except OSError:
        return False


def secure_tree(root, system=None):
    """Best-effort recursive private permissions; returns (dirs, files).

    An absent, ``None`` or blank path is a no-op. In particular ``""`` must
    not become ``"."`` and chmod the process working directory.
    """
    dirs = files = 0
    if root is None or not str(root).strip():
        return dirs, files
    root = Path(root)
    if not root.exists():
        return dirs, files
    for current, subdirs, names in os.walk(str(root)):
        if private_permissions(current, system, is_dir=True):
            dirs += 1
        for name in names:
            if private_permissions(os.path.join(current, name), system, is_dir=False):
                files += 1
    return dirs, files
