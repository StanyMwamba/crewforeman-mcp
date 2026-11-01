"""Configuration for the DeepSeek MCP server.

Precedence (lowest to highest):

1. built-in defaults
2. JSON config file (``~/.config/deepseek-harness-mcp/config.json`` by default;
   override the location with ``DEEPSEEK_MCP_CONFIG``)
3. environment variables (see ``ENV_MAP``)
4. explicit per-call tool arguments (handled by the callers)

Python 3.9 compatible; standard library only.
"""

import json
import os
import sys
import warnings
from pathlib import Path

SERVER_NAME = "crewforeman-mcp"
SERVER_VERSION = "3.7.0"
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18",
                               "2025-03-26", "2024-11-05")

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
ENTRY_SCRIPT = ROOT_DIR / "deepseek_mcp.py"


def _base_dir():
    """Per-OS private work root for runs, jobs, state and skills-less data.

    The historical ``~/DeepSeek-MCP`` location is kept on macOS and Linux so
    existing installs and run histories keep working. Windows uses
    ``%LOCALAPPDATA%`` because ``~/.config`` is not a Windows convention.
    """
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "DeepSeek-MCP")
    return "~/DeepSeek-MCP"


BASE_DIR = _base_dir()


def _config_path_default():
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(root, "deepseek-harness-mcp", "config.json")
    if sys.platform.startswith("linux"):
        root = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(root, "deepseek-harness-mcp", "config.json")
    return "~/.config/deepseek-harness-mcp/config.json"


DEFAULT_CONFIG_PATH = _config_path_default()

# The app bundle path only exists on macOS; elsewhere the documented CLI is
# expected on PATH. DSH_BIN always overrides this.
_DSH_DEFAULT = ("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
                if sys.platform == "darwin" else "dsh")

