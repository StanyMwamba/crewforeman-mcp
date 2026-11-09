"""Deterministic loop detector for CrewForeman harness runs.

This module watches raw Harness ``--json`` events and workspace digests.  It
does not call a model, inspect a repository, print, or integrate itself into the
run harness; callers feed events and poll for a conservative intervention.

The idea of detecting wasted agent loops was inspired by TokenEconomizer
(Apache-2.0).  No TokenEconomizer code is copied here: this is an original,
standard-library-only implementation.

Signals
-------
``loop_guard_repeat_command``
    The same failing command plus error is observed repeatedly.
``loop_guard_edit_pingpong``
    A file (or explicit hunk) alternates between the same content hashes.
``loop_guard_nochange_calls``
    Tool calls accumulate without a workspace digest change.
``loop_guard_same_test_failure``
    The same parsed test-failure signature repeats.

Each episode follows one ladder: ``nudge``, then ``replan``, then ``stop``.
``nudge`` is emitted at most once per episode.  Token savings are an explicit
estimate of wasted calls beyond a threshold times a nominal token cost, never
an exact billing claim.
"""

import hashlib
import json
import re
import time

try:  # Keep direct module loading and package imports both usable.
    from .config import DEFAULTS as _CONFIG_DEFAULTS
except ImportError:  # pragma: no cover - only for unusual direct imports
    _CONFIG_DEFAULTS = {}

SIGNAL_REPEAT = "loop_guard_repeat_command"
SIGNAL_PINGPONG = "loop_guard_edit_pingpong"
SIGNAL_NOCHANGE = "loop_guard_nochange_calls"
SIGNAL_TEST = "loop_guard_same_test_failure"
SIGNALS = (SIGNAL_REPEAT, SIGNAL_PINGPONG, SIGNAL_NOCHANGE, SIGNAL_TEST)

_BUILTIN_DEFAULTS = {
    "loop_guard_enabled": True,
    "loop_guard_confirm": False,
    "loop_guard_repeat_command": 3,
    "loop_guard_edit_pingpong": 3,
    "loop_guard_nochange_calls": 40,
    "loop_guard_same_test_failure": 3,
    "loop_guard_tokens_per_call": 1200,
    "loop_guard_max_actions": 3,
}

_CALL_TYPES = {"tool", "tool-call", "tool-use", "toolcall", "tooluse",
               "tool-invocation", "toolinvocation"}
_RESULT_TYPES = {"tool-result", "tool-output", "tool-response", "tool-result",
                 "tool-error", "error", "failed", "failure", "result"}
_EDIT_NAMES = ("edit", "write", "patch", "replace", "apply", "create",
               "notebookedit", "notebook-edit", "multiedit", "multi-edit")
# Read-only exploration is not a "no workspace change" loop.  A long audit can
# make hundreds of read calls and must never be nudged or stopped for it.
_READ_ONLY_NAMES = {
    "read", "view", "cat", "head", "tail", "grep", "rg", "glob", "list",
    "ls", "search", "find", "open", "inspect", "status", "diff", "log",
    "stat", "tree", "wc", "fetch", "webfetch", "websearch", "todo",
    "todowrite", "todoread", "todolist",
}
_SHELL_NAME_TOKENS = ("bash", "shell", "command", "exec", "terminal")
_READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "diff", "log", "show", "branch", "remote", "rev-parse",
            "ls-files", "grep", "describe", "tag", "config"},
    "go": {"test", "vet", "list"},
    "cargo": {"test", "check", "clippy", "tree"},
}
_READ_ONLY_FIRST = {
    "cat", "head", "tail", "grep", "rg", "ls", "pwd", "wc", "stat", "tree",
    "sed", "awk", "jq", "sort", "uniq", "cut", "tr", "diff", "comm", "file",
    "du", "df", "ps", "pgrep", "which", "type", "echo", "printf", "true",
    "test", "pytest", "tox", "nox", "make",
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FAILURE_RE = re.compile(
    r"(?im)^(?:traceback \(most recent call last\):|assertionerror\b|"
    r"assertion failed|fatal:|command not found|no such file or directory|"
    r"exit (?:code|status)\s+[1-9]|failed\b|error\b)")
_PYTEST_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.+))?$")
_UNITTEST_RE = re.compile(r"^(?:FAIL|ERROR):\s+(\S+)(?:\s+\(([^)]+)\))?")
_GO_RE = re.compile(r"^--- FAIL:\s+(\S+)")
_JEST_RE = re.compile(r"^FAIL\s+(\S+)")
_GENERIC_FAILURE_RE = re.compile(
    r"(?im)^(?:assertionerror|assertion failed|traceback|"
    r"(?:[A-Za-z_][\w.]*)?(?:Error|Exception):\s+\S)")
