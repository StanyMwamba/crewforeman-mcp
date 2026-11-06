"""Documented DSH SDK (`dsh --profile sdk`) stdio JSON-RPC 2.0 client.

The client speaks newline-delimited JSON-RPC over the child process' stdin and
stdout.  stdout must contain frames only; diagnostics (which are drained so the
pipe cannot fill up) live on stderr and are never put into exception text.

Public API (v3.5.0 wave 1, section B)::

    SdkClient(dsh_bin, workspace, env=None, timeout=600.0,
              popen_factory=None, log=None, usage_observer=None)
    .start() .initialize(...) .prompt(...) .wait(...) .close() .run(...)
    TransportError, SdkUnavailable, BudgetStopped, fallback_reason(exc)

``usage_observer`` is an optional synchronous callback ``(event, usage)``.
It is invoked from the reader thread as soon as a ``session.event`` carries
usage; a truthy return value means the whole-job budget ceiling was crossed.
The client then signals the child process group and aborts the in-flight
JSON-RPC request with :class:`BudgetStopped`, so the caller can settle the
usage observed so far instead of waiting for the turn to finish.

Only the Python 3.9 standard library is used.  No network access is performed.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid

DEFAULT_PROFILE = "sdk"
DEFAULT_TIMEOUT = 600.0
MAX_EVENTS = 500
MAX_STDERR_LINES = 100

_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_IDLE_STATES = ("idle", "done", "completed", "ready")
_USAGE_KEYS = {
    "inputTokens": "input_tokens",
    "input_tokens": "input_tokens",
    "promptTokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "cacheReadTokens": "cache_read_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cacheReadInputTokens": "cache_read_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cachedTokens": "cache_read_tokens",
    "cached_tokens": "cache_read_tokens",
    "outputTokens": "output_tokens",
    "output_tokens": "output_tokens",
    "completionTokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "totalTokens": "total_tokens",
    "total_tokens": "total_tokens",
}


class TransportError(Exception):
    """A JSON-RPC transport failure: timeout, error frame, broken pipe."""


class SdkUnavailable(TransportError):
    """The dsh SDK transport could not be established; use a fallback."""


class Cancelled(TransportError):
    """The caller's cancel check fired; the SDK child was signalled to stop."""


class BudgetStopped(TransportError):
    """A live usage observer crossed the budget ceiling at an event boundary.

    This is a deliberate interruption, not a transport failure.  The caller
    must settle the usage observed so far and mark the phase ``budget``; it
    must not retry or fall back to another transport.
    """


def redact(text):
    """Remove credential-looking substrings from diagnostic text."""
    text = _SECRET_RE.sub("sk-***", str(text))
    return _BEARER_RE.sub("Bearer ***", text)


def fallback_reason(exc):
    """Return a short redacted reason if *exc* means the SDK is unusable.

    ``None`` means the failure is a normal transport error (for example a
    per-request timeout) and a fallback would risk duplicating work.
    """
    if isinstance(exc, SdkUnavailable):
        return redact(str(exc) or "dsh sdk unavailable")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 2:
        return redact("dsh executable not found")
    return None


def _snippet(text, limit=120):
    text = redact(text).strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _text_from_content(content, depth=0):
    """Flatten a message ``content`` value (string, block list, dict)."""
    if content is None or depth > 6:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            part = _text_from_content(item, depth + 1)
            if part:
                parts.append(part)
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        ctype = content.get("type")
        if ctype in ("text", "text_delta", "output_text", "input_text"):
            text = content.get("text")
            if isinstance(text, str):
                return text.strip()
            if isinstance(text, dict) and isinstance(text.get("value"), str):
                return text["value"].strip()
            value = content.get("value")
            if isinstance(value, str):
                return value.strip()
            return ""
        for key in ("text", "content", "delta", "message", "value"):
            if key in content:
                text = _text_from_content(content[key], depth + 1)
                if text:
                    return text
        return ""
    return ""


def _message_text(message):
    """Return assistant text from a message object; ignore non-assistant roles."""
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    role = message.get("role")
    if role is not None and str(role).lower() not in ("assistant", "agent", "model"):
        return ""
    text = _text_from_content(message.get("content"))
    if not text and isinstance(message.get("text"), str):
        text = message["text"].strip()
    return text


