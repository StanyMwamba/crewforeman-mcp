"""Detect how many workflow children the installed Harness can run at once.

The Harness has two independent limits:

* ``@deepseek-ai/dsh-subagent`` caps live children with
  ``maxActiveSubagents`` (documented default **8**).  It does **not** queue:
  a start above the cap is rejected with ``ACTIVATION_LIMIT_REACHED``.
* ``@deepseek-ai/dsh-workflow-ptc`` caps concurrent ``agent()`` calls with
  ``maxConcurrentAgents`` (``0`` means ``min(16, availableParallelism() - 2)``).

The effective ceiling is the smaller of the two.  This module first reads the
documented default from the installed app (read-only), then falls back to a
cheap probe.  Results are cached for seven days in the state directory and
invalidated when the Harness version changes or an agent-limit error is seen.

Python 3.9 compatible; standard library only.
"""

import datetime as dt
import json
import os
import re
import struct
import subprocess
import tempfile
import time
from pathlib import Path

FALLBACK_CONCURRENCY = 8
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_FILENAME = "harness-concurrency.json"
PROBE_AGENTS = 16
PROBE_MARKER = "CONCURRENCY_PROBE"
# Mirrors harness.parse_event FINAL_TYPES: the harness --json stream uses
# several final-ish event types, and the answer may live under several keys.
FINAL_EVENT_TYPES = ("final", "result", "done", "completion", "completed")
ANSWER_KEYS = ("text", "answer", "message", "content", "result", "output")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(now=None):
    return (now or _now()).isoformat(timespec="seconds")


def _cfg_value(cfg, name, default=None):
    try:
        return cfg.get(name, default) if hasattr(cfg, "get") else getattr(cfg, name, default)
    except Exception:
        return default


def state_dir(cfg):
    raw = _cfg_value(cfg, "state_dir", "") or "~/DeepSeek-MCP/state"
    return Path(os.path.expanduser(str(raw)))


def cache_path(cfg):
    return state_dir(cfg) / CACHE_FILENAME


# --------------------------------------------------------------- app/version

def app_bundle(cfg):
    """Return the ``.app`` bundle that contains ``dsh_bin``, if any."""
    raw = _cfg_value(cfg, "dsh_bin", "")
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))
    try:
        path = path.resolve()
    except Exception:
        pass
    for parent in (path,) + tuple(path.parents):
        if parent.name.endswith(".app"):
            return parent
    return None


def app_asar(cfg):
    bundle = app_bundle(cfg)
    if not bundle:
        return None
    candidate = bundle / "Contents" / "Resources" / "app.asar"
    return candidate if candidate.is_file() else None


def harness_version(cfg):
    """Read the app bundle version (plist), then fall back to the dsh package."""
    bundle = app_bundle(cfg)
    if bundle:
        plist = bundle / "Contents" / "Info.plist"
        try:
            import plistlib
            with plist.open("rb") as handle:
                data = plistlib.load(handle)
            version = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
            if version:
                return str(version)
        except Exception:
            pass
    asar = app_asar(cfg)
    if asar:
        try:
            raw = _asar_read_text(
                asar, "dsh/node_modules/@deepseek-ai/dsh-subagent/package.json")
            data = json.loads(raw)
            if data.get("version"):
                return str(data["version"])
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- asar read
# asar is a Chromium pickle: 4-byte marker, 4-byte pickle size, 4-byte header
# size, then the JSON directory at offset 16.  File offsets are relative to
# (12 + header_size).  This reader intentionally supports only regular files.

def _asar_read_text(asar_path, inner_path):
    with open(str(asar_path), "rb") as handle:
        head = handle.read(16)
        if len(head) < 16:
            raise ValueError("short asar header")
        _marker, _pickle_size, header_size = struct.unpack("<III", head[:12])
        raw = handle.read(header_size)
        text = raw.decode("utf-8", "ignore")
        header, _end = json.JSONDecoder().raw_decode(text)
        node = header
        for part in inner_path.split("/"):
            node = node["files"][part]
        if node.get("files") is not None:
            raise ValueError("%s is a directory" % inner_path)
        offset = int(node.get("offset", 0))
        size = int(node.get("size", 0))
        data_start = 12 + header_size
        handle.seek(data_start + offset)
        return handle.read(size).decode("utf-8", "replace")


