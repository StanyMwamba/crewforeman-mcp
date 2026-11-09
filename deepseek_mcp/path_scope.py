"""Path classification and change scoping (DESIGN 3120 S4, 9.1-9.3).

Shared by ``gate.py`` and ``plan_contract.py``. Pure, stdlib-only helpers that
classify repository-relative paths into one of four categories:

  product       - real product code / docs / config
  artifact      - disposable build/run output
  split_fixture - the 3.11.0 gate/integration/vendor test fixtures
  plan          - the configured PLAN.json contract file

Split fixtures are deliberately NOT artifacts: they count as product for
lint/tests and are only skipped by the secret scan.
"""
from __future__ import annotations

import re

CATEGORY_PRODUCT = "product"
CATEGORY_ARTIFACT = "artifact"
CATEGORY_SPLIT_FIXTURE = "split_fixture"
CATEGORY_PLAN = "plan"

DEFAULT_SPLIT_FIXTURES = [
    "tests/test_v3110_gate.py",
    "tests/test_v3110_integration.py",
    "tests/test_v3110_vendor.py",
    # Older 3.11 redaction/render tests also contain split fake credentials;
    # they are known fixtures, not product leaks.
    "tests/test_p3_vendor_error.py",
    "tests/test_v340_render.py",
]

DEFAULT_ARTIFACT_GLOBS = [
    "logs/**",
    "**/logs/**",
    "**/*.log",
    "**/*.diff",
    "**/*.patch",
    "**/*.rej",
    "**/*.orig",
    "repo-ref/**",
    "**/repo-ref/**",
    ".dsh-tmp-*",
    ".dsh-model-overlay-*",
    "**/cache/**",
    "cache/**",
    "**/coverage/**",
    "coverage/**",
    ".coverage",
    "PLAN.json",
    "MODEL_AUDIT.json",
    "REVIEW*.md",
    "QUALITY_REPORT*.md",
    "WASTE_REPORT.md",
    "**/*.md",
    "docs/**",
    "LICENSE*",
]

_MAX_SAMPLES = 20


def _cfg_get(cfg, key, default=None):
    """Read a config key from a mapping or namespace; never raise."""
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except Exception:
            return default
        return default if value is None else value
    value = getattr(cfg, key, default)
    return default if value is None else value


def normalize_rel(rel):
    """Normalize a path to a forward-slash repo-relative string."""
    if rel is None:
        return ""
    text = str(rel).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    return text


def _glob_to_regex(glob):
    """Translate a glob with ** and * into an anchored regex."""
    glob = normalize_rel(glob)
    out = ["^"]
    index = 0
    length = len(glob)
    while index < length:
        char = glob[index]
        if char == "*":
            if glob[index:index + 2] == "**":
                index += 2
                if index < length and glob[index] == "/":
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = glob.find("]", index + 1)
            if end != -1:
                out.append(glob[index:end + 1])
                index = end + 1
            else:
                out.append(re.escape(char))
                index += 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return re.compile("".join(out))


_GLOB_CACHE = {}


def match_glob(path, glob):
    """Match a repo-relative path against a glob supporting ** and *."""
    path = normalize_rel(path)
    if not path or not glob:
        return False
    key = normalize_rel(glob)
    regex = _GLOB_CACHE.get(key)
    if regex is None:
        regex = _glob_to_regex(key)
        _GLOB_CACHE[key] = regex
    return bool(regex.match(path))


def match_any(path, globs):
    """True when path matches any glob in globs."""
    if not globs:
        return False
    return any(match_glob(path, glob) for glob in globs)


def split_fixture_paths(cfg=None):
    raw = _cfg_get(cfg, "gate_split_fixture_paths", None)
    if raw is None:
        return list(DEFAULT_SPLIT_FIXTURES)
    return [normalize_rel(item) for item in raw if item]


def artifact_globs(cfg=None):
    raw = _cfg_get(cfg, "gate_noproduct_globs", None)
    if raw is None:
        return list(DEFAULT_ARTIFACT_GLOBS)
    return [normalize_rel(item) for item in raw if item]


