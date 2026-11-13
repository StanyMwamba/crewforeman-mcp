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
SERVER_VERSION = "3.14.3"
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
    # Commit identity for every connector-created commit/rebase. The
    # pre-push privacy gate allowlists this email (plus noreply@example.com
    # and any email-shaped git_identity_exceptions entry).
    "git_identity": {"name": "crewforeman-mcp", "email": "noreply@example.com"},
    # Remote names/URL substrings that keep their own repo-local identity,
    # e.g. "owner/repo" (example only; empty means no exceptions).
    "git_identity_exceptions": [],
    "builder_practices": True,
    # depends_on policy: "success" (default) waits for a successful
    # dependency; "finished" starts after any terminal state (error included).
    "depends_on_policy": "success",
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
    # Optional reviewer-only override in USD per million tokens:
    # {"model": {"input": 0.0, "cache_read": 0.0, "output": 0.0}}. Empty falls
    # back to model_prices and then the built-in DeepSeek rates.
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
    # Deterministic loop detector (E1). Conservative defaults; actions never
    # call a model and token savings are estimates, not billed amounts.
    "loop_guard_enabled": True,
    "loop_guard_confirm": False,
    "loop_guard_repeat_command": 3,
    "loop_guard_edit_pingpong": 3,
    "loop_guard_nochange_calls": 40,
    "loop_guard_same_test_failure": 3,
    "loop_guard_tokens_per_call": 1200,
    "loop_guard_max_actions": 3,
    "loop_guard_poll_seconds": 5.0,
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
    # "off"/false disables it; "require" refuses to start a builder when no
    # enforced boundary is available. It is a file-write boundary around DSH,
    # not a container and not a network sandbox.
    "os_sandbox": "auto",
    # Linux: auto = Landlock ABI >= linux_min_abi, then bwrap, then none.
    "linux_sandbox": "auto",
    "linux_min_abi": 3,
    # Windows: auto = write-restricted token (W1); appcontainer is opt-in.
    "windows_sandbox": "auto",
    # Absolute extra write roots; canonicalized, deduped, capped at 32.
    "sandbox_extra_writes": [],
    # Probe cache lifetime; 0 re-probes every policy.
    "sandbox_probe_ttl": 300.0,
    # auto runs a deny canary once per policy hash; off is recorded.
    "sandbox_canary": "auto",
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
    # 3.12.1: provider keys may also live in the Harness credentials file.
    "credentials_file": "~/.dsh/.credentials.yaml",
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
    # v3.11.0 F1: deterministic gate (tests + lint + secret scan) before the
    # paid reviewer. gate_tests/gate_lint accept "auto", "off" or a command
    # string ("off" is checked by the caller, so they stay plain strings).
    "gate_enabled": True,
    "gate_tests": "auto",
    "gate_test_command": "",
    "gate_lint": "auto",
    "gate_lint_command": "",
    "gate_secret_scan": True,
    "gate_secret_patterns": [],
    "gate_secret_scan_budget_seconds": 120.0,
    "gate_timeout_seconds": 300.0,
    "gate_max_revisions": 2,
    "gate_max_output_chars": 4000,
    # v3.11.0 F2: PLAN.json contract check.
    "plan_contract": "warn",
    "plan_file": "PLAN.json",
    "plan_max_files": 200,
    "plan_max_risks": 20,
    # v3.11.0 F3: bounded lesson store for repeated failure notes.
    "failure_notes": "on",
    "failure_notes_max": 3,
    "failure_notes_half_life_days": 30.0,
    "failure_notes_min_occurrences": 2,
    "failure_notes_max_store": 200,
    "failure_notes_store": "",
    # v3.11.0 F4: structured pipeline of independent jobs and patch queue.
    "pipeline_enabled": True,
    "pipeline_max_extra_jobs": 1,
    "pipeline_require_disjoint": True,
    "pipeline_isolation": "worktree",
    "pipeline_worktree_root": "",
    "pipeline_patch_dir": "",
    "pipeline_install": "auto",
    "pipeline_min_remaining_usd": 1.0,
    "pipeline_max_pending_patches": 8,
    "pipeline_queue_scan": "head",
    "pipeline_patch_retention_days": 3,
    # v3.14.2 crosscheck/audit/automatic-parallel features.
    "default_mode": "",
    "crosscheck_threshold": 10.0,
    "crosscheck_crews": 2,
    "crosscheck_crew_size": 4,
    "crosscheck_max_rounds": 3,
    "audit_review": None,
    "audit_sample_rate": 1.0,
    "audit_spot_requirements": 3,
    "audit_spot_fixes": 2,
    "audit_forced_jobs": 5,
    "audit_seed": 0,
    "audit_risk_globs": [],
    "risk_paths": [],
    "auto_parallel": True,
    "auto_parallel_min_confidence": 0.85,
    # v3.12.0 S1/S2: builder target, mixed-model plan and model-aware records.
    # Empty string means inherit (builder_* -> dsh_*).
    "builder_model": "",
    "builder_provider": "",
    "builder_allow_unlisted_models": False,
    "builder_model_max_slices": 8,
    "slice_models": {},
    "model_candidates_max": 3,
    "model_plan_enforcement": "warn",
    # Generic per-model rates for builders and reviewer fallback:
    # {"provider/model": {"input": 0.0, "cache_read": 0.0, "output": 0.0}}.
    "model_prices": {},
    "model_usage_max_models": 8,
    "history_model_min_runs": 2,
    "history_model_max": 6,
    "failure_notes_models": "on",
    "failure_notes_max_models": 6,
    "failure_notes_model_min_runs": 2,
    # v3.12.0 backup builder. Empty model disables it; 0 disables its ceiling.
    "backup_builder_model": "",
    "backup_builder_provider": "",
    "backup_builder_max_cost_usd": 1.0,
    "backup_builder_min_attempts": 2,
    # v3.12.0 reviewer selection and gate scope.
    "reviewers": [],
    "reviewer_vendor": "",
    "reviewer_provider": "",
    "reviewer_model": "",
    "reviewer_max_preflights": 8,
    "reviewer_same_vendor_policy": "ask",
    "gate_no_product_mode": "skip",
    "gate_noproduct_globs": [
        "logs/**", "**/logs/**", "**/*.log", "**/*.diff", "**/*.patch",
        "**/*.rej", "**/*.orig", "repo-ref/**", "**/repo-ref/**",
        ".dsh-tmp-*", ".dsh-model-overlay-*", ".pytest_cache/**", "**/__pycache__/**", ".mypy_cache/**",
        ".ruff_cache/**", "htmlcov/**", "coverage/**", ".coverage",
        "**/.uv-python/**", ".uv-python/**", "**/.venv/**", ".venv/**",
        "**/venv/**", "venv/**", "**/node_modules/**", "node_modules/**",
        "**/.tox/**", ".tox/**", "**/site-packages/**", "site-packages/**",
        "**/.eggs/**", ".eggs/**", "**/.nox/**", ".nox/**",
        "**/.pixi/**", ".pixi/**",
        "PLAN.json", "MODEL_AUDIT.json", "REVIEW*.md",
        "QUALITY_REPORT*.md", "WASTE_REPORT.md",
        "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.svg",
        "**/*.mp4", "**/*.webm", "**/*.pdf", "**/*.zip",
        "*.md", "docs/**", "LICENSE*",
    ],
    "gate_secret_skip_globs": [
        "**/*.log", "**/*.diff", "**/*.patch", "**/*.rej", "**/*.orig",
        "repo-ref/**", "**/repo-ref/**", ".dsh-tmp-*", ".dsh-model-overlay-*",
        ".pytest_cache/**",
        "**/__pycache__/**", ".mypy_cache/**", ".ruff_cache/**",
        "htmlcov/**", "coverage/**", ".coverage",
        "**/.uv-python/**", ".uv-python/**", "**/.venv/**", ".venv/**",
        "**/venv/**", "venv/**", "**/node_modules/**", "node_modules/**",
        "**/.tox/**", ".tox/**", "**/site-packages/**", "site-packages/**",
        "**/.eggs/**", ".eggs/**", "**/.nox/**", ".nox/**",
        "**/.pixi/**", ".pixi/**",
    ],
    "gate_split_fixture_paths": [
        "tests/test_v3110_gate.py", "tests/test_v3110_integration.py",
        "tests/test_v3110_vendor.py", "tests/test_p3_vendor_error.py",
        "tests/test_v340_render.py",
    ],
    "plan_optional_max_files": 1,
}

