"""Detached restart helper for ``restart_client_app`` (P3.2 item 4).

The MCP/desktop client normally dies when the app quits, so the reopen must
happen in a process that is not a child of the client.  ``spawn_restart_helper``
starts this module with a new session (POSIX) or ``DETACHED_PROCESS``
(Windows); the child waits, reopens the app, verifies that it is running and
retries a few times.  Progress is written as small JSON to a status file so
the caller can inspect the outcome after the restart.

Standard library only, Python 3.9 compatible, no network access.
"""

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_APP_NAME = "Claude"
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_SECONDS = 2.0
MAX_ATTEMPTS = 10
MAX_RETRY_SECONDS = 60.0
COMMAND_TIMEOUT = 15.0
_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]+$")


def utc_now():
    """Return a compact UTC timestamp suitable for the JSON status file."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _family(system=None):
    text = str(system if system is not None else sys.platform).strip().lower()
    if text.startswith("darwin") or text in ("mac", "macos", "osx"):
        return "macos"
    if text.startswith("win"):
        return "windows"
    if text.startswith("linux"):
        return "linux"
    return ""


def _app_name(value):
    name = str(value or "").strip()
    if (not name or len(name) > 60 or not _NAME_RE.fullmatch(name)
            or not any(char.isalnum() for char in name)):
        raise ValueError(
            "app_name must be 1-60 characters using only letters, digits, "
            "spaces, dot, dash and underscore")
    return name


def _command(value):
    """Normalise a command: argv list, JSON list string, or shell-like string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        argv = [str(item) for item in value]
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("command must be a list or a shell-like string")
            argv = [str(item) for item in parsed]
        else:
            argv = shlex.split(text)
    if not argv:
        raise ValueError("command must not be empty")
    return argv


def _linux_candidates(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    candidates = []
    for item in (slug, slug + "-desktop", slug + "-app"):
        if item and item not in candidates:
            candidates.append(item)
    return candidates


def resolve_commands(family, app_name, overrides=None, which=None):
    """Return ``close``/``open``/``verify`` argv lists for one platform."""
    family = _family(family)
    if not family:
        raise ValueError("unsupported platform for the restart helper")
    name = _app_name(app_name)
    if family == "macos":
        commands = {
            "close": ["osascript", "-e", 'tell application "%s" to quit' % name],
            "open": ["open", "-a", name],
            "verify": ["osascript", "-e", 'application "%s" is running' % name],
        }
    elif family == "windows":
        commands = {
            "close": ["taskkill", "/IM", name + ".exe", "/F"],
            "open": ["cmd", "/c", "start", "", name],
            "verify": ["tasklist", "/FI", "IMAGENAME eq %s.exe" % name],
        }
    else:
        lookup = which or shutil.which
        opener = None
        for candidate in _linux_candidates(name):
            try:
                found = lookup(candidate)
            except Exception:
                found = None
            if found:
                opener = [str(found)]
                break
        expected = str(opener[0]) if opener else None
        slug = _linux_candidates(name)[0]
        process_name = (os.path.basename(expected.rstrip("/"))
                        if expected else slug)
        # Linux comm names are truncated to 15 characters.  Use the exact
        # process name (``pkill -x``/``pgrep -x``) instead of ``-f``: a
        # full-command match also matches this helper, whose own argv contains
        # ``--app-name <name>``, and would kill/verify the helper instead of
        # the application.
        process_name = process_name[:15]
        if opener is None:
            opener = ["xdg-open", name]
            expected = None
        commands = {
            "close": ["pkill", "-x", process_name],
            "open": opener,
            "verify": ["pgrep", "-x", process_name],
            "expected_executable": expected,
        }
    overrides = overrides or {}
    if isinstance(overrides, dict):
        for key in ("close", "open", "verify"):
            if overrides.get(key) is not None:
                parsed = _command(overrides[key])
                if parsed:
                    commands[key] = parsed
        if overrides.get("open") is not None and family == "linux":
            # An explicit opener may launch a different executable; only the
            # caller's expected_exe override can verify it.
            commands["expected_executable"] = None
    return commands


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _execute(argv, runner=None, timeout=COMMAND_TIMEOUT):
    """Run one command and return ``(returncode, stdout, stderr)``; never raises."""
    if runner is not None:
        try:
            try:
                proc = runner(argv, timeout=timeout)
            except TypeError:
                proc = runner(argv)
        except Exception as exc:
            return 1, "", str(exc)
        try:
            code = int(getattr(proc, "returncode", 1))
        except (TypeError, ValueError):
            code = 1
        return (code, _text(getattr(proc, "stdout", "")),
                _text(getattr(proc, "stderr", "")))
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"
    except OSError as exc:
        return 126, "", str(exc)
    return (int(proc.returncode), _text(proc.stdout), _text(proc.stderr))


def _process_exe(pid):
    """Best-effort absolute executable path for one PID."""
    try:
        return os.readlink("/proc/%d/exe" % int(pid))
    except (OSError, TypeError, ValueError):
        pass
    if sys.platform == "darwin":
        # macOS has no /proc; libproc.proc_pidpath is the supported stdlib
        # equivalent (ctypes ships with Python).
        try:
            import ctypes
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            buffer = ctypes.create_string_buffer(4096)
            length = libproc.proc_pidpath(
                ctypes.c_int(int(pid)), buffer, ctypes.c_uint32(4096))
            if length > 0:
                return buffer.value.decode("utf-8", "replace")
        except Exception:
            pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "comm="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5, check=False)
        lines = _text(proc.stdout).strip().splitlines()
        return lines[0].strip() if lines else None
    except Exception:
        return None