def is_plan_path(rel, plan_rel="PLAN.json"):
    path = normalize_rel(rel)
    plan = normalize_rel(plan_rel or "PLAN.json")
    if not path:
        return False
    return path == plan


def is_split_fixture(rel, cfg=None):
    path = normalize_rel(rel)
    return bool(path) and path in split_fixture_paths(cfg)


def is_artifact(rel, cfg=None):
    path = normalize_rel(rel)
    if not path:
        return False
    if is_plan_path(path):
        return True
    return match_any(path, artifact_globs(cfg))


def classify_path(rel, cfg=None, plan_rel="PLAN.json"):
    """Classify one repo-relative path. Plan wins over artifact."""
    path = normalize_rel(rel)
    if not path:
        return CATEGORY_PRODUCT
    if is_plan_path(path, plan_rel):
        return CATEGORY_PLAN
    if is_split_fixture(path, cfg):
        return CATEGORY_SPLIT_FIXTURE
    if match_any(path, artifact_globs(cfg)):
        return CATEGORY_ARTIFACT
    return CATEGORY_PRODUCT


def common_leading_dir(paths):
    """Return the one top-level directory that prefixes every path.

    ``["repo/a.py", "repo/b.py"]`` -> ``"repo"``.  Returns ``""`` when the
    paths disagree, when one lives at the root, or when the list is empty.
    Used to relate repo-relative plan entries to workspace-relative diffs when
    the product lives in one subdirectory of the workspace (for example
    ``repo/``).
    """
    heads = set()
    for raw in paths or []:
        path = normalize_rel(raw)
        if not path or "/" not in path:
            return ""
        heads.add(path.split("/", 1)[0])
    if len(heads) != 1:
        return ""
    return heads.pop()


def strip_leading_dir(path, prefix):
    """Remove one leading directory segment when it matches ``prefix``."""
    text = normalize_rel(path)
    if not prefix:
        return text
    head = normalize_rel(prefix).rstrip("/")
    if head and text.startswith(head + "/"):
        return text[len(head) + 1:]
    return text


def relative_variants(path, prefix):
    """The path plus its product-relative form (deduplicated, ordered)."""
    text = normalize_rel(path)
    stripped = strip_leading_dir(text, prefix)
    if stripped != text:
        return [text, stripped]
    return [text]


def changed_paths(changes):
    """Collect path strings from change dicts, snapshots or plain strings."""
    out = []
    if not changes:
        return out
    if isinstance(changes, dict):
        if "changes" in changes or "paths" in changes:
            return changed_paths(changes.get("changes") or changes.get("paths"))
        if any(key in changes for key in ("path", "file", "rel")):
            changes = [changes]
        else:
            changes = list(changes.keys())
    for change in changes:
        if change is None:
            continue
        if isinstance(change, str):
            path = change
        elif isinstance(change, dict):
            path = change.get("path") or change.get("file") or change.get("rel")
        else:
            path = getattr(change, "path", None)
        path = normalize_rel(path)
        if path:
            out.append(path)
    return out


