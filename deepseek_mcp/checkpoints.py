"""Durable per-wave/phase checkpoint manifest.

One JSON manifest per run under ``state_dir/checkpoints``.  Writes are atomic
(``mkstemp`` in the target directory + ``os.replace``) so a crash can never
leave a half-written checkpoint.  Reads are defensive: a missing or corrupt
manifest loads as ``None`` instead of raising.

Python 3.9 compatible; standard library only.
"""

import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path

VERSION = 1
STATUSES = ("pending", "running", "done", "failed", "interrupted")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_.\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
)
# Local state is private, but it is still easy to paste a checkpoint into an
# issue. Store home-relative paths so the manifest never carries an operator
# account name. This mirrors api.redact_secrets; Windows needs its own pattern.
_HOME_PATTERNS = (
    re.compile(r"(?<![\w.-])/(?:Users|home)/[^/\s\"']+"),
    re.compile(r"(?i)\b[A-Za-z]:\\+Users\\+[^\\\s\"']+"),
)
# A review report is longer than a normal error string. Keep a generous bound so
# a resumed run can restore the real defects and fix list, not a stub.
TEXT_LIMIT = 200000


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _redact(value, limit=TEXT_LIMIT):
    """Stringify and scrub key material and home paths; never raise."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        text = "<unprintable>"
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    for pattern in _HOME_PATTERNS:
        text = pattern.sub("~", text)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def _safe_run_id(run_id):
    text = str(run_id or "")
    if not _SAFE_RUN_ID.match(text) or ".." in text or text in (".", ".."):
        raise ValueError("unsafe run id")
    return text


def _clean(value):
    """Recursively redact every string, not only non-serialisable objects.

    ``json.dumps(default=...)`` is never called for a string, so a checkpoint
    that stored a path or a key inside a normal dict would previously keep it
    verbatim.  This walks the value instead.
    """
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value)


def _jsonable(value):
    """Best-effort JSON-safe, redacted copy; unknown objects become strings."""
    try:
        return json.loads(json.dumps(_clean(value), ensure_ascii=True))
    except Exception:
        return _redact(value)


def _normalise_plan(plan):
    if plan is None:
        return []
    if isinstance(plan, (str, bytes)):
        raise TypeError("plan must be a sequence of phase names")
    names = []
    for raw in plan:
        name = str(raw).strip()
        if name and name not in names:
            names.append(name)
    return names


class Checkpoint:
    """A single run's durable checkpoint manifest."""

    def __init__(self, root, run_id):
        self.root = Path(root)
        self.run_id = _safe_run_id(run_id)
        self.path = self.root / (self.run_id + ".json")

    # -- persistence -----------------------------------------------------
    def _write(self, manifest):
        self.root.mkdir(parents=True, exist_ok=True)
        manifest["version"] = VERSION
        manifest["run_id"] = self.run_id
        manifest["updated"] = _now()
        payload = json.dumps(
            manifest, ensure_ascii=True, sort_keys=True, indent=2
        ).encode("utf-8")
        fd, tmp = tempfile.mkstemp(
            prefix=".checkpoint-", suffix=".tmp", dir=str(self.root)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, str(self.path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(str(self.path), 0o600)
        except OSError:
            pass
        return manifest

    def load(self):
        """Return the manifest, or ``None`` when missing/corrupt."""
        try:
            if not self.path.is_file():
                return None
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if (not isinstance(data, dict) or data.get("version") != VERSION
                or data.get("run_id") != self.run_id
                or not isinstance(data.get("phases"), dict)
                or not isinstance(data.get("plan"), list)):
            return None
        return data

    @staticmethod
    def find(root, run_id):
        try:
            return Checkpoint(root, run_id).load()
        except (ValueError, OSError):
            return None

    # -- lifecycle -------------------------------------------------------
    def create(self, plan, metadata=None):
        names = _normalise_plan(plan)
        now = _now()
        manifest = {
            "version": VERSION,
            "run_id": self.run_id,
            "plan": names,
            "phases": {
                name: {
                    "status": "pending",
                    "started": None,
                    "finished": None,
                    "data": {},
                }
                for name in names
            },
            "status": "running",
            "created": now,
            "updated": now,
            "metadata": _jsonable(metadata or {}),
        }
        return self._write(manifest)

    def update(self, **fields):
        manifest = self.load()
        if manifest is None:
            raise FileNotFoundError("checkpoint manifest not found")
        for key, value in fields.items():
            if key in ("version", "run_id", "path", "phases", "plan"):
                continue
            manifest[key] = _jsonable(value)
        return self._write(manifest)

    def _ensure(self):
        manifest = self.load()
        if manifest is None:
            manifest = self.create([])
        return manifest

    def _entry(self, manifest, phase, create=True):
        name = str(phase)
        phases = manifest.setdefault("phases", {})
        if name not in phases and create:
            phases[name] = {
                "status": "pending",
                "started": None,
                "finished": None,
                "data": {},
            }
            if name not in manifest.setdefault("plan", []):
                manifest["plan"].append(name)
        return phases.get(name)

    def begin(self, phase, **data):
        manifest = self._ensure()
        entry = self._entry(manifest, phase)
        if entry.get("status") != "done":
            entry["status"] = "running"
        if not entry.get("started"):
            entry["started"] = _now()
        entry["finished"] = None
        merged = dict(entry.get("data") or {})
        merged.update(_jsonable(data))
        entry["data"] = merged
        manifest["status"] = "running"
        return self._write(manifest)

    def finish(self, phase, status="done", **data):
        manifest = self._ensure()
        entry = self._entry(manifest, phase)
        now = _now()
        if not entry.get("started"):
            entry["started"] = now
        entry["status"] = str(status or "done")
        entry["finished"] = now
        merged = dict(entry.get("data") or {})
        merged.update(_jsonable(data))
        entry["data"] = merged
        return self._write(manifest)

    def fail(self, phase, error):
        return self.finish(phase, status="failed", error=_redact(error))

    def interrupted(self, reason):
        manifest = self._ensure()
        now = _now()
        for entry in manifest.get("phases", {}).values():
            if entry.get("status") == "running":
                entry["status"] = "interrupted"
                entry["finished"] = now
                entry.setdefault("data", {})["interrupted"] = _redact(reason)
        manifest["status"] = "interrupted"
        manifest["interrupted"] = _redact(reason)
        return self._write(manifest)

    def next_phase(self):
        manifest = self.load()
        if manifest is None:
            return None
        phases = manifest.get("phases") or {}
        for name in manifest.get("plan") or []:
            entry = phases.get(name) or {}
            if entry.get("status") != "done":
                return name
        return None

    def phase(self, name):
        manifest = self.load()
        if manifest is None:
            return None
        entry = (manifest.get("phases") or {}).get(str(name))
        return dict(entry) if isinstance(entry, dict) else None

    def finalize(self, result=None):
        manifest = self._ensure()
        manifest["status"] = "finished"
        manifest["finished"] = _now()
        if result is not None:
            manifest["result"] = _redact(result)
        return self._write(manifest)


def resume_plan(plan, manifest):
    """Summarise how to resume ``plan`` from a loaded manifest."""
    names = _normalise_plan(plan)
    phases = (manifest or {}).get("phases") or {}
    if not isinstance(phases, dict):
        phases = {}
    skip = [
        name for name in names
        if (phases.get(name) or {}).get("status") == "done"
    ]
    resume = None
    for name in names:
        if (phases.get(name) or {}).get("status") != "done":
            resume = name
            break
    restart_builder = bool(names) and (
        (phases.get(names[0]) or {}).get("status") != "done"
    )
    return {"skip": skip, "resume": resume, "restart_builder": restart_builder}