# Environment variable -> config key. New names are listed first; legacy v2
# names keep working so existing MCP launch configs do not need editing.
ENV_MAP = {
    "DSH_BUILDER_PRACTICES": "builder_practices",
    "DSH_BUILDER_MODEL": "builder_model",
    "DSH_BUILDER_PROVIDER": "builder_provider",
    "DSH_BACKUP_BUILDER_MODEL": "backup_builder_model",
    "DSH_BACKUP_BUILDER_PROVIDER": "backup_builder_provider",
    "DSH_BACKUP_BUILDER_MAX_COST_USD": "backup_builder_max_cost_usd",
    "DSH_REVIEWER_MAX_PREFLIGHTS": "reviewer_max_preflights",
    "DSH_REVIEWER_VENDOR": "reviewer_vendor",
    "DSH_REVIEWER_MODEL": "reviewer_model",
    "DSH_REVIEWER_PROVIDER": "reviewer_provider",
    "DSH_GATE_NO_PRODUCT_MODE": "gate_no_product_mode",
    "DSH_PLAN_OPTIONAL_MAX_FILES": "plan_optional_max_files",
    "DSH_MAX_HARNESS_JOBS": "max_harness_jobs",
    "DEEPSEEK_MAX_HARNESS_JOBS": "max_harness_jobs",
    "DSH_RESERVE_RESUME_SLOT": "reserve_resume_slot",
    "DEEPSEEK_RESERVE_RESUME_SLOT": "reserve_resume_slot",
    "DSH_DEPENDS_ON_POLICY": "depends_on_policy",
    "DEEPSEEK_DEPENDS_ON_POLICY": "depends_on_policy",
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
    "CREWFOREMAN_CREDENTIALS_FILE": "credentials_file",
    "DSH_CREDENTIALS_FILE": "credentials_file",
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
    "DEEPSEEK_MCP_LINUX_SANDBOX": "linux_sandbox",
    "DEEPSEEK_MCP_LINUX_MIN_ABI": "linux_min_abi",
    "DEEPSEEK_MCP_WINDOWS_SANDBOX": "windows_sandbox",
    "DEEPSEEK_MCP_SANDBOX_EXTRA_WRITES": "sandbox_extra_writes",
    "DEEPSEEK_MCP_SANDBOX_PROBE_TTL": "sandbox_probe_ttl",
    "DEEPSEEK_MCP_SANDBOX_CANARY": "sandbox_canary",
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
    # v3.11.0 F1 gate keys.
    "DSH_GATE_ENABLED": "gate_enabled",
    "DEEPSEEK_GATE_ENABLED": "gate_enabled",
    "DSH_GATE_TESTS": "gate_tests",
    "DEEPSEEK_GATE_TESTS": "gate_tests",
    "DSH_GATE_TEST_COMMAND": "gate_test_command",
    "DEEPSEEK_GATE_TEST_COMMAND": "gate_test_command",
    "DSH_GATE_LINT": "gate_lint",
    "DEEPSEEK_GATE_LINT": "gate_lint",
    "DSH_GATE_LINT_COMMAND": "gate_lint_command",
    "DEEPSEEK_GATE_LINT_COMMAND": "gate_lint_command",
    "DSH_GATE_SECRET_SCAN": "gate_secret_scan",
    "DEEPSEEK_GATE_SECRET_SCAN": "gate_secret_scan",
    "DSH_GATE_SECRET_PATTERNS": "gate_secret_patterns",
    "DEEPSEEK_GATE_SECRET_PATTERNS": "gate_secret_patterns",
    "DSH_GATE_SECRET_SCAN_BUDGET_SECONDS": "gate_secret_scan_budget_seconds",
    "DEEPSEEK_GATE_SECRET_SCAN_BUDGET_SECONDS": "gate_secret_scan_budget_seconds",
    "DSH_GATE_TIMEOUT_SECONDS": "gate_timeout_seconds",
    "DEEPSEEK_GATE_TIMEOUT_SECONDS": "gate_timeout_seconds",
    "DSH_GATE_MAX_REVISIONS": "gate_max_revisions",
    "DEEPSEEK_GATE_MAX_REVISIONS": "gate_max_revisions",
    "DSH_GATE_MAX_OUTPUT_CHARS": "gate_max_output_chars",
    "DEEPSEEK_GATE_MAX_OUTPUT_CHARS": "gate_max_output_chars",
    # v3.11.0 F2 plan keys.
    "DSH_PLAN_CONTRACT": "plan_contract",
    "DEEPSEEK_PLAN_CONTRACT": "plan_contract",
    "DSH_PLAN_FILE": "plan_file",
    "DEEPSEEK_PLAN_FILE": "plan_file",
    "DSH_PLAN_MAX_FILES": "plan_max_files",
    "DEEPSEEK_PLAN_MAX_FILES": "plan_max_files",
    "DSH_PLAN_MAX_RISKS": "plan_max_risks",
    "DEEPSEEK_PLAN_MAX_RISKS": "plan_max_risks",
    # v3.11.0 F3 failure-note keys.
    "DSH_FAILURE_NOTES": "failure_notes",
    "DEEPSEEK_FAILURE_NOTES": "failure_notes",
    "DSH_FAILURE_NOTES_MAX": "failure_notes_max",
    "DEEPSEEK_FAILURE_NOTES_MAX": "failure_notes_max",
    "DSH_FAILURE_NOTES_HALF_LIFE_DAYS": "failure_notes_half_life_days",
    "DEEPSEEK_FAILURE_NOTES_HALF_LIFE_DAYS": "failure_notes_half_life_days",
    "DSH_FAILURE_NOTES_MIN_OCCURRENCES": "failure_notes_min_occurrences",
    "DEEPSEEK_FAILURE_NOTES_MIN_OCCURRENCES": "failure_notes_min_occurrences",
    "DSH_FAILURE_NOTES_MAX_STORE": "failure_notes_max_store",
    "DEEPSEEK_FAILURE_NOTES_MAX_STORE": "failure_notes_max_store",
    "DSH_FAILURE_NOTES_STORE": "failure_notes_store",
    "DEEPSEEK_FAILURE_NOTES_STORE": "failure_notes_store",
    # v3.11.0 F4 pipeline keys.
    "DSH_PIPELINE_ENABLED": "pipeline_enabled",
    "DEEPSEEK_PIPELINE_ENABLED": "pipeline_enabled",
    "DSH_PIPELINE_MAX_EXTRA_JOBS": "pipeline_max_extra_jobs",
    "DEEPSEEK_PIPELINE_MAX_EXTRA_JOBS": "pipeline_max_extra_jobs",
    "DSH_PIPELINE_REQUIRE_DISJOINT": "pipeline_require_disjoint",
    "DEEPSEEK_PIPELINE_REQUIRE_DISJOINT": "pipeline_require_disjoint",
    "DSH_PIPELINE_ISOLATION": "pipeline_isolation",
    "DEEPSEEK_PIPELINE_ISOLATION": "pipeline_isolation",
    "DSH_PIPELINE_WORKTREE_ROOT": "pipeline_worktree_root",
    "DEEPSEEK_PIPELINE_WORKTREE_ROOT": "pipeline_worktree_root",
    "DSH_PIPELINE_PATCH_DIR": "pipeline_patch_dir",
    "DEEPSEEK_PIPELINE_PATCH_DIR": "pipeline_patch_dir",
    "DSH_PIPELINE_INSTALL": "pipeline_install",
    "DEEPSEEK_PIPELINE_INSTALL": "pipeline_install",
    "DSH_PIPELINE_MIN_REMAINING_USD": "pipeline_min_remaining_usd",
    "DEEPSEEK_PIPELINE_MIN_REMAINING_USD": "pipeline_min_remaining_usd",
    "DSH_PIPELINE_MAX_PENDING_PATCHES": "pipeline_max_pending_patches",
    "DEEPSEEK_PIPELINE_MAX_PENDING_PATCHES": "pipeline_max_pending_patches",
    "DSH_PIPELINE_QUEUE_SCAN": "pipeline_queue_scan",
    "DEEPSEEK_PIPELINE_QUEUE_SCAN": "pipeline_queue_scan",
    "DSH_PIPELINE_PATCH_RETENTION_DAYS": "pipeline_patch_retention_days",
    "DEEPSEEK_PIPELINE_PATCH_RETENTION_DAYS": "pipeline_patch_retention_days",
    # v3.14.2 crosscheck/audit/automatic-parallel keys.
    "DSH_DEFAULT_MODE": "default_mode",
    "DEEPSEEK_DEFAULT_MODE": "default_mode",
    "DSH_CROSSCHECK_THRESHOLD": "crosscheck_threshold",
    "DEEPSEEK_CROSSCHECK_THRESHOLD": "crosscheck_threshold",
    "DSH_CROSSCHECK_CREWS": "crosscheck_crews",
    "DEEPSEEK_CROSSCHECK_CREWS": "crosscheck_crews",
    "DSH_CROSSCHECK_CREW_SIZE": "crosscheck_crew_size",
    "DEEPSEEK_CROSSCHECK_CREW_SIZE": "crosscheck_crew_size",
    "DSH_CROSSCHECK_MAX_ROUNDS": "crosscheck_max_rounds",
    "DEEPSEEK_CROSSCHECK_MAX_ROUNDS": "crosscheck_max_rounds",
    "DSH_AUDIT_REVIEW": "audit_review",
    "DEEPSEEK_AUDIT_REVIEW": "audit_review",
    "DSH_AUDIT_SAMPLE_RATE": "audit_sample_rate",
    "DEEPSEEK_AUDIT_SAMPLE_RATE": "audit_sample_rate",
    "DSH_AUDIT_SPOT_REQUIREMENTS": "audit_spot_requirements",
    "DEEPSEEK_AUDIT_SPOT_REQUIREMENTS": "audit_spot_requirements",
    "DSH_AUDIT_SPOT_FIXES": "audit_spot_fixes",
    "DEEPSEEK_AUDIT_SPOT_FIXES": "audit_spot_fixes",
    "DSH_AUDIT_FORCED_JOBS": "audit_forced_jobs",
    "DEEPSEEK_AUDIT_FORCED_JOBS": "audit_forced_jobs",
    "DSH_AUDIT_SEED": "audit_seed",
    "DEEPSEEK_AUDIT_SEED": "audit_seed",
    "DSH_AUTO_PARALLEL": "auto_parallel",
    "DEEPSEEK_AUTO_PARALLEL": "auto_parallel",
    "DSH_AUTO_PARALLEL_MIN_CONFIDENCE": "auto_parallel_min_confidence",
    "DEEPSEEK_AUTO_PARALLEL_MIN_CONFIDENCE": "auto_parallel_min_confidence",
}

