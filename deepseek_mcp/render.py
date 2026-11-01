"""Discover headless Chrome and probe whether it can actually render here.

A live first-wave run showed eight children each
retrying broken Chrome variants for minutes.  A cheap, bounded probe before the
wave tells builders either the exact binary/flags that work or that rendering
is unavailable and they should ship source instead of installing browsers.

The probe writes only a throwaway profile under the workspace and a small cache
under ``state_dir``.  It never installs or downloads anything, and every child
process runs under a hard timeout with a killed process group.  Only operator
configuration, ``PATH`` and standard system app paths are trusted: the
model-writable workspace is never searched for an executable, and the browser
gets a minimal environment without connector API keys.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import platforms

POSITIVE_TTL = 24 * 60 * 60
NEGATIVE_TTL = 60 * 60
CACHE_NAME = "render-probe.json"
PATH_NAMES = ("chrome-headless-shell", "chromium", "chromium-browser",
              "google-chrome", "google-chrome-stable")
APP_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def _cfg(cfg, name, default=None):
    try:
        return cfg.get(name, default) if hasattr(cfg, "get") else getattr(cfg, name, default)
    except Exception:
        return default


def display_path(path):
    """Home-relative display path; never put a full personal path in a task."""
    text = str(path or "")
    try:
        home = str(Path.home())
        if home and (text == home or text.startswith(home + os.sep)):
            return "~" + text[len(home):]
    except Exception:
        pass
    return text


def _redact(text):
    """Remove the user's home prefix from probe diagnostics."""
    text = str(text or "")
    try:
        home = str(Path.home())
        if home:
            # Only a complete path component: a longer sibling with a numeric
            # suffix must not be rewritten to ~2.
            text = re.sub(re.escape(home) + r"(?=$|[\\/])", "~", text)
    except Exception:
        pass
    return text


def _is_executable(path):
    try:
        return Path(path).is_file() and os.access(str(path), os.X_OK)
    except OSError:
        return False


def find_chromes(cfg, workspace=None):
    """Return trusted Chrome candidates in preference order, de-duplicated.

    Trust boundary: only operator configuration (``chrome_bin``), ``PATH`` and
    the standard system application paths are trusted.  The Harness workspace
    is model-writable, so a browser dropped in ``<workspace>/.tools`` is not
    discovered or executed implicitly.  An operator who wants that binary must
    opt in with ``DSH_CHROME_BIN``.
    """
    candidates = []
    seen = set()

    def add(raw):
        try:
            text = str(raw)
            key = os.path.realpath(text)
        except (TypeError, ValueError, OSError):
            return
        if not text or key in seen or not _is_executable(text):
            return
        seen.add(key)
        candidates.append(Path(text))

    configured = str(_cfg(cfg, "chrome_bin", "") or "").strip()
    if configured:
        add(os.path.expanduser(configured))
    for name in PATH_NAMES:
        found = shutil.which(name)
        if found:
            add(found)
    for raw in APP_PATHS:
        add(raw)
    # Per-OS Chrome/Edge locations (Windows Program Files, Linux snap, ...).
    for raw in platforms.chrome_candidates():
        add(raw)
    return candidates


def find_chrome(cfg, workspace=None):
    """Return the first usable trusted Chrome binary, or ``None``."""
    candidates = find_chromes(cfg, workspace)
    return candidates[0] if candidates else None


def _probe_env(home, tmp):
    """Minimal browser environment: no API keys or other connector secrets."""
    keep = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "DISPLAY",
            "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "SystemRoot", "WINDIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["NO_COLOR"] = "1"
    return env


