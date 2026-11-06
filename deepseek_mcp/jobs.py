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

from . import events as events_mod
from . import platforms, providers, safety, tasks, work
from .api import redact_secrets
from .config import ENTRY_SCRIPT, load_config

_status_path = None
_work_ctx = None
_listeners = {}
_listener_lock = threading.Lock()


def _cfg_get(cfg, name, default=None):
    """Read a config key from Config, a dict or a SimpleNamespace test stub."""
    try:
        return cfg.get(name, default)
    except AttributeError:
        return getattr(cfg, name, default)


def _work_store(cfg):
    """Return the P1-1 work store rooted at ``<state_dir>/work``."""
    stale = _cfg_get(cfg, "work_stale_seconds")
    return work.WorkStore(
        Path(cfg.state_dir) / "work",
        heartbeat_seconds=float(_cfg_get(cfg, "work_heartbeat_seconds") or 30.0),
        stale_after=(float(stale) if stale is not None else None),
    )


def _event_log(work_id, cfg):
    if not work_id or not work.valid_work_id(str(work_id)):
        return None
    return events_mod.EventLog(Path(cfg.state_dir) / "work" / str(work_id))


def _event(work_id, cfg, event_type, payload=None):
    """Append a journal event; journal problems never break a job."""
    log_obj = _event_log(work_id, cfg)
    if log_obj is None:
        return None
    try:
        return log_obj.append(event_type, payload or {})
    except Exception:
        return None


def _result(work_id, cfg, text, status, run_id=None):
    log_obj = _event_log(work_id, cfg)
    if log_obj is None:
        return None
    try:
        return log_obj.write_result(text, status=status, run_id=run_id)
    except Exception:
        return None


def _finish_work(work_id, cfg, status, run_id=None):
    if not work_id:
        return None
    try:
        return _work_store(cfg).finish(work_id, status, run_id=run_id)
    except Exception:
        return None


def _has_terminal(work_id, cfg):
    log_obj = _event_log(work_id, cfg)
    if log_obj is None:
        return False
    try:
        return events_mod.terminal_after_latest_admission(
            log_obj.read_since(0)) is not None
    except Exception:
        return False


def _canonical_run_id(work_id, cfg):
    if not work_id:
        return None
    try:
        return _work_store(cfg).load(work_id).get("canonical_run_id")
    except Exception:
        return None