BOOL_KEYS = {"builder_practices", "builder_allow_unlisted_models", "quality", "independent_review", "child_progress",
             "render_probe", "transient_retry", "checkpoint", "dedupe_active",
             "provider_fallback", "trace", "browser_evidence",
             "workspace_progress", "live_scaling",
             "auto_skills_allow_single_severe", "auto_skills_refresh",
             "auto_skills_store_evidence_text",
             "auto_skills_capture_explicit_none", "allow_writing_reviewer",
             "health_gate", "allow_first_phase_overrun",
             "reserve_resume_slot", "review_preflight",
             "gate_enabled", "gate_secret_scan", "pipeline_enabled",
             "pipeline_require_disjoint", "auto_parallel",
             "loop_guard_enabled", "loop_guard_confirm"}
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
            "cost_reservation_ttl_seconds",
            "gate_max_revisions", "gate_max_output_chars",
            "plan_max_files", "plan_max_risks",
            "loop_guard_repeat_command", "loop_guard_edit_pingpong",
            "loop_guard_nochange_calls", "loop_guard_same_test_failure",
            "loop_guard_tokens_per_call", "loop_guard_max_actions",
            "failure_notes_max",
            "failure_notes_min_occurrences", "failure_notes_max_store",
            "pipeline_max_extra_jobs", "pipeline_max_pending_patches",
            "pipeline_patch_retention_days",
            "builder_model_max_slices", "model_candidates_max",
            "model_usage_max_models", "history_model_min_runs",
            "history_model_max", "failure_notes_max_models",
            "failure_notes_model_min_runs", "backup_builder_min_attempts",
            "reviewer_max_preflights", "plan_optional_max_files",
            "linux_min_abi", "crosscheck_crews", "crosscheck_crew_size",
            "crosscheck_max_rounds", "audit_spot_requirements",
            "audit_spot_fixes", "audit_forced_jobs", "audit_seed"}
