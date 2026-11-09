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


def macos_process_check(name):
    """Return the argv for the macOS application process check.

    ``pgrep -a`` is required to *see* a running app when this helper is
    started from inside Claude's process tree: plain ``pgrep -x`` excludes its
    own ancestors, so it returns no match even though a process named exactly
    ``Claude`` is running.  ``-a`` includes ancestors; the caller filters the
    ancestor PIDs again before treating the output as corroboration.
    """
    return ["pgrep", "-a", "-x", _app_name(name)]


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
            # Executed alongside osascript on every attempt and recorded as
            # corroboration.  osascript remains authoritative for ``reopened``:
            # pgrep alone never overrides a failed/false osascript.  ``-a``
            # includes ancestors, which plain ``pgrep -x`` excludes, so a
            # helper inside Claude's process tree can still see Claude; the
            # ancestor PIDs are filtered before the check counts as
            # corroboration.
            "process_check": macos_process_check(name),
        }
    elif family == "windows":
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        commands = {
            "close": ["taskkill", "/IM", exe, "/F"],
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
            # No desktop entry resolved: gtk-launch is the documented first
            # fallback; xdg-open remains available through an override.
            opener = ["gtk-launch", _linux_candidates(name)[0]]
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
        actual_real = os.path.realpath(actual)
        expected_real = os.path.realpath(expected)
        if actual_real == expected_real:
            return True
        # Hard links share an inode.  On multicall distributions (Ubuntu
        # 26.04 cargo coreutils) every applet is a hard link to one binary,
        # so inode equality alone would confuse ``sleep`` with ``cat``.
        # Require the same basename as well; that keeps genuine hard-link
        # aliases working without collapsing distinct applets.
        if os.path.basename(actual_real) != os.path.basename(expected_real):
            return False
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


def _parent_pid(pid):
    """Best-effort parent PID for *pid* on macOS/Linux; None when unknown."""
    if sys.platform.startswith("linux"):
        try:
            data = Path("/proc/%d/stat" % int(pid)).read_text(encoding="utf-8")
            # The comm field may contain spaces and parentheses; the fields
            # after the last ')' are state, ppid, ...
            fields = data.rsplit(")", 1)[1].split()
            return int(fields[1])
        except (OSError, IndexError, ValueError):
            return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "ppid="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=2, check=False)
        text = _text(proc.stdout).strip().splitlines()
        return int(text[0].strip()) if text else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _ancestor_pids(pid=None):
    """Return this process's ancestor PIDs, or ``None`` when unknown.

    ``pgrep -a`` deliberately includes ancestors so a helper running inside
    the app's process tree can see the app.  That is useful for diagnostics,
    but an ancestor must not *by itself* prove that a reopened app is running:
    the old app would still be an ancestor while osascript reports it gone.
    Excluding ancestors keeps the process check a real second signal.

    A failed parent lookup (for example ``ps`` denied by a sandbox) makes the
    result explicitly unknown instead of silently returning an empty set.  A
    caller must treat ``None`` as "cannot corroborate", never as "no
    ancestors", or a stale ancestor PID would look like a fresh process.
    """
    seen = set()
    current = int(pid if pid is not None else os.getpid())
    for _ in range(64):
        parent = _parent_pid(current)
        if parent is None:
            return None
        if parent <= 0:
            return seen
        if parent in seen:
            # A cycle means the walk is incomplete and the evidence is unsafe.
            return None
        seen.add(parent)
        if parent == 1:
            return seen
        current = parent
    return None


def _process_check_result(code, stdout, ancestors=None):
    """Return the process check result as an explicit, fail-closed mapping.

    ``pgrep`` exits 0 on a match and prints one ``<pid> <command>`` line per
    match.  Require both the zero exit status and a numeric PID so a broken or
    stubbed ``pgrep`` that exits 0 with empty output cannot verify a restart.
    *ancestors* may be supplied for tests.  When it is omitted the ancestor
    walk runs; if that walk is incomplete the check is inconclusive
    (``ancestry_known=False``) and never confirms a restart.
    """
    result = {
        "ok": False,
        "ancestry_known": False,
        "pids": [],
        "matches": [],
    }
    try:
        if int(code) != 0:
            return result
    except (TypeError, ValueError):
        return result
    pids = []
    for line in _text(stdout).splitlines():
        tokens = line.split()
        if tokens and tokens[0].isdigit():
            pids.append(int(tokens[0]))
    result["pids"] = pids
    if not pids:
        return result
    if ancestors is None:
        ancestors = _ancestor_pids()
    if ancestors is None:
        return result
    ancestor_set = set(ancestors)
    matches = [pid for pid in pids if pid not in ancestor_set]
    result["ancestry_known"] = True
    result["matches"] = matches
    result["ok"] = bool(matches)
    return result


def _process_check_ok(code, stdout, ancestors=None):
    """Return True when the process check found a non-ancestor process.

    Thin boolean wrapper around :func:`_process_check_result`; an unknown
    ancestor walk is falsey (fail closed).  Ancestors of this helper are
    ignored unless *ancestors* is supplied.
    """
    return _process_check_result(code, stdout, ancestors)["ok"]


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
    if commands.get("process_check"):
        # Executed alongside osascript in the attempt loop; kept here so the
        # status file shows the exact argv even before the first attempt.
        record["process_check"] = list(commands["process_check"])
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
        process_check = commands.get("process_check")
        if process_check:
            detail["verify_returncode"] = verify_code
        verified_by = ""
        if _verify_ok(family, verify_code, verify_out, verify_err,
                      expected_executable=expected_exe,
                      helper_pid=os.getpid()):
            verified_by = "osascript"
        if process_check:
            # macOS executes both signals on every attempt.  osascript is
            # authoritative for ``reopened``; pgrep is corroboration only and
            # can never override a failed or false osascript result.  An
            # incomplete ancestor walk is recorded as inconclusive and cannot
            # confirm either.
            check_code, check_out, _ = _execute(process_check, runner, timeout)
            check = _process_check_result(check_code, check_out)
            detail["process_check_returncode"] = check_code
            detail["process_check_output"] = check_out.strip()[:240]
            detail["process_check_ok"] = check["ok"]
            detail["process_check_ancestry_known"] = check["ancestry_known"]
            detail["process_check_pids"] = check["pids"]
            detail["process_check_matches"] = check["matches"]
            # A stable top-level view for operators and tests, not only the
            # per-attempt detail.
            record["process_check_returncode"] = check_code
            record["process_check_output"] = check_out.strip()[:240]
            record["process_check_ok"] = check["ok"]
            record["process_check_ancestry_known"] = check["ancestry_known"]
            if check["ok"] and verified_by == "osascript":
                verified_by = "both"
        if verified_by:
            detail["verified"] = True
            detail["verified_by"] = verified_by
            record["verified_by"] = verified_by
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

        if process_check:
            detail["verified_by"] = ""
        last_error = "verification failed with exit code %d" % verify_code
        if process_check:
            if not detail["process_check_ancestry_known"]:
                last_error += (" (process check inconclusive: ancestor "
                               "lookup failed)")
            elif detail["process_check_ok"]:
                last_error += (" (process check found a process but "
                               "osascript did not confirm the app)")
            else:
                last_error += " (process check exit code %d)" % (
                    detail["process_check_returncode"])
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
