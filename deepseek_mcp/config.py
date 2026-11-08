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
SERVER_VERSION = "3.10.0"
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
    "builder_practices": True,
    # P3.4a: optional root for connector-owned mutable state. Empty keeps
    # the historical per-key defaults; when set it derives state_dir,
    # log_dir, jobs_dir and pid_file (four roots, see
    # DERIVED_STATE_ROOT_KEYS). The Harness session directory is not derived:
    # dsh keys its home off DSH_HOME, which workers do not receive, so
    # dsh_sessions_dir stays ~/.dsh/sessions unless explicitly configured
    # (DSH_SESSIONS_DIR / DEEPSEEK_DSH_SESSIONS_DIR or the config file).
    "state_root": "",
    "log_dir": os.path.join(BASE_DIR, "runs"),
    "jobs_dir": os.path.join(BASE_DIR, "jobs"),
    "pid_file": os.path.join(BASE_DIR, "keep_awake.pid"),
    "skills_dir": "",
    "dsh_bin": _DSH_DEFAULT,
    # Optional backup reviewer binary and its vendor/model label. Empty
    # keeps the historical single-reviewer behaviour.
    "backup_dsh_bin": "",
    "backup_reviewer_vendor": "",
    "backup_reviewer_model": "",
    "harness_workspace": os.path.join(BASE_DIR, "harness-workspace"),
    "dsh_sessions_dir": "~/.dsh/sessions",
    # Optional per-model reviewer rates in USD per million tokens:
    # {"model": {"input": 0.0, "cache_read": 0.0, "output": 0.0}}. Empty means
    # tokens are recorded but reviewer cost stays unknown.
    "reviewer_prices": {},
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
    # P3-1: bounded, fair admission of Harness child jobs. 0 disables the
    # connector-side cap (the Harness child limit still applies).
    "max_harness_jobs": 4,
    "reserve_resume_slot": True,
    "admission_heartbeat_seconds": 30.0,
    "admission_lease_seconds": 90.0,
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
    # P3-2: "observe" records the would-stop convergence decision but keeps the
    # normal loop; "auto" stops the revision loop and asks one question.
    "quality_convergence": "observe",
    "independent_review": True,
    # R1: before review the connector runs the full test suite on the reviewed
    # tree and hands the reviewer signed evidence. "auto" detects a tests/
    # directory; "" disables; a string/list overrides the command.
    "review_test_suite": "auto",
    "review_test_command": "",
    "review_test_timeout_seconds": 300.0,
    # P3-2: a tiny reviewer pre-flight (same reviewer path, short timeout)
    # must answer READY before the connector starts the paid review.
    "review_preflight": True,
    "review_preflight_timeout": 45.0,
    "review_context_max_chars": 60000,
    "daily_alert_usd": None,
    "daily_limit_usd": None,
    "max_file_chars": 400000,
    # v3.5.0 safety: DSH permission preset for builders and the independent
    # reviewer. ``danger`` is accepted only as an explicit per-call opt-in.
    "sandbox": "workspace-write",
    "reviewer_sandbox": "read-only",
    # P2-1: the reviewer is read-only unless the operator explicitly allows a
    # writing reviewer; such a run records reviewer_override and gets the same
    # workspace-diff guard as a builder.
    "allow_writing_reviewer": False,
    # Optional positive allow-list. When set, a new run's workspace (and a
    # resumed run's recorded root) must be inside one of these roots.
    "allowed_workspace_roots": [],
    "authority_ttl_seconds": 604800,
    # Opt-in 3.8.0 compatibility: when true, a fresh run's first paid phase may
    # start above an unusually small ceiling and records admission_overrun.
    # Default false so every admission enforces committed+reserved+estimate.
    "allow_first_phase_overrun": False,
    # P2-2: retry/health. A quarantined component blocks new automatic phases
    # until deepseek_maintenance action=clear_health.
    "health_gate": True,
    "retry_backoff_base_seconds": 1.0,
    "retry_backoff_cap_seconds": 60.0,
    # P2-3: reservations left by a dead worker are reclaimed after this lease.
    "cost_reservation_ttl_seconds": 900,
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
    # P1-1: return an existing active run for an identical request fingerprint
    # instead of paying for a duplicate pipeline. Operators can disable it;
    # an explicit idempotency_key always selects its own work item.
    "dedupe_active": True,
    # Optional OpenAI-compatible fallbacks. Config holds env-var NAMES only;
    # API keys are never stored here. Empty list keeps DeepSeek-only behaviour.
    "providers": [],
    "consult_models": [],
    "provider_fallback": False,
    "task_ttl": 3600,
    # P1-3: "job" keeps an MCP task working until the detached job reaches a
    # terminal journal state; "request" restores the old complete-on-return
    # behaviour for a client migration window.
    "task_completion": "job",
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
    # P3.2 detached restart helper. Empty restart_status_path means
    # <state_dir>/restart-status.json.
    "restart_status_path": "",
    "restart_helper_attempts": 3,
    "restart_helper_retry_seconds": 2.0,
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
    "DSH_BUILDER_PRACTICES": "builder_practices",
    "DSH_MAX_HARNESS_JOBS": "max_harness_jobs",
    "DEEPSEEK_MAX_HARNESS_JOBS": "max_harness_jobs",
    "DSH_RESERVE_RESUME_SLOT": "reserve_resume_slot",
    "DEEPSEEK_RESERVE_RESUME_SLOT": "reserve_resume_slot",
    "DEEPSEEK_MCP_LOG_DIR": "log_dir",
    "DEEPSEEK_MCP_JOBS_DIR": "jobs_dir",
    "DEEPSEEK_MCP_PID_FILE": "pid_file",
    "DSH_SKILLS_DIR": "skills_dir",
    "DEEPSEEK_SKILLS_DIR": "skills_dir",
    "DSH_BIN": "dsh_bin",
    "DEEPSEEK_DSH_BIN": "dsh_bin",
    "DSH_BACKUP_BIN": "backup_dsh_bin",
    "DEEPSEEK_BACKUP_BIN": "backup_dsh_bin",
    "DSH_BACKUP_REVIEWER_VENDOR": "backup_reviewer_vendor",
    "DEEPSEEK_BACKUP_REVIEWER_VENDOR": "backup_reviewer_vendor",
    "DSH_BACKUP_REVIEWER_MODEL": "backup_reviewer_model",
    "DEEPSEEK_BACKUP_REVIEWER_MODEL": "backup_reviewer_model",
    "DSH_WORKSPACE": "harness_workspace",
    "DEEPSEEK_WORKSPACE": "harness_workspace",
    "DSH_SESSIONS_DIR": "dsh_sessions_dir",
    "DEEPSEEK_DSH_SESSIONS_DIR": "dsh_sessions_dir",
    "CREWFOREMAN_STATE_DIR": "state_root",
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
    "DSH_QUALITY_CONVERGENCE": "quality_convergence",
    "DEEPSEEK_QUALITY_CONVERGENCE": "quality_convergence",
    "DSH_REVIEW_TEST_SUITE": "review_test_suite",
    "DEEPSEEK_REVIEW_TEST_SUITE": "review_test_suite",
    "DSH_REVIEW_TEST_COMMAND": "review_test_command",
    "DEEPSEEK_REVIEW_TEST_COMMAND": "review_test_command",
    "DSH_REVIEW_PREFLIGHT": "review_preflight",
    "DSH_REVIEW_PREFLIGHT_TIMEOUT": "review_preflight_timeout",
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
    "DSH_ALLOW_WRITING_REVIEWER": "allow_writing_reviewer",
    "DEEPSEEK_ALLOW_WRITING_REVIEWER": "allow_writing_reviewer",
    "DSH_ALLOWED_WORKSPACE_ROOTS": "allowed_workspace_roots",
    "DEEPSEEK_ALLOWED_WORKSPACE_ROOTS": "allowed_workspace_roots",
    "DSH_AUTHORITY_TTL_SECONDS": "authority_ttl_seconds",
    "DSH_ALLOW_FIRST_PHASE_OVERRUN": "allow_first_phase_overrun",
    "DEEPSEEK_ALLOW_FIRST_PHASE_OVERRUN": "allow_first_phase_overrun",
    "DEEPSEEK_AUTHORITY_TTL_SECONDS": "authority_ttl_seconds",
    "DSH_HEALTH_GATE": "health_gate",
    "DEEPSEEK_HEALTH_GATE": "health_gate",
    "DSH_RETRY_BACKOFF_BASE_SECONDS": "retry_backoff_base_seconds",
    "DEEPSEEK_RETRY_BACKOFF_BASE_SECONDS": "retry_backoff_base_seconds",
    "DSH_RETRY_BACKOFF_CAP_SECONDS": "retry_backoff_cap_seconds",
    "DEEPSEEK_RETRY_BACKOFF_CAP_SECONDS": "retry_backoff_cap_seconds",
    "DSH_COST_RESERVATION_TTL_SECONDS": "cost_reservation_ttl_seconds",
    "DEEPSEEK_COST_RESERVATION_TTL_SECONDS": "cost_reservation_ttl_seconds",
    "DSH_OS_SANDBOX": "os_sandbox",
    "DEEPSEEK_OS_SANDBOX": "os_sandbox",
    "DSH_TRANSPORT": "dsh_transport",
    "DEEPSEEK_DSH_TRANSPORT": "dsh_transport",
    "DSH_PROVIDER": "dsh_provider",
    "DSH_MODEL": "dsh_model",
    "DSH_CHECKPOINT": "checkpoint",
    "DEEPSEEK_CHECKPOINT": "checkpoint",
    "DSH_DEDUPE_ACTIVE": "dedupe_active",
    "DEEPSEEK_DEDUPE_ACTIVE": "dedupe_active",
    "DEEPSEEK_PROVIDERS": "providers",
    "DEEPSEEK_CONSULT_MODELS": "consult_models",
    "DEEPSEEK_PROVIDER_FALLBACK": "provider_fallback",
    "DSH_TASK_TTL": "task_ttl",
    "DEEPSEEK_TASK_TTL": "task_ttl",
    "DSH_TASK_COMPLETION": "task_completion",
    "DEEPSEEK_TASK_COMPLETION": "task_completion",
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
    "DSH_RESTART_STATUS_PATH": "restart_status_path",
    "DEEPSEEK_MCP_RESTART_STATUS_PATH": "restart_status_path",
    "DSH_RESTART_HELPER_ATTEMPTS": "restart_helper_attempts",
    "DEEPSEEK_MCP_RESTART_HELPER_ATTEMPTS": "restart_helper_attempts",
    "DSH_RESTART_HELPER_RETRY_SECONDS": "restart_helper_retry_seconds",
    "DEEPSEEK_MCP_RESTART_HELPER_RETRY_SECONDS": "restart_helper_retry_seconds",
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