# Generic, portable defaults. Every path is overridable.
DEFAULTS = {
    "log_dir": os.path.join(BASE_DIR, "runs"),
    "jobs_dir": os.path.join(BASE_DIR, "jobs"),
    "pid_file": os.path.join(BASE_DIR, "keep_awake.pid"),
    "skills_dir": "",
    "dsh_bin": _DSH_DEFAULT,
    "harness_workspace": os.path.join(BASE_DIR, "harness-workspace"),
    "dsh_sessions_dir": "~/.dsh/sessions",
    "state_dir": os.path.join(BASE_DIR, "state"),
    "zstd_bin": "zstd",
    "model": "deepseek-flash",
    "reasoning": "max",
    "base_url": "https://api.deepseek.com",
    "timeout": 600.0,
    # Kill a Harness process when it emits no progress event for this long.
    # None (or 0) disables the watchdog; max_seconds still applies.
    "stall_seconds": 480.0,
    # Wall-clock cap for one Harness process. It is progress-gated: when the
    # cap is reached but this run produced progress recently, the cap is
    # extended, because killing a demonstrably progressing process wastes the
    # work. A whole-job deadline (job_max_seconds) is the real hard bound.
    # The cap is scaled per phase (reviewer/revision/finisher multipliers).
    "stall_max_seconds": 1800.0,
    "stall_cap_grace_seconds": 120.0,
    "stall_cap_extension_seconds": 300.0,
    # Even a phase that keeps making progress is stopped at this absolute
    # ceiling. None/0 disables only the ceiling, not the progress-gated cap.
    "stall_cap_hard_max_seconds": 7200.0,
    "cap_reviewer_multiplier": 1.5,
    "cap_revision_multiplier": 1.25,
    "cap_finisher_multiplier": 0.5,
    # A changed file under the workspace is progress even when the parent
    # emits no JSON event (long render/build tool calls).
    "workspace_progress": True,
    # Whole-job bounds across builder, live-scaling waves, reviewer, revisions,
    # rescue and retries. None/0 disables a bound. With a cost ceiling set, an
    # unknown cost is over budget: it cannot be proven under the ceiling, so
    # further paid phases stop and the run records that.
    "job_max_seconds": 7200.0,
    "job_max_cost_usd": None,
    "max_parallel": 10,
    "wait_seconds": 45.0,
    "default_subagents": 16,
    "subagent_soft_cap": 32,
    # Hard ceiling for an explicit ``subagents`` value. Automatic sizing is
    # already bounded by subagent_soft_cap; an explicit count is not a licence
    # to launch an unbounded team.
    "max_explicit_subagents": 32,
    # None means auto-detect from the installed Harness (cached in state_dir).
    "max_concurrent_children": None,
    # Count child-session log growth as progress while a workflow wave waits.
    "child_progress": True,
    # Live scaling: score the first builder wave, and add a bounded extra wave
    # with new approaches when the candidates are weak and the job budget
    # allows it. Strong candidates add nothing.
    "live_scaling": True,
    "live_scaling_threshold": 7.0,
    "live_scaling_max_extra_waves": 2,
    "live_scaling_min_seconds": 120.0,
    # Probe headless Chrome once before a visual run and inject the result.
    "render_probe": True,
    "render_probe_timeout": 12.0,
    "chrome_bin": "",
    # Retry one fast, output-less builder failure with a transient error.
    "transient_retry": True,
    "quality": True,
    "quality_max_rounds": 3,
    "independent_review": True,
    "daily_alert_usd": None,
    "daily_limit_usd": None,
    "max_file_chars": 400000,
    # v3.5.0 safety: DSH permission preset for builders and the independent
    # reviewer. ``danger`` is accepted only as an explicit per-call opt-in.
    "sandbox": "workspace-write",
    "reviewer_sandbox": "read-only",
    # macOS Seatbelt wrapper. "auto" uses sandbox-exec when it is present;
    # "off"/false disables it. It is an extra boundary around DSH, not a
    # container and not a network sandbox.
    "os_sandbox": "auto",
    # Documented DSH transport: headless (stable default), sdk (JSON-RPC), or
    # auto (SDK first, then headless fallback; the fallback is recorded).
    "dsh_transport": "headless",
    "dsh_provider": "deepseek-official",
    "dsh_model": "deepseek-flash",
    # Durable phase checkpoints and resume (state_dir/checkpoints).
    "checkpoint": True,
    # Optional OpenAI-compatible fallbacks. Config holds env-var NAMES only;
    # API keys are never stored here. Empty list keeps DeepSeek-only behaviour.
    "providers": [],
    "consult_models": [],
    "provider_fallback": False,
    "task_ttl": 3600,
    # Structured per-run trace + timeline. Export is opt-in and best-effort.
    "trace": True,
    "otlp_endpoint": "",
    "langfuse_host": "",
    "service_name": "deepseek-harness-mcp",
    "browser_evidence": True,
    "add_dirs": [],
    # Data hygiene. 0 disables retention; cleanup always keeps the newest N.
    "retention_days": 0,
    "keep_runs": 50,
    "keep_jobs": 50,
    # Restart helper: configurable client application name ("" means ask the
    # caller; the tool accepts app_name per call).
    "client_app_name": "Claude",
    # Automatic skill building (v3.7.0). Thresholds are read defensively by
    # callers, so a bare Config({}) or SimpleNamespace still works.
    "auto_skills": "auto",
    "auto_skills_min_confidence": 0.55,
    "auto_skills_min_score": 2.0,
    "auto_skills_tie_margin": 0.10,
    "auto_skills_cluster_min": 3,
    "auto_skills_cluster_min_severe": 2,
    "auto_skills_reviewed_min": 2,
    "auto_skills_allow_single_severe": False,
    "auto_skills_cluster_similarity": 0.62,
    "auto_skills_capture_limit": 500,
    "auto_skills_max_keywords": 12,
    "auto_skills_capture_explicit_none": False,
    "auto_skills_max_clusters": 50,
    "auto_skills_cluster_max_age_days": 90,
    "auto_skills_max_members": 20,
    "auto_skills_quality_gate": 8.5,
    "auto_skills_review_rounds": 2,
    "auto_skills_author_seconds": 180.0,
    "auto_skills_review_seconds": 120.0,
    "auto_skills_build_seconds": 360.0,
    "auto_skills_max_drafts_per_run": 1,
    "auto_skills_max_drafts_per_day": 4,
    "auto_skills_max_cost_usd": 1.0,
    "auto_skills_cluster_cooldown_hours": 24.0,
    "auto_skills_promote_after": 5,
    "auto_skills_demote_after": 3,
    "auto_skills_retire_after_days": 30,
    "auto_skills_score_tolerance": 0.3,
    "auto_skills_dedup_threshold": 0.55,
    "auto_skills_max_skills": 64,
    "auto_skills_max_candidates": 8,
    "auto_skills_max_skill_chars": 24000,
    "auto_skills_max_versions": 10,
    "auto_skills_refresh": True,
    "auto_skills_refresh_after": 3,
    "auto_skills_refresh_cooldown_days": 7.0,
    "auto_skills_store_evidence_text": False,
}

