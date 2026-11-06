"""Authority envelope for a supervised Harness run (P2-1).

A *direct* envelope is opened only by a new ``harness_deepseek`` call.  Every
later phase (resume, rescue, live scaling, reviewer, revision, auto-skill) is a
*continuation*: it inherits the envelope and may only narrow it.  A resume
revalidates the recorded envelope against the current config, records the
policy drift and uses the stricter intersection.

This is a local, single-operator audit/containment layer, not a multi-user
authorization service.  There are no admission tables, bot lists or accept
modes.  P1-2 (prompt bundle hashing) is not implemented, so the envelope has no
``prompt_bundle_hash`` field; adding one before the prompt bundle exists would
claim provenance the repository cannot prove.

Python 3.9 compatible; standard library only.
"""

import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import math
import os
import re

from . import safety

AUTHORITY_VERSION = 1
BASES = ("direct", "continuation")
SANDBOX_RANK = {"read-only": 0, "workspace-write": 1,
                "danger-full-access": 2}
_READ_ONLY = "read-only"
_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


class AuthorityError(RuntimeError):
    """Base class for authority failures."""


class InvalidAuthority(AuthorityError):
    """A stored/current envelope is missing, malformed or from a newer run."""


class WideningError(AuthorityError):
    """A continuation attempted to increase an authority field."""


class WorkspaceRootError(AuthorityError):
    """A workspace is outside the allowed or recorded root."""


class ReviewerPolicyError(AuthorityError):
    """A caller asked for a reviewer posture the operator has not allowed."""


def _cfg(cfg, name, default=None):
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(name, default)
        except Exception:
            return default
    return getattr(cfg, name, default)


def _now(now=None):
    if isinstance(now, dt.datetime):
        return now if now.tzinfo else now.astimezone()
    if isinstance(now, (int, float)):
        return dt.datetime.fromtimestamp(float(now)).astimezone()
    return dt.datetime.now().astimezone()


def _iso(value):
    if isinstance(value, dt.datetime):
        return value.astimezone().isoformat(timespec="seconds")
    return str(value or "")


def _seconds(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _mode(value, default=_READ_ONLY):
    try:
        return safety.normalize_sandbox(value, default=default)
    except (TypeError, ValueError) as exc:
        raise InvalidAuthority(str(exc))


def _rank(mode):
    return SANDBOX_RANK.get(str(mode), 0)


_INT_RE = re.compile(r"^[+-]?[0-9]+$")


def _strict_int(value, field, minimum=None):
    """Parse a persisted integer without the old ``int()`` coercion holes."""
    if value is None or isinstance(value, bool):
        raise InvalidAuthority("invalid authority %s" % field)
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise InvalidAuthority("invalid authority %s" % field)
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _INT_RE.match(text):
            raise InvalidAuthority("invalid authority %s" % field)
        number = int(text)
    else:
        raise InvalidAuthority("invalid authority %s" % field)
    if minimum is not None and number < minimum:
        raise InvalidAuthority("invalid authority %s" % field)
    return number


def _strict_budget(value, field):
    """A persisted budget is None (unbounded) or a finite positive number.

    Numeric strings are rejected: the old ``float()`` coercion turned malformed
    text into a silent ``None``, which failed open into an unbounded budget.
    """
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, str) or not isinstance(
            value, (int, float)):
        raise InvalidAuthority("invalid authority %s" % field)
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise InvalidAuthority("invalid authority %s" % field)
    return number


def _strict_bool(value, field, default=False):
    if value is None:
        return bool(default)
    if not isinstance(value, bool):
        raise InvalidAuthority("invalid authority %s" % field)
    return value