ENUM_KEYS = {"auto_skills": ("auto", "propose", "off"),
             "task_completion": ("job", "request"),
             "quality_convergence": ("observe", "auto"),
             "plan_contract": ("warn", "block", "off"),
             "failure_notes": ("on", "off"),
             "pipeline_install": ("auto", "manual", "off"),
             "pipeline_queue_scan": ("head", "first_fit"),
             "model_plan_enforcement": ("warn", "strict"),
             "failure_notes_models": ("on", "off"),
             "reviewer_same_vendor_policy": ("ask", "never"),
             "gate_no_product_mode": ("skip", "off"),
             "linux_sandbox": ("auto", "landlock", "bwrap", "off"),
             "windows_sandbox": ("auto", "restricted", "appcontainer", "off"),
             "sandbox_canary": ("auto", "off"),
             "depends_on_policy": ("success", "finished")}
# New v3.12.0 enums never raise on an invalid value: they fall back to the
# documented default so a bad env/config value cannot break startup.
ENUM_FALLBACKS = {
    "model_plan_enforcement": "warn",
    "failure_notes_models": "on",
    "reviewer_same_vendor_policy": "ask",
    "gate_no_product_mode": "skip",
}
LIST_KEYS = {"providers", "consult_models", "add_dirs",
             "allowed_workspace_roots", "gate_secret_patterns",
             "reviewers", "gate_noproduct_globs", "gate_split_fixture_paths",
             "sandbox_extra_writes", "audit_risk_globs", "risk_paths",
             "git_identity_exceptions"}