def _bundled_readme(cfg, package):
    asar = app_asar(cfg)
    if not asar:
        return None
    try:
        return _asar_read_text(
            asar, "dsh/node_modules/@deepseek-ai/%s/README.md" % package)
    except Exception:
        return None


# ------------------------------------------------------------- documentation

def discover_documented(cfg):
    """Read documented child limits from the installed Harness.

    Returns ``{"limit", "subagent_limit", "engine_limit", "source"}`` or an
    empty dict when the install is not readable.
    """
    subagent = None
    engine = None
    readme = _bundled_readme(cfg, "dsh-subagent")
    if readme:
        match = re.search(
            r"maxActiveSubagents[^\n]{0,240}?defaults to\s*`?(\d+)`?", readme,
            re.IGNORECASE | re.DOTALL)
        if match:
            try:
                subagent = int(match.group(1))
            except ValueError:
                subagent = None
    ptc = _bundled_readme(cfg, "dsh-workflow-ptc")
    if ptc:
        match = re.search(
            r"maxConcurrentAgents[^\n]{0,240}?\|\s*`?(\d+)`?\s*\|", ptc,
            re.IGNORECASE)
        if match:
            try:
                engine = int(match.group(1))
            except ValueError:
                engine = None
        if engine == 0:
            code_text = None
            asar = app_asar(cfg)
            if asar:
                try:
                    code_text = _asar_read_text(
                        asar, "dsh/node_modules/@deepseek-ai/dsh-workflow-ptc/lib/index.js")
                except Exception:
                    code_text = None
            code_match = re.search(
                r"Math\.min\(\s*(\d+)\s*,\s*Math\.max\(\s*1\s*,\s*"
                r"availableParallelism\(\)\s*-\s*(\d+)\s*\)\s*\)",
                code_text or ptc)
            if code_match:
                ceiling = int(code_match.group(1))
                reserve = int(code_match.group(2))
                cpus = os.cpu_count() or 1
                engine = max(1, min(ceiling, cpus - reserve))
            else:
                engine = None
        elif engine is None:
            # The README table says 0; try the shipped engine code once.
            code_match = re.search(
                r"maxConcurrentAgents:\s*this\.config\.maxConcurrentAgents === 0\s*"
                r"\?\s*Math\.min\(\s*(\d+)\s*,\s*Math\.max\(\s*1\s*,\s*"
                r"availableParallelism\(\)\s*-\s*(\d+)\s*\)\s*\)", ptc)
            if code_match:
                cpus = os.cpu_count() or 1
                engine = max(1, min(int(code_match.group(1)), cpus - int(code_match.group(2))))
    limits = [value for value in (subagent, engine) if isinstance(value, int) and value > 0]
    if not limits:
        return {}
    return {
        "limit": min(limits),
        "subagent_limit": subagent,
        "engine_limit": engine,
        "source": "documented",
    }


# --------------------------------------------------------------------- cache

def read_cache(cfg, now=None):
    path = cache_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("max_concurrent_children")
    if not isinstance(value, int) or value <= 0:
        return None
    stamp = data.get("detected_at_epoch")
    if not isinstance(stamp, (int, float)):
        try:
            stamp = dt.datetime.fromisoformat(str(data.get("detected_at"))).timestamp()
        except Exception:
            return None
    age = (now or _now()).timestamp() - float(stamp)
    if age < 0:
        age = 0
    data["age_seconds"] = age
    data["expired"] = age > CACHE_TTL_SECONDS
    return data


