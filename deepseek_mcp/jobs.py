"""Detached background jobs and their status files.

A tool call writes a job spec, starts ``deepseek_mcp.py --job <spec>`` in a new
session (so it survives the MCP client reconnecting), waits up to
``wait_seconds``, and otherwise returns a run id for ``deepseek_job_status``.
"""

import datetime as dt
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import platforms, providers, safety, tasks
from .api import redact_secrets
from .config import ENTRY_SCRIPT, load_config

_status_path = None
_listeners = {}
_listener_lock = threading.Lock()


def add_listener(run_id, callback):
    """Register a progress callback for one running job (best-effort)."""
    if not run_id or callback is None:
        return
    with _listener_lock:
        _listeners.setdefault(str(run_id), []).append(callback)


def remove_listener(run_id, callback=None):
    with _listener_lock:
        if callback is None:
            _listeners.pop(str(run_id), None)
            return
        callbacks = _listeners.get(str(run_id)) or []
        _listeners[str(run_id)] = [item for item in callbacks if item is not callback]
        if not _listeners[str(run_id)]:
            _listeners.pop(str(run_id), None)


def _notify(run_id, state):
    with _listener_lock:
        callbacks = list(_listeners.get(str(run_id)) or [])
    for callback in callbacks:
        try:
            callback(dict(state))
        except Exception:
            pass


def set_child_pid(pid):
    """Record the currently running Harness child so cancel can stop it."""
    try:
        pid = int(pid)
    except Exception:
        return
    if not pid or pid <= 0:
        return
    if _status_path:
        write_status(_status_path, child_pid=pid)


def log(message):
    print("[deepseek-mcp] %s" % message, file=sys.stderr, flush=True)


