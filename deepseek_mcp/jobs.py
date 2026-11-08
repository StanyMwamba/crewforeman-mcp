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
from . import admission as admission_mod
from . import platforms, providers, safety, tasks, work
from .api import redact_secrets
from .config import ENTRY_SCRIPT, load_config

_status_path = None
_work_ctx = None
_admission_store = None
_admission_work_id = None
_admission_token = None
_admission_last_hb = 0.0
_admission_released = False
_admission_controllers = {}
_admission_controllers_lock = threading.Lock()


def _admission_store_for(cfg):
    root = os.path.join(str(_cfg_get(cfg, "state_dir", os.path.join(os.getcwd(), "state"))), "queue")
    return admission_mod.AdmissionStore(
        root,
        heartbeat_seconds=float(_cfg_get(cfg, "admission_heartbeat_seconds", 30.0) or 30.0),
        lease_seconds=float(_cfg_get(cfg, "admission_lease_seconds", 90.0) or 90.0),
        dependency_probe=admission_mod._status_probe(cfg))


def _admission_controller(cfg):
    """One controller (and therefore one dispatcher) per state directory."""
    root = str(_cfg_get(cfg, "state_dir", os.path.join(os.getcwd(), "state")))
    with _admission_controllers_lock:
        controller = _admission_controllers.get(root)
        if controller is None:
            controller = admission_mod.AdmissionController(
                cfg, store=_admission_store_for(cfg),
                idle_probe=admission_mod._idle_probe(cfg),
                event_sink=_admission_event_sink(cfg),
                fail_sink=_admission_fail_sink(cfg))

            def _review_probe(record, _cfg=cfg, _controller=controller):
                """True only for a live lease whose run is in the review phase."""
                try:
                    work_id = record.get("work_id")
                    lease = _controller.store._load_lease(work_id)
                    if not lease or not _controller.store._lease_live(lease):
                        return False
                    run_id = (record.get("run_id")
                              or record.get("canonical_run_id"))
                    if not run_id:
                        return False
                    in_flight = _review_gate_state(_cfg, run_id)[0]
                    return bool(in_flight)
                except Exception:
                    return False

            controller.review_probe = _review_probe
            _admission_controllers[root] = controller
        return controller


def _admission_note_status(status):
    global _admission_last_hb, _admission_released
    if _admission_store is None or not _admission_work_id or _admission_released:
        return
    try:
        if status in ("done", "error", "cancelled"):
            _admission_store.release(_admission_work_id, status=status,
                                     token=_admission_token)
            _admission_released = True
        elif status in (None, "running") and _admission_token is not None:
            interval = max(1.0, float(_admission_store.heartbeat_seconds) / 2.0)
            if time.monotonic() - _admission_last_hb >= interval:
                if _admission_store.heartbeat(_admission_work_id, _admission_token, pid=os.getpid()):
                    _admission_last_hb = time.monotonic()
    except Exception:
        pass
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


def _admission_event_sink(cfg):
    """Mirror admission events into the P1 work journal when an id is valid."""
    def sink(event_type, payload=None):
        data = dict(payload or {})
        run_id = data.get("run_id") or data.get("work_id")
        work_id = None
        if run_id:
            try:
                status = json.loads((Path(cfg.jobs_dir) /
                                     (str(run_id) + ".status.json"))
                                    .read_text(encoding="utf-8"))
                work_id = status.get("work_id")
            except Exception:
                work_id = None
        work_id = work_id or data.get("work_id") or run_id
        if work_id:
            _event(str(work_id), cfg, event_type, data)
    return sink


def _admission_fail_sink(cfg):
    """Write a terminal error for a queued job whose dependency failed."""
    def sink(record, message):
        run_id = record.get("run_id") or record.get("work_id")
        if not run_id:
            return
        status_path = Path(cfg.jobs_dir) / (str(run_id) + ".status.json")
        if status_path.exists():
            write_status(status_path, status="error", progress="failed",
                         error=str(message), waiting_on=None,
                         dependency_status="error")
        work_id = record.get("work_id")
        if work_id:
            _event(str(work_id), cfg, "failed", {
                "run_id": run_id, "error": str(message),
                "phase": "admission_dependency"})
    return sink


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
    _admission_note_status(fields.get("status"))


def progress(text):
    if _status_path:
        write_status(_status_path, status="running", progress=text)
        _heartbeat()