BOOL_KEYS = {"builder_practices", "quality", "independent_review", "child_progress",
             "render_probe", "transient_retry", "checkpoint", "dedupe_active",
             "provider_fallback", "trace", "browser_evidence",
             "workspace_progress", "live_scaling",
             "auto_skills_allow_single_severe", "auto_skills_refresh",
             "auto_skills_store_evidence_text",
             "auto_skills_capture_explicit_none", "allow_writing_reviewer",
             "health_gate", "allow_first_phase_overrun",
             "reserve_resume_slot", "review_preflight"}
INT_KEYS = {"max_parallel", "max_harness_jobs", "default_subagents", "subagent_soft_cap",
             "restart_helper_attempts",
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
            "auto_skills_refresh_after", "authority_ttl_seconds",
            "cost_reservation_ttl_seconds"}
ENUM_KEYS = {"auto_skills": ("auto", "propose", "off"),
             "task_completion": ("job", "request"),
             "quality_convergence": ("observe", "auto")}
LIST_KEYS = {"providers", "consult_models", "add_dirs",
             "allowed_workspace_roots"}
FLOAT_KEYS = {"timeout", "wait_seconds", "stall_seconds", "stall_max_seconds",
              "admission_heartbeat_seconds", "admission_lease_seconds",
              "review_test_timeout_seconds",
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
              "auto_skills_refresh_cooldown_days",
              "retry_backoff_base_seconds", "retry_backoff_cap_seconds",
              "review_context_max_chars", "review_preflight_timeout",
              "restart_helper_retry_seconds"}