def _extract_text(event):
    """Best-effort assistant text from a tolerant set of event shapes."""
    if not isinstance(event, dict):
        return ""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    for holder in (data, event):
        text = _message_text(holder.get("message"))
        if text:
            return text
        delta = holder.get("delta")
        if isinstance(delta, dict):
            text = _message_text(delta)
            if not text and isinstance(delta.get("text"), str):
                text = delta["text"].strip()
            if text:
                return text
        if isinstance(holder.get("content"), (str, list, dict)):
            text = _text_from_content(holder.get("content"))
            if text:
                return text
    kind = str(event.get("type") or event.get("event") or "").lower()
    if kind in ("assistant_message", "assistant", "message"):
        text = _text_from_content(event.get("text"))
        if text:
            return text
    return ""


def _find_node(node, key, depth=0, max_depth=5):
    """Find the first dict value stored under *key* in nested dicts."""
    if depth > max_depth or not isinstance(node, dict):
        return None
    value = node.get(key)
    if isinstance(value, dict) and value:
        return value
    for child_key in ("data", "event", "message", "result", "params"):
        found = _find_node(node.get(child_key), key, depth + 1, max_depth)
        if found is not None:
            return found
    return None


def _normalize_usage(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[_USAGE_KEYS.get(key, key)] = value
    return out


def _extract_usage(event):
    if not isinstance(event, dict):
        return {}
    return _normalize_usage(_find_node(event, "usage"))


def _find_session_id(node, depth=0):
    if depth > 4 or not isinstance(node, dict):
        return None
    for key in ("sessionId", "session_id", "conversationId", "conversation_id"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("session", "event", "params", "data"):
        child = node.get(key)
        if isinstance(child, dict):
            found = _find_session_id(child, depth + 1)
            if found:
                return found
    return None


def _find_status(node, depth=0):
    if depth > 4 or not isinstance(node, dict):
        return None
    for key in ("status", "state"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value.lower()
        if isinstance(value, dict):
            for sub in ("type", "name", "state"):
                if isinstance(value.get(sub), str):
                    return value[sub].lower()
    for key in ("session", "event"):
        child = node.get(key)
        if isinstance(child, dict):
            found = _find_status(child, depth + 1)
            if found:
                return found
    return None


class SdkClient:
    """One `dsh --profile sdk` child process speaking JSON-RPC on stdio."""

    def __init__(self, dsh_bin, workspace, env=None, timeout=600.0,
                 popen_factory=None, log=None, command=None,
                 usage_observer=None):
        self.dsh_bin = dsh_bin
        self.workspace = workspace
        self.timeout = float(timeout)
        self.env = dict(env) if env is not None else None
        self.log = log
        self._popen_factory = popen_factory or subprocess.Popen
        self._usage_observer = usage_observer
        self._budget_stop_reason = None
        # ``command`` is the complete argv when the caller already wrapped it
        # (for example with the macOS Seatbelt sandbox).  The default keeps
        # the documented ``dsh --profile sdk`` invocation.
        self._cmd = (list(command) if command
                     else [str(dsh_bin), "--profile", DEFAULT_PROFILE])
        self._cancel_check = None
        self._proc = None
        self._stdin = None
        self._reader = None
        self._stderr_reader = None
        self._lock = threading.Lock()
        self._pending = {}
        self._next_id = 0
        self._events = []
        self._text = ""
        self._usage = {}
        # The SDK server requires a client-supplied sessionId on prompt and
        # wait. Generate one per client instance; session/resume can replace it.
        self._session_id = "sdk-" + uuid.uuid4().hex
        self._session_pinned = False
        self._status = None
        self._protocol_error = None
        self._closed = False
        self._initialized = False
        self._prompt_started_at = None
        self._stderr_tail = []

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Spawn the child once; idempotent.  Returns ``self``."""
        if self._proc is not None:
            return self
        kwargs = {
            "cwd": self.workspace,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": dict(self.env) if self.env is not None else None,
            "text": True,
            "bufsize": 1,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        try:
            self._proc = self._popen_factory(self._cmd, **kwargs)
        except (OSError, ValueError) as exc:
            raise SdkUnavailable(redact("cannot start dsh: %s" % (exc,)))
        self._stdin = getattr(self._proc, "stdin", None)
        self._reader = threading.Thread(
            target=self._read_loop, name="dsh-sdk-stdout", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name="dsh-sdk-stderr", daemon=True)
        self._stderr_reader.start()
        return self

    def close(self, timeout=5.0):
        """Best-effort shutdown, then guaranteed process-group cleanup."""
        proc = self._proc
        if proc is None:
            return
        deadline = time.monotonic() + max(0.1, float(timeout or 5.0))
        if not self._closed:
            self._closed = True
            if self._poll() is None and not self._protocol_error:
                remaining = deadline - time.monotonic()
                wait_for = min(0.6, max(0.05, remaining))
                try:
                    self._request("shutdown", {}, timeout=wait_for)
                except Exception:
                    pass
        try:
            if self._stdin is not None:
                self._stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=min(0.4, max(0.0, deadline - time.monotonic())))
        except Exception:
            pass
        if self._poll() is None:
            self._signal_group(signal.SIGTERM)
            try:
                proc.wait(timeout=0.4)
            except Exception:
                pass
        if self._poll() is None:
            self._signal_group(signal.SIGKILL)
            try:
                proc.wait(timeout=0.3)
            except Exception:
                pass
        for thread in (self._reader, self._stderr_reader):
            if thread is not None:
                try:
                    thread.join(0.2)
                except RuntimeError:
                    pass
        for stream in (getattr(proc, "stdout", None),
                       getattr(proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- requests ----------------------------------------------------------
    def initialize(self, provider="deepseek-official", model="deepseek-flash",
                   reasoning_effort="max", max_tokens=None, timeout=None):
        """Send ``initialize`` and return the serverInfo dict."""
        params = {
            "cwd": str(self.workspace),
            "provider": provider,
            "model": model,
            "reasoningEffort": reasoning_effort,
        }
        if max_tokens is not None:
            params["maxTokens"] = int(max_tokens)
        self.start()
        result = self._request("initialize", params, timeout=timeout)
        self._initialized = True
        if isinstance(result, dict):
            server = result.get("serverInfo")
            if isinstance(server, dict):
                return dict(server)
            return dict(result)
        return {}

    def prompt(self, text, timeout=None):
        """Send ``session/prompt`` with one text block; return messageId."""
        if not isinstance(text, str) or not text.strip():
            raise TransportError("prompt text must be a non-empty string")
        self.start()
        self._text = ""
        self._status = None
        self._prompt_started_at = time.monotonic()
        params = {
            "sessionId": self._session_id,
            "contentBlocks": [{"type": "text", "text": text}],
        }
        result = self._request("session/prompt", params, timeout=timeout)
        if isinstance(result, dict):
            message_id = result.get("messageId")
            if message_id is None:
                message_id = result.get("message_id")
            return message_id
        return None

    def resume(self, session_id, timeout=None):
        """Adopt a persisted SDK session with ``session/resume``."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise TransportError("session/resume requires a non-empty session id")
        self._session_id = session_id.strip()
        self._session_pinned = True
        result = self._request("session/resume", {"sessionId": self._session_id},
                               timeout=timeout)
        return result if isinstance(result, dict) else {}

    def wait(self, timeout=None):
        """Send ``session/wait`` and return the collected turn result."""
        timeout = self.timeout if timeout is None else float(timeout)
        started = self._prompt_started_at or time.monotonic()
        self._request("session/wait", {"sessionId": self._session_id},
                      timeout=timeout)
        return {
            "answer": self._text,
            "events": list(self._events),
            "usage": dict(self._usage),
            "session_id": self._session_id,
            "status": self._status or "idle",
            "seconds": round(time.monotonic() - started, 3),
        }

    def run(self, task, timeout=None, session_id=None, resume=False,
            provider="deepseek-official", model="deepseek-flash",
            reasoning_effort="max", cancel_check=None, usage_observer=None):
        """initialize -> (resume) -> prompt -> wait -> close.

        ``cancel_check`` is polled while each request waits; when it returns
        true the child process group is signalled and :class:`Cancelled` is
        raised.  ``usage_observer`` overrides the client-level observer for
        this run only.  A truthy observer result stops the process group and
        raises :class:`BudgetStopped` at the usage-event boundary.  The
        caller's finally block still performs the hard close.
        """
        total = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + total
        started = time.monotonic()

        def remaining():
            return max(0.05, deadline - time.monotonic())

        previous_check = self._cancel_check
        previous_observer = self._usage_observer
        if usage_observer is not None:
            self._usage_observer = usage_observer
        self._cancel_check = cancel_check
        try:
            self.start()
            self.initialize(provider=provider, model=model,
                            reasoning_effort=reasoning_effort,
                            timeout=remaining())
            if resume and session_id:
                self.resume(session_id, timeout=remaining())
            elif session_id:
                self._session_id = str(session_id)
            self.prompt(task, timeout=remaining())
            result = self.wait(timeout=remaining())
        finally:
            self._cancel_check = previous_check
            self._usage_observer = previous_observer
            self.close()
        result["seconds"] = round(time.monotonic() - started, 3)
        result["transport"] = "sdk"
        return result

    def _request(self, method, params=None, timeout=None):
        self.start()
        if self._budget_stop_reason is not None:
            raise BudgetStopped(
                "live usage crossed the budget ceiling; %s was not sent"
                % method)
        if self._protocol_error:
            raise SdkUnavailable(self._protocol_error)
        timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, timeout)
        rc = self._poll()
        if rc is not None:
            raise self._exit_error(rc)
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            slot = {"event": threading.Event(), "result": None,
                    "error": None, "exc": None}
            self._pending[str(request_id)] = slot
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            self._write(frame)
        except (OSError, ValueError) as exc:
            with self._lock:
                self._pending.pop(str(request_id), None)
            raise SdkUnavailable(redact("cannot write to dsh stdin: %s" % (exc,)))
        while True:
            if slot["event"].is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._pending.pop(str(request_id), None)
                raise TransportError("%s timed out after %.1fs" % (method, timeout))
            if slot["event"].wait(min(0.2, remaining)):
                break
            check = self._cancel_check
            if check is not None:
                try:
                    cancelled = bool(check())
                except Exception:
                    cancelled = False
                if cancelled:
                    with self._lock:
                        self._pending.pop(str(request_id), None)
                    self._signal_group(signal.SIGTERM)
                    raise Cancelled("cancelled by client")
            rc = self._poll()
            if rc is not None:
                with self._lock:
                    self._pending.pop(str(request_id), None)
                raise self._exit_error(rc)
        if slot["exc"] is not None:
            raise slot["exc"]
        if slot["error"] is not None:
            raise self._jsonrpc_error(method, slot["error"])
        return slot["result"]

    def _write(self, frame):
        if self._stdin is None:
            raise ValueError("stdin is not available")
        line = json.dumps(frame, separators=(",", ":"))
        self._stdin.write(line + "\n")
        self._stdin.flush()

    def _jsonrpc_error(self, method, error):
        if not isinstance(error, dict):
            error = {"message": str(error)}
        message = redact(error.get("message") or "JSON-RPC error")
        code = error.get("code")
        text = "%s failed: %s" % (method, message)
        if code is not None:
            text += " (code %s)" % code
        if method == "initialize":
            return SdkUnavailable(text)
        return TransportError(text)

    # -- reader / dispatch -------------------------------------------------
    def _read_loop(self):
        stdout = getattr(self._proc, "stdout", None)
        if stdout is None:
            return
        while True:
            try:
                line = stdout.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                reason = "non-JSON stdout from dsh: %s" % _snippet(line)
                self._protocol_error = reason
                self._fail_all(SdkUnavailable(reason))
                break
            if not isinstance(frame, dict):
                continue
            try:
                self._dispatch(frame)
            except Exception:  # pragma: no cover - defensive
                continue
        self._fail_all(self._exit_error(self._poll()))

    def _drain_stderr(self):
        stderr = getattr(self._proc, "stderr", None)
        if stderr is None:
            return
        while True:
            try:
                line = stderr.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = line.strip()
            if line:
                self._stderr_tail.append(_snippet(line))
                del self._stderr_tail[:-MAX_STDERR_LINES]

    def _dispatch(self, frame):
        if "id" in frame and ("result" in frame or "error" in frame):
            with self._lock:
                slot = self._pending.pop(str(frame.get("id")), None)
            if slot is None:
                return
            if "error" in frame:
                slot["error"] = frame["error"]
            else:
                slot["result"] = frame.get("result")
            slot["event"].set()
            return
        method = frame.get("method")
        if not method:
            return
        if "id" in frame:
            try:
                self._write({"jsonrpc": "2.0", "id": frame["id"],
                             "error": {"code": -32601,
                                       "message": "method not supported"}})
            except (OSError, ValueError):
                pass
            return
        self._handle_notification(frame)

    def _handle_notification(self, frame):
        method = frame.get("method")
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {} if params is None else {"value": params}
        session_id = _find_session_id(params)
        # The server's session.event/status id is authoritative for a fresh
        # client; otherwise a later resume/session lookup would address a
        # session the server never created. An explicit resume pins the id.
        if session_id and method in ("session.event", "session.status"):
            if not getattr(self, "_session_pinned", False):
                self._session_id = session_id
        if method == "session.event":
            event = params.get("event")
            if not isinstance(event, dict):
                event = params
            text = _extract_text(event)
            if text:
                self._text = text
            usage = _extract_usage(event)
            if usage:
                self._usage.update(usage)
            self._append_event({"method": method, "event": event})
            if usage:
                self._notify_usage(event)
        elif method == "session.status":
            status = _find_status(params)
            if status:
                self._status = status
            self._append_event({"method": method, "status": status})
        elif method in ("subagent.started", "subagent.finished"):
            summary = {"method": method}
            for key, value in params.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    summary[key] = value
            self._append_event(summary)
        else:
            self._append_event({"method": str(method), "params": params})

    @property
    def usage(self):
        """Usage observed so far (also preserved after a budget stop)."""
        return dict(self._usage)

    def _notify_usage(self, event):
        """Call the live observer at one usage boundary.

        A truthy return is a whole-job budget crossing.  Stop the child
        process group immediately and fail the in-flight request with
        :class:`BudgetStopped`; the aggregated ``self._usage`` stays readable
        so the caller can price and settle what was actually consumed.
        """
        observer = self._usage_observer
        if observer is None or self._budget_stop_reason is not None:
            return
        try:
            reason = observer(event, dict(self._usage))
        except Exception:
            return
        if not reason:
            return
        self._budget_stop_reason = str(reason)
        self._signal_group(signal.SIGTERM)
        self._fail_all(BudgetStopped(
            "live usage crossed the budget ceiling at an SDK event boundary"))

    def _append_event(self, event):
        self._events.append(event)
        if len(self._events) > MAX_EVENTS:
            del self._events[:len(self._events) - MAX_EVENTS]

    def _fail_all(self, exc):
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["exc"] = exc
            slot["event"].set()

    def _exit_error(self, rc):
        if self._initialized:
            message = "dsh exited unexpectedly"
        else:
            message = "dsh exited before the SDK session was ready"
        if rc is not None:
            message += " (exit code %s)" % rc
        if self._initialized:
            return TransportError(message)
        return SdkUnavailable(message)

    def _poll(self):
        if self._proc is None:
            return None
        try:
            return self._proc.poll()
        except Exception:  # pragma: no cover - defensive
            return None

    def _signal_group(self, sig):
        proc = self._proc
        pid = getattr(proc, "pid", None)
        if not pid:
            return
        try:
            if os.name == "posix" and hasattr(os, "killpg"):
                os.killpg(os.getpgid(pid), sig)
            else:  # pragma: no cover - non-posix fallback
                proc.send_signal(sig)
        except (OSError, ValueError):
            pass