# Environment variable -> config key. New names are listed first; legacy v2
# names keep working so existing MCP launch configs do not need editing.
ENV_MAP = {
    "DEEPSEEK_MCP_LOG_DIR": "log_dir",
    "DEEPSEEK_MCP_JOBS_DIR": "jobs_dir",
    "DEEPSEEK_MCP_PID_FILE": "pid_file",
    "DSH_SKILLS_DIR": "skills_dir",
    "DEEPSEEK_SKILLS_DIR": "skills_dir",
    "DSH_BIN": "dsh_bin",
    "DEEPSEEK_DSH_BIN": "dsh_bin",
    "DSH_WORKSPACE": "harness_workspace",
    "DEEPSEEK_WORKSPACE": "harness_workspace",
    "DSH_SESSIONS_DIR": "dsh_sessions_dir",
    "DEEPSEEK_DSH_SESSIONS_DIR": "dsh_sessions_dir",
    "DSH_STATE_DIR": "state_dir",
    "DEEPSEEK_MCP_STATE_DIR": "state_dir",
    "DSH_ZSTD_BIN": "zstd_bin",
    "DEEPSEEK_MODEL": "model",
    "DEEPSEEK_REASONING": "reasoning",
    "DEEPSEEK_BASE_URL": "base_url",
    "DEEPSEEK_TIMEOUT": "timeout",
    "DSH_STALL_SECONDS": "stall_seconds",
    "DEEPSEEK_STALL_SECONDS": "stall_seconds",
    "DSH_STALL_MAX_SECONDS": "stall_max_seconds",
    "DEEPSEEK_STALL_MAX_SECONDS": "stall_max_seconds",
    "DSH_STALL_CAP_GRACE_SECONDS": "stall_cap_grace_seconds",
    "DEEPSEEK_STALL_CAP_GRACE_SECONDS": "stall_cap_grace_seconds",
    "DSH_STALL_CAP_EXTENSION_SECONDS": "stall_cap_extension_seconds",
    "DEEPSEEK_STALL_CAP_EXTENSION_SECONDS": "stall_cap_extension_seconds",
    "DSH_STALL_CAP_HARD_MAX_SECONDS": "stall_cap_hard_max_seconds",
    "DEEPSEEK_STALL_CAP_HARD_MAX_SECONDS": "stall_cap_hard_max_seconds",
    "DSH_CAP_REVIEWER_MULTIPLIER": "cap_reviewer_multiplier",
    "DSH_CAP_REVISION_MULTIPLIER": "cap_revision_multiplier",
    "DSH_CAP_FINISHER_MULTIPLIER": "cap_finisher_multiplier",
    "DSH_WORKSPACE_PROGRESS": "workspace_progress",
    "DEEPSEEK_WORKSPACE_PROGRESS": "workspace_progress",
    "DSH_JOB_MAX_SECONDS": "job_max_seconds",
    "DEEPSEEK_JOB_MAX_SECONDS": "job_max_seconds",
    "DSH_JOB_MAX_COST_USD": "job_max_cost_usd",
    "DEEPSEEK_JOB_MAX_COST_USD": "job_max_cost_usd",
    "DSH_MAX_EXPLICIT_SUBAGENTS": "max_explicit_subagents",
    "DEEPSEEK_MAX_EXPLICIT_SUBAGENTS": "max_explicit_subagents",
    "DSH_LIVE_SCALING": "live_scaling",
    "DEEPSEEK_LIVE_SCALING": "live_scaling",
    "DSH_LIVE_SCALING_THRESHOLD": "live_scaling_threshold",
    "DEEPSEEK_LIVE_SCALING_THRESHOLD": "live_scaling_threshold",
    "DSH_LIVE_SCALING_MAX_EXTRA_WAVES": "live_scaling_max_extra_waves",
    "DEEPSEEK_LIVE_SCALING_MAX_EXTRA_WAVES": "live_scaling_max_extra_waves",
    "DSH_LIVE_SCALING_MIN_SECONDS": "live_scaling_min_seconds",
    "DEEPSEEK_LIVE_SCALING_MIN_SECONDS": "live_scaling_min_seconds",
    "DEEPSEEK_MAX_PARALLEL": "max_parallel",
    "DEEPSEEK_WAIT_SECONDS": "wait_seconds",
    "DSH_SUBAGENTS": "default_subagents",
    "DEEPSEEK_DEFAULT_SUBAGENTS": "default_subagents",
    "DSH_SUBAGENT_SOFT_CAP": "subagent_soft_cap",
    "DEEPSEEK_SUBAGENT_SOFT_CAP": "subagent_soft_cap",
    "DSH_MAX_CONCURRENT_CHILDREN": "max_concurrent_children",
    "DEEPSEEK_MAX_CONCURRENT_CHILDREN": "max_concurrent_children",
    "DSH_CHILD_PROGRESS": "child_progress",
    "DEEPSEEK_CHILD_PROGRESS": "child_progress",
    "DSH_RENDER_PROBE": "render_probe",
    "DEEPSEEK_RENDER_PROBE": "render_probe",
    "DSH_RENDER_PROBE_TIMEOUT": "render_probe_timeout",
    "DEEPSEEK_RENDER_PROBE_TIMEOUT": "render_probe_timeout",
    "DSH_CHROME_BIN": "chrome_bin",
    "DEEPSEEK_CHROME_BIN": "chrome_bin",
    "DSH_TRANSIENT_RETRY": "transient_retry",
    "DEEPSEEK_TRANSIENT_RETRY": "transient_retry",
    "DSH_QUALITY": "quality",
    "DEEPSEEK_QUALITY": "quality",
    "DSH_QUALITY_ROUNDS": "quality_max_rounds",
    "DEEPSEEK_QUALITY_ROUNDS": "quality_max_rounds",
    "DSH_INDEPENDENT_REVIEW": "independent_review",
    "DEEPSEEK_INDEPENDENT_REVIEW": "independent_review",
    "DSH_DAILY_ALERT_USD": "daily_alert_usd",
    "DEEPSEEK_DAILY_ALERT_USD": "daily_alert_usd",
    "DSH_DAILY_LIMIT_USD": "daily_limit_usd",
    "DEEPSEEK_DAILY_LIMIT_USD": "daily_limit_usd",
    "DEEPSEEK_MAX_FILE_CHARS": "max_file_chars",
    "DSH_SANDBOX": "sandbox",
    "DEEPSEEK_SANDBOX": "sandbox",
    "DSH_REVIEWER_SANDBOX": "reviewer_sandbox",
    "DEEPSEEK_REVIEWER_SANDBOX": "reviewer_sandbox",
    "DSH_OS_SANDBOX": "os_sandbox",
    "DEEPSEEK_OS_SANDBOX": "os_sandbox",
    "DSH_TRANSPORT": "dsh_transport",
    "DEEPSEEK_DSH_TRANSPORT": "dsh_transport",
    "DSH_PROVIDER": "dsh_provider",
    "DSH_MODEL": "dsh_model",
    "DSH_CHECKPOINT": "checkpoint",
    "DEEPSEEK_CHECKPOINT": "checkpoint",
    "DEEPSEEK_PROVIDERS": "providers",
    "DEEPSEEK_CONSULT_MODELS": "consult_models",
    "DEEPSEEK_PROVIDER_FALLBACK": "provider_fallback",
    "DSH_TASK_TTL": "task_ttl",
    "DEEPSEEK_TASK_TTL": "task_ttl",
    "DSH_TRACE": "trace",
    "DEEPSEEK_TRACE": "trace",
    "DSH_OTLP_ENDPOINT": "otlp_endpoint",
    "DEEPSEEK_OTLP_ENDPOINT": "otlp_endpoint",
    "DSH_LANGFUSE_HOST": "langfuse_host",
    "DEEPSEEK_LANGFUSE_HOST": "langfuse_host",
    "DSH_SERVICE_NAME": "service_name",
    "DEEPSEEK_SERVICE_NAME": "service_name",
    "DSH_BROWSER_EVIDENCE": "browser_evidence",
    "DEEPSEEK_BROWSER_EVIDENCE": "browser_evidence",
    "DSH_ADD_DIRS": "add_dirs",
    "DEEPSEEK_ADD_DIRS": "add_dirs",
    "DSH_RETENTION_DAYS": "retention_days",
    "DEEPSEEK_RETENTION_DAYS": "retention_days",
    "DSH_KEEP_RUNS": "keep_runs",
    "DEEPSEEK_KEEP_RUNS": "keep_runs",
    "DSH_KEEP_JOBS": "keep_jobs",
    "DEEPSEEK_KEEP_JOBS": "keep_jobs",
    "DSH_CLIENT_APP_NAME": "client_app_name",
    "DEEPSEEK_CLIENT_APP_NAME": "client_app_name",
    # Newer DSH_* first; only the aliases frozen in the v3.7.0 contract table
    # exist (no invented per-threshold legacy names).
    "DSH_AUTO_SKILLS": "auto_skills",
    "DEEPSEEK_AUTO_SKILLS": "auto_skills",
    "DSH_AUTO_SKILLS_MIN_CONFIDENCE": "auto_skills_min_confidence",
    "DSH_AUTO_SKILLS_CLUSTER_MIN": "auto_skills_cluster_min",
    "DSH_AUTO_SKILLS_SEVERE_MIN": "auto_skills_cluster_min_severe",
    "DSH_AUTO_SKILLS_GATE": "auto_skills_quality_gate",
    "DSH_AUTO_SKILLS_REVIEW_ROUNDS": "auto_skills_review_rounds",
    "DSH_AUTO_SKILLS_PROMOTE_AFTER": "auto_skills_promote_after",
}