# A long healthy-then-sick run can produce many distinct errors; keep the
# detector's memory and the run-record snapshot bounded.
_MAX_TRACKED = 64
_MAX_SNAPSHOT_DETAILS = 20


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_int(value, default, minimum=1):
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result >= minimum else default


def _clip(value, limit=240):
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _clean_text(value):
    return _ANSI_RE.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")


def _stable_json(value):
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _config_value(config, name):
    fallback = _CONFIG_DEFAULTS.get(name, _BUILTIN_DEFAULTS.get(name))
    if config is None:
        return fallback
    getter = getattr(config, "get", None)
    if callable(getter):
        sentinel = object()
        try:
            value = getter(name, sentinel)
        except TypeError:
            try:
                value = getter(name)
            except Exception:
                value = sentinel
        except Exception:
            value = sentinel
        if value is not sentinel:
            return value
    try:
        return getattr(config, name)
    except (AttributeError, KeyError, TypeError):
        return fallback


def _content_text(value):
    """Flatten common Harness result/content shapes into text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_content_text(item) for item in value)
                         if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "error", "result",
                    "stdout", "stderr"):
            if key in value and value[key] not in (None, "", [], {}):
                return _content_text(value[key])
        return _stable_json(value)
    return str(value)


class LoopGuard:
    """Observe run events and return at most one intervention per poll."""

    def __init__(self, config=None, label="builder", clock=None):
        self.config = config
        self.label = str(label or "builder")
        self.clock = clock
        self.enabled = _as_bool(_config_value(config, "loop_guard_enabled"), True)
        self.confirm = _as_bool(_config_value(config, "loop_guard_confirm"), False)
        self.thresholds = {
            SIGNAL_REPEAT: _as_int(
                _config_value(config, "loop_guard_repeat_command"), 3),
            SIGNAL_PINGPONG: _as_int(
                _config_value(config, "loop_guard_edit_pingpong"), 3),
            SIGNAL_NOCHANGE: _as_int(
                _config_value(config, "loop_guard_nochange_calls"), 40),
            SIGNAL_TEST: _as_int(
                _config_value(config, "loop_guard_same_test_failure"), 3),
        }
        self.tokens_per_call = _as_int(
            _config_value(config, "loop_guard_tokens_per_call"), 1200, minimum=0)
        self.max_actions = min(3, _as_int(
            _config_value(config, "loop_guard_max_actions"), 3))
        self._repeat = {}
        self._edits = {}
        self._tests = {}
        self._calls_since_change = 0
        self._workspace_signature = None
        self._workspace_seen = False
        self.workspace_progress = _as_bool(
            _config_value(config, "workspace_progress"), True)
        self._loops_caught = 0
        self._actions = 0
        self._action_log = []
        self._active = None
        self._exhausted = set()
        self._pending_calls = []
        self._seen_call_ids = []
        self._seen_call_set = set()

    # ------------------------------------------------------------------ events

    def _event_data(self, event):
        data = event.get("data")
        return data if isinstance(data, dict) else {}

    def _event_type(self, event):
        data = self._event_data(event)
        raw = (event.get("type") or event.get("event") or data.get("type")
               or data.get("event") or "")
        return str(raw).strip().lower().replace("_", "-").replace("/", "-")

    def _event_name(self, event, parsed=None):
        data = self._event_data(event)
        candidates = [event.get("name"), event.get("tool"), data.get("name"),
                      data.get("tool")]
        for holder in (event.get("call"), data.get("call"), data.get("tool")):
            if isinstance(holder, dict):
                candidates.append(holder.get("name"))
        if isinstance(parsed, dict):
            candidates.append(parsed.get("name"))
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _event_input(self, event):
        data = self._event_data(event)
        candidates = [event.get("input"), event.get("arguments"),
                      event.get("args"), event.get("parameters"),
                      event.get("tool_input"), data.get("input"),
                      data.get("arguments"), data.get("args"),
                      data.get("parameters"), data.get("tool_input")]
        for holder in (event.get("call"), data.get("call")):
            if isinstance(holder, dict):
                candidates.extend([holder.get("input"),
                                   holder.get("arguments")])
        for value in candidates:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    return value
            if isinstance(value, (dict, list)):
                return value
        return {}

    def _event_result_text(self, event):
        data = self._event_data(event)
        chunks = []
        for holder in (event, data):
            for key in ("output", "error", "message", "result", "stdout",
                        "stderr", "content"):
                if key in holder and holder[key] not in (None, "", [], {}):
                    chunks.append(_content_text(holder[key]))
        # A direct text field is a result only when no call input is present.
        if not chunks and not self._event_input(event):
            text = event.get("text") or data.get("text")
            if text:
                chunks.append(_content_text(text))
        seen = []
        for chunk in chunks:
            if chunk and chunk not in seen:
                seen.append(chunk)
        return "\n".join(seen)

    def _is_call(self, event, typ, parsed):
        if typ in _CALL_TYPES or typ.endswith("-call") or typ.endswith("-use"):
            return True
        if typ in _RESULT_TYPES or "result" in typ or "output" in typ:
            return False
        if isinstance(parsed, dict) and parsed.get("kind") == "tool":
            return not ("result" in typ or "output" in typ)
        return bool(self._event_name(event, parsed) and self._event_input(event))

    def _is_result(self, event, typ, parsed):
        if typ in _RESULT_TYPES or "result" in typ or "output" in typ:
            return True
        if typ in _CALL_TYPES:
            return False
        if isinstance(parsed, dict) and parsed.get("kind") in ("error", "final"):
            return True
        return bool((event.get("output") is not None
                     or event.get("error") is not None
                     or event.get("message") is not None)
                    and not self._event_input(event))

    def _looks_failed(self, event, text):
        data = self._event_data(event)
        for holder in (event, data):
            if holder.get("is_error") is True or holder.get("isError") is True:
                return True
            if holder.get("success") is False:
                return True
            for key in ("exit_code", "exitCode", "returncode", "return_code"):
                if key in holder:
                    try:
                        if int(holder[key]) != 0:
                            return True
                    except (TypeError, ValueError):
                        pass
            status = str(holder.get("status") or holder.get("outcome") or "").lower()
            if status in ("error", "failed", "failure", "timeout"):
                return True
            if holder.get("error") not in (None, "", False):
                return True
        return bool(_FAILURE_RE.search(_clean_text(text)))

    def _command(self, name, tool_input, event):
        if isinstance(tool_input, dict):
            for key in ("command", "cmd", "script", "command_line"):
                value = tool_input.get(key)
                if isinstance(value, list):
                    value = " ".join(str(part) for part in value)
                if value not in (None, ""):
                    return _clip(value, 500)
        for key in ("command", "cmd"):
            if event.get(key) not in (None, ""):
                return _clip(event.get(key), 500)
        return name or "tool"

    def _test_failure_signature(self, text):
        text = _clean_text(text)
        matches = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _PYTEST_RE.match(line)
            if match:
                item = "FAILED %s" % match.group(1)
                if match.group(2):
                    item += " - " + self._normal_failure_detail(match.group(2))
                matches.append(item)
                continue
            match = _UNITTEST_RE.match(line)
            if match:
                item = "FAIL %s" % match.group(1)
                if match.group(2):
                    item += " (%s)" % match.group(2)
                matches.append(item)
                continue
            match = _GO_RE.match(line)
            if match:
                matches.append("FAIL %s" % match.group(1))
                continue
            match = _JEST_RE.match(line)
            if match:
                matches.append("FAIL %s" % match.group(1))
        if not matches:
            match = _GENERIC_FAILURE_RE.search(text)
            if match:
                matches.append(self._normal_failure_detail(match.group(0)))
        unique = []
        for item in matches:
            item = re.sub(r"\s+", " ", item).strip()
            if item and item not in unique:
                unique.append(item)
        return " || ".join(sorted(unique[:8]))

    def _normal_failure_detail(self, detail):
        detail = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x...", str(detail or ""))
        detail = re.sub(r"\s+", " ", detail).strip()
        return detail[:300]

    def _observe_message_blocks(self, event, typ):
        """Handle Anthropic-style assistant/user content blocks as a fallback."""
        data = self._event_data(event)
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return False
        handled = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "").strip().lower()
            if kind == "tool_use":
                self._observe_call({
                    "type": "tool_call",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                }, None)
                handled = True
            elif kind == "tool_result":
                self._observe_result({
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": block.get("is_error"),
                    "content": block.get("content"),
                }, None)
                handled = True
        return handled

    def observe_event(self, event, parsed=None):
        """Feed one raw Harness ``--json`` event dict."""
        if not self.enabled or not isinstance(event, dict):
            return None
        typ = self._event_type(event)
        if typ in ("assistant/message", "assistant-message",
                   "user/message", "user-message"):
            if self._observe_message_blocks(event, typ):
                self._maybe_start_episode()
                return None
        if self._is_call(event, typ, parsed):
            self._observe_call(event, parsed)
        elif self._is_result(event, typ, parsed):
            self._observe_result(event, parsed)
        self._maybe_start_episode()
        return None

    def _read_only_command(self, command):
        """Classify a shell command as read-only unless it can plausibly write."""
        text = " ".join(str(command or "").strip().lower().split())
        if not text:
            return False
        # ``cd repo && git status`` / ``env X=1 pytest`` / ``time pytest``.
        parts = re.split(r"&&|\|\||;", text)
        if len(parts) > 1:
            text = " ".join(parts[-1].split())
        for prefix in ("time ", "env "):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if text.startswith("cd "):
            return True
        try:
            tokens = text.split()
        except Exception:
            return False
        if not tokens:
            return False
        first = tokens[0].rsplit("/", 1)[-1]
        if first in ("python", "python3") and "-m" in tokens:
            index = tokens.index("-m")
            if index + 1 < len(tokens) and tokens[index + 1] in (
                    "pytest", "unittest", "compileall", "py_compile"):
                return True
        if first in ("uv", "poetry", "hatch") and any(
                token in tokens for token in ("pytest", "test", "run")):
            return "run" not in tokens or "pytest" in tokens
        if first in ("npm", "pnpm", "yarn") and "test" in tokens:
            return True
        if first == "git":
            index = 1
            while index < len(tokens) and tokens[index] in ("-C", "-c"):
                index += 2
            subcommand = tokens[index] if index < len(tokens) else ""
            return subcommand in _READ_ONLY_SUBCOMMANDS["git"]
        if first in _READ_ONLY_SUBCOMMANDS:
            subcommand = tokens[1] if len(tokens) > 1 else ""
            return subcommand in _READ_ONLY_SUBCOMMANDS[first]
        if first == "find":
            return not any(token in ("-delete", "-exec", "-execdir")
                           for token in tokens)
        if first in _READ_ONLY_FIRST:
            if first == "make":
                return any(token in ("test", "check", "lint") for token in tokens)
            return True
        return False

    def _may_mutate(self, name, command):
        """True when a call could plausibly write the workspace.

        The no-change signal must not punish exploration.  Unknown tools are
        treated as mutating so the guard stays conservative for real editors.
        """
        lowered = str(name or "").strip().lower()
        base = lowered.rsplit("/", 1)[-1]
        compact = re.sub(r"[^a-z0-9]", "", base)
        if compact in {re.sub(r"[^a-z0-9]", "", item)
                       for item in _READ_ONLY_NAMES}:
            return False
        if any(token in base for token in _EDIT_NAMES):
            return True
        if any(token in base for token in _SHELL_NAME_TOKENS) or base in (
                "run", "sh"):
            return not self._read_only_command(command)
        return True

    def _observe_call(self, event, parsed):
        name = self._event_name(event, parsed)
        tool_input = self._event_input(event)
        call_id = (event.get("id") or event.get("tool_use_id")
                   or event.get("toolUseId") or event.get("call_id"))
        if call_id is not None:
            key = str(call_id)
            # Some providers replay the same call as both a tool_call event and
            # an assistant tool_use block; count it once.
            if key in self._seen_call_set:
                return
            self._seen_call_set.add(key)
            self._seen_call_ids.append(key)
            if len(self._seen_call_ids) > 256:
                old = self._seen_call_ids.pop(0)
                self._seen_call_set.discard(old)
        command = self._command(name, tool_input, event)
        self._pending_calls.append({
            "id": str(call_id) if call_id is not None else None,
            "name": name,
            "input": tool_input,
            "command": command,
        })
        if len(self._pending_calls) > 64:
            del self._pending_calls[:-64]
        self._observe_edit(name, tool_input, event)
        if self.workspace_progress and self._may_mutate(name, command):
            self._calls_since_change += 1
            if self._calls_since_change >= self.thresholds[SIGNAL_NOCHANGE]:
                self._consider_signal(
                    SIGNAL_NOCHANGE, "calls_since_workspace_change",
                    self._calls_since_change)

    def _take_pending(self, event):
        call_id = (event.get("tool_use_id") or event.get("toolUseId")
                   or event.get("call_id") or event.get("id"))
        if call_id is not None:
            for index, pending in enumerate(self._pending_calls):
                if pending.get("id") == str(call_id):
                    return self._pending_calls.pop(index)
        if self._pending_calls:
            return self._pending_calls.pop(0)
        return {}

    def _observe_result(self, event, parsed):
        text = self._event_result_text(event)
        pending = self._take_pending(event)
        failed = self._looks_failed(event, text)
        command = pending.get("command") or self._command(
            self._event_name(event, parsed), self._event_input(event), event)
        test_signature = self._test_failure_signature(text)
        # A parsed test failure belongs to the dedicated test signal; counting
        # it as a command error too would double-count evidence and make the
        # reported signal depend on iteration order.
        if failed and not test_signature:
            error = _clip(text, 400) or "tool reported an error"
            key = command + "\n" + error
            item = self._repeat.get(key)
            if item is None:
                item = {"count": 0, "command": command, "error": error}
                self._repeat[key] = item
            item["count"] += 1
            self._prune(self._repeat, "count")
            if item["count"] >= self.thresholds[SIGNAL_REPEAT]:
                self._consider_signal(SIGNAL_REPEAT, key, item["count"])
        elif not failed and command:
            for key in list(self._repeat):
                if self._repeat[key].get("command") == command:
                    del self._repeat[key]
                    self._exhausted.discard(SIGNAL_REPEAT + ":" + key)

        if test_signature:
            item = self._tests.get(test_signature)
            if item is None:
                item = {"count": 0, "signature": _clip(test_signature, 400)}
                self._tests[test_signature] = item
            item["count"] += 1
            self._prune(self._tests, "count")
            if item["count"] >= self.thresholds[SIGNAL_TEST]:
                self._consider_signal(SIGNAL_TEST, test_signature, item["count"])

    # ------------------------------------------------------------------ edits

    def _observe_edit(self, name, tool_input, event):
        if not isinstance(tool_input, dict):
            return
        lowered = name.lower()
        edit_like = any(token in lowered for token in _EDIT_NAMES)
        path = None
        for key in ("path", "file_path", "filepath", "file", "filename",
                    "notebook_path", "target_file"):
            if tool_input.get(key):
                path = tool_input.get(key)
                break
        if not path:
            path = event.get("path") or event.get("file_path")
        if not path:
            return
        explicit_content = False
        content = None
        for key in ("new_content", "content", "new_string", "new_text",
                    "patch", "diff"):
            if key in tool_input and tool_input[key] is not None:
                content = tool_input[key]
                explicit_content = True
                break
        if not explicit_content:
            if not edit_like:
                return
            old = tool_input.get("old_string", tool_input.get("old_text", ""))
            new = tool_input.get("new_string", tool_input.get("new_text", ""))
            content = {"old": old, "new": new, "input": tool_input}
        hunk = None
        for key in ("hunk", "hunk_id", "hunkId", "range", "line_range"):
            if tool_input.get(key):
                hunk = tool_input.get(key)
                break
        normalized_path = str(path).replace("\\", "/").strip()
        if not normalized_path:
            return
        state_key = normalized_path
        if hunk is not None:
            state_key += "|" + _stable_json(hunk)
        digest = hashlib.sha256(_stable_json(content).encode("utf-8")).hexdigest()
        state = self._edits.get(state_key)
        if state is None:
            self._edits[state_key] = {
                "path": normalized_path, "hunk": _clip(hunk, 80),
                "last": digest, "other": None, "flips": 0,
            }
            return
        if digest == state["last"]:
            return
        previous_last = state["last"]
        if state["other"] is None:
            state["other"] = previous_last
            state["last"] = digest
            state["flips"] = 1
        elif digest == state["other"]:
            state["other"] = previous_last
            state["last"] = digest
            state["flips"] += 1
        else:
            state["other"] = previous_last
            state["last"] = digest
            state["flips"] = 1
        self._prune(self._edits, "flips")
        if state["flips"] >= self.thresholds[SIGNAL_PINGPONG]:
            self._consider_signal(SIGNAL_PINGPONG, state_key, state["flips"])

    @staticmethod
    def _prune(mapping, count_key):
        if len(mapping) <= _MAX_TRACKED:
            return
        keep = set(sorted(
            mapping, key=lambda key: mapping[key].get(count_key, 0),
            reverse=True)[:_MAX_TRACKED])
        for key in list(mapping):
            if key not in keep:
                del mapping[key]

    # --------------------------------------------------------------- workspace

    def note_workspace(self, signature):
        """Report a workspace digest; any changed digest breaks no-change."""
        if not self.enabled:
            return None
        normalized = _stable_json(signature)
        changed = not self._workspace_seen or normalized != self._workspace_signature
        self._workspace_seen = True
        self._workspace_signature = normalized
        if changed:
            self._calls_since_change = 0
            self._exhausted.discard(SIGNAL_NOCHANGE + ":calls_since_workspace_change")
            if self._active and self._active.get("signal") == SIGNAL_NOCHANGE:
                self._active = None
        return None

    # ---------------------------------------------------------------- episodes

    def _best_entry(self, entries, count_key="count"):
        if not entries:
            return None, 0
        key = max(entries, key=lambda candidate: (
            entries[candidate].get(count_key, 0), candidate))
        return key, entries[key].get(count_key, 0)

    def _consider_signal(self, signal, key, count):
        trigger = signal + ":" + key
        if self._active or trigger in self._exhausted:
            return
        if count < self.thresholds[signal]:
            return
        self._active = {"signal": signal, "key": key, "step": 0,
                        "threshold": self.thresholds[signal]}
        self._loops_caught += 1

    def _maybe_start_episode(self):
        if self._active or not self.enabled:
            return
        key, count = self._best_entry(self._repeat)
        if key is not None:
            self._consider_signal(SIGNAL_REPEAT, key, count)
            if self._active:
                return
        key, count = self._best_entry(self._edits, "flips")
        if key is not None:
            self._consider_signal(SIGNAL_PINGPONG, key, count)
            if self._active:
                return
        if self._calls_since_change >= self.thresholds[SIGNAL_NOCHANGE]:
            self._consider_signal(SIGNAL_NOCHANGE, "calls_since_workspace_change",
                                  self._calls_since_change)
            if self._active:
                return
        key, count = self._best_entry(self._tests)
        if key is not None:
            self._consider_signal(SIGNAL_TEST, key, count)

    def _signal_count(self, signal, key):
        if signal == SIGNAL_REPEAT:
            return self._repeat.get(key, {}).get("count", 0)
        if signal == SIGNAL_PINGPONG:
            return self._edits.get(key, {}).get("flips", 0)
        if signal == SIGNAL_NOCHANGE:
            return self._calls_since_change
        return self._tests.get(key, {}).get("count", 0)

    def _finish_episode(self, active):
        self._exhausted.add(active["signal"] + ":" + active["key"])
        self._active = None

    def poll(self):
        """Return ``None`` or one ladder action for the active loop episode."""
        if not self.enabled:
            return None
        if self._active is None:
            self._maybe_start_episode()
        if self._active is None:
            return None
        active = self._active
        step = active["step"]
        if step >= self.max_actions:
            self._finish_episode(active)
            return None
        count = self._signal_count(active["signal"], active["key"])
        # Escalation needs new evidence, not just another poll: nudge at the
        # threshold, then one further threshold of repeats for re-plan, and
        # another for stop. A single transient signal can never stop a run.
        if step > 0:
            if count < active.get("next_count", count + active["threshold"]):
                return None
        if step == 0:
            action = "nudge"
        elif step == 1:
            action = "replan"
        else:
            action = "stop"
        wasted = max(0, count - active["threshold"])
        evidence = {
            "label": self.label,
            "signal": active["signal"],
            "key": _clip(active["key"], 320),
            "count": count,
            "threshold": active["threshold"],
            "wasted_calls_beyond_threshold": wasted,
            "token_estimate": wasted * self.tokens_per_call,
            "estimate_basis": ("wasted calls beyond threshold * "
                               "loop_guard_tokens_per_call; approximate, "
                               "not an exact or billing claim"),
        }
        result = {"action": action, "signal": active["signal"],
                  "evidence": evidence}
        if self.confirm:
            evidence["needs_confirm"] = True
            result["needs_confirm"] = True
        self._actions += 1
        self._action_log.append({"action": action,
                                 "signal": active["signal"],
                                 "count": count})
        active["step"] = step + 1
        active["next_count"] = count + active["threshold"]
        if action == "stop" or active["step"] >= self.max_actions:
            self._finish_episode(active)
        return result

    # ---------------------------------------------------------------- snapshot

    @staticmethod
    def _snapshot_items(mapping, count_key="count"):
        ordered = sorted(mapping.items(),
                         key=lambda item: item[1].get(count_key, 0),
                         reverse=True)
        return ordered[:_MAX_SNAPSHOT_DETAILS]

    def snapshot(self):
        """Return a JSON-friendly view without mutating detector state."""
        repeat_details = {}
        repeat_count = 0
        for key, item in self._snapshot_items(self._repeat):
            repeat_count = max(repeat_count, item.get("count", 0))
            repeat_details[_clip(key, 320)] = {
                "count": item.get("count", 0),
                "command": _clip(item.get("command"), 160),
                "error": _clip(item.get("error"), 160),
            }
        edit_details = {}
        edit_count = 0
        for key, item in self._snapshot_items(self._edits, "flips"):
            edit_count = max(edit_count, item.get("flips", 0))
            edit_details[_clip(key, 320)] = {
                "flips": item.get("flips", 0),
                "path": item.get("path"),
                "last_hash": str(item.get("last"))[:12],
                "alternate_hash": str(item.get("other"))[:12],
            }
        test_details = {}
        test_count = 0
        for key, item in self._snapshot_items(self._tests):
            test_count = max(test_count, item.get("count", 0))
            test_details[_clip(key, 320)] = {"count": item.get("count", 0)}
        signals = {
            SIGNAL_REPEAT: {
                "count": repeat_count,
                "threshold": self.thresholds[SIGNAL_REPEAT],
                "details": repeat_details,
            },
            SIGNAL_PINGPONG: {
                "count": edit_count,
                "threshold": self.thresholds[SIGNAL_PINGPONG],
                "details": edit_details,
            },
            SIGNAL_NOCHANGE: {
                "count": self._calls_since_change,
                "threshold": self.thresholds[SIGNAL_NOCHANGE],
                "details": {
                    "calls_since_change": self._calls_since_change,
                    "workspace_progress": self.workspace_progress,
                    "workspace_seen": self._workspace_seen,
                    "workspace_signature": (
                        str(self._workspace_signature)[:16]
                        if self._workspace_seen else None),
                },
            },
            SIGNAL_TEST: {
                "count": test_count,
                "threshold": self.thresholds[SIGNAL_TEST],
                "details": test_details,
            },
        }
        estimated = 0
        for signal in SIGNALS:
            estimated += max(0, signals[signal]["count"]
                             - signals[signal]["threshold"])
        estimated *= self.tokens_per_call
        return {
            "enabled": self.enabled,
            "label": self.label,
            "signals": signals,
            "loops_caught": self._loops_caught,
            "actions": self._actions,
            "action_log": list(self._action_log),
            "estimated_tokens_saved": estimated,
            "estimated_tokens_saved_basis": (
                "sum over signals of max(0, count - threshold) * "
                "loop_guard_tokens_per_call; approximate, not exact"),
        }