def write_cache(cfg, value, source, harness_version_value=None, probe=None):
    record = {
        "max_concurrent_children": int(value),
        "source": source,
        "detected_at": _iso(),
        "detected_at_epoch": round(time.time(), 3),
        "harness_version": harness_version_value,
        "ttl_days": 7,
    }
    if probe:
        record["probe"] = probe
    path = cache_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        pass
    return record


def invalidate(cfg):
    """Forget the cached value after an agent-limit error or version change."""
    try:
        cache_path(cfg).unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# --------------------------------------------------------------------- probe

def _probe_task(n):
    script = """const N = %d;
let active = 0, maxActive = 0, started = 0;
const failures = [];
async function one(i) {
  started += 1;
  active += 1;
  if (active > maxActive) maxActive = active;
  try {
    return await agent('Reply with exactly: OK ' + i);
  } catch (e) {
    failures.push(String(e && e.message || e));
    return null;
  } finally {
    active -= 1;
  }
}
const out = await parallel(Array.from({length: N}, (_, i) => () => one(i)));
return {maxActive, started, failures};
""" % int(n)
    return (
        "This is a one-off Harness concurrency probe. Do not create deliverables and do not "
        "modify existing files; the only file you may write is `concurrency-probe.json` "
        "described below. Use the workflow tool ONCE with exactly this JavaScript body "
        "(meta name: concurrency-probe):\n\n"
        "```javascript\n%s```\n\n"
        "The workflow must launch %d trivial agents at the same time. Each agent prompt is just "
        "'Reply with exactly: OK <number>'. Then write the returned JSON to the file "
        "`concurrency-probe.json` in the current working folder and end your answer with a line "
        "containing exactly: %s followed by that JSON. If an agent is rejected because of an "
        "active-child limit, the script must catch the error and still return maxActive/started/"
        "failures; do not retry and do not launch more than %d agents.\n"
        % (script, int(n), PROBE_MARKER, int(n))
    )


def _content_text(value):
    """Flatten a harness answer field that may be str, list, or dict."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    if isinstance(value, dict):
        return _content_text(value.get("text") or value.get("content")
                             or value.get("answer"))
    return ""


def _event_answer_text(event):
    """Best-effort answer text from one harness ``--json`` event.

    Mirrors harness.parse_event: the text may live under text/answer/
    message/content/result/output at the top level or inside ``data``.
    """
    if not isinstance(event, dict):
        return ""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    for source in (event, data):
        for key in ANSWER_KEYS:
            text = _content_text(source.get(key))
            if text.strip():
                return text
    return ""


def _extract_probe_result(text):
    """Find a JSON object containing maxActive/started/failures in text.

    Handles both a bare JSON object in prose and one escaped inside a string
    value of a JSON event line (``{"type":"text","text":"{...}"}``), which is
    how streamed assistant text carries the probe answer in ``--json`` mode.
    """
    if not text:
        return None
    found = _scan_probe_json(text)
    if found is not None:
        return found
    return _scan_embedded_probe_json(text)


def _scan_probe_json(text):
    """Return the first bare JSON object in text that carries probe keys."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if _looks_like_probe_result(value):
            return value
    return None


def _looks_like_probe_result(value):
    return isinstance(value, dict) and any(
        key in value for key in ("maxActive", "max_active",
                                 "max_concurrent", "max_concurrent_children",
                                 "concurrency"))


def _scan_embedded_probe_json(text, depth=2):
    """Find probe JSON escaped inside the string values of JSON in text."""
    if depth <= 0:
        return None
    for value in _decoded_json_values(text):
        for string in _string_values(value):
            if "{" not in string:
                continue
            found = _scan_probe_json(string)
            if found is not None:
                return found
            found = _scan_embedded_probe_json(string, depth - 1)
            if found is not None:
                return found
    return None


def _decoded_json_values(text):
    """Yield JSON values decoded from text (the whole text or each line)."""
    try:
        value = json.loads(text)
    except Exception:
        value = None
    if value is not None:
        yield value
        return
    for line in text.splitlines():
        try:
            yield json.loads(line)
        except Exception:
            continue