BOOL_KEYS = {"quality", "independent_review", "child_progress",
             "render_probe", "transient_retry", "checkpoint",
             "provider_fallback", "trace", "browser_evidence",
             "workspace_progress", "live_scaling",
             "auto_skills_allow_single_severe", "auto_skills_refresh",
             "auto_skills_store_evidence_text",
             "auto_skills_capture_explicit_none"}
INT_KEYS = {"max_parallel", "default_subagents", "subagent_soft_cap",
            "quality_max_rounds", "max_file_chars", "max_concurrent_children",
            "task_ttl", "max_explicit_subagents",
            "live_scaling_max_extra_waves", "retention_days", "keep_runs",
            "keep_jobs",
            "auto_skills_cluster_min", "auto_skills_cluster_min_severe",
            "auto_skills_reviewed_min", "auto_skills_capture_limit",
            "auto_skills_max_keywords", "auto_skills_max_clusters",
            "auto_skills_cluster_max_age_days", "auto_skills_max_members",
            "auto_skills_review_rounds", "auto_skills_max_drafts_per_run",
            "auto_skills_max_drafts_per_day", "auto_skills_promote_after",
            "auto_skills_demote_after", "auto_skills_retire_after_days",
            "auto_skills_max_skills", "auto_skills_max_candidates",
            "auto_skills_max_skill_chars", "auto_skills_max_versions",
            "auto_skills_refresh_after"}