def _strict_timestamp(value, field, required=False):
    """Return an aware datetime for *value*, rejecting unreadable timestamps."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise InvalidAuthority("invalid authority %s" % field)
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except (TypeError, ValueError, OverflowError):
            raise InvalidAuthority("invalid authority %s" % field)
    else:
        raise InvalidAuthority("invalid authority %s" % field)
    if parsed.tzinfo is None:
        # Legacy naive timestamps are documented as local wall-clock time.
        parsed = parsed.astimezone()
    return parsed


def _reviewer_mode(value, allow_writing=False, override=False):
    """Never allow a danger-full-access reviewer, even with an override."""
    mode = _mode(value, _READ_ONLY)
    if _rank(mode) >= _rank("danger-full-access"):
        mode = "workspace-write" if (allow_writing and override) else _READ_ONLY
    return mode


def _canonical_path(value):
    return os.path.realpath(os.path.expanduser(str(value)))


def _normalise_roots(roots):
    if roots is None:
        return []
    if isinstance(roots, str):
        roots = [item.strip() for item in roots.split(",") if item.strip()]
    result = []
    for raw in roots:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = _canonical_path(raw.strip())
        if path not in result:
            result.append(path)
    return result


def inside_root(path, root):
    """True when *path* is *root* itself or a descendant (symlink-resolved)."""
    if not path or not root:
        return False
    path = os.path.normcase(_canonical_path(path))
    root = os.path.normcase(_canonical_path(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def canonical_json(value):
    """Stable JSON used for digests (key order and whitespace independent)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def policy_digest(cfg):
    """Digest of the policy inputs that can change a run's authority.

    Only keys that actually affect confinement/spend are hashed, so adding an
    unrelated setting cannot spuriously block a resume.  Missing keys hash as
    ``None``; the result is stable across mapping key order.
    """
    policy = {
        "sandbox": _cfg(cfg, "sandbox", "workspace-write"),
        "reviewer_sandbox": _cfg(cfg, "reviewer_sandbox", "read-only"),
        "job_max_seconds": _cfg(cfg, "job_max_seconds", None),
        "job_max_cost_usd": _cfg(cfg, "job_max_cost_usd", None),
        "max_explicit_subagents": _cfg(cfg, "max_explicit_subagents", 32),
        "default_subagents": _cfg(cfg, "default_subagents", 16),
        "subagent_soft_cap": _cfg(cfg, "subagent_soft_cap", 32),
        "allow_writing_reviewer": bool(_cfg(cfg, "allow_writing_reviewer",
                                            False)),
        "allowed_workspace_roots": _normalise_roots(
            _cfg(cfg, "allowed_workspace_roots", [])),
    }
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def resolve_workspace(args, cfg):
    """Return the absolute workspace, refusing one outside allowed roots."""
    args = args or {}
    raw = args.get("workspace") or _cfg(cfg, "harness_workspace", "")
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspaceRootError("a workspace path is required")
    workspace = _canonical_path(raw.strip())
    roots = _normalise_roots(_cfg(cfg, "allowed_workspace_roots", []))
    if roots and not any(inside_root(workspace, root) for root in roots):
        raise WorkspaceRootError(
            "workspace %s is outside allowed_workspace_roots" % workspace)
    return workspace


def _stricter_budget(old, new, field):
    """Budgets: None is unbounded; a positive number is strict, smaller is stricter."""
    old_value = _seconds(old)
    new_value = _seconds(new)
    if new_value is None and old_value is not None:
        raise WideningError("%s cannot be widened (removing the bound)" % field)
    if old_value is not None and new_value is not None and new_value > old_value:
        raise WideningError("%s cannot be widened" % field)
    return new_value