PATH_KEYS = {"state_root", "log_dir", "jobs_dir", "pid_file", "skills_dir",
             "harness_workspace", "backup_dsh_bin",
             "dsh_sessions_dir", "state_dir", "chrome_bin", "restart_status_path"}

# P3.4a: the four connector-owned keys derived from a non-empty state_root
# (env CREWFOREMAN_STATE_DIR or config file state_root). This sits below
# explicit config-file values and explicit environment aliases in the
# precedence order. dsh_sessions_dir is deliberately absent: Harness session
# logs live under dsh's own home (~/.dsh/sessions, keyed off DSH_HOME), which
# worker children do not inherit, so deriving a read path here would silently
# blind token/cost accounting and child activity. Set DSH_SESSIONS_DIR
# explicitly to co-locate.
DERIVED_STATE_ROOT_KEYS = {
    "state_dir": "state",
    "log_dir": "runs",
    "jobs_dir": "jobs",
    "pid_file": "keep_awake.pid",
}


class Config:
    """Small attribute/dict hybrid so values are easy to pass around and test."""

    def __init__(self, values=None, explicit=None):
        self._values = dict(DEFAULTS)
        if values:
            self._values.update(values)
        # Marker kept outside _values/as_dict. A bare mapping passed by a
        # caller (tests, embedded use) counts as explicit; load_config passes
        # only the keys that actually came from the config file or env.
        if explicit is None:
            explicit = set(values) if values else set()
        self._explicit = set(explicit)

    def is_explicit(self, name):
        """True when ``name`` was set by a config file, env or direct mapping."""
        return name in self._explicit

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