ENUM_KEYS = {"auto_skills": ("auto", "propose", "off")}
LIST_KEYS = {"providers", "consult_models", "add_dirs"}
FLOAT_KEYS = {"timeout", "wait_seconds", "stall_seconds", "stall_max_seconds",
              "daily_alert_usd", "daily_limit_usd", "render_probe_timeout",
              "stall_cap_grace_seconds", "stall_cap_extension_seconds",
              "stall_cap_hard_max_seconds",
              "cap_reviewer_multiplier", "cap_revision_multiplier",
              "cap_finisher_multiplier", "job_max_seconds", "job_max_cost_usd",
              "live_scaling_threshold", "live_scaling_min_seconds",
              "auto_skills_min_confidence", "auto_skills_min_score",
              "auto_skills_tie_margin", "auto_skills_cluster_similarity",
              "auto_skills_quality_gate", "auto_skills_score_tolerance",
              "auto_skills_dedup_threshold", "auto_skills_author_seconds",
              "auto_skills_review_seconds", "auto_skills_build_seconds",
              "auto_skills_max_cost_usd",
              "auto_skills_cluster_cooldown_hours",
              "auto_skills_refresh_cooldown_days"}
PATH_KEYS = {"log_dir", "jobs_dir", "pid_file", "skills_dir", "harness_workspace",
             "dsh_sessions_dir", "state_dir", "chrome_bin"}