def _string_values(value):
    """Yield every string inside a decoded JSON value, however nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            for text in _string_values(item):
                yield text
    elif isinstance(value, list):
        for item in value:
            for text in _string_values(item):
                yield text


def _probe_value(result):
    if not isinstance(result, dict):
        return None, None
    raw = result.get("maxActive", result.get("max_active"))
    if raw is None:
        raw = result.get("max_concurrent", result.get("max_concurrent_children",
                                                       result.get("concurrency")))
    try:
        max_active = int(raw)
    except (TypeError, ValueError):
        max_active = 0
    failures = result.get("failures") or result.get("errors") or []
    if isinstance(failures, str):
        failures = [failures]
    limit_error = any(re.search(
        r"ACTIVATION_LIMIT_REACHED|subagent limit reached|active child limit|"
        r"agent.?limit|capacity", str(item), re.IGNORECASE) for item in failures)
    if max_active <= 0:
        return None, {"max_active": max_active, "limit_error": limit_error,
                      "failures": failures[:8]}
    # If every launch succeeded and maxActive reached N, N is a lower bound.
    return max_active, {"max_active": max_active, "limit_error": limit_error,
                        "failures": failures[:8]}


def _run_probe_process(cfg, n):
    """Run the probe through the Harness CLI. Returns a dict or raises."""
    dsh = Path(os.path.expanduser(str(_cfg_value(cfg, "dsh_bin", ""))))
    if not dsh.is_file() or not os.access(str(dsh), os.X_OK):
        raise RuntimeError("Harness CLI is not executable: %s" % dsh)
    root = state_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="concurrency-probe-", dir=str(root)))
    timeout = min(240.0, max(30.0, float(_cfg_value(cfg, "timeout", 600.0) or 600.0)))
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    try:
        proc = subprocess.run(
            [str(dsh), "--profile", "headless", "--json", _probe_task(n)],
            cwd=str(workspace), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    finally:
        # The workspace is only for the probe's one JSON file; leave it in
        # place on failure for diagnostics, remove it on success below.
        pass
    answer_texts = []
    other_texts = []
    for line in (proc.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        text = _event_answer_text(event)
        if not text:
            continue
        typ = str(event.get("type") or "").lower() if isinstance(event, dict) else ""
        if typ in FINAL_EVENT_TYPES:
            answer_texts.append(text)
        else:
            other_texts.append(text)
    result = None
    probe_file = workspace / "concurrency-probe.json"
    if probe_file.is_file():
        try:
            result = json.loads(probe_file.read_text(encoding="utf-8"))
        except Exception:
            result = None
    if result is None:
        # Last final-ish answer first, then any other streamed text.
        for text in list(reversed(answer_texts)) + list(reversed(other_texts)):
            result = _extract_probe_result(text)
            if result is not None:
                break
    if result is None:
        result = _extract_probe_result(proc.stdout or "")
    if result is None:
        raise RuntimeError("probe produced no readable result")
    value, details = _probe_value(result)
    details["exit_code"] = proc.returncode
    details["stderr_tail"] = (proc.stderr or "")[-500:]
    if value is None:
        raise RuntimeError("probe reported no concurrent child (limit error: %s)"
                           % details.get("limit_error"))
    # Keep successful probe artifacts out of the state directory.
    try:
        for path in workspace.iterdir():
            path.unlink()
        workspace.rmdir()
    except OSError:
        pass
    return {"limit": value, "details": details, "source": "probe"}


def probe_concurrency(cfg, runner=None, n=PROBE_AGENTS):
    """Cheap probe: launch ``n`` trivial workflow agents and read max active.

    ``runner`` is injectable for tests; it receives ``(cfg, n)`` and returns
    either an int or a dict with ``limit``/``maxActive``.
    """
    runner = runner or _run_probe_process
    try:
        result = runner(cfg, int(n))
    except Exception as exc:
        return {"limit": None, "error": str(exc), "source": "probe"}
    if isinstance(result, int):
        return {"limit": result, "source": "probe", "details": {}}
    if isinstance(result, str):
        result = _extract_probe_result(result)
    if isinstance(result, dict):
        if isinstance(result.get("limit"), int):
            return {"limit": result["limit"], "source": "probe",
                    "details": result.get("details") or {}}
        value, details = _probe_value(result)
        return {"limit": value, "source": "probe", "details": details or {},
                "error": None if value else "probe reported no concurrent child"}
    return {"limit": None, "error": "probe returned an unsupported result",
            "source": "probe"}


# ------------------------------------------------------------------ detection

def detect(cfg, force_probe=False, probe_runner=None, now=None, allow_probe=True):
    """Return the effective child concurrency and metadata.

    Order: explicit config override, fresh cache, documented install default,
    probe, fallback 8.  ``force_probe`` skips the documented step and is used
    after an agent-limit error or when the caller wants a fresh probe.
    """
    override = _cfg_value(cfg, "max_concurrent_children", None)
    if override is not None:
        try:
            override = int(override)
        except (TypeError, ValueError):
            override = None
    if isinstance(override, int) and override > 0:
        return {"limit": override, "source": "config", "cached": False,
                "harness_version": harness_version(cfg), "age_seconds": 0}

    version = harness_version(cfg)
    cached = read_cache(cfg, now=now)
    if cached and not cached.get("expired"):
        cached_version = cached.get("harness_version")
        if not version or not cached_version or str(version) == str(cached_version):
            return {"limit": int(cached["max_concurrent_children"]),
                    "source": cached.get("source") or "cache", "cached": True,
                    "harness_version": cached_version,
                    "age_seconds": round(cached.get("age_seconds", 0), 1)}

    documented = discover_documented(cfg)
    if documented.get("limit") and not force_probe and probe_runner is None:
        record = write_cache(cfg, documented["limit"], "documented", version,
                             probe={"subagent_limit": documented.get("subagent_limit"),
                                    "engine_limit": documented.get("engine_limit")})
        return {"limit": int(documented["limit"]), "source": "documented",
                "cached": False, "harness_version": version,
                "subagent_limit": documented.get("subagent_limit"),
                "engine_limit": documented.get("engine_limit"),
                "detected_at": record.get("detected_at")}

    if allow_probe:
        probe = probe_concurrency(cfg, runner=probe_runner)
    else:
        probe = {"limit": None, "source": "probe", "error": "probe skipped"}
    if probe.get("limit"):
        record = write_cache(cfg, probe["limit"], "probe", version,
                             probe=probe.get("details"))
        return {"limit": int(probe["limit"]), "source": "probe", "cached": False,
                "harness_version": version, "probe": probe.get("details"),
                "detected_at": record.get("detected_at"),
                "probe_error": probe.get("error")}

    # Last resort: documented value even if force_probe was requested and the
    # probe failed, then the generic fallback.  A probe that produced no
    # result is never cached as authoritative truth: the soft default is
    # returned for this call only (source stays "documented"/"fallback", never
    # "probe"), so the next detect() re-probes instead of pinning the failure
    # for the whole cache lifetime.
    if documented.get("limit"):
        value = int(documented["limit"])
        source = "documented"
    else:
        value = FALLBACK_CONCURRENCY
        source = "fallback"
    return {"limit": value, "source": source, "cached": False,
            "harness_version": version, "probe_error": probe.get("error"),
            "detected_at": _iso()}


def get_concurrency(cfg, **kwargs):
    """Small compatibility alias used by callers/tests."""
    return detect(cfg, **kwargs)


def note_agent_limit(cfg):
    """Invalidate the cache and re-detect after ACTIVATION_LIMIT_REACHED."""
    invalidate(cfg)
    return detect(cfg, force_probe=True)


# Friendly aliases for callers that name the operation instead of the module.
detect_concurrency = detect
detect_harness_concurrency = detect


def get_max_concurrent_children(cfg, **kwargs):
    return detect(cfg, **kwargs)["limit"]
