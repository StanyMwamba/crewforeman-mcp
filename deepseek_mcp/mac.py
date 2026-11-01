"""Host helpers: keep awake, screenshots and client-app restart.

All three are cross-platform. They degrade to a clear ``unsupported`` message
where the host has no supported mechanism; none of them is required for the
MCP server itself to work.

Python 3.9 compatible; standard library only.
"""

import ctypes
import datetime as dt
import os
import subprocess
import threading
import time
from pathlib import Path

from . import clients, platforms

_win_awake = {"event": None, "thread": None, "active": False}


def _system():
    return platforms.normalize_system()


def _pid_file(cfg):
    return Path(cfg.pid_file)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _process_name(pid):
    """Best-effort process name for stale-PID safety; never raise."""
    try:
        if _system() == "Linux":
            raw = Path("/proc/%d/comm" % int(pid)).read_text(encoding="utf-8")
            return raw.strip().lower()
    except Exception:
        pass
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=5)
        return proc.stdout.decode("utf-8", "replace").strip().lower()
    except Exception:
        return ""


def _is_keep_awake(pid):
    name = _process_name(pid)
    return any(token in name for token in ("caffeinate", "systemd-inhibit", "sleep"))


def awake_pid(cfg):
    try:
        pid = int(_pid_file(cfg).read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if _system() == "Windows":
        return pid if pid == os.getpid() and _win_awake["active"] else None
    return pid if _pid_alive(pid) else None


def _start_windows_awake():
    """SetThreadExecutionState in a daemon thread; writes the current PID."""
    kernel32 = ctypes.windll.kernel32
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    es_display_required = 0x00000002
    kernel32.SetThreadExecutionState(
        es_continuous | es_system_required | es_display_required)
    stop = threading.Event()
    _win_awake["event"] = stop

    def run():
        while not stop.wait(60):
            kernel32.SetThreadExecutionState(
                es_continuous | es_system_required | es_display_required)
        kernel32.SetThreadExecutionState(es_continuous)

    thread = threading.Thread(target=run, name="keep-awake", daemon=True)
    thread.start()
    _win_awake["thread"] = thread
    _win_awake["active"] = True


def _stop_windows_awake():
    stop = _win_awake.get("event")
    if stop is not None:
        stop.set()
    _win_awake["active"] = False
    _win_awake["event"] = None
    _win_awake["thread"] = None


def tool_keep_awake(args, cfg):
    action = (args.get("action") or "status").lower()
    if action not in ("start", "stop", "status"):
        raise RuntimeError("action must be start, stop or status")
    system = _system()
    if system not in ("Darwin", "Linux", "Windows"):
        message = "unsupported: keep-awake has no mechanism on this platform"
        if action == "status":
            return message
        raise RuntimeError(message)
    pid = awake_pid(cfg)
    pid_file = _pid_file(cfg)
    if action == "start":
        if pid:
            return "Keep-awake is already on (pid %d)." % pid
        if system == "Windows":
            _start_windows_awake()
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
            platforms.private_permissions(pid_file, is_dir=False)
            return ("Keeping this machine awake with SetThreadExecutionState "
                    "(server pid %d). It lasts until stop or the process exits."
                    % os.getpid())
        command = platforms.keep_awake_command(system)
        if not command:
            raise RuntimeError(
                "unsupported: no keep-awake command found on this system "
                "(install systemd-inhibit or use caffeinate on macOS)")
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                **platforms.popen_kwargs())
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        platforms.private_permissions(pid_file, is_dir=False)
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError("keep-awake command stopped immediately")
        return ("Keeping this machine awake (pid %d). It stays on until you stop "
                "it or the machine restarts." % proc.pid)
    if action == "stop":
        if not pid:
            return "Keep-awake was not running."
        if system == "Windows":
            _stop_windows_awake()
            try:
                pid_file.unlink()
            except OSError:
                pass
            return "Stopped keeping this machine awake."
        if not _is_keep_awake(pid):
            try:
                pid_file.unlink()
            except OSError:
                pass
            return ("Refusing to stop PID %d: it is not a keep-awake process "
                    "(stale PID file). Removed the stale file." % pid)
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass
        return "Stopped keeping this machine awake; normal sleep settings apply."
    return "Keep-awake is %s." % (("ON (pid %d)" % pid) if pid else "OFF")


def take_screenshot(args, cfg):
    target = args.get("path")
    if target:
        path = Path(os.path.expanduser(str(target)))
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(cfg.harness_workspace) / "screenshots" / ("screenshot-%s.png" % stamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    delay = float(args.get("delay_seconds") or 0)
    if delay > 0:
        time.sleep(min(delay, 30))
    command = platforms.screenshot_command(_system(), str(path))
    if not command:
        raise RuntimeError(
            "unsupported: no supported screenshot tool on this platform "
            "(macOS screencapture, Linux gnome-screenshot/scrot/grim, "
            "Windows PowerShell)")
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
    except Exception as exc:
        raise RuntimeError("screenshot failed: %s" % exc)
    if proc.returncode != 0 or not path.is_file():
        detail = proc.stderr.decode("utf-8", "replace")[:500]
        raise RuntimeError("screenshot failed: %s" % detail)
    return "Screenshot saved: %s" % path


# Backward-compatible aliases (v3.5.0 tests and any direct importers).
mac_screenshot = take_screenshot


def restart_claude_app(args, cfg):
    return clients.restart_client_app(args, cfg)