def write_status(path, **fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        current = {}
    current.update(fields)
    current["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(redact_secrets(json.dumps(current, ensure_ascii=False, indent=2)))
        os.replace(tmp, str(path))
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _notify(path.name.split(".", 1)[0], current)


def progress(text):
    if _status_path:
        write_status(_status_path, status="running", progress=text)


def keep_awake():
    """macOS: stop idle/system sleep while this worker is alive."""
    if sys.platform != "darwin" or os.environ.get("DEEPSEEK_NO_CAFFEINATE"):
        return
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _worker_env(cfg):
    """Environment for a detached worker: only the keys it can use.

    The parent server may hold unrelated credentials (GitHub tokens, cloud
    keys); a worker only needs the DeepSeek key and the env vars named by the
    configured providers. The base URL is operator config, not a secret.
    """
    keep = ["DEEPSEEK_API_KEY"]
    try:
        for item in providers.load_providers(cfg):
            name = item.get("api_key_env")
            if name:
                keep.append(name)
    except Exception:
        pass
    env = safety.scrubbed_env(keep=tuple(keep))
    try:
        base_url = cfg.get("base_url")
        if base_url:
            env["DEEPSEEK_BASE_URL"] = str(base_url)
    except Exception:
        pass
    env.setdefault("NO_COLOR", "1")
    return env


def _signal_group(pid, sig, timeout=2.0):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        # Windows has no killpg; taskkill /T /F ends the whole tree.
        result = platforms.terminate_tree(pid, timeout=timeout)
        return bool(result.get("terminated"))
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (OSError, ValueError):
        try:
            os.kill(pid, sig)
            return True
        except (OSError, ValueError):
            return False


def cancel_job(run_id, cfg=None, reason="cancelled by client"):
    """Cancel one detached job and its Harness process group.

    Returns the final status dict. Cancellation is best-effort and idempotent:
    a terminal job is left alone, and a dead pid is recorded as cancelled.
    """
    cfg = cfg or load_config()
    run_id = str(run_id or "").strip()
    if not _valid_run_id(run_id):
        raise RuntimeError("Invalid job id: %r" % run_id)
    jobs_dir = Path(cfg.jobs_dir)
    status_path = jobs_dir / (run_id + ".status.json")
    try:
        state = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("status") in ("done", "error", "cancelled"):
        return state
    pid = state.get("pid")
    child_pid = state.get("child_pid")
    # The worker leads its own session; the Harness child has its own group.
    for target in (child_pid, pid):
        _signal_group(target, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not (pid and _pid_alive(pid)):
            break
        time.sleep(0.1)
    for target in (child_pid, pid):
        if target and _pid_alive(target):
            _signal_group(target, signal.SIGKILL)
    write_status(status_path, status="cancelled", progress="cancelled",
                 error=reason, cancel_reason=reason)
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "cancelled", "run_id": run_id}


def tool_cancel(args, cfg=None):
    cfg = cfg or load_config()
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("run_id is required")
    state = cancel_job(run_id, cfg, reason=args.get("reason") or "cancelled by client")
    return "[Job %s %s.]" % (run_id, state.get("status") or "cancelled")


def run_job(spec_path, handlers, cfg=None):
    """Entry point of a detached worker process."""
    global _status_path
    spec_path = Path(spec_path)
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        safe = spec_path.name
        if safe.endswith(".job.json"):
            safe = safe[:-9]
        write_status(spec_path.parent / (safe + ".status.json"),
                     status="error", progress="failed",
                     error="Invalid job spec: %s" % exc)
        return spec_path.parent / (safe + ".status.json")
    run_id = spec.get("run_id")
    if not _valid_run_id(run_id):
        safe = spec_path.name[:-9] if spec_path.name.endswith(".job.json") else spec_path.name
        status = spec_path.parent / (safe + ".status.json")
        write_status(status, status="error", progress="failed",
                     error="Invalid run id in job spec: %r" % run_id)
        return status
    try:
        cfg = cfg or load_config(config_path=spec.get("config_path"))
    except Exception as exc:
        status = spec_path.parent / (run_id + ".status.json")
        write_status(status, status="error", progress="failed", error="Config error: %s" % exc)
        return status
    jobs_dir = Path(cfg.jobs_dir)
    _status_path = jobs_dir / (run_id + ".status.json")
    try:
        existing = json.loads(_status_path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    if existing.get("status") == "cancelled":
        return _status_path
    keep_awake()
    write_status(_status_path, status="running", pid=os.getpid(), progress="started")
    fn = handlers.get(spec["tool"])
    try:
        if fn is None:
            raise RuntimeError("Unknown job tool: %s" % spec.get("tool"))
        args = spec.get("args") or {}
        # Pass the worker's config explicitly. Otherwise tools such as
        # tool_harness would reload the environment/default config and ignore
        # the server's --config file.
        try:
            accepts_cfg = "cfg" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts_cfg = False
        text = fn(args, cfg) if accepts_cfg else fn(args)
        write_status(_status_path, status="done", progress="finished", result=text)
    except Exception as exc:
        write_status(_status_path, status="error", progress="failed", error=str(exc))
    return _status_path


def _pid_alive(pid):
    return platforms.pid_alive(pid)


def start_job(tool, args, cfg=None, wait=None, python=None, entry=None,
              progress_cb=None, cancel_event=None, on_start=None):
    """Start a detached job and wait up to ``wait`` seconds for its result.

    ``progress_cb`` receives status-dict snapshots while this call waits (the
    server uses it to emit MCP progress notifications). ``cancel_event`` is
    polled between snapshots; when it is set the worker and its Harness child
    are killed. ``on_start(run_id, status_path)`` lets a task-augmented caller
    learn the id immediately.
    """
    cfg = cfg or load_config()
    jobs_dir = Path(cfg.jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(jobs_dir), 0o700)
    except OSError:
        pass
    from .store import new_id
    run_id = new_id()
    call_args = dict(args)
    wait = float(call_args.pop("wait_seconds", cfg.wait_seconds if wait is None else wait) or 0)
    call_args["_run_id"] = run_id
    spec = jobs_dir / (run_id + ".job.json")
    # Redact the arguments (they may carry prompts/secrets) but keep the
    # config path resolved and absolute. A detached worker can run under a
    # different HOME; storing "~/.config/..." made the child silently load a
    # different file (or defaults) even after read_config_file learned to
    # expand "~".  The spec itself is 0600 and is runtime state, not a
    # deliverable.
    safe_args = json.loads(redact_secrets(
        json.dumps(call_args, ensure_ascii=False)))
    config_path = cfg.get("_config_path")
    if config_path:
        config_path = str(Path(os.path.expanduser(str(config_path))).resolve())
    spec.write_text(json.dumps({
        "tool": tool,
        "run_id": run_id,
        "args": safe_args,
        "config_path": config_path,
    }, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(str(spec), 0o600)
    except OSError:
        pass
    status = jobs_dir / (run_id + ".status.json")
    write_status(status, status="queued", tool=tool,
                 title=call_args.get("title") or call_args.get("prompt", "")[:60],
                 created=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    log_path = jobs_dir / (run_id + ".log")
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    log_handle = os.fdopen(log_fd, "ab")
    try:
        try:
            # The worker only gets keys it can actually use; unrelated server
            # credentials are dropped instead of forked into every job.
            if entry:
                worker_cmd = [python or sys.executable, str(entry), "--job", str(spec)]
                worker_cwd = str(Path(entry).parent)
            elif ENTRY_SCRIPT.is_file():
                worker_cmd = [python or sys.executable, str(ENTRY_SCRIPT),
                              "--job", str(spec)]
                worker_cwd = str(ENTRY_SCRIPT.parent)
            else:
                # Installed wheel/pipx/uvx: there is no adjacent entry script.
                worker_cmd = [python or sys.executable, "-m", "deepseek_mcp",
                              "--job", str(spec)]
                worker_cwd = str(Path(cfg.jobs_dir))
            child = subprocess.Popen(
                worker_cmd,
                stdin=subprocess.DEVNULL, stdout=log_handle, stderr=log_handle,
                cwd=worker_cwd, env=_worker_env(cfg),
                **platforms.popen_kwargs(),
            )
        except Exception as exc:
            write_status(status, status="error", progress="failed", error="Could not start job: %s" % exc)
            raise RuntimeError("Could not start job: %s" % exc)
    finally:
        log_handle.close()
    if on_start:
        try:
            on_start(run_id, status)
        except Exception:
            pass

    last_seen = None

    def snapshot():
        nonlocal last_seen
        try:
            state = json.loads(status.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if progress_cb and state.get("updated") != last_seen:
            last_seen = state.get("updated")
            try:
                progress_cb(dict(state, run_id=run_id))
            except Exception:
                pass
        return state

    def cancelled(state):
        if cancel_event is not None and cancel_event.is_set():
            cancel_job(run_id, cfg, reason="cancelled by MCP client")
            raise RuntimeError("Job %s cancelled by client" % run_id)
        if state.get("status") == "cancelled":
            raise RuntimeError("Job %s cancelled" % run_id)

    deadline = time.time() + wait
    while time.time() < deadline:
        state = snapshot()
        cancelled(state)
        if state.get("status") == "done":
            try:
                child.wait(timeout=5)
            except Exception:
                pass
            return state.get("result", "")
        if state.get("status") == "error":
            try:
                child.wait(timeout=5)
            except Exception:
                pass
            raise RuntimeError(state.get("error") or "Job failed")
        pid = state.get("pid")
        if state.get("status") == "running" and pid and not _pid_alive(pid):
            raise RuntimeError("Job %s stopped without finishing (last step: %s)"
                               % (run_id, state.get("progress")))
        if child.poll() is not None and state.get("status") in ("queued", "running"):
            # Give an atexit status write a moment, then surface the failure.
            time.sleep(0.2)
            state = snapshot()
            if state.get("status") == "done":
                return state.get("result", "")
            if state.get("status") == "error":
                raise RuntimeError(state.get("error") or "Job failed")
            raise RuntimeError("Job %s exited before finishing (last step: %s)"
                               % (run_id, state.get("progress")))
        time.sleep(0.5)
    state = snapshot()
    cancelled(state)
    if state.get("status") == "done":
        return state.get("result", "")
    if state.get("status") == "error":
        raise RuntimeError(state.get("error") or "Job failed")
    return ("[DeepSeek job %s is still running in the background (%s). It keeps going even if "
            "this call returns. Check it with deepseek_job_status run_id=%s.]"
            % (run_id, state.get("progress", "started"), run_id))


def _valid_run_id(run_id):
    return bool(run_id) and Path(run_id).name == run_id and run_id not in (".", "..")


def tool_status(args, cfg=None):
    cfg = cfg or load_config()
    jobs_dir = Path(cfg.jobs_dir)
    run_id = (args.get("run_id") or "").strip()
    if run_id and not _valid_run_id(run_id):
        raise RuntimeError("Invalid job id: %r" % run_id)
    if not run_id:
        if not jobs_dir.exists():
            return "No background jobs yet."
        rows = []
        for path in sorted(jobs_dir.glob("*.status.json"), reverse=True)[:10]:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append("- %s | %s | %s | %s | %s" % (
                path.name.split(".")[0], state.get("status"), state.get("progress"),
                state.get("updated"), state.get("title")))
        return "Recent background jobs:\n" + "\n".join(rows) if rows else "No background jobs yet."
    path = jobs_dir / (run_id + ".status.json")
    if not path.exists():
        raise RuntimeError("No background job with id %r" % run_id)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("status") == "done":
        return state.get("result", "")
    if state.get("status") == "error":
        raise RuntimeError("Job %s failed: %s" % (run_id, state.get("error")))
    pid = state.get("pid")
    if pid and not _pid_alive(pid):
        return ("[Job %s stopped without finishing (last step: %s). The machine may have been "
                "shut down. Start it again.]" % (run_id, state.get("progress")))
    updated = state.get("updated") or ""
    if not pid and updated:
        try:
            when = dt.datetime.fromisoformat(updated)
            if (dt.datetime.now().astimezone() - when).total_seconds() > 60:
                return ("[Job %s did not start (last step: %s, updated %s).]"
                        % (run_id, state.get("progress"), updated))
        except Exception:
            pass
    return "[Job %s still running: %s (updated %s).]" % (
        run_id, state.get("progress"), state.get("updated"))