def _read_status(jobs_dir, run_id):
    try:
        state = json.loads((Path(jobs_dir) / (run_id + ".status.json")).read_text(
            encoding="utf-8"))
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _heartbeat():
    """Refresh the current worker's work lease (throttled by WorkStore)."""
    if not _work_ctx:
        return
    work_id, cfg, run_id = _work_ctx
    try:
        _work_store(cfg).heartbeat(work_id, run_id=run_id, pid=os.getpid())
    except Exception:
        pass


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
        _heartbeat()


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
    held_question = (state.get("status") == "done"
                     and state.get("progress") == "needs_input"
                     and state.get("work_id"))
    if state.get("status") in ("done", "error", "cancelled") and not held_question:
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
    work_id = state.get("work_id")
    if work_id:
        canonical = _canonical_run_id(work_id, cfg)
        owns_work = (not canonical or canonical == run_id or held_question)
        if owns_work:
            target_run = canonical or run_id
            if held_question or not _has_terminal(work_id, cfg):
                _event(work_id, cfg, "cancelled", {
                    "run_id": target_run, "reason": reason})
            _result(work_id, cfg, reason, "cancelled", target_run)
            _finish_work(work_id, cfg, "cancelled", target_run)
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
    global _status_path, _work_ctx
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
    spec_args = spec.get("args") or {}
    work_id = spec.get("work_id") or spec_args.get("_work_id")
    attempt_no = spec.get("attempt_no") or spec_args.get("_attempt_no")
    _work_ctx = (work_id, cfg, run_id) if work_id else None
    if existing.get("status") == "cancelled":
        if work_id:
            if not _has_terminal(work_id, cfg):
                _event(work_id, cfg, "cancelled", {
                    "run_id": run_id, "reason": existing.get("error")})
            _result(work_id, cfg, existing.get("error") or "cancelled",
                    "cancelled", run_id)
            _finish_work(work_id, cfg, "cancelled", run_id)
        return _status_path
    keep_awake()
    if work_id:
        _event(work_id, cfg, "phase", {
            "run_id": run_id, "attempt_no": attempt_no, "phase": "job_started"})
        try:
            _work_store(cfg).heartbeat(work_id, run_id=run_id,
                                       pid=os.getpid(), force=True)
        except Exception:
            pass
    running_fields = {"status": "running", "pid": os.getpid(),
                      "progress": "started"}
    if work_id:
        running_fields.update({"work_id": work_id, "attempt_no": attempt_no})
    write_status(_status_path, **running_fields)
    fn = handlers.get(spec["tool"])
    try:
        if fn is None:
            raise RuntimeError("Unknown job tool: %s" % spec.get("tool"))
        args = spec_args
        # Pass the worker's config explicitly. Otherwise tools such as
        # tool_harness would reload the environment/default config and ignore
        # the server's --config file.
        try:
            accepts_cfg = "cfg" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts_cfg = False
        text = fn(args, cfg) if accepts_cfg else fn(args)
        if _read_status(jobs_dir, run_id).get("status") == "cancelled":
            # A client cancel is terminal even if the handler happened to
            # finish; never resurrect the job as done.
            _finish_work(work_id, cfg, "cancelled", run_id)
            return _status_path
        canonical = _canonical_run_id(work_id, cfg)
        if work_id and canonical and canonical != run_id:
            # A resume/another attempt owns the journal and result.json. This
            # job wrapper only reflects the handler outcome in its own status.
            log_obj = _event_log(work_id, cfg)
            journal = log_obj.read_since(0) if log_obj is not None else []
            pending = events_mod.pending_question(journal)
            if pending is not None:
                write_status(_status_path, status="done",
                             progress="needs_input", result=text,
                             question=pending.get("question"))
            else:
                stored = log_obj.read_result() if log_obj is not None else None
                write_status(_status_path, status="done",
                             progress="finished",
                             result=(stored or {}).get("result") or text)
            return _status_path
        # A validated one-question ATTENTION block keeps the work open: the
        # job handler returned, but a human answer is still required.
        question = None
        if work_id:
            log_obj = _event_log(work_id, cfg)
            if log_obj is not None:
                try:
                    question = events_mod.record_attention(
                        log_obj, text, source=spec.get("tool"))
                except Exception:
                    question = None
        if question is not None:
            try:
                _work_store(cfg).hold(work_id, run_id=run_id,
                                      status="needs_input")
            except Exception:
                pass
            write_status(_status_path, status="done",
                         progress="needs_input", result=text,
                         question=question.get("payload", {}).get("question"))
        else:
            if work_id:
                log_obj = _event_log(work_id, cfg)
                if log_obj is not None:
                    try:
                        already = events_mod.terminal_after_latest_admission(
                            log_obj.read_since(0)) is not None
                        if not already:
                            events_mod.record_terminal(
                                log_obj,
                                job={"run_id": run_id, "status": "done",
                                     "result": text},
                                text=text)
                    except Exception:
                        _result(work_id, cfg, text, "ok", run_id)
                else:
                    _result(work_id, cfg, text, "ok", run_id)
                # The harness may already have closed the work with a richer
                # terminal status (ok_below_gate, ok_unreviewed, ...). Do not
                # downgrade it to a generic "ok" here.
                try:
                    current_work = _work_store(cfg).load(work_id)
                except Exception:
                    current_work = None
                if not (isinstance(current_work, dict)
                        and current_work.get("state") == "finished"):
                    _finish_work(work_id, cfg, "ok", run_id)
            write_status(_status_path, status="done", progress="finished",
                         result=text)
    except Exception as exc:
        message = str(exc)
        if _read_status(jobs_dir, run_id).get("status") == "cancelled":
            _finish_work(work_id, cfg, "cancelled", run_id)
            return _status_path
        canonical = _canonical_run_id(work_id, cfg)
        if work_id and canonical and canonical != run_id:
            write_status(_status_path, status="error", progress="failed",
                         error=message)
            return _status_path
        if work_id:
            log_obj = _event_log(work_id, cfg)
            if log_obj is not None and not _has_terminal(work_id, cfg):
                try:
                    events_mod.record_terminal(
                        log_obj,
                        job={"run_id": run_id, "status": "error",
                             "error": message},
                        text=message)
                except Exception:
                    _result(work_id, cfg, message, "error", run_id)
            else:
                _result(work_id, cfg, message, "error", run_id)
            try:
                current_work = _work_store(cfg).load(work_id)
            except Exception:
                current_work = None
            if not (isinstance(current_work, dict)
                    and current_work.get("state") == "finished"):
                _finish_work(work_id, cfg, "error", run_id)
        write_status(_status_path, status="error", progress="failed",
                     error=message)
    return _status_path


def _pid_alive(pid):
    return platforms.pid_alive(pid)