def keep_awake():
    """macOS: stop idle/system sleep while this worker is alive."""
    if sys.platform != "darwin" or os.environ.get("DEEPSEEK_NO_CAFFEINATE"):
        return
    try:
        proc = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        # caffeinate is intentionally detached and exits when this worker
        # exits; do not wait for it.  Mark the Popen handle as reaped so
        # CPython's __del__ does not emit "subprocess N is still running"
        # ResourceWarnings when the handle is collected (same pattern as
        # restart_helper.spawn_restart_helper).
        try:
            proc.returncode = 0
        except Exception:
            pass
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
    try:
        queue_id = state.get("admission_work_id") or run_id
        _admission_store_for(cfg).release(queue_id, status="cancelled")
    except Exception:
        pass
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
    """Entry point of a detached worker process.

    Worker globals are process-wide; restoring them in ``finally`` keeps an
    in-process caller (and sequential tests) from leaving ``progress()`` and
    heartbeat writes pointed at a stale job status file.
    """
    global _status_path, _work_ctx
    global _admission_store, _admission_work_id, _admission_token
    global _admission_last_hb, _admission_released
    saved = (_status_path, _work_ctx, _admission_store, _admission_work_id,
             _admission_token, _admission_last_hb, _admission_released)
    try:
        return _run_job_impl(spec_path, handlers, cfg)
    finally:
        (_status_path, _work_ctx, _admission_store, _admission_work_id,
         _admission_token, _admission_last_hb, _admission_released) = saved


def _review_gate_state(cfg, run_id):
    """Return ``(in_flight, review_expected, recorded, run_status, run_error)``.

    Best-effort read of the durable run record.  A missing, unreadable or
    malformed record means "no gate applies" so old runs and ad-hoc specs keep
    their previous terminal behaviour.
    """
    try:
        from .store import find_run
        run = find_run(run_id, cfg)[0]
    except Exception:
        return False, False, False, None, None
    if not isinstance(run, dict):
        return False, False, False, None, None
    phase = str(run.get("phase") or "")
    review_status = str(run.get("review_status") or "").lower()
    rounds = run.get("review_rounds")
    recorded = (
        any(isinstance(item, dict) and item.get("kind") == "review"
            for item in rounds)
        if isinstance(rounds, list) else False)
    in_flight = bool(
        run.get("review_in_flight")
        or run.get("review_phase_in_flight")
        or (phase.startswith("review-") and not phase.endswith("_done"))
        or review_status in ("pending", "running", "in_progress"))
    review_expected = bool(run.get("independent_review"))
    return (in_flight, review_expected, recorded, run.get("status"),
            run.get("review_error") or run.get("status_detail")
            or run.get("error"))


def _review_gate_error(gate, canonical=False):
    """Return the refusal message when *gate* must not close the job.

    ``ok_unreviewed`` is a recorded reviewer failure, so it stays a successful
    job; only a run that genuinely ended in ``error`` is propagated.
    """
    in_flight, review_expected, recorded, run_status, run_error = gate
    if run_status == "gate_failed":
        detail = str(run_error or "").strip() or "see status_detail"
        return ("deterministic gate failed; reviewer was not called: %s"
                % detail)
    if in_flight:
        return ("review phase is still in flight; refusing a "
                "terminal done status")
    success_shaped = ("ok", "ok_rescued", "ok_unreviewed", "ok_below_gate")
    if (review_expected and not recorded
            and run_status in success_shaped):
        return ("review was requested but no review round was recorded")
    if run_status == "error":
        # A terminal run error belongs to the job even when no independent
        # review was requested (install conflict, gate error, builder error).
        detail = str(run_error or "").strip()
        subject = "the canonical run" if canonical else "the run"
        message = ("%s ended with status 'error'" % subject)
        if detail:
            message += ": " + detail
        return message
    return None


