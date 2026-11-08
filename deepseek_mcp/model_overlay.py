"""Generate the temporary DSH model overlay for a resolved target (§4.2).

The overlay contains provider/model ids and an ``apiKeyEnv`` *variable name*;
it never contains a key value, prompt or user path.  The file lives in a fresh
0600 ``.dsh-model-overlay-*`` directory inside the workspace and is removed by
``cleanup()`` (idempotent) or by the context manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
OVERLAY_FILENAME = "overlay.yml"
OVERLAY_PREFIX = ".dsh-model-overlay-"
MAX_OVERLAY_BYTES = 256 * 1024

_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _text(value, limit=200):
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    return text.replace("\x00", "").strip()[:limit]


def _q(value):
    """A JSON string is also a valid YAML double-quoted scalar."""
    return json.dumps(_text(value, 300), ensure_ascii=True)


def _cfg_get(cfg, key, default=None):
    if cfg is None:
        return default
    try:
        value = cfg.get(key, default)
    except Exception:
        try:
            value = getattr(cfg, key, default)
        except Exception:
            value = default
    return default if value is None else value


def _field(target, name, default=""):
    if target is None:
        return default
    if isinstance(target, dict):
        return _text(target.get(name), 300) or default
    return _text(getattr(target, name, default), 300) or default


def _provider_entry(cfg, provider):
    listed = _cfg_get(cfg, "providers", [])
    if not isinstance(listed, list):
        return None
    for item in listed:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("provider") or item.get("id"), 64)
        if name == provider:
            return item
    return None


def _model_ids(entry, target_model):
    models = [target_model] if target_model else []
    raw = entry.get("models") if isinstance(entry, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                item = item.get("id") or item.get("name") or item.get("model")
            text = _text(item, 128)
            if text and text not in models:
                models.append(text)
    return models


def overlay_yaml(target, cfg):
    """Emit the deterministic two-entry YAML overlay for ``target``."""
    provider = _field(target, "provider")
    model = _field(target, "model")
    if not provider or not model:
        raise ValueError("overlay target needs a provider and a model")
    entry = _provider_entry(cfg, provider) or {}
    display = _text(entry.get("displayName") or entry.get("display_name")
                    or entry.get("vendor"), 120)
    if not display:
        display = _field(target, "vendor") or provider
    api_key_env = (_field(target, "api_key_env")
                   or _text(entry.get("api_key_env") or entry.get("apiKeyEnv"), 128)
                   or DEFAULT_API_KEY_ENV)
    api = _field(target, "api") or _text(entry.get("api") or entry.get("api_type"), 80)
    base_url = (_field(target, "base_url")
                or _text(entry.get("base_url") or entry.get("baseURL")
                         or entry.get("url"), 300))

    key = provider if _SAFE_PROVIDER.match(provider) else _q(provider)
    lines = [
        "- id: llm-pi-ai",
        "  config:",
        "    providers:",
        "      %s:" % key,
        "        displayName: %s" % _q(display),
        "        apiKeyEnv: %s" % _q(api_key_env),
    ]
    if api:
        lines.append("        api: %s" % _q(api))
    if base_url:
        lines.append("        baseURL: %s" % _q(base_url.rstrip("/")))
    lines.append("        models:")
    for item in _model_ids(entry, model):
        if not _SAFE_ID.match(item):
            raise ValueError("unsafe model id in overlay")
        lines.append("          - id: %s" % _q(item))
        lines.append("            name: %s" % _q(item))
    lines.extend([
        "- id: agent-default-model",
        "  config:",
        "    provider: %s" % _q(provider),
        "    model: %s" % _q(model),
    ])
    return "\n".join(lines) + "\n"


class ModelOverlay:
    """A written overlay file plus its private directory and digest."""

    def __init__(self, path, directory, digest):
        self.path = path
        self.dir = directory
        self.sha256 = digest

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False

    def cleanup(self):
        """Delete the overlay file and directory; safe to call repeatedly."""
        for attempt in (0, 1):
            try:
                os.unlink(self.path)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt:
                    break
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


def create_overlay(workspace, target, cfg):
    """Write a fresh 0600 overlay and return :class:`ModelOverlay`."""
    workspace = os.path.abspath(str(workspace))
    if not os.path.isdir(workspace):
        raise ValueError("workspace is not a directory")
    content = overlay_yaml(target, cfg).encode("utf-8")
    if len(content) > MAX_OVERLAY_BYTES:
        raise ValueError("overlay is too large")
    digest = hashlib.sha256(content).hexdigest()
    directory = tempfile.mkdtemp(prefix=OVERLAY_PREFIX, dir=workspace)
    path = os.path.join(directory, OVERLAY_FILENAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(content):
            written += os.write(fd, content[written:])
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return ModelOverlay(path, directory, digest)