class Config:
    """Small attribute/dict hybrid so values are easy to pass around and test."""

    def __init__(self, values=None):
        self._values = dict(DEFAULTS)
        if values:
            self._values.update(values)

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, name):
        return self._values[name]

    def get(self, name, default=None):
        return self._values.get(name, default)

    def as_dict(self):
        return dict(self._values)

    def __repr__(self):
        return "Config(%r)" % (self._values,)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _coerce(key, value):
    if value is None:
        return None
    if key in ENUM_KEYS:
        text = str(value).strip().lower()
        allowed = ENUM_KEYS[key]
        if text not in allowed:
            raise ValueError("%s must be one of: %s"
                             % (key, ", ".join(allowed)))
        return text
    if isinstance(value, str) and not value.strip() and key in (
            FLOAT_KEYS | INT_KEYS):
        return None
    if key in BOOL_KEYS:
        return _as_bool(value)
    if key in INT_KEYS:
        number = int(value)
        if key == "auto_skills_max_skill_chars" and number < 4000:
            warnings.warn("auto_skills_max_skill_chars clamped to 4000",
                          RuntimeWarning)
            return 4000
        return number
    if key in FLOAT_KEYS:
        number = float(value)
        if key == "auto_skills_quality_gate" and not 0.0 <= number <= 10.0:
            raise ValueError("auto_skills_quality_gate must be between 0 and 10")
        if key == "auto_skills_min_confidence" and not 0.0 <= number <= 1.0:
            warnings.warn("auto_skills_min_confidence clamped to [0, 1]",
                          RuntimeWarning)
            return min(1.0, max(0.0, number))
        return number
    if key in LIST_KEYS:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                parsed = json.loads(text)
                if not isinstance(parsed, list):
                    raise ValueError("%s must be a JSON list" % key)
                return parsed
            return [item.strip() for item in text.split(",") if item.strip()]
        raise ValueError("%s must be a list" % key)
    if key in PATH_KEYS:
        # Strip outer whitespace first: a leading space turns "/abs/path" into
        # a relative path, and a trailing space can make a cloud-sync folder
        # unwritable. Spaces *inside* a component ("My Drive") are preserved.
        text = str(value).strip()
        if text == "":
            return ""
        return str(Path(os.path.expanduser(text)))
    return value


def default_config_path(env=None):
    env = os.environ if env is None else env
    raw = env.get("DEEPSEEK_MCP_CONFIG") or DEFAULT_CONFIG_PATH
    return Path(os.path.expanduser(raw))


def secure_permissions(path, is_dir=False):
    """Best-effort 0700/0600 on POSIX; documented no-op on Windows."""
    if os.name == "nt":
        return False
    try:
        os.chmod(str(path), 0o700 if is_dir else 0o600)
        return True
    except OSError:
        return False


def _expand_config_path(path):
    """Expand ``~`` in an explicit config path (relative paths stay relative)."""
    return Path(os.path.expanduser(str(path)))


def secure_config_file(path=None, env=None):
    """Tighten an existing config file's mode; never create or fail."""
    path = (_expand_config_path(path) if path is not None
            else default_config_path(env))
    if path.is_file():
        secure_permissions(path, is_dir=False)
    return path


def read_config_file(path=None, env=None):
    """Return the JSON object from the config file (empty dict if missing)."""
    path = (_expand_config_path(path) if path is not None
            else default_config_path(env))
    secure_config_file(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeError("Could not read config file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise RuntimeError("Config file %s must contain a JSON object" % path)
    return data


def load_config(config_path=None, env=None, include_file=True):
    """Build a Config using defaults < config file < environment."""
    env = dict(os.environ) if env is None else env
    values = {key: _coerce(key, value) for key, value in DEFAULTS.items()}

    if include_file:
        path = (_expand_config_path(config_path) if config_path is not None
                else default_config_path(env))
        for key, raw in read_config_file(path, env).items():
            if key in DEFAULTS:
                values[key] = _coerce(key, raw)

    # Iterate oldest alias first so the newer DSH_* name wins if both are set.
    for env_name, key in reversed(list(ENV_MAP.items())):
        if env_name in env and env[env_name] != "":
            values[key] = _coerce(key, env[env_name])

    # A config-file path is useful for child job processes.
    config_file = (_expand_config_path(config_path) if config_path is not None
                   else default_config_path(env))
    values["_config_path"] = str(config_file.expanduser().resolve())
    return Config(values)