def _run_job_impl(spec_path, handlers, cfg=None):
    """Implementation of :func:`run_job`; owns the worker globals."""
    global _status_path, _work_ctx
    global _admission_store, _admission_work_id, _admission_token
    global _admission_last_hb, _admission_released
    _admission_store = None
    _admission_work_id = None
    _admission_token = None
    _admission_last_hb = 0.0
    _admission_released = False
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
    try:
        _aw = spec.get("admission_work_id")
        _at = spec.get("admission_token")
        if _aw and _at is not None:
            _admission_store = _admission_store_for(cfg)
            _admission_work_id = str(_aw)
            _admission_token = int(_at)
            _admission_last_hb = 0.0
            _admission_released = False
    except Exception:
        _admission_store = None
    if existing.get("status") == "cancelled":
        try:
            _admission_store.release(_admission_work_id, status="cancelled",
                                     token=_admission_token)
        except Exception:
            pass
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
            # It must still honour the canonical run's terminal review gate:
            # a stale attempt cannot advertise done while that run failed.
            canonical_error = _review_gate_error(
                _review_gate_state(cfg, canonical), canonical=True)
            if canonical_error:
                write_status(_status_path, status="error", progress="failed",
                             error=canonical_error)
                return _status_path
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
        # Durable review gate: the handler returning does not mean the
        # independent review phase is done.  Poll briefly for finalization,
        # then refuse to write a terminal "done" while a review is in flight,
        # was expected but never recorded, or ended in a terminal run error.
        gate = _review_gate_state(cfg, run_id)
        gate_deadline = time.monotonic() + 2.0
        while gate[0] and time.monotonic() < gate_deadline:
            time.sleep(0.1)
            gate = _review_gate_state(cfg, run_id)
        message = _review_gate_error(gate)
        if message:
            if work_id:
                _result(work_id, cfg, message, "error", run_id)
                _finish_work(work_id, cfg, "error", run_id)
            write_status(_status_path, status="error", progress="failed",
                         error=message)
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


def _spawn_worker(spec, status, log_path, cfg, entry=None, python=None):
    """Popen the detached ``--job`` worker; shared by immediate and queued starts."""
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    log_handle = os.fdopen(log_fd, "ab")
    try:
        if entry:
            worker_cmd = [python or sys.executable, str(entry), "--job", str(spec)]
            worker_cwd = str(Path(entry).parent)
        elif ENTRY_SCRIPT.is_file():
            worker_cmd = [python or sys.executable, str(ENTRY_SCRIPT), "--job", str(spec)]
            worker_cwd = str(ENTRY_SCRIPT.parent)
        else:
            worker_cmd = [python or sys.executable, "-m", "deepseek_mcp",
                          "--job", str(spec)]
            worker_cwd = str(Path(cfg.jobs_dir))
        return subprocess.Popen(
            worker_cmd,
            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=log_handle,
            cwd=worker_cwd, env=_worker_env(cfg),
            **platforms.popen_kwargs(),
        )
    finally:
        log_handle.close()