def _same_executable(actual, expected):
    if not actual or not expected:
        return False
    try:
        if not (os.path.exists(actual) and os.path.exists(expected)):
            return False
        if os.path.realpath(actual) == os.path.realpath(expected):
            return True
        # Different real paths can still be the same inode (hard links).
        return os.path.samefile(actual, expected)
    except (OSError, ValueError):
        return False


def _verify_ok(family, code, stdout, stderr,
               expected_executable=None, helper_pid=None):
    if family == "macos":
        return code == 0 and stdout.strip().lower() in ("true", "yes", "1")
    if family == "windows":
        output = (stdout + " " + stderr).lower()
        return code == 0 and ".exe" in output and "no tasks" not in output
    if code != 0:
        return False
    if not expected_executable:
        return True
    for token in re.findall(r"\b\d+\b", stdout):
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid == os.getpid() or (helper_pid is not None
                                  and pid == int(helper_pid)):
            continue
        if _same_executable(_process_exe(pid), expected_executable):
            return True
    return False


def read_status(path):
    """Read a status file, returning ``{}`` when absent or malformed."""
    try:
        with open(str(path), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(path, record):
    """Atomically write the small, human-readable JSON status file."""
    path = Path(os.path.expanduser(str(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(str(tmp), str(path))
    return str(path)


def _number(value, default, low, high, label, integer=False):
    if value is None or value == "":
        value = default
    if isinstance(value, bool):
        raise ValueError("%s must be a number between %s and %s" % (label, low, high))
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number between %s and %s" % (label, low, high))
    if number != number or number < low or number > high:
        raise ValueError("%s must be a number between %s and %s" % (label, low, high))
    return number


def _default_status_path():
    state = os.environ.get("DSH_STATE_DIR") or os.environ.get("DEEPSEEK_MCP_STATE_DIR")
    if not state:
        try:
            from .config import load_config
            state = load_config().state_dir
        except Exception:
            state = os.path.join(os.path.expanduser("~"), ".deepseek-mcp", "state")
    return Path(os.path.expanduser(str(state))) / "restart-status.json"


def _opts(args):
    if args is None:
        return {}
    if isinstance(args, dict):
        return dict(args)
    return dict(vars(args))


def run_once(args):
    """Close, reopen and verify the app once, writing status as it goes.

    Accepted keys: ``app_name``, ``system``, ``attempts``, ``retry_seconds``,
    ``initial_delay``, ``status_path``, ``timeout``, ``close``/``open``/
    ``verify`` command overrides, and optional ``runner``/``sleep`` test seams.
    Returns the final status record; status ``state`` is ``reopened`` or
    ``failed``.
    """
    opts = _opts(args)
    name = _app_name(opts.get("app_name") or DEFAULT_APP_NAME)
    family = _family(opts.get("system"))
    if not family:
        raise ValueError("unsupported platform for the restart helper")
    attempts = _number(opts.get("attempts"), DEFAULT_ATTEMPTS, 1, MAX_ATTEMPTS,
                       "attempts", integer=True)
    retry = _number(opts.get("retry_seconds"), DEFAULT_RETRY_SECONDS, 0.0,
                     MAX_RETRY_SECONDS, "retry_seconds")
    initial = _number(opts.get("initial_delay"), 0.0, 0.0, MAX_RETRY_SECONDS,
                      "initial_delay")
    timeout = _number(opts.get("timeout"), COMMAND_TIMEOUT, 0.1, 120.0, "timeout")
    status_path = Path(os.path.expanduser(
        str(opts.get("status_path") or _default_status_path())))
    overrides = {key: opts.get(key) for key in ("close", "open", "verify")
                 if opts.get(key) is not None}
    commands = resolve_commands(family, name, overrides,
                                 which=opts.get("which"))
    expected_exe = (opts.get("expected_exe")
                    or commands.get("expected_executable"))
    runner = opts.get("runner")
    sleeper = opts.get("sleep") or time.sleep
    started = utc_now()
    record = {
        "state": "waiting",
        "app_name": name,
        "platform": family,
        "helper_pid": os.getpid(),
        "status_path": str(status_path),
        "attempts": 0,
        "max_attempts": attempts,
        "retry_seconds": retry,
        "started_at": started,
        "updated_at": started,
        "error": "",
        "message": "Restart helper started.",
        "attempts_detail": [],
    }
    write_status(status_path, record)

    close_code, _, close_err = _execute(commands["close"], runner, timeout)
    record["close_returncode"] = close_code
    if close_code != 0:
        record["close_error"] = (close_err or "close command failed")[:240]
    if initial > 0:
        sleeper(initial)

    last_error = ""
    for attempt in range(1, attempts + 1):
        record["state"] = "waiting"
        record["attempts"] = attempt
        record["updated_at"] = utc_now()
        record["message"] = ("Waiting for %s to reopen (attempt %d/%d)."
                             % (name, attempt, attempts))
        write_status(status_path, record)

        open_code, _, open_err = _execute(commands["open"], runner, timeout)
        detail = {"attempt": attempt, "open_returncode": open_code,
                  "verified": False}
        if open_code != 0:
            last_error = "open command failed with exit code %d" % open_code
            if open_err:
                last_error += ": " + open_err
            detail["error"] = last_error[:240]
            record["attempts_detail"].append(detail)
            record["error"] = last_error[:400]
            write_status(status_path, record)
            if attempt < attempts and retry > 0:
                sleeper(retry)
            continue

        if retry > 0:
            sleeper(retry)
        verify_code, verify_out, verify_err = _execute(
            commands["verify"], runner, timeout)
        if _verify_ok(family, verify_code, verify_out, verify_err,
                      expected_executable=expected_exe,
                      helper_pid=os.getpid()):
            detail["verified"] = True
            if expected_exe:
                detail["executable"] = str(expected_exe)
            record["attempts_detail"].append(detail)
            record["state"] = "reopened"
            record["error"] = ""
            record["updated_at"] = utc_now()
            record["message"] = ("%s reopened and verified on attempt %d/%d."
                                 % (name, attempt, attempts))
            write_status(status_path, record)
            return record

        last_error = "verification failed with exit code %d" % verify_code
        if verify_err:
            last_error += ": " + verify_err
        detail["error"] = last_error[:240]
        record["attempts_detail"].append(detail)
        record["error"] = last_error[:400]
        write_status(status_path, record)
        if attempt < attempts and retry > 0:
            sleeper(retry)

    record["state"] = "failed"
    record["updated_at"] = utc_now()
    record["error"] = (last_error or "application was not verified as running")[:400]
    record["message"] = ("%s could not be reopened and verified after %d attempt(s)."
                         % (name, attempts))
    write_status(status_path, record)
    return record


def spawn_restart_helper(args, popen=None):
    """Start this module detached and return its pid.

    POSIX uses ``start_new_session=True``; Windows uses
    ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``.  Standard streams are
    discarded so the helper cannot keep the caller's pipes open.
    """
    opts = _opts(args)
    name = _app_name(opts.get("app_name") or DEFAULT_APP_NAME)
    family = _family(opts.get("system"))
    if not family:
        raise ValueError("unsupported platform for the restart helper")
    attempts = _number(opts.get("attempts"), DEFAULT_ATTEMPTS, 1, MAX_ATTEMPTS,
                       "attempts", integer=True)
    retry = _number(opts.get("retry_seconds"), DEFAULT_RETRY_SECONDS, 0.0,
                     MAX_RETRY_SECONDS, "retry_seconds")
    initial = _number(opts.get("initial_delay"), 0.0, 0.0, MAX_RETRY_SECONDS,
                      "initial_delay")
    timeout = _number(opts.get("timeout"), COMMAND_TIMEOUT, 0.1, 120.0, "timeout")
    status_path = Path(os.path.expanduser(
        str(opts.get("status_path") or _default_status_path())))

    command = [
        sys.executable, "-m", "deepseek_mcp.restart_helper",
        "--app-name", name,
        "--system", family,
        "--attempts", str(attempts),
        "--retry-seconds", repr(retry),
        "--initial-delay", repr(initial),
        "--timeout", repr(timeout),
        "--status-path", str(status_path),
    ]
    for key in ("close", "open", "verify"):
        value = opts.get(key)
        if value is not None:
            command.extend(["--%s-command" % key, json.dumps(_command(value))])
    if opts.get("expected_exe"):
        command.extend(["--expected-exe", str(opts["expected_exe"])])

    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (package_root + (os.pathsep + old_path if old_path else ""))
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    proc = (popen or subprocess.Popen)(command, **kwargs)
    pid = int(proc.pid)
    # The helper is intentionally not waited for.  Popen.__del__ warns when a
    # still-running child is garbage-collected; marking the handle as reaped
    # silences that warning without touching the detached process itself.
    try:
        proc.returncode = 0
    except Exception:
        pass
    return pid


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Detached reconnect helper for restart_client_app.")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--system", default=sys.platform)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--retry-seconds", type=float,
                        default=DEFAULT_RETRY_SECONDS)
    parser.add_argument("--initial-delay", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=COMMAND_TIMEOUT)
    parser.add_argument("--status-path", default=None)
    parser.add_argument("--close-command", default=None,
                        help="argv list as JSON or a shell-like string")
    parser.add_argument("--open-command", default=None)
    parser.add_argument("--verify-command", default=None)
    parser.add_argument("--expected-exe", default=None,
                        help="absolute app executable the verify step must find")
    options = vars(parser.parse_args(argv))
    options["close"] = _command(options.pop("close_command"))
    options["open"] = _command(options.pop("open_command"))
    options["verify"] = _command(options.pop("verify_command"))
    try:
        record = run_once(options)
    except Exception as exc:
        path = options.get("status_path") or _default_status_path()
        failed = {
            "state": "failed",
            "app_name": options.get("app_name") or DEFAULT_APP_NAME,
            "status_path": str(path),
            "attempts": 0,
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "error": str(exc)[:400],
            "message": "The detached restart helper could not run.",
        }
        try:
            write_status(path, failed)
        except Exception:
            pass
        sys.stderr.write("restart helper failed: %s\n" % exc)
        return 1
    summary = {key: record.get(key) for key in
               ("state", "app_name", "attempts", "status_path", "error")}
    print(json.dumps(summary, sort_keys=True))
    return 0 if record.get("state") == "reopened" else 1


if __name__ == "__main__":
    raise SystemExit(main())