def _run(cmd, env, timeout):
    """Run a command in its own process group; always bound it.

    Returns ``(returncode, stdout, stderr, seconds, timed_out)``.  On timeout
    the whole group is killed, not just the direct child.
    """
    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            text=True, **platforms.popen_kwargs())
    except Exception as exc:
        return None, "", str(exc), round(time.time() - started, 2), False
    try:
        out, err = proc.communicate(timeout=max(0.1, float(timeout)))
        return proc.returncode, out or "", err or "", round(time.time() - started, 2), False
    except subprocess.TimeoutExpired:
        try:
            platforms.terminate_tree(proc.pid, timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            out, err = proc.communicate(timeout=2.0)
        except Exception:
            out, err = "", ""
        return None, out or "", err or "", round(time.time() - started, 2), True


def _binary_key(binary):
    """Cache identity: home-relative path plus size/mtime.

    A Chrome update in place changes the file metadata, so a stale positive
    cannot hide a newly broken binary.  The path is stored home-relative to
    avoid writing a personal absolute path into the state cache.
    """
    path = Path(binary)
    try:
        stat = path.stat()
        return "%s:%d:%d" % (display_path(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return display_path(path)


def _cache_path(cfg):
    raw = str(_cfg(cfg, "state_dir", "") or "").strip()
    if not raw:
        return None
    state = Path(os.path.expanduser(raw))
    if str(state) in ("", "."):
        return None
    return state / CACHE_NAME


def _read_cache(path):
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(path, binary, result):
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Store the home-relative display path, not a personal absolute path.
        payload = {"key": _binary_key(binary), "at": time.time(), "result": result}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(str(temp), str(path))
    except Exception:
        pass


def _cache_valid(payload, binary, now=None):
    if not isinstance(payload, dict) or payload.get("key") != _binary_key(binary):
        return False
    result = payload.get("result") or {}
    ttl = POSITIVE_TTL if result.get("available") else NEGATIVE_TTL
    try:
        return (float(now or time.time()) - float(payload.get("at") or 0)) < ttl
    except (TypeError, ValueError):
        return False


PROBE_MAX_CANDIDATES = 4


def _probe_binary(binary, env, profile, limit, deadline):
    """Probe one candidate inside the overall budget.

    Fast failures return ``stop=False`` so the caller can try the next
    discovered binary.  A timeout or an exhausted budget returns ``stop=True``:
    retrying another binary after a hang would only double the wait.
    """
    attempts = 0
    version = None
    reason = None

    def remaining():
        return deadline - time.time()

    if remaining() <= 0:
        return {"available": False, "version": None, "headless": None,
                "reason": "probe budget exhausted", "attempts": 0, "stop": True}
    try:
        timeout = max(0.1, min(limit, 5.0, remaining()))
        code, out, err, _seconds, timed_out = _run(
            [str(binary), "--version"], env, timeout)
        attempts += 1
        if timed_out:
            return {"available": False, "version": None, "headless": None,
                    "reason": "Chrome --version timed out after %.1f s" % timeout,
                    "attempts": attempts, "stop": True}
        if code != 0:
            return {"available": False, "version": None, "headless": None,
                    "reason": "Chrome --version failed (exit %s): %s" % (
                        code, _redact((err or out or "no output").strip())[:300]),
                    "attempts": attempts, "stop": False}
        version = ((out or err or "").strip().splitlines()[0][:200]
                   if (out or err) else None)
        for headless in ("--headless=new", "--headless"):
            if remaining() <= 0:
                return {"available": False, "version": version, "headless": None,
                        "reason": "probe budget exhausted before render",
                        "attempts": attempts, "stop": True}
            command = [
                str(binary), headless, "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-sync",
                "--user-data-dir=%s" % profile, "--dump-dom", "about:blank",
            ]
            timeout = max(0.1, min(limit, remaining()))
            code, out, err, _seconds, timed_out = _run(command, env, timeout)
            attempts += 1
            if timed_out:
                # A timed-out render is not a flag mismatch; retrying the other
                # headless spelling (or candidate) would just double the hang.
                return {"available": False, "version": version, "headless": None,
                        "reason": "headless render timed out after %.1f s" % timeout,
                        "attempts": attempts, "stop": True}
            if code == 0 and (out or "").strip():
                return {"available": True, "version": version, "headless": headless,
                        "reason": None, "attempts": attempts, "stop": False}
            reason = "headless render failed (exit %s): %s" % (
                code, _redact((err or out or "no output").strip())[:300])
    except Exception as exc:
        reason = "probe error: %s" % _redact(exc)
    return {"available": False, "version": version, "headless": None,
            "reason": reason or "unknown render failure",
            "attempts": attempts, "stop": False}


def probe(cfg, workspace, timeout=None, force=False):
    """Probe discovered browsers in order; return the first working result.

    Each command is bounded and the whole probe has a wall-clock budget, so a
    broken first candidate (for example macOS Chrome failing on Crashpad)
    cannot hide a working ``chrome-headless-shell`` later in the list.  The
    positive result is cached for a day and a negative one for an hour.  Any
    failure returns a JSON-serialisable ``available=False`` result; it never
    raises.
    """
    try:
        limit = float(timeout if timeout is not None
                      else (_cfg(cfg, "render_probe_timeout", 12.0) or 12.0))
    except (TypeError, ValueError):
        limit = 12.0
    limit = max(1.0, min(limit, 60.0))
    candidates = find_chromes(cfg, workspace)
    if not candidates:
        return {"available": False, "probed": False, "binary": None,
                "version": None, "headless": None, "seconds": 0.0,
                "attempts": 0, "reason": "no headless Chrome binary found"}
    cache = _cache_path(cfg)
    cached = _read_cache(cache)
    if not force:
        for candidate in candidates:
            if _cache_valid(cached, candidate):
                result = dict(cached.get("result") or {})
                result["cached"] = True
                return result

    workspace = Path(workspace)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    stamp = "%d-%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    profile = workspace / (".chrome-probe-" + stamp)
    home = workspace / (".chrome-probe-home-" + stamp)
    tmp = workspace / (".chrome-probe-tmp-" + stamp)
    env = _probe_env(home, tmp)
    started = time.time()
    attempts = 0
    result = None
    last_binary = candidates[0]
    try:
        for directory in (profile, home, tmp):
            directory.mkdir(parents=True, exist_ok=True)
        # One slow candidate must not consume an unbounded number of retries.
        deadline = started + max(limit * 2.0, 6.0)
        for index, binary in enumerate(candidates[:PROBE_MAX_CANDIDATES]):
            candidate_profile = profile / ("candidate-%d" % index)
            try:
                candidate_profile.mkdir(parents=True, exist_ok=True)
            except OSError:
                candidate_profile = profile
            outcome = _probe_binary(binary, env, candidate_profile, limit, deadline)
            attempts += outcome["attempts"]
            result = {
                "available": bool(outcome["available"]),
                "probed": True,
                "binary": display_path(binary),
                "version": outcome["version"],
                "headless": outcome["headless"],
                "seconds": round(time.time() - started, 2),
                "attempts": attempts,
                "reason": None if outcome["available"] else (
                    outcome["reason"] or "unknown render failure"),
            }
            last_binary = binary
            if outcome["available"] or outcome["stop"]:
                break
    except Exception as exc:
        result = {
            "available": False, "probed": True,
            "binary": display_path(last_binary), "version": None,
            "headless": None, "seconds": round(time.time() - started, 2),
            "attempts": attempts,
            "reason": "probe error: %s" % _redact(exc),
        }
    finally:
        for directory in (profile, home, tmp):
            shutil.rmtree(str(directory), ignore_errors=True)
    if result is None:
        result = {
            "available": False, "probed": True,
            "binary": display_path(last_binary), "version": None,
            "headless": None, "seconds": round(time.time() - started, 2),
            "attempts": attempts,
            "reason": "probe budget exhausted before any candidate",
        }
    _write_cache(cache, last_binary, result)
    return result


def hint(result):
    """Turn a probe result into builder instructions (positive or negative)."""
    if not result:
        return None
    if result.get("available"):
        binary = result.get("binary") or "chrome"
        headless = result.get("headless") or "--headless=new"
        text = (
            "Connector health probe passed: `%s` rendered `about:blank` in %.1f s "
            "(version %s). Use exactly this binary for every visual render, with "
            "`%s --no-sandbox --disable-gpu`, a fresh "
            "`--user-data-dir=\"$PWD/.chrome-profile\"`, `HOME`/`TMPDIR` under the "
            "workspace, and a hard shell timeout. Do not download or install another "
            "browser."
            % (binary, float(result.get("seconds") or 0.0),
               result.get("version") or "unknown", headless))
        if str(binary).startswith("~"):
            text += (
                " The leading tilde (`~`) is the login home used by the probe, "
                "not the workspace-local `HOME` you will set; resolve it to the "
                "absolute binary path before you override `HOME`, or the "
                "executable will not be found.")
        return text
    reason = result.get("reason") or "unknown failure"
    reason = _redact(reason)
    return (
        "Connector health probe: headless Chrome is unavailable here (%s). Do NOT "
        "install, download or repair Chrome, Chromium, Edge, Playwright or any browser. "
        "Write the HTML/source deliverable first, render only if a single bounded "
        "attempt succeeds, and otherwise record `render unavailable: %s` in the result "
        "and ship the source." % (reason, reason))


def _html_candidates(workspace, html_path=None):
    root = Path(workspace)
    found = []
    if html_path:
        candidate = Path(html_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            found.append(candidate)
    for name in ("index.html", "poster.html", "report.html"):
        candidate = root / name
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    try:
        for candidate in sorted(root.glob("*.html")):
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    except OSError:
        pass
    return found


def _parse_console(stderr):
    console, network = [], []
    for raw in str(stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if "net::err_" in lowered or "failed to load resource" in lowered:
            network.append(_redact(line)[:300])
        elif ("console(" in lowered or "uncaught" in lowered
              or " javascript error" in lowered) and "error" in lowered:
            console.append(_redact(line)[:300])
    return console[-20:], network[-20:]


def _dom_summary(text):
    body = str(text or "")
    try:
        title = re.search(r"<title[^>]*>(.*?)</title>", body,
                          re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title.group(1)).strip()[:200] if title else ""
    except Exception:
        title = ""
    images = re.findall(r"<img\b[^>]*>", body, re.IGNORECASE)
    missing_alt = sum(1 for item in images if "alt=" not in item.lower())
    return {
        "bytes": len(body.encode("utf-8", "replace")),
        "title": title,
        "elements_estimate": body.lower().count("<"),
        "scripts": len(re.findall(r"<script\b", body, re.IGNORECASE)),
        "images": len(images),
        "images_missing_alt": missing_alt,
        "forms": len(re.findall(r"<form\b", body, re.IGNORECASE)),
        "has_viewport": bool(re.search(r'name=["\']viewport["\']', body,
                                       re.IGNORECASE)),
    }


def collect_evidence(cfg, workspace, html_path=None, timeout=None):
    """Capture browser evidence next to a visual deliverable.

    Returns a JSON-serialisable summary and writes
    ``<workspace>/.browser-evidence/`` (DOM snapshot, console/network log,
    screenshot, evidence.json). Any missing browser, timeout or crash returns
    ``available=False`` with a short reason; it never raises and never
    downloads anything.
    """
    workspace = Path(workspace)
    evidence_dir = workspace / ".browser-evidence"
    result = {
        "available": False,
        "reason": "no HTML deliverable found",
        "binary": None,
        "url": None,
        "files": [],
        "console_errors": [],
        "network_failures": [],
        "dom": {},
        "performance": {},
    }
    pages = _html_candidates(workspace, html_path)
    if not pages:
        return result
    page = pages[0]
    try:
        probe_result = probe(cfg, workspace)
    except Exception as exc:
        probe_result = {"available": False, "reason": "probe error: %s" % _redact(exc)}
    if not probe_result.get("available"):
        result["reason"] = _redact(probe_result.get("reason")
                                   or "headless Chrome unavailable")
        result["probe"] = probe_result
        _write_evidence(evidence_dir, result)
        return result
    binary = Path(os.path.expanduser(str(probe_result.get("binary") or "")))
    if not binary.is_file():
        result["reason"] = "probe binary is not executable"
        _write_evidence(evidence_dir, result)
        return result
    try:
        limit = float(timeout if timeout is not None
                      else (_cfg(cfg, "render_probe_timeout", 12.0) or 12.0))
    except (TypeError, ValueError):
        limit = 12.0
    limit = max(2.0, min(limit, 60.0))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = "%d-%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    profile = evidence_dir / (".profile-" + stamp)
    home = evidence_dir / ("home-" + stamp)
    tmp = evidence_dir / ("tmp-" + stamp)
    env = _probe_env(home, tmp)
    headless = probe_result.get("headless") or "--headless=new"
    base = [str(binary), headless, "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--hide-scrollbars",
            "--user-data-dir=" + str(profile), "--enable-logging=stderr",
            "--v=1", "--virtual-time-budget=5000"]
    try:
        for directory in (profile, home, tmp):
            directory.mkdir(parents=True, exist_ok=True)
        domain = _run(base + ["--dump-dom", page.as_uri()], env, limit)
        rc, out, err, seconds, timed_out = domain
        if timed_out or rc not in (0, None):
            result["reason"] = ("DOM capture failed (exit %s%s)"
                                % (rc, ", timeout" if timed_out else ""))
            result["performance"] = {"seconds": seconds, "timed_out": bool(timed_out)}
            _write_evidence(evidence_dir, result)
            return result
        dom_text = out or ""
        screenshot_path = evidence_dir / "screenshot.png"
        shot = _run(base + ["--screenshot=" + str(screenshot_path),
                            "--window-size=1440,900", page.as_uri()], env, limit)
        shot_ok = shot[0] == 0 and screenshot_path.is_file()
        try:
            (evidence_dir / "dom.html").write_text(dom_text, encoding="utf-8")
        except OSError:
            pass
        console, network = _parse_console(err)
        result.update({
            "available": True,
            "reason": None,
            "binary": display_path(binary),
            "url": display_path(page),
            "files": sorted(
                str(path.relative_to(workspace))
                for path in evidence_dir.iterdir() if path.is_file()),
            "console_errors": console,
            "network_failures": network,
            "dom": _dom_summary(dom_text),
            "performance": {
                "seconds": round(float(seconds or 0), 2),
                "screenshot_seconds": round(float(shot[3] or 0), 2),
                "timed_out": False,
                "screenshot": bool(shot_ok),
            },
        })
        _write_evidence(evidence_dir, result)
    except Exception as exc:
        result["reason"] = "evidence error: %s" % _redact(exc)
        try:
            _write_evidence(evidence_dir, result)
        except Exception:
            pass
    finally:
        for directory in (profile, home, tmp):
            shutil.rmtree(str(directory), ignore_errors=True)
    return result


def _write_evidence(directory, payload):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "evidence.json"
        tmp = directory / ".evidence.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        pass


def evidence_hint(evidence):
    """Short reviewer-facing summary of a browser-evidence result."""
    if not evidence:
        return None
    if not evidence.get("available"):
        return ("Browser evidence unavailable: %s. Do not treat a missing browser "
                "as a deliverable defect unless the brief required a rendered result."
                % _redact(evidence.get("reason") or "unknown reason"))
    dom = evidence.get("dom") or {}
    lines = [
        "Browser evidence captured automatically (`.browser-evidence/`).",
        "- Console errors: %d" % len(evidence.get("console_errors") or []),
        "- Failed network requests: %d" % len(evidence.get("network_failures") or []),
        "- DOM: %s bytes, %s elements (estimate), title %r"
        % (dom.get("bytes"), dom.get("elements_estimate"), dom.get("title") or ""),
        "- Images missing alt: %s" % dom.get("images_missing_alt"),
    ]
    for item in (evidence.get("console_errors") or [])[:5]:
        lines.append("- console: %s" % item)
    for item in (evidence.get("network_failures") or [])[:5]:
        lines.append("- network: %s" % item)
    return "\n".join(lines)


def is_visual(task_type, skill_key=None):
    """True for tasks that normally need a rendered visual deliverable."""
    return task_type == "creative" or skill_key in ("poster", "web")