def _rewrite_admission_spec(record, spec_path):
    """Persist the claimed work id/token into the queueable job spec."""
    path = Path(spec_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["admission_work_id"] = record.get("work_id")
    data["admission_token"] = record.get("fencing_token")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _materialize_pipeline_worktree(cfg, record_data, run_id):
    """Create a deferred pipeline worktree at dispatch time (depends_on)."""
    try:
        from . import pipeline as pipeline_mod
        args = record_data.get("args") if isinstance(record_data.get("args"), dict) else {}
        pipe = args.get("_pipeline") or record_data.get("pipeline") or {}
        if not isinstance(pipe, dict) or not pipe.get("eligible"):
            return record_data
        worktree = pipe.get("worktree")
        if worktree and os.path.isdir(str(worktree)):
            return record_data
        canonical = (pipe.get("canonical_workspace")
                     or args.get("_canonical_workspace"))
        if not canonical:
            pipe.update({"eligible": False,
                         "unavailable_reason": "pipeline_lost"})
            return record_data
        created = pipeline_mod.create_worktree(cfg, canonical, run_id)
        if not created.get("ok"):
            pipe.update({"eligible": False, "worktree": None,
                         "unavailable_reason": created.get("reason")
                         or "worktree_add_failed"})
            return record_data
        pipe.update({
            "eligible": True, "decided": True,
            "worktree": created.get("path"),
            "base_commit": created.get("base_commit") or pipe.get("base_commit"),
            "base_ref": created.get("base_ref") or pipe.get("base_ref"),
            "canonical_workspace": canonical,
        })
        args["workspace"] = created.get("path")
        args["_canonical_workspace"] = canonical
        args["_pipeline"] = dict(pipe)
        args["_pipeline_decided"] = True
        record_data["args"] = args
        record_data["pipeline"] = dict(pipe)
        return record_data
    except Exception as exc:
        try:
            pipe = record_data.setdefault("pipeline", {})
            pipe.update({"eligible": False,
                         "unavailable_reason": "pipeline_setup_failed",
                         "detail": redact_secrets(str(exc))[:300]})
        except Exception:
            pass
        return record_data


def _write_spec(path, data):
    path = Path(path)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _status_is_cancelled(status_path):
    try:
        state = json.loads(Path(status_path).read_text(encoding="utf-8"))
    except Exception:
        return False
    return state.get("status") == "cancelled"


def _dispatch_admitted(record, cfg):
    """Spawn a worker for a record claimed by the persistent dispatcher."""
    spec_path = record.get("spec_path")
    if not spec_path:
        try:
            _admission_store_for(cfg).release(record.get("work_id"), status="failed")
        except Exception:
            pass
        return None
    _rewrite_admission_spec(record, spec_path)
    data = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    run_id = data.get("run_id")
    if data.get("pipeline") or (data.get("args") or {}).get("_pipeline"):
        data = _materialize_pipeline_worktree(cfg, data, run_id)
        try:
            _write_spec(spec_path, data)
        except Exception as exc:
            log("pipeline worktree spec update skipped: %s" % exc)
    jobs_dir = Path(cfg.jobs_dir)
    status = jobs_dir / (run_id + ".status.json")
    if _status_is_cancelled(status):
        try:
            _admission_store_for(cfg).release(
                record.get("work_id"), status="cancelled",
                token=record.get("fencing_token"))
        except Exception:
            pass
        return None
    entry = data.get("worker_entry") or None
    python = data.get("worker_python") or None
    child = _spawn_worker(spec_path, status, jobs_dir / (run_id + ".log"), cfg,
                          entry=entry, python=python)
    try:
        if record.get("fencing_token") is not None:
            _admission_store_for(cfg).heartbeat(
                record.get("work_id"), record.get("fencing_token"),
                pid=child.pid)
    except Exception:
        pass
    try:
        write_status(status, status="running", pid=child.pid, queue_position=0,
                     priority=record.get("priority"),
                     urgent=bool(record.get("urgent")),
                     depends_on=record.get("depends_on"),
                     waiting_on=None,
                     priority_label=(record.get("priority_label") or
                                     admission_mod.priority_label(
                                         record.get("priority"),
                                         bool(record.get("urgent")))))
    except Exception:
        pass
    return child


def resume_admission(cfg):
    """Start the lazy persistent-queue dispatcher (idempotent)."""
    controller = _admission_controller(cfg)
    controller.start(spawn=lambda record: _dispatch_admitted(record, cfg))
    return controller


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
    urgent = call_args.get("urgent", False)
    if urgent is None:
        urgent = False
    if not isinstance(urgent, bool):
        raise RuntimeError("urgent must be a boolean")
    depends_on = call_args.get("depends_on")
    if depends_on is not None:
        if not isinstance(depends_on, str) or not _valid_run_id(depends_on.strip()):
            raise RuntimeError("Invalid depends_on run id: %r" % (depends_on,))
        depends_on = depends_on.strip()
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
    # P3.5 pipeline: decide write-set/eligibility once, before the builder,
    # and run an eligible job in its own detached worktree. Non-eligible jobs
    # keep today's canonical-workspace behaviour.
    pipeline_info = {}
    if tool == "harness_deepseek" and not call_args.get("resume_run_id"):
        try:
            from . import pipeline as pipeline_mod
            canonical = os.path.abspath(os.path.expanduser(str(
                call_args.get("workspace") or cfg.harness_workspace)))
            declared = pipeline_mod.declared_write_set(call_args, cfg)
            pre_source = declared.get("source") in ("arg", "slice", "prompt")
            if not (pre_source and declared.get("write_set")):
                # No pre-builder declaration: not pipeline-eligible, and do
                # not run any git probe. This preserves the serial path
                # byte-for-byte (and keeps existing job tests subprocess-free).
                pipeline_info = {
                    "enabled": bool(_cfg_get(cfg, "pipeline_enabled", True)),
                    "eligible": False, "decided": True,
                    "unavailable_reason": "no_declared_write_set",
                    "slot": "base", "worktree": None,
                    "canonical_workspace": canonical,
                    "base_ref": None, "base_commit": None,
                    "write_set": list(declared.get("write_set") or []),
                    "write_set_source": declared.get("source") or "none",
                    "write_set_impl": declared.get("impl") or "builtin",
                    "write_set_invalid": declared.get("invalid") or [],
                    "install": {"status": "n/a", "applied": [],
                                "reason": None},
                }
                call_args["_pipeline_decided"] = True
                call_args["_pipeline"] = dict(pipeline_info)
                raise StopIteration
            stub = {"id": run_id, "workspace": canonical,
                    "pipeline": {"slot": "base"}}
            info = pipeline_mod.prepare_args(cfg, call_args, stub)
            if not isinstance(info, dict):
                info = {}
            info["decided"] = True
            info["slot"] = "base"
            info["canonical_workspace"] = canonical
            if (pre_source and info.get("eligible")
                    and declared.get("write_set")):
                created = ({"ok": False, "reason": "deferred_until_dispatch"}
                           if depends_on else
                           pipeline_mod.create_worktree(cfg, canonical, run_id))
                if created.get("ok"):
                    info["worktree"] = created.get("path")
                    info["base_commit"] = (created.get("base_commit")
                                           or info.get("base_commit"))
                    info["base_ref"] = (created.get("base_ref")
                                        or info.get("base_ref"))
                    call_args["workspace"] = created.get("path")
                    call_args["_canonical_workspace"] = canonical
                    call_args["_pipeline_decided"] = True
                    call_args["_pipeline"] = dict(info)
                elif depends_on and created.get("reason") == "deferred_until_dispatch":
                    info["worktree"] = None
                    info["deferred_worktree"] = True
                    call_args["_pipeline_decided"] = True
                    call_args["_pipeline"] = dict(info)
                else:
                    info["eligible"] = False
                    info["unavailable_reason"] = (
                        created.get("reason") or "worktree_add_failed")
                    info["detail"] = created.get("detail")
                    info["worktree"] = None
                    call_args["_pipeline_decided"] = True
                    call_args["_pipeline"] = dict(info)
            else:
                if not pre_source:
                    info["eligible"] = False
                    info["unavailable_reason"] = (
                        info.get("unavailable_reason") or "no_declared_write_set")
                info["worktree"] = None
                call_args["_pipeline_decided"] = True
                call_args["_pipeline"] = dict(info)
            pipeline_info = dict(info)
        except StopIteration:
            pass
        except Exception as exc:
            pipeline_info = {
                "enabled": bool(_cfg_get(cfg, "pipeline_enabled", True)),
                "eligible": False, "decided": True,
                "unavailable_reason": "pipeline_setup_failed",
                "detail": redact_secrets(str(exc))[:300],
                "slot": "base", "worktree": None,
                "canonical_workspace": str(
                    call_args.get("workspace") or cfg.harness_workspace),
                "write_set": [], "write_set_source": "none",
                "write_set_impl": "builtin",
                "install": {"status": "n/a", "applied": [], "reason": None},
            }
    if resume_source and not pipeline_info:
        try:
            from .store import find_run
            saved_resume = find_run(str(resume_source), cfg)[0] or {}
            pipe = saved_resume.get("pipeline") or {}
        except Exception:
            pipe = {}
        if isinstance(pipe, dict) and pipe.get("eligible"):
            worktree = pipe.get("worktree")
            if worktree and os.path.isdir(str(worktree)):
                call_args["workspace"] = str(worktree)
                if pipe.get("canonical_workspace"):
                    call_args["_canonical_workspace"] = pipe.get(
                        "canonical_workspace")
                call_args["_pipeline"] = dict(pipe)
                call_args["_pipeline_decided"] = True
                pipeline_info = dict(pipe)
            else:
                lost = dict(pipe, eligible=False,
                            unavailable_reason="pipeline_lost")
                call_args["_pipeline"] = lost
                call_args["_pipeline_lost"] = True
                pipeline_info = lost

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
        "worker_entry": str(entry) if entry else None,
        "worker_python": str(python) if python else None,
        "pipeline": pipeline_info or None,
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
    spec_data["admission_work_id"] = (admission or {}).get("work_id") or run_id
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
        "admission_work_id": run_id,
        "write_set": list((pipeline_info or {}).get("write_set") or []),
        "pipeline_slot": (pipeline_info or {}).get("slot") or "base",
        "pipeline_eligible": bool((pipeline_info or {}).get("eligible")),
        "install_status": (("pending" if (pipeline_info or {}).get("eligible")
                            else "n/a")),
        "base_commit": (pipeline_info or {}).get("base_commit"),
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
    priority = (admission_mod.PRIORITY_RESUME
                if tool == "resume_deepseek" or call_args.get("resume_run_id")
                else admission_mod.PRIORITY_BUILD)
    # One queue record per run attempt. The durable P1 work id can be shared
    # by several attempts (dedupe disabled); the run id is the queue identity.
    queue_work_id = run_id
    controller = _admission_controller(cfg)
    spawned_child = {"child": None}

    def _spawn_record(record):
        # A shared dispatcher may start a record created by an earlier call;
        # always use the spec/status paths stored on that record.
        record_spec = Path(record.get("spec_path") or spec)
        record_run_id = record.get("run_id") or run_id
        record_status = jobs_dir / (record_run_id + ".status.json")
        record_log = jobs_dir / (record_run_id + ".log")
        try:
            _rewrite_admission_spec(record, record_spec)
        except Exception as exc:
            log("admission token persist skipped: %s" % exc)
        try:
            record_data = json.loads(record_spec.read_text(encoding="utf-8"))
        except Exception:
            record_data = {}
        if record_data.get("pipeline") or (
                record_data.get("args") or {}).get("_pipeline"):
            record_data = _materialize_pipeline_worktree(
                cfg, record_data, record_run_id)
            try:
                _write_spec(record_spec, record_data)
            except Exception as exc:
                log("pipeline worktree spec update skipped: %s" % exc)
        record_entry = record_data.get("worker_entry") or None
        record_python = record_data.get("worker_python") or None
        if _status_is_cancelled(record_status):
            return None
        child = _spawn_worker(record_spec, record_status, record_log, cfg,
                              record_entry, record_python)
        if record_run_id == run_id:
            spawned_child["child"] = child
        try:
            if record.get("fencing_token") is not None:
                controller.store.heartbeat(record.get("work_id"),
                                           record.get("fencing_token"),
                                           pid=child.pid)
        except Exception:
            pass
        try:
            write_status(record_status, status="running", pid=child.pid,
                         queue_position=0,
                         admission_work_id=record_run_id,
                         priority=record.get("priority", priority),
                         urgent=bool(record.get("urgent")),
                         depends_on=record.get("depends_on"),
                         waiting_on=None,
                         write_set=record.get("write_set") or [],
                         pipeline_slot=("extra" if record.get(
                             "pipeline_extra") else "base"),
                         pipeline_extra=bool(record.get("pipeline_extra")),
                         pipeline_eligible=bool(record.get("pipeline_eligible")),
                         base_commit=record.get("base_commit"),
                         install_status=record.get("install_status") or "n/a",
                         priority_label=(record.get("priority_label") or
                                         admission_mod.priority_label(
                                             record.get("priority", priority),
                                             bool(record.get("urgent")))))
        except Exception:
            pass
        return child

    def _fail_start(message):
        """Record a failed start without ever spawning a worker."""
        message = redact_secrets(str(message))
        failed_work = ((admission or {}).get("work_id")
                       or spec_data.get("work_id"))
        if failed_work:
            _event(failed_work, cfg, "failed", {
                "run_id": run_id, "error": message})
            _result(failed_work, cfg, message, "error", run_id)
            _finish_work(failed_work, cfg, "error", run_id)
        try:
            write_status(status, status="error", progress="failed",
                         error=message)
        except Exception:
            pass
        return message

    # The bounded queue is scoped to Harness jobs. ask/consult/swarm keep the
    # pre-P3 direct spawn path: an operator cap for Harness children must not
    # silently queue unrelated tools.
    if tool in ("harness_deepseek", "resume_deepseek"):
        try:
            queue_pipeline = {}
            if pipeline_info:
                queue_pipeline = {
                    "write_set": list(pipeline_info.get("write_set") or []),
                    "write_set_source": pipeline_info.get("write_set_source"),
                    "write_set_impl": pipeline_info.get("write_set_impl"),
                    "pipeline_eligible": bool(pipeline_info.get("eligible")),
                    "pipeline_extra": False,
                    "base_commit": pipeline_info.get("base_commit"),
                    "base_ref": pipeline_info.get("base_ref"),
                    "canonical_workspace": pipeline_info.get(
                        "canonical_workspace"),
                    "defer_next_start": bool(pipeline_info.get(
                        "defer_next_start")),
                    "job_max_cost_usd": (call_args.get("job_max_cost_usd")
                                         if call_args.get("job_max_cost_usd")
                                         is not None
                                         else cfg.get("job_max_cost_usd")),
                    "install_status": ("pending"
                                       if pipeline_info.get("eligible")
                                       else "n/a"),
                }
            submitted = controller.submit(
                queue_work_id, priority=priority, run_id=run_id,
                spec_path=str(spec), tool=tool, spawn=_spawn_record,
                urgent=urgent, depends_on=depends_on,
                pipeline=queue_pipeline)
        except admission_mod.DependencyError as exc:
            # A dependency rejection is caller-visible and must not be
            # disguised as an admission/lock failure.
            message = _fail_start(str(exc))
            log(message)
            raise RuntimeError(message)
        except Exception as exc:
            # Fail closed: if the queue or its lock cannot be acquired, do not
            # bypass max_harness_jobs with an unbounded direct spawn.
            try:
                controller.store.release(queue_work_id, status="failed")
            except Exception:
                pass
            message = _fail_start(
                "Admission unavailable; job not started: %s" % exc)
            log(message)
            raise RuntimeError(message)
    else:
        try:
            _spawn_record({"work_id": queue_work_id, "fencing_token": None})
        except Exception as exc:
            message = _fail_start("Could not start job: %s" % exc)
            raise RuntimeError(message)
        submitted = {"state": "started", "position": 0, "priority": priority,
                     "priority_label": admission_mod.priority_label(priority)}
    # A task-augmented caller must learn the run id before either return path
    # (queued or started), otherwise the MCP task completes on the queue
    # marker while the detached job is still pending.
    if on_start:
        try:
            on_start(run_id, status)
        except Exception:
            pass
    if submitted.get("state") == "queued":
        fields = {}
        try:
            fields = controller.store.status_fields(queue_work_id)
        except Exception:
            pass
        position = fields.get("queue_position")
        if position is None:
            position = submitted.get("position")
        if position is None:
            position = 1
        label = (submitted.get("priority_label")
                 or admission_mod.priority_label(priority, urgent))
        waiting_on = submitted.get("waiting_on") or fields.get("waiting_on")
        dependency_status = (submitted.get("dependency_status")
                             or fields.get("dependency_status"))
        write_status(status, status="queued", queue_position=position,
                     priority=submitted.get("priority"),
                     priority_label=label,
                     urgent=bool(submitted.get("urgent", urgent)),
                     depends_on=depends_on,
                     waiting_on=waiting_on,
                     dependency_status=dependency_status,
                     holder=fields.get("holder"),
                     lease_until=fields.get("lease_until"),
                     write_set=(fields.get("write_set")
                                or (pipeline_info or {}).get("write_set")
                                or []),
                     pipeline_slot=(pipeline_info or {}).get("slot") or "base",
                     pipeline_eligible=bool((pipeline_info or {}).get(
                         "eligible")),
                     base_commit=(fields.get("base_commit")
                                  or (pipeline_info or {}).get("base_commit")),
                     install_status=fields.get("install_status") or (
                         "pending" if (pipeline_info or {}).get("eligible")
                         else "n/a"),
                     admission_work_id=queue_work_id)
        # A queue-only submit needs a live dispatcher in this process; the
        # startup dispatcher exits when it has nothing to do.
        try:
            controller.start(spawn=_spawn_record)
        except Exception as exc:
            log("admission dispatcher start skipped: %s" % exc)
        if waiting_on:
            return ("[DeepSeek job ID %s queued at position %d (%s); waiting "
                    "for run %s to finish before it can start. Check "
                    "deepseek_job_status run_id=%s.]"
                    % (run_id, position, label, waiting_on, run_id))
        return ("[DeepSeek job ID %s queued at position %d (%s); it will start "
                "when a slot frees. Check deepseek_job_status run_id=%s.]"
                % (run_id, position, label, run_id))

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
            if spawned_child["child"] is not None:
                try:
                    spawned_child["child"].wait(timeout=5)
                except Exception:
                    pass
            return state.get("result", "")
        if state.get("status") == "error":
            if spawned_child["child"] is not None:
                try:
                    spawned_child["child"].wait(timeout=5)
                except Exception:
                    pass
            raise RuntimeError(state.get("error") or "Job failed")
        pid = state.get("pid")
        if state.get("status") == "running" and pid and not _pid_alive(pid):
            raise RuntimeError("Job %s stopped without finishing (last step: %s)"
                               % (run_id, state.get("progress")))
        if (spawned_child["child"] is not None
                and spawned_child["child"].poll() is not None
                and state.get("status") in ("queued", "running")):
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


def _admission_status_fields(cfg, run_id, state):
    """Queue projection for a run id; zeros/nulls once running or done."""
    work_id = state.get("admission_work_id") or run_id
    try:
        fields = _admission_store_for(cfg).status_fields(work_id)
    except Exception:
        fields = {}
    position = fields.get("queue_position")
    if position is None:
        position = state.get("queue_position") or 0
    dependency_error = fields.get("dependency_error")
    if not dependency_error and fields.get("status") in ("failed", "error"):
        dependency_error = fields.get("error")
    return {
        "queue_position": position,
        "priority": fields.get("priority", state.get("priority")),
        "priority_label": fields.get("priority_label") or state.get("priority_label"),
        "urgent": fields.get("urgent", state.get("urgent", False)),
        "depends_on": fields.get("depends_on", state.get("depends_on")),
        "waiting_on": fields.get("waiting_on", state.get("waiting_on")),
        "dependency_status": fields.get("dependency_status",
                                         state.get("dependency_status")),
        "dependency_error": dependency_error or state.get("dependency_error"),
        "admission_status": fields.get("status"),
        "holder": fields.get("holder"),
        "lease_until": fields.get("lease_until"),
    }


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
    payload.update(_admission_status_fields(cfg, run_id, state))
    # P2-3: the reservation view is part of the structured status contract.
    # The plain (no events_since) text response stays byte-for-byte compatible.
    payload["cost"] = (run or {}).get("cost_ledger")
    pipe = (run or {}).get("pipeline") or {}
    payload["pipeline"] = pipe
    payload["write_set"] = pipe.get("write_set") or []
    payload["pipeline_slot"] = pipe.get("slot") or "base"
    payload["base_commit"] = pipe.get("base_commit")
    payload["install_status"] = ((pipe.get("install") or {}).get("status")
                                 or "n/a")
    if (run or {}).get("admission_overrun"):
        payload["admission_overrun"] = run.get("admission_overrun")
    # P3-2: reviewer token usage and per-round cost are part of the structured
    # status contract, so callers need not parse the run record.
    run = run or {}
    rounds_view = []
    for item in run.get("review_rounds") or []:
        if not isinstance(item, dict):
            continue
        rounds_view.append({
            "round": item.get("round"),
            "kind": item.get("kind"),
            "tokens_in": item.get("tokens_in"),
            "cache_read_tokens": item.get("cache_read_tokens"),
            "tokens_out": item.get("tokens_out"),
            "total_tokens": item.get("total_tokens"),
            "usage": item.get("usage"),
            "models": item.get("models"),
            "cost": item.get("cost"),
            "cost_known": item.get("cost_known"),
            "status": item.get("status"),
        })
    payload["reviewer_usage"] = run.get("reviewer_usage") or {}
    payload["review_rounds"] = rounds_view
    if run.get("reviewer_preflight"):
        payload["reviewer_preflight"] = run.get("reviewer_preflight")
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
    try:
        state.update(_admission_status_fields(cfg, run_id, state))
    except Exception:
        pass
    if "events_since" in args and args.get("events_since") is not None:
        return _structured_status(args, state, run_id, cfg)
    if state.get("dependency_error"):
        raise RuntimeError("Job %s failed: %s"
                           % (run_id, state.get("dependency_error")))
    if state.get("status") == "queued":
        if state.get("waiting_on"):
            return ("[DeepSeek job ID %s queued at position %d (%s); waiting "
                    "for run %s to finish.]"
                    % (run_id, state.get("queue_position") or 0,
                       state.get("priority_label") or "P1 new build",
                       state.get("waiting_on")))
        return ("[DeepSeek job ID %s queued at position %d (%s); waiting for a "
                "free slot.]" % (run_id, state.get("queue_position") or 0,
                                 state.get("priority_label") or "P1 new build"))
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