# Dict-valued keys keep JSON objects intact (never comma-split the text).
DICT_KEYS = {"slice_models", "model_prices", "git_identity"}
FLOAT_KEYS = {"timeout", "wait_seconds", "stall_seconds", "stall_max_seconds",
              "admission_heartbeat_seconds", "admission_lease_seconds",
              "review_test_timeout_seconds", "failure_notes_half_life_days",
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
              "restart_helper_retry_seconds",
              "gate_timeout_seconds", "gate_secret_scan_budget_seconds",
              "loop_guard_poll_seconds",
              "pipeline_min_remaining_usd", "crosscheck_threshold",
              "audit_sample_rate", "auto_parallel_min_confidence",
              "backup_builder_max_cost_usd", "sandbox_probe_ttl"}
PATH_KEYS = {"state_root", "log_dir", "jobs_dir", "pid_file", "skills_dir",
             "harness_workspace", "backup_dsh_bin",
             "dsh_sessions_dir", "state_dir", "chrome_bin", "restart_status_path",
             "failure_notes_store", "pipeline_worktree_root",
             "pipeline_patch_dir", "credentials_file"}

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
            if key in ENUM_FALLBACKS:
                return ENUM_FALLBACKS[key]
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
        if isinstance(value, bool):
            raise ValueError("%s must be a number, not a boolean" % key)
        number = float(value)
        if key == "job_max_seconds" and number < 0:
            raise ValueError("job_max_seconds must be >= 0")
        if key == "auto_skills_quality_gate" and not 0.0 <= number <= 10.0:
            raise ValueError("auto_skills_quality_gate must be between 0 and 10")
        if key == "auto_skills_min_confidence" and not 0.0 <= number <= 1.0:
            warnings.warn("auto_skills_min_confidence clamped to [0, 1]",
                          RuntimeWarning)
            return min(1.0, max(0.0, number))
        return number
    if key in DICT_KEYS:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("%s must be a JSON object" % key)
            return parsed
        raise ValueError("%s must be an object" % key)
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