def _state_root_value(env, file_values):
    """Return the effective state root ("" when neither source sets one).

    ``CREWFOREMAN_STATE_DIR`` (environment) beats the config-file key
    ``state_root``. Blank values count as unset so the next source can apply.
    """
    for raw in (env.get("CREWFOREMAN_STATE_DIR"), file_values.get("state_root")):
        if raw is None:
            continue
        if isinstance(raw, str) and not raw.strip():
            continue
        return _coerce("state_root", raw)
    return ""


def load_config(config_path=None, env=None, include_file=True):
    """Build a Config: defaults < derived state root < file < environment."""
    env = dict(os.environ) if env is None else env
    values = {key: _coerce(key, value) for key, value in DEFAULTS.items()}

    file_values = {}
    if include_file:
        path = (_expand_config_path(config_path) if config_path is not None
                else default_config_path(env))
        for key, raw in read_config_file(path, env).items():
            if key in DEFAULTS:
                file_values[key] = _coerce(key, raw)

    # P3.4a: a non-empty state root relocates the four connector-owned roots
    # (state/log/jobs/pid only; Harness sessions are never derived). It sits
    # below config-file values and explicit environment aliases, which are
    # applied afterwards and therefore win key by key.
    explicit = set()
    root = _state_root_value(env, file_values)
    if root:
        for key, leaf in DERIVED_STATE_ROOT_KEYS.items():
            values[key] = os.path.join(root, leaf)
            explicit.add(key)

    for key, value in file_values.items():
        values[key] = value
        explicit.add(key)

    # Iterate oldest alias first so the newer DSH_* name wins if both are set.
    for env_name, key in reversed(list(ENV_MAP.items())):
        if env_name in env and env[env_name] != "":
            values[key] = _coerce(key, env[env_name])
            explicit.add(key)

    # A config-file path is useful for child job processes.
    config_file = (_expand_config_path(config_path) if config_path is not None
                   else default_config_path(env))
    values["_config_path"] = str(config_file.expanduser().resolve())
    return Config(values, explicit=explicit)