def _expiry(created, cfg):
    try:
        ttl = float(_cfg(cfg, "authority_ttl_seconds", _DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_SECONDS
    if ttl <= 0:
        return None
    return _iso(created + dt.timedelta(seconds=ttl))


class AuthorityEnvelope:
    """The immutable-ish grant that authorizes Harness phases.

    Instances are treated as values: every mutation helper returns a new
    envelope, so a caller cannot widen a shared object in place.
    """

    FIELDS = ("basis", "origin", "policy_digest", "sandbox", "reviewer_policy",
              "workspace_root", "job_max_seconds", "job_max_cost_usd",
              "max_agents", "created", "expires", "reviewer_override",
              "version")

    def __init__(self, basis="continuation", origin="", policy_digest="",
                 sandbox=_READ_ONLY, reviewer_policy=_READ_ONLY,
                 workspace_root="", job_max_seconds=None,
                 job_max_cost_usd=None, max_agents=0, created=None,
                 expires=None, reviewer_override=False,
                 version=AUTHORITY_VERSION, legacy_default=False,
                 downgraded_from=None):
        self.basis = str(basis)
        self.origin = str(origin or "")
        self.policy_digest = str(policy_digest or "")
        self.sandbox = str(sandbox)
        self.reviewer_policy = str(reviewer_policy)
        self.workspace_root = str(workspace_root or "")
        self.job_max_seconds = _seconds(job_max_seconds)
        self.job_max_cost_usd = _seconds(job_max_cost_usd)
        try:
            self.max_agents = max(0, int(max_agents or 0))
        except (TypeError, ValueError):
            self.max_agents = 0
        self.created = _iso(created) if created else _iso(_now())
        self.expires = _iso(expires) if expires else None
        self.reviewer_override = bool(reviewer_override)
        self.version = int(version or AUTHORITY_VERSION)
        self.legacy_default = bool(legacy_default)
        self.downgraded_from = downgraded_from

    # -- serialisation ---------------------------------------------------
    def to_dict(self):
        data = {
            "version": self.version,
            "basis": self.basis,
            "origin": self.origin,
            "policy_digest": self.policy_digest,
            "sandbox": self.sandbox,
            "reviewer_policy": self.reviewer_policy,
            "reviewer_override": self.reviewer_override,
            "workspace_root": self.workspace_root,
            "job_max_seconds": self.job_max_seconds,
            "job_max_cost_usd": self.job_max_cost_usd,
            "max_agents": self.max_agents,
            "created": self.created,
            "expires": self.expires,
        }
        if self.legacy_default:
            data["legacy_default"] = True
        if self.downgraded_from:
            data["downgraded_from"] = self.downgraded_from
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise InvalidAuthority("authority must be an object")
        version = _strict_int(
            data.get("version", AUTHORITY_VERSION), "version", minimum=1)
        if version > AUTHORITY_VERSION:
            raise InvalidAuthority(
                "authority version %s is newer than this build (%s); refusing "
                "to resume" % (version, AUTHORITY_VERSION))
        basis = data.get("basis")
        if not isinstance(basis, str) or basis not in BASES:
            raise InvalidAuthority("invalid authority basis")
        sandbox = _mode(data.get("sandbox"), _READ_ONLY)
        if (basis == "continuation"
                and _rank(sandbox) >= _rank("danger-full-access")):
            raise InvalidAuthority(
                "a continuation authority cannot carry danger-full-access")
        reviewer_override = _strict_bool(
            data.get("reviewer_override"), "reviewer_override")
        raw_reviewer = _mode(data.get("reviewer_policy"), _READ_ONLY)
        if _rank(raw_reviewer) >= _rank("danger-full-access"):
            raise InvalidAuthority(
                "reviewer_policy cannot be danger-full-access")
        reviewer = _reviewer_mode(
            data.get("reviewer_policy"),
            allow_writing=True,
            override=reviewer_override)
        if _rank(reviewer) >= _rank("danger-full-access"):
            raise InvalidAuthority(
                "reviewer_policy cannot be danger-full-access")
        if _rank(reviewer) > 0 and not reviewer_override:
            raise InvalidAuthority(
                "reviewer_policy is writing without a recorded override")
        workspace = data.get("workspace_root")
        if (not isinstance(workspace, str) or not workspace.strip()
                or "\x00" in workspace):
            raise InvalidAuthority("invalid authority workspace_root")
        workspace = os.path.expanduser(workspace.strip())
        if not os.path.isabs(workspace):
            raise InvalidAuthority("invalid authority workspace_root")
        workspace = _canonical_path(workspace)
        origin = data.get("origin") or ""
        digest = data.get("policy_digest") or ""
        if not isinstance(origin, str) or not isinstance(digest, str):
            raise InvalidAuthority("invalid authority metadata")
        created_dt = _strict_timestamp(
            data.get("created"), "created", required=True)
        expires_dt = _strict_timestamp(data.get("expires"), "expires")
        if expires_dt is not None and expires_dt < created_dt:
            raise InvalidAuthority("authority expires before it was created")
        downgraded = data.get("downgraded_from")
        if downgraded is not None:
            if not isinstance(downgraded, str):
                raise InvalidAuthority("invalid authority downgraded_from")
            _mode(downgraded, sandbox)
        return cls(
            basis=basis,
            origin=origin,
            policy_digest=digest,
            sandbox=sandbox,
            reviewer_policy=reviewer,
            workspace_root=workspace,
            job_max_seconds=_strict_budget(
                data.get("job_max_seconds"), "job_max_seconds"),
            job_max_cost_usd=_strict_budget(
                data.get("job_max_cost_usd"), "job_max_cost_usd"),
            max_agents=_strict_int(
                data.get("max_agents", 0), "max_agents", minimum=0),
            created=_iso(created_dt),
            expires=_iso(expires_dt) if expires_dt is not None else None,
            reviewer_override=reviewer_override,
            version=version,
            legacy_default=_strict_bool(
                data.get("legacy_default"), "legacy_default"),
            downgraded_from=downgraded,
        )

    # -- narrowing -------------------------------------------------------
    def _copy(self, **changes):
        values = self.to_dict()
        values.pop("version", None)
        values.update(changes)
        return AuthorityEnvelope(version=self.version, **values)

    def narrow(self, sandbox=None, reviewer_policy=None, job_max_seconds=None,
               job_max_cost_usd=None, max_agents=None, workspace_root=None):
        """Return a stricter envelope; any widening raises ``WideningError``."""
        changes = {}
        if sandbox is not None:
            mode = _mode(sandbox, self.sandbox)
            if _rank(mode) >= _rank("danger-full-access"):
                raise WideningError(
                    "sandbox cannot be danger-full-access: danger cannot be inherited "
                    "by a narrowing")
            if _rank(mode) > _rank(self.sandbox):
                raise WideningError(
                    "sandbox cannot be widened from %s to %s"
                    % (self.sandbox, mode))
            changes["sandbox"] = mode
        if reviewer_policy is not None:
            mode = _reviewer_mode(reviewer_policy, True,
                                  self.reviewer_override)
            if _rank(mode) >= _rank("danger-full-access"):
                raise ReviewerPolicyError(
                    "reviewer_policy cannot be danger-full-access")
            if _rank(mode) > _rank(self.reviewer_policy):
                raise WideningError(
                    "reviewer_policy cannot be widened from %s to %s"
                    % (self.reviewer_policy, mode))
            if mode != _READ_ONLY and not self.reviewer_override:
                raise ReviewerPolicyError(
                    "a continuation cannot grant a writing reviewer")
            changes["reviewer_policy"] = mode
        if job_max_seconds is not None:
            changes["job_max_seconds"] = _stricter_budget(
                self.job_max_seconds, job_max_seconds, "job_max_seconds")
        if job_max_cost_usd is not None:
            changes["job_max_cost_usd"] = _stricter_budget(
                self.job_max_cost_usd, job_max_cost_usd, "job_max_cost_usd")
        if max_agents is not None:
            try:
                value = max(0, int(max_agents))
            except (TypeError, ValueError):
                raise WideningError("max_agents must be an integer")
            if value > self.max_agents:
                raise WideningError(
                    "max_agents cannot be widened from %s to %s"
                    % (self.max_agents, value))
            changes["max_agents"] = value
        if workspace_root is not None:
            root = _canonical_path(workspace_root)
            if not inside_root(root, self.workspace_root):
                raise WideningError(
                    "workspace_root cannot be widened outside the recorded root")
            changes["workspace_root"] = root
        return self._copy(**changes)

    def continuation(self, origin, cfg=None):
        """Turn this grant into a continuation; danger is never inherited.

        The recorded ``created``/``expires`` pair is preserved: a continuation
        is not a fresh grant, and refreshing the expiry here would hide expiry
        drift from :meth:`revalidate` on resume.
        """
        downgraded = None
        sandbox = self.sandbox
        if _rank(sandbox) >= _rank("danger-full-access"):
            sandbox = "workspace-write"
            downgraded = self.sandbox
        reviewer = self.reviewer_policy
        if _rank(reviewer) >= _rank("danger-full-access"):
            reviewer = ("workspace-write" if self.reviewer_override
                        else _READ_ONLY)
            downgraded = downgraded or self.reviewer_policy
        elif reviewer != _READ_ONLY and not self.reviewer_override:
            reviewer = _READ_ONLY
        return self._copy(
            basis="continuation",
            origin=str(origin or "continuation"),
            sandbox=sandbox,
            reviewer_policy=reviewer,
            created=self.created,
            expires=self.expires,
            downgraded_from=downgraded or self.downgraded_from,
        )

    # -- revalidation ----------------------------------------------------
    def revalidate(self, cfg, workspace=None):
        """Narrow this envelope to the current config; report the drift.

        Returns ``(effective_envelope, policy_drift)``.  A resume can only get
        stricter.  The workspace must stay inside the recorded root.
        """
        changed = {}
        updates = {}

        cfg_sandbox = _mode(_cfg(cfg, "sandbox", "workspace-write"))
        if _rank(self.sandbox) > _rank(cfg_sandbox):
            updates["sandbox"] = cfg_sandbox
            changed["sandbox"] = [self.sandbox, cfg_sandbox]

        allow_reviewer = bool(_cfg(cfg, "allow_writing_reviewer", False))
        cfg_reviewer = _reviewer_mode(
            _cfg(cfg, "reviewer_sandbox", _READ_ONLY),
            allow_writing=allow_reviewer, override=True)
        effective_reviewer = _reviewer_mode(
            self.reviewer_policy, allow_writing=allow_reviewer,
            override=self.reviewer_override)
        if not (allow_reviewer and self.reviewer_override):
            effective_reviewer = _READ_ONLY
        elif _rank(cfg_reviewer) < _rank(effective_reviewer):
            # The operator tightened the reviewer default after the run was
            # created: the intersection is the stricter mode.
            effective_reviewer = cfg_reviewer
        if effective_reviewer != self.reviewer_policy:
            updates["reviewer_policy"] = effective_reviewer
            changed["reviewer_policy"] = [self.reviewer_policy,
                                          effective_reviewer]

        def _cap(old, cfg_value, name):
            cap = _seconds(cfg_value)
            if cap is None:
                return
            if old is None or old > cap:
                updates[name] = cap
                changed[name] = [old, cap]

        if self.job_max_seconds is not None or _seconds(
                _cfg(cfg, "job_max_seconds", None)) is not None:
            _cap(self.job_max_seconds, _cfg(cfg, "job_max_seconds", None),
                 "job_max_seconds")
        if self.job_max_cost_usd is not None or _seconds(
                _cfg(cfg, "job_max_cost_usd", None)) is not None:
            _cap(self.job_max_cost_usd, _cfg(cfg, "job_max_cost_usd", None),
                 "job_max_cost_usd")

        try:
            agent_cap = max(0, int(_cfg(cfg, "max_explicit_subagents", 32) or 32))
        except (TypeError, ValueError):
            agent_cap = 32
        try:
            soft_cap = max(1, int(_cfg(cfg, "subagent_soft_cap", 32) or 32))
        except (TypeError, ValueError):
            soft_cap = 32
        agent_cap = min(agent_cap, soft_cap)
        if self.max_agents > agent_cap:
            updates["max_agents"] = agent_cap
            changed["max_agents"] = [self.max_agents, agent_cap]

        recorded = _canonical_path(self.workspace_root) if self.workspace_root else ""
        target = _canonical_path(workspace) if workspace else recorded
        if not recorded:
            raise WorkspaceRootError("authority has no recorded workspace root")
        if not inside_root(target, recorded):
            raise WorkspaceRootError(
                "workspace %s is outside the recorded authority root %s"
                % (target, recorded))
        roots = _normalise_roots(_cfg(cfg, "allowed_workspace_roots", []))
        if roots and not any(inside_root(recorded, root) for root in roots):
            raise WorkspaceRootError(
                "recorded workspace root %s is outside allowed_workspace_roots"
                % recorded)

        digest_after = policy_digest(cfg)
        drift = {
            "changed": changed,
            "digest_before": self.policy_digest,
            "digest_after": digest_after,
            "digest_changed": digest_after != self.policy_digest,
            "stricter": bool(changed),
            "legacy_default": bool(self.legacy_default),
        }
        if digest_after != self.policy_digest:
            updates["policy_digest"] = digest_after
        now = _now()
        expired = False
        if self.expires:
            try:
                expiry = dt.datetime.fromisoformat(str(self.expires))
                if expiry.tzinfo is None:
                    # A legacy naive timestamp is local wall-clock time; keep
                    # that documented assumption rather than forcing UTC.
                    expiry = expiry.astimezone()
                expired = expiry < now
            except (TypeError, ValueError, OverflowError):
                # An unreadable expiry cannot be trusted; refresh it and
                # record the drift rather than crashing the resume.
                expired = True
        if expired:
            updates["expires"] = _expiry(now, cfg)
            drift["changed"]["expired"] = [self.expires, updates["expires"]]
        if updates:
            drift["effective"] = updates
        return self._copy(**updates), drift


def direct(args, cfg, origin="harness_deepseek", now=None):
    """Open a direct envelope for a brand-new ``harness_deepseek`` call."""
    args = args or {}
    workspace = resolve_workspace(args, cfg)
    builder = safety.resolve(args, cfg, reviewer=False)

    caller_reviewer = args.get("reviewer_sandbox")
    explicit_reviewer = (caller_reviewer is not None
                         and not (isinstance(caller_reviewer, str)
                                  and not caller_reviewer.strip()))
    if explicit_reviewer:
        reviewer_mode = _mode(caller_reviewer, _READ_ONLY)
    else:
        reviewer_mode = _mode(_cfg(cfg, "reviewer_sandbox", _READ_ONLY),
                              _READ_ONLY)
    allow_writing = bool(_cfg(cfg, "allow_writing_reviewer", False))
    if _rank(reviewer_mode) >= _rank("danger-full-access"):
        # A reviewer is never a full-access escape hatch, even with the
        # operator's writing-reviewer override.
        raise ReviewerPolicyError(
            "reviewer_sandbox cannot be danger-full-access")
    reviewer_override = reviewer_mode != _READ_ONLY
    if reviewer_override:
        if not allow_writing:
            raise ReviewerPolicyError(
                "reviewer_sandbox must be read-only unless the operator sets "
                "allow_writing_reviewer=true")
    elif explicit_reviewer:
        # A caller may always narrow to read-only even when config is writing.
        reviewer_mode = _READ_ONLY

    def _requested(raw, field):
        if raw is None:
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise InvalidAuthority("%s must be a number" % field)
        if not math.isfinite(number):
            raise InvalidAuthority("%s must be finite" % field)
        if number <= 0:
            return None
        return number

    cfg_seconds = _seconds(_cfg(cfg, "job_max_seconds", None))
    requested_seconds = _requested(args.get("job_max_seconds"),
                                   "job_max_seconds")
    if requested_seconds is None:
        requested_seconds = cfg_seconds
    elif cfg_seconds is not None:
        requested_seconds = min(requested_seconds, cfg_seconds)
    cfg_cost = _seconds(_cfg(cfg, "job_max_cost_usd", None))
    requested_cost = _requested(args.get("job_max_cost_usd"),
                                "job_max_cost_usd")
    if requested_cost is None:
        requested_cost = cfg_cost
    elif cfg_cost is not None:
        requested_cost = min(requested_cost, cfg_cost)

    hard_agents = _cfg(cfg, "max_explicit_subagents", 32)
    try:
        hard_agents = max(1, int(hard_agents or 32))
    except (TypeError, ValueError):
        hard_agents = 32
    if args.get("subagents") is not None:
        try:
            max_agents = max(0, int(args.get("subagents")))
        except (TypeError, ValueError):
            max_agents = hard_agents
        max_agents = min(max_agents, hard_agents)
    else:
        try:
            default_agents = max(0, int(_cfg(cfg, "default_subagents", 16) or 0))
        except (TypeError, ValueError):
            default_agents = 16
        try:
            soft_cap = max(1, int(_cfg(cfg, "subagent_soft_cap", 32) or 32))
        except (TypeError, ValueError):
            soft_cap = 32
        max_agents = min(default_agents, soft_cap, hard_agents)

    created = _now(now)
    envelope = AuthorityEnvelope(
        basis="direct",
        origin=origin,
        policy_digest=policy_digest(cfg),
        sandbox=builder["sandbox"],
        reviewer_policy=reviewer_mode,
        workspace_root=workspace,
        job_max_seconds=requested_seconds,
        job_max_cost_usd=requested_cost,
        max_agents=max_agents,
        created=_iso(created),
        expires=_expiry(created, cfg),
        reviewer_override=reviewer_override,
    )
    if not explicit_reviewer and reviewer_mode == _READ_ONLY and _rank(
            _mode(_cfg(cfg, "reviewer_sandbox", _READ_ONLY))) > 0:
        envelope.downgraded_from = _cfg(cfg, "reviewer_sandbox")
    return envelope


def reviewer_policy_for(envelope, cfg):
    """Effective reviewer mode: read-only unless an operator override exists."""
    if envelope.reviewer_override and bool(
            _cfg(cfg, "allow_writing_reviewer", False)):
        return _reviewer_mode(envelope.reviewer_policy, True, True)
    return _READ_ONLY


# The active run envelope for helper phases that run deep inside another
# module (auto-skills).  A ContextVar keeps the authority explicit without
# threading it through every author/reviewer helper signature; when nothing is
# active, auto_skill_continuation() still opens a conservative read-only
# continuation rather than granting an unrelated direct envelope.
_ACTIVE = contextvars.ContextVar("deepseek_active_authority", default=None)


@contextlib.contextmanager
def activate(envelope):
    """Make *envelope* (object or dict) the active run authority."""
    if isinstance(envelope, dict):
        envelope = AuthorityEnvelope.from_dict(envelope)
    if envelope is not None and not isinstance(envelope, AuthorityEnvelope):
        raise InvalidAuthority("active authority must be an envelope")
    token = _ACTIVE.set(envelope)
    try:
        yield envelope
    finally:
        _ACTIVE.reset(token)


def active():
    """Return the active :class:`AuthorityEnvelope`, if any."""
    return _ACTIVE.get()


def auto_skill_continuation(cfg, workspace=None, envelope=None):
    """Return the read-only continuation that authorizes an auto-skill call.

    Auto-skill agents are always read-only.  They still flow through
    ``continuation()`` so they cannot inherit a direct danger grant, and a
    background call with no active run gets a conservative read-only default.
    """
    base = envelope if envelope is not None else _ACTIVE.get()
    if base is None:
        root = (workspace or _cfg(cfg, "harness_workspace", "")
                or os.getcwd())
        base = AuthorityEnvelope(
            basis="direct",
            origin="auto-skill-default",
            policy_digest=policy_digest(cfg),
            sandbox=_READ_ONLY,
            reviewer_policy=_READ_ONLY,
            workspace_root=_canonical_path(root),
            job_max_seconds=_cfg(cfg, "job_max_seconds", None),
            job_max_cost_usd=_cfg(cfg, "job_max_cost_usd", None),
            max_agents=0,
            legacy_default=True,
        )
    elif isinstance(base, dict):
        base = AuthorityEnvelope.from_dict(base)
    elif not isinstance(base, AuthorityEnvelope):
        raise InvalidAuthority("auto-skill authority is not an envelope")
    return base.continuation("auto-skill", cfg).narrow(sandbox=_READ_ONLY)


def _saved_sandbox(run, key, default):
    value = run.get(key)
    if isinstance(value, dict):
        value = value.get("sandbox")
    if isinstance(value, str) and value.strip():
        try:
            return safety.normalize_sandbox(value, default=default)
        except ValueError:
            return default
    return default


def legacy_default(run, cfg, metadata=None):
    """Conservative envelope for a 3.8.0 record written before authority existed."""
    run = run or {}
    metadata = metadata or {}
    spec = metadata.get("spec") or {}
    saved_sandbox = _saved_sandbox(run, "builder_sandbox",
                                   _cfg(cfg, "sandbox", "workspace-write"))
    cfg_sandbox = _mode(_cfg(cfg, "sandbox", "workspace-write"))
    sandbox = saved_sandbox if _rank(saved_sandbox) <= _rank(cfg_sandbox) else cfg_sandbox
    if _rank(sandbox) >= _rank("danger-full-access"):
        sandbox = "workspace-write"
    workspace = (run.get("workspace") or spec.get("workspace")
                 or _cfg(cfg, "harness_workspace", ""))
    workspace = _canonical_path(workspace) if workspace else ""
    roots = _normalise_roots(_cfg(cfg, "allowed_workspace_roots", []))
    if roots and workspace and not any(inside_root(workspace, root) for root in roots):
        raise WorkspaceRootError(
            "legacy run workspace %s is outside allowed_workspace_roots" % workspace)
    saved_seconds = _seconds(run.get("job_max_seconds"))
    if saved_seconds is None:
        saved_seconds = _seconds(spec.get("job_max_seconds"))
    cfg_seconds = _seconds(_cfg(cfg, "job_max_seconds", None))
    if saved_seconds is None:
        seconds = cfg_seconds
    elif cfg_seconds is None:
        seconds = saved_seconds
    else:
        seconds = min(saved_seconds, cfg_seconds)
    saved_cost = _seconds(run.get("job_max_cost_usd"))
    if saved_cost is None:
        saved_cost = _seconds(spec.get("job_max_cost_usd"))
    cfg_cost = _seconds(_cfg(cfg, "job_max_cost_usd", None))
    if saved_cost is None:
        cost = cfg_cost
    elif cfg_cost is None:
        cost = saved_cost
    else:
        cost = min(saved_cost, cfg_cost)
    agents = run.get("subagents")
    if agents is None:
        agents = spec.get("subagents")
    try:
        agents = max(0, int(agents or 0))
    except (TypeError, ValueError):
        agents = 0
    try:
        agents = min(agents, max(1, int(_cfg(cfg, "max_explicit_subagents", 32) or 32)))
    except (TypeError, ValueError):
        pass
    try:
        agents = min(agents, max(1, int(_cfg(cfg, "subagent_soft_cap", 32) or 32)))
    except (TypeError, ValueError):
        pass
    created = _now()
    return AuthorityEnvelope(
        basis="continuation",
        origin="legacy-3.8.0",
        policy_digest=policy_digest(cfg),
        sandbox=sandbox,
        reviewer_policy=_READ_ONLY,
        workspace_root=workspace,
        job_max_seconds=seconds,
        job_max_cost_usd=cost,
        max_agents=agents,
        created=_iso(created),
        expires=_expiry(created, cfg),
        legacy_default=True,
    )


def revalidate_run(run, cfg, metadata=None):
    """Return ``(effective_envelope, policy_drift)`` for a resume.

    A pre-authority 3.8.0 record gets the conservative legacy default.  Once a
    record advertises ``authority_version`` it must carry a valid envelope: a
    missing, corrupt or newer envelope fails closed instead of silently
    granting a continuation.
    """
    run = run or {}
    metadata = metadata or {}
    raw = run.get("authority")
    version = run.get("authority_version")
    if raw is None:
        if version is not None:
            raise InvalidAuthority(
                "run advertises authority_version %r but has no valid authority; "
                "refusing to resume" % (version,))
        return legacy_default(run, cfg, metadata).revalidate(
            cfg, workspace=run.get("workspace") or None)
    if not isinstance(raw, dict):
        raise InvalidAuthority("authority is not an object; refusing to resume")
    if version is None:
        version = raw.get("version")
    try:
        version_int = _strict_int(version, "version", minimum=1)
    except InvalidAuthority:
        raise InvalidAuthority(
            "authority version is invalid; refusing to resume")
    if version_int > AUTHORITY_VERSION:
        raise InvalidAuthority(
            "authority version %s is newer than this build (%s); refusing to "
            "resume" % (version_int, AUTHORITY_VERSION))
    envelope = AuthorityEnvelope.from_dict(raw)
    workspace = run.get("workspace")
    effective = envelope.continuation("resume", cfg)
    effective, drift = effective.revalidate(cfg, workspace=workspace)
    if not effective.workspace_root:
        raise InvalidAuthority("authority has no workspace root")
    return effective, drift