class Scope(dict):
    """Bounded scope record; supports both mapping and attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __bool__(self):
        return bool(self.get("product_outputs"))

    @property
    def product(self):
        # A split fixture is not "product code" for the tiny-plan threshold,
        # but it *is* a test change for lint/tests, so callers that ask "did
        # this job change anything testable?" must see it as product.
        return int((self.get("counts") or {}).get(CATEGORY_PRODUCT) or 0) > 0

    @property
    def baseline_known(self):
        return bool(self.get("baseline"))

    def summary(self):
        return {
            "baseline": self.get("baseline"),
            "baseline_missing": bool(self.get("baseline_missing")),
            "product_outputs": bool(self.get("product_outputs")),
            "counts": dict(self.get("counts") or {}),
            "product_paths": list(self.get("product_paths") or []),
            "artifact_paths": list(self.get("artifact_paths") or []),
            "split_fixture_paths": list(
                self.get("split_fixture_paths") or []),
        }


def _new_scope(baseline_known, baseline_missing):
    return Scope(
        baseline="pre_builder" if baseline_known else None,
        baseline_missing=bool(baseline_missing),
        product_outputs=False,
        counts={
            CATEGORY_PRODUCT: 0,
            CATEGORY_ARTIFACT: 0,
            CATEGORY_SPLIT_FIXTURE: 0,
            CATEGORY_PLAN: 0,
        },
        product_paths=[],
        artifact_paths=[],
        split_fixture_paths=[],
    )


def _bucket(paths, cfg, plan_rel):
    buckets = {
        CATEGORY_PRODUCT: [],
        CATEGORY_ARTIFACT: [],
        CATEGORY_SPLIT_FIXTURE: [],
        CATEGORY_PLAN: [],
    }
    seen = set()
    for raw in paths:
        path = normalize_rel(raw)
        if not path or path in seen:
            continue
        seen.add(path)
        category = classify_path(path, cfg, plan_rel)
        buckets[category].append(path)
        if category == CATEGORY_SPLIT_FIXTURE:
            # A fixture is a test change for lint/tests, so the gate's product
            # count includes it; the split_fixture bucket stays exact.
            buckets[CATEGORY_PRODUCT].append(path)
    return buckets


def _finish(scope, buckets):
    scope["counts"][CATEGORY_PRODUCT] = len(buckets[CATEGORY_PRODUCT])
    scope["counts"][CATEGORY_ARTIFACT] = len(buckets[CATEGORY_ARTIFACT])
    scope["counts"][CATEGORY_SPLIT_FIXTURE] = len(
        buckets[CATEGORY_SPLIT_FIXTURE])
    scope["counts"][CATEGORY_PLAN] = len(buckets[CATEGORY_PLAN])
    scope["product_paths"] = buckets[CATEGORY_PRODUCT][:_MAX_SAMPLES]
    scope["artifact_paths"] = buckets[CATEGORY_ARTIFACT][:_MAX_SAMPLES]
    scope["split_fixture_paths"] = buckets[CATEGORY_SPLIT_FIXTURE][
        :_MAX_SAMPLES]
    return scope


def _is_snapshot(value):
    return (isinstance(value, dict) and value
            and "changes" not in value and "paths" not in value)


def _snapshot_diff(before, after):
    paths = []
    for path in sorted(set(before) | set(after)):
        if path not in before or path not in after or before[path] != after[path]:
            paths.append(path)
    return paths


def classify_changes(before, after, cfg=None, plan_rel="PLAN.json"):
    """Classify a workspace change set.

    ``before`` is the pre-builder snapshot (falsy means no baseline).
    ``after`` may be a change list or a snapshot mapping.
    """
    baseline_known = bool(before)
    scope = _new_scope(baseline_known, not baseline_known)
    if not baseline_known:
        # No pre-builder snapshot: do not pretend every file is new. The
        # caller supplies run output/deliverable files through
        # classify_outputs() and scans only those paths.
        paths = []
    elif _is_snapshot(before) and _is_snapshot(after):
        paths = _snapshot_diff(before, after)
    else:
        paths = changed_paths(after)
    buckets = _bucket(paths, cfg, plan_rel)
    scope["product_outputs"] = bool(buckets[CATEGORY_PRODUCT])
    return _finish(scope, buckets)


def classify_outputs(run, cfg=None):
    """Classify run output/deliverable files for the no-baseline fallback."""
    scope = _new_scope(True, False)
    paths = []
    if isinstance(run, dict):
        for key in ("output_files", "deliverable_files"):
            paths.extend(changed_paths(run.get(key) or []))
    elif run:
        paths.extend(changed_paths(run))
    buckets = _bucket(paths, cfg, "PLAN.json")
    scope["product_outputs"] = bool(
        buckets[CATEGORY_PRODUCT] or buckets[CATEGORY_SPLIT_FIXTURE])
    return _finish(scope, buckets)