def _admit_job(tool, args, cfg):
    """Admit a harness request; return ``(admission, fingerprint)``."""
    workspace = args.get("workspace") or cfg.harness_workspace
    fingerprint = work.request_fingerprint(tool, args, workspace)
    store = _work_store(cfg)
    dedupe = bool(_cfg_get(cfg, "dedupe_active", True))
    admission = store.admit(
        fingerprint, idempotency_key=args.get("idempotency_key"),
        dedupe_active=dedupe, note="job admission")
    return admission, fingerprint


def _await_binding(store, work_id, timeout=2.0):
    """Wait briefly for the admitting caller to bind the minted run id."""
    deadline = time.time() + max(0.0, float(timeout))
    while time.time() <= deadline:
        item = store.load(work_id)
        if isinstance(item, dict) and item.get("canonical_run_id"):
            return item.get("canonical_run_id")
        time.sleep(0.02)
    return None


def _resume_work_identity(cfg, source_run_id):
    """Resolve the durable work identity of the run being resumed.

    The job wrapper needs it before spawning so its status file (and therefore
    the structured status/task bridge) can find the journal.  The checkpoint
    spec is the fallback when the run record is missing; old runs without a
    work id simply return ``(None, None)``.
    """
    source = str(source_run_id or "").strip()
    if not source or not _valid_run_id(source):
        return None, None
    work_id = None
    try:
        from .store import find_run
        saved = find_run(source, cfg)[0]
        work_id = saved.get("work_id")
    except Exception:
        work_id = None
    if not work_id:
        try:
            from . import checkpoints
            manifest = checkpoints.Checkpoint(
                Path(cfg.state_dir) / "checkpoints", source).load() or {}
            spec = (manifest.get("metadata") or {}).get("spec") or {}
            work_id = spec.get("work_id")
        except Exception:
            work_id = None
    if not work_id or not work.valid_work_id(str(work_id)):
        return None, None
    attempt_no = 1
    try:
        doc = _work_store(cfg).load(str(work_id)) or {}
        attempt_no = int(doc.get("attempt_no") or 1)
    except Exception:
        pass
    return str(work_id), attempt_no


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
    call_args = dict(args)
    wait = float(call_args.pop("wait_seconds", cfg.wait_seconds if wait is None else wait) or 0)
    admission = None
    fingerprint = None
    resume_identity = None
    resume_source = None
    if tool == "resume_deepseek":
        resume_source = call_args.get("run_id")
    elif call_args.get("resume_run_id"):
        resume_source = call_args.get("resume_run_id")
    if resume_source:
        try:
            work_id, attempt_no = _resume_work_identity(cfg, resume_source)
            if work_id:
                resume_identity = (work_id, attempt_no)
        except Exception as exc:
            log("resume work identity skipped: %s" % exc)
    if (tool == "harness_deepseek" and not call_args.get("resume_run_id")
            and resume_identity is None):
        try:
            admission, fingerprint = _admit_job(tool, call_args, cfg)
        except ValueError as exc:
            # A malformed explicit key is a caller error, not a reason to
            # silently disable deduplication.
            raise RuntimeError("Invalid idempotency_key: %s" % exc)
        except Exception as exc:
            # A state-dir problem is an admission limitation, never a reason
            # to refuse work that the old version would have started.
            log("work admission skipped: %s" % exc)
    if admission and admission.get("reused"):
        existing = admission.get("run_id")
        if not existing:
            try:
                existing = _await_binding(
                    _work_store(cfg), admission["work_id"], timeout=2.0)
            except Exception:
                existing = None
        if existing and not _valid_run_id(str(existing)):
            existing = None
        if existing:
            if on_start:
                try:
                    on_start(existing, jobs_dir / (existing + ".status.json"))
                except Exception:
                    pass
            state = _read_status(jobs_dir, existing)
            if (state.get("status") == "done"
                    and (state.get("progress") == "needs_input"
                         or state.get("question"))):
                question = state.get("question") or "a validated question"
                return ("[DeepSeek job %s already asked: %s. It is waiting for "
                        "an answer; use resume_deepseek run_id=%s answer=\"...\" "
                        "or deepseek_job_status run_id=%s events_since=0.]"
                        % (existing, question, existing, existing))
            if state.get("status") == "done":
                return state.get("result", "")
            if state.get("status") == "error":
                raise RuntimeError(state.get("error") or "Job failed")
            return ("[DeepSeek job %s is already running for this request "
                    "(work %s, attempt %s); not starting a duplicate. Check it "
                    "with deepseek_job_status run_id=%s.]"
                    % (existing, admission["work_id"],
                       admission.get("attempt_no"), existing))
        return ("[A matching DeepSeek job is being admitted (work %s); retry "
                "deepseek_job_status in a moment.]" % admission["work_id"])
    call_args.pop("idempotency_key", None)
    run_id = new_id()
    call_args["_run_id"] = run_id
    if resume_identity is not None:
        call_args["_work_id"] = resume_identity[0]
        call_args["_attempt_no"] = resume_identity[1]
    if admission:
        call_args["_work_id"] = admission["work_id"]
        call_args["_attempt_no"] = admission["attempt_no"]
        if fingerprint:
            call_args["_fingerprint"] = fingerprint
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
    spec_data = {
        "tool": tool,
        "run_id": run_id,
        "args": safe_args,
        "config_path": config_path,
    }
    if resume_identity is not None:
        spec_data.update({
            "work_id": resume_identity[0],
            "attempt_no": resume_identity[1],
        })
    if admission:
        spec_data.update({
            "work_id": admission["work_id"],
            "attempt_no": admission["attempt_no"],
            "fingerprint": fingerprint,
        })
    spec.write_text(json.dumps(spec_data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(str(spec), 0o600)
    except OSError:
        pass
    status = jobs_dir / (run_id + ".status.json")
    queued = {
        "status": "queued", "tool": tool,
        "title": call_args.get("title") or call_args.get("prompt", "")[:60],
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if resume_identity is not None:
        queued.update({
            "work_id": resume_identity[0],
            "attempt_no": resume_identity[1],
        })
    if admission:
        queued.update({
            "work_id": admission["work_id"],
            "attempt_no": admission["attempt_no"],
            "request_fingerprint": fingerprint,
        })
    write_status(status, **queued)
    if admission:
        try:
            _work_store(cfg).bind_run(
                admission["work_id"], admission["attempt_no"], run_id)
        except Exception as exc:
            log("work binding skipped: %s" % exc)
        _event(admission["work_id"], cfg, "admitted", {
            "run_id": run_id,
            "attempt_no": admission["attempt_no"],
            "tool": tool,
        })
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
            message = "Could not start job: %s" % exc
            if admission:
                _event(admission["work_id"], cfg, "failed", {
                    "run_id": run_id, "error": message})
                _result(admission["work_id"], cfg, message, "error", run_id)
                _finish_work(admission["work_id"], cfg, "error", run_id)
            write_status(status, status="error", progress="failed", error=message)
            raise RuntimeError(message)
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


def _structured_status(args, state, run_id, cfg):
    """Cursor-based journal view plus the pure stage/attention projection."""
    try:
        since = max(0, int(args.get("events_since") or 0))
    except (TypeError, ValueError):
        raise RuntimeError("events_since must be a non-negative integer")
    work_id = state.get("work_id")
    log_obj = _event_log(work_id, cfg) if work_id else None
    all_events = []
    if log_obj is not None:
        try:
            all_events = log_obj.read_since(0)
        except Exception:
            all_events = []
    fresh = [event for event in all_events
             if int(event.get("seq") or 0) > since]
    result_doc = None
    if log_obj is not None:
        try:
            result_doc = log_obj.read_result()
        except Exception:
            result_doc = None
    run = None
    try:
        from .store import find_run
        run = find_run(run_id, cfg)[0]
    except Exception:
        run = None
    projection = events_mod.project(run, state, all_events, result_doc)
    next_seq = 0
    if log_obj is not None:
        try:
            next_seq = log_obj.next_seq()
        except Exception:
            next_seq = 0
    payload = {
        "events": fresh,
        "next_seq": next_seq,
        "stage": projection["stage"],
        "attention": projection["attention"],
        "question": projection["question"],
        "options": projection["options"],
        "next_action": projection["next_action"],
        "terminal": projection["terminal"],
        "result": projection["result"],
    }
    if work_id:
        payload["work_id"] = work_id
    payload["run_id"] = run_id
    # P2-3: the reservation view is part of the structured status contract.
    # The plain (no events_since) text response stays byte-for-byte compatible.
    payload["cost"] = (run or {}).get("cost_ledger")
    if (run or {}).get("admission_overrun"):
        payload["admission_overrun"] = run.get("admission_overrun")
    try:
        from . import health as health_mod
        board = health_mod.HealthBoard(cfg.state_dir)
        payload["health"] = board.snapshot().get("components") or {}
    except Exception:
        payload["health"] = {}
    return payload


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
    if "events_since" in args and args.get("events_since") is not None:
        return _structured_status(args, state, run_id, cfg)
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
