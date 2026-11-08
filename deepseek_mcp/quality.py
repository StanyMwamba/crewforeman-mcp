"""Adaptive subagent sizing, Harness task construction and the quality loop.

The actual loop is executed by the Harness lead agent; this module decides how
many subagents to use, writes unambiguous instructions into the task text and
extracts the final quality score from the workspace afterwards.
"""

import hashlib
import os
import re
from pathlib import Path

from .builder_practices import block as builder_practice_block, record as builder_practice_record


DEFAULT_QUALITY_TARGET = 8.5
DEFAULT_QUALITY_ROUNDS = 3
DEFAULT_SUBAGENTS = 16
DEFAULT_SOFT_CAP = 32
DEFAULT_CONCURRENCY = 8

# Automatic starting counts per task type.  The size estimate only raises the
# code count; creative/research work starts at one full default wave.
AUTO_START = {
    "code": {"small": 4, "medium": 4, "large": 8},
    "creative": {"small": 6, "medium": 8, "large": 12},
    "research": {"small": 6, "medium": 8, "large": 12},
}
TASK_TYPE_LABELS = {
    "simple": "simple question",
    "code": "code",
    "creative": "creative/design",
    "research": "research/data",
}
CODE_TERMS = (
    "code", "script", "cli", "api", "server", "function", "library", "package",
    "bug", "refactor", "python", "javascript", "typescript", "program",
    "database", "backend", "frontend", "unit test", "test suite", "compile",
    "deploy", "algorithm", "sdk", "module",
)
CREATIVE_TERMS = (
    "poster", "flyer", "logo", "banner", "brochure", "cover", "design",
    "typography", "layout", "illustration", "brand", "website", "web page",
    "webpage", "html", "css", "landing page", "deck", "slides", "presentation",
    "letter", "document", "report", "essay", "resume", "story", "poem",
)
RESEARCH_TERMS = (
    "research", "analyze", "analyse", "analysis", "study", "survey", "compare",
    "comparison", "sources", "citation", "data", "dataset", "csv", "chart",
    "graph", "statistics", "metrics", "regression", "experiment", "benchmark",
    "audit", "dashboard", "evidence",
)

# Signals that a task produces a real deliverable.
ARTIFACT_WORDS = (
    "poster", "flyer", "brochure", "logo", "banner", "thumbnail", "cover",
    "letter", "document", "report", "essay", "article", "proposal", "resume",
    "cv", "deck", "slides", "presentation", "website", "web page", "webpage",
    "landing page", "html", "css", "app", "application", "script", "cli",
    "program", "code", "api", "server", "database", "dashboard", "chart",
    "spreadsheet", "dataset", "analysis", "model", "book", "chapter", "manual",
    "spec", "specification", "readme", "invoice", "menu", "certificate",
)
DELIVERABLE_WORDS = (
    "deliverable", "deliverables", "section", "sections", "page", "pages",
    "file", "files", "output", "outputs", "requirement", "requirements",
    "acceptance", "checklist", "component", "components", "chapter", "step",
    "steps", "part", "parts", "item", "items",
)
ACTION_WORDS = (
    "build", "create", "make", "write", "design", "redesign", "fix", "debug",
    "implement", "develop", "refactor", "test", "analyze", "analyse", "audit",
    "review", "research", "compare", "generate", "produce", "draft", "improve",
    "optimize", "optimise", "convert", "migrate", "deploy", "plan", "rewrite",
    "summarize", "summarise", "transcribe", "translate", "calculate", "render",
)
SIMPLE_STARTS = (
    "what ", "why ", "who ", "when ", "where ", "which ", "is ", "are ",
    "can ", "could ", "should ", "does ", "do ", "define ", "explain ",
)


def _words(text):
    return re.findall(r"\b[\w'-]+\b", text or "")


def _contains(text, phrase):
    """Word-boundary match for single words, substring match for phrases."""
    if " " in phrase:
        return phrase in text
    return re.search(r"\b%s\b" % re.escape(phrase), text) is not None


_DELIVERABLE_SECTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*{0,2})(?:deliverables?|outputs?|requirements?|"
    r"files?\s+to\s+(?:create|produce|deliver))(?:\*{0,2})\s*:?\s*$", re.IGNORECASE)
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_OUTPUT_FILE_RE = re.compile(
    r"\b[\w./~-]+\.(?:html?|xhtml|css|js|mjs|ts|tsx|jsx|py|png|jpe?g|webp|gif|"
    r"svg|pdf|md|txt|csv|tsv|xlsx?|docx?|pptx?|mp4|mov|zip|json|ya?ml)\b",
    re.IGNORECASE)
_BULLET_STARTS_DELIVERABLE_RE = re.compile(
    r"^(?:deliverable|output|file|section|page|component|chapter|slide|card)s?\b",
    re.IGNORECASE)


def _declared_deliverable_count(text, numbered):
    """Count only *declared* deliverables, not every fact bullet.

    A live sizing case counted the ten fact/role/benefit
    bullets in a recruitment-ad brief as "10 deliverables" and started twelve
    agents.  A deliverable is an item under a Deliverables/Outputs/Requirements
    section, an explicit output file name, or a bullet that literally starts
    with deliverable/output/file/section.  Other bullet lists are requirements
    or facts and must not inflate the size estimate.
    """
    text = text or ""
    lines = text.splitlines()
    section_count = 0
    counted = set()
    in_section = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if _DELIVERABLE_SECTION_RE.match(line):
            in_section = True
            continue
        bullet = _BULLET_LINE_RE.match(line)
        if in_section and bullet:
            section_count += 1
            counted.add(index)
            continue
        if in_section and not bullet:
            # A new heading or a non-bullet sentence closes the section.
            if section_count or re.match(r"^#{1,6}\s", line):
                in_section = False

    inline = 0
    for match in re.finditer(r"(?i)(?:deliverables?|outputs?)\s*:\s*", text):
        # The list may wrap over several lines.  Read up to the next blank line
        # or heading, capped at 8 lines, so a long HTML/PNG declaration counts
        # completely without swallowing unrelated fact bullets.
        chunk = []
        for raw in text[match.end():].splitlines():
            if not raw.strip() or re.match(r"^\s*#{1,6}\s", raw):
                break
            chunk.append(raw)
            if len(chunk) >= 8:
                break
        inline += len(set(_OUTPUT_FILE_RE.findall("\n".join(chunk))))

    global_bullets = 0
    for index, raw in enumerate(lines):
        if index in counted:
            continue
        bullet = _BULLET_LINE_RE.match(raw.strip())
        if not bullet:
            continue
        body = re.sub(r"^[\s*_`#>]+", "", bullet.group(1))
        if _BULLET_STARTS_DELIVERABLE_RE.match(body):
            global_bullets += 1
    return max(1, int(numbered or 0), section_count, inline, global_bullets)


def _task_signals(text, files=None):
    """Extract the cheap, deterministic signals used for sizing."""
    files = files or []
    text = text or ""
    words = _words(text)
    lines = [line for line in text.splitlines() if line.strip()]
    bullet_count = sum(1 for line in lines
                       if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line))
    lower = text.lower()
    artifact_hits = sorted({word for word in ARTIFACT_WORDS if _contains(lower, word)})
    deliverable_hits = sorted({word for word in DELIVERABLE_WORDS if _contains(lower, word)})
    action_hits = sorted({word for word in ACTION_WORDS
                          if re.search(r"\b%s\b" % re.escape(word), lower)})
    question_like = (text.strip().endswith("?")
                     or lower.lstrip().startswith(SIMPLE_STARTS))
    number_words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    }
    number_hits = re.findall(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"\d{1,2})[\s-]*(?:pages?|sections?|files?|deliverables?|items?|"
        r"parts?|slides?|cards?|chapters?|components?)\b", lower)
    numbered = 0
    for item in number_hits:
        value = int(item) if item.isdigit() else number_words.get(item, 0)
        numbered += min(20, value)
    deliverable_count = _declared_deliverable_count(text, numbered)
    return {
        "words": len(words), "lines": len(lines), "bullets": bullet_count,
        "artifacts": artifact_hits, "deliverables": deliverable_hits,
        "actions": action_hits, "files": len(files),
        "deliverable_count": deliverable_count,
        "declared_deliverables": deliverable_count,
        "question_like": question_like,
    }


def _is_simple(signals):
    """True for a short question with no action, file or numbered deliverable.

    Keyword hits such as ``api``, ``html`` or ``csv`` must not turn a
    definitional question into a build task: "What is an API?" still has a
    one-answer shape.  An action verb, a file, a numbered requirement or more
    than a few lines keeps the task in the real-work path.
    """
    if (signals["words"] > 50 or signals["lines"] > 3 or signals["files"]
            or signals["actions"] or signals["bullets"]):
        return False
    # "What is an API? 3 pages" is a question word plus real work.  A declared
    # multi-item deliverable must never fall through the question shortcut.
    if int(signals.get("deliverable_count") or 0) > 1:
        return False
    if signals["question_like"]:
        return True
    return (signals["words"] <= 12 and not signals["artifacts"]
            and not signals["deliverables"])


def _term_score(lower, terms):
    return sum(1 for term in terms if _contains(lower, term))


def _size_estimate(signals):
    """Small/medium/large from prompt length and the number of deliverables."""
    deliverables = max(1, int(signals.get("deliverable_count") or 1))
    if signals["words"] <= 80 and deliverables <= 2:
        size = "small"
    elif signals["words"] >= 220 or deliverables >= 6:
        size = "large"
    else:
        size = "medium"
    return {
        "size": size,
        "words": signals["words"],
        "deliverables": deliverables,
        "files": signals["files"],
        "bullets": signals["bullets"],
    }


def classify_task(prompt, files=None):
    """Classify a task as simple/code/creative/research plus a size estimate.

    Deterministic keyword scoring, with the existing skill detector used only
    as a small bonus.  Ties prefer code, then research, then creative.  A
    real-work prompt with no signal defaults to creative/design.
    """
    text = prompt or ""
    signals = _task_signals(text, files)
    if _is_simple(signals):
        return {
            "task_type": "simple",
            "label": TASK_TYPE_LABELS["simple"],
            "size": _size_estimate(signals),
            "signals": signals,
            "scores": {"code": 0, "creative": 0, "research": 0},
            "reason": ("Simple single-step question (%d words, no action or numbered "
                       "deliverable)." % signals["words"]),
        }

    lower = text.lower()
    scores = {
        "code": _term_score(lower, CODE_TERMS),
        "creative": _term_score(lower, CREATIVE_TERMS),
        "research": _term_score(lower, RESEARCH_TERMS),
    }
    skill = None
    try:
        from .skills import detect_skill
        skill = detect_skill(text)
    except Exception:
        skill = None
    if skill == "code":
        scores["code"] += 3
    elif skill == "data":
        scores["research"] += 3
    elif skill in ("poster", "web"):
        scores["creative"] += 2
    elif skill == "document":
        scores["creative"] += 1

    task_type = "creative"
    best = -1
    for key in ("code", "research", "creative"):
        if scores[key] > best:
            task_type, best = key, scores[key]
    if best <= 0:
        # Real work with no keyword signal: treat it as a design/document task.
        task_type = "creative"
        scores["creative"] = 1
    size = _size_estimate(signals)
    return {
        "task_type": task_type,
        "label": TASK_TYPE_LABELS[task_type],
        "size": size,
        "signals": signals,
        "scores": scores,
        "skill": skill,
        "reason": ("Classified as %s from %d code, %d creative and %d research "
                   "signal(s)." % (TASK_TYPE_LABELS[task_type], scores["code"],
                                   scores["creative"], scores["research"])),
    }


def _is_explicit_run(run):
    """True for a run whose count the caller fixed; history ignores those."""
    choice = run.get("agent_choice") or {}
    if choice.get("explicit") is True or run.get("subagent_source") == "explicit":
        return True
    return "explicitly requested" in str(run.get("subagent_reason") or "").lower()


_SEED_TASK_TYPES = {
    "code": "code",
    "data": "research",
    "poster": "creative",
    "web": "creative",
    "document": "creative",
}
_REGISTRY = None
_REGISTRY_FAILED = False


def _seed_skill_key(name):
    """Return the canonical seed key for *name*, or None when it is not a seed."""
    if not name:
        return None
    try:
        from .skills import normalise_skill_name
        key = normalise_skill_name(name)
        if key in _SEED_TASK_TYPES:
            return key
    except Exception:
        pass
    raw = str(name).strip().lower()
    if raw.endswith(".md"):
        raw = raw[:-3]
    return raw if raw in _SEED_TASK_TYPES else None


def _registry_entry(name):
    """Read a generated skill through the registry, or return None.

    This is deliberately lazy and read-only: a missing registry, a missing
    index or any registry error falls back to the legacy seed mapping.  It never
    creates or saves the registry just to classify history.
    """
    global _REGISTRY, _REGISTRY_FAILED
    if not name:
        return None
    if _REGISTRY is None:
        try:
            from .config import load_config
            from .skill_registry import SkillRegistry
            cfg = load_config()
            registry = SkillRegistry(cfg)
            # Resolve the same directory the registry uses (a writable checkout
            # or the state-dir fallback), then probe for an existing index
            # without calling load()/migrate(): history classification must not
            # create a registry as a side effect.
            if not (Path(str(registry.directory)) / "index.json").is_file():
                _REGISTRY_FAILED = True
                return None
            _REGISTRY = registry
        except Exception:
            _REGISTRY_FAILED = True
            return None
    if _REGISTRY_FAILED:
        return None
    try:
        entry = _REGISTRY.get(name)
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _run_task_type(run):
    """Task type stored on a run, or derived from its prompt/skill (history)."""
    value = run.get("task_type") or run.get("complexity")
    if value == "simple":
        return "simple"
    # An explicit canonical task_type on the record is authoritative. It is how
    # history written by older versions distinguishes a code run that reused a
    # creative skill; only a missing/legacy value falls through to the registry.
    if value in ("code", "creative", "research", "document"):
        return value
    skill = run.get("skill")
    entry = _registry_entry(skill)
    if entry:
        canonical = entry.get("task_type")
        if canonical in ("code", "research", "creative", "document"):
            return canonical
    seed = _seed_skill_key(skill)
    if seed:
        return _SEED_TASK_TYPES[seed]
    if value == "creative-design":
        return "creative"
    if value == "research-data":
        return "research"
    if skill == "code":
        return "code"
    if skill == "data":
        return "research"
    if skill in ("poster", "web", "document"):
        return "creative"
    return classify_task(run.get("prompt") or "")["task_type"]


def _run_utility(run):
    """Quality score minus explicit penalties for stalls, rescues and errors."""
    try:
        score = run.get("quality_score")
        if score is None:
            score = run.get("self_score")
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    status = run.get("status") or "unknown"
    if status in ("ok", "ok_rescued"):
        base = score if score is not None else 8.0
    elif status == "ok_unreviewed":
        base = min(score, 7.5) if score is not None else 7.0
    else:
        base = 3.0
    penalty = 0.0
    if run.get("stalled"):
        penalty += 3.0
    if (run.get("stop_reason") in ("stalled", "time_limit")
            or run.get("timed_out")):
        penalty += 0.5
    if status == "ok_rescued":
        penalty += 0.5
    if status in ("error", "no_balance"):
        penalty += 1.0
    return round(base - penalty, 4), score


WASTE_NUDGE_THRESHOLD = 0.35
WASTE_NUDGE_STEP = 2


def _review_scope_module():
    """Return the review-scope helper (lazy to keep import order simple)."""
    from . import review_scope
    return review_scope


def history_stats(runs, task_type, concurrency=DEFAULT_CONCURRENCY,
                  target=DEFAULT_QUALITY_TARGET):
    """Summarise comparable past runs per starting count.

    A run counts at its exact ``subagents`` value, so its bucket is directly
    comparable with the automatic starting counts.  The utility penalises
    stalled, timed-out, rescued and failed runs; ties prefer the lower mean
    cost, then the shorter mean duration, then the smaller count.
    """
    try:
        concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY
    groups = {}
    total = 0
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        if run.get("kind") and run.get("kind") != "harness":
            continue
        if _is_explicit_run(run):
            continue
        if _run_task_type(run) != task_type:
            continue
        try:
            count = int(run.get("subagents") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        bucket = count
        utility, score = _run_utility(run)
        try:
            cost = float(run.get("estimated_cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            seconds = float(run.get("seconds") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        group = groups.setdefault(bucket, {
            "count": bucket, "runs": 0, "utilities": [], "scores": [],
            "costs": [], "seconds": [], "wastes": [], "stalled": 0,
            "rescued": 0, "errors": 0,
        })
        group["runs"] += 1
        group["utilities"].append(utility)
        if score is not None:
            group["scores"].append(score)
        group["costs"].append(cost)
        group["seconds"].append(seconds)
        try:
            waste = (run.get("waste_report") or {}).get("waste_ratio")
            if waste is None:
                waste = (run.get("metrics") or {}).get("waste_ratio")
            if waste is not None:
                group["wastes"].append(min(1.0, max(0.0, float(waste))))
        except (TypeError, ValueError, AttributeError):
            pass
        if run.get("stalled") or run.get("stop_reason") == "stalled":
            group["stalled"] += 1
        if run.get("status") == "ok_rescued" or run.get("rescued"):
            group["rescued"] += 1
        if run.get("status") in ("error", "no_balance"):
            group["errors"] += 1
        total += 1

    rows = []
    for bucket in sorted(groups):
        group = groups[bucket]
        runs_n = group["runs"]
        mean_score = (round(sum(group["scores"]) / len(group["scores"]), 2)
                      if group["scores"] else None)
        rows.append({
            "count": bucket,
            "runs": runs_n,
            "mean_utility": round(sum(group["utilities"]) / runs_n, 2),
            "mean_score": mean_score,
            "mean_cost": round(sum(group["costs"]) / runs_n, 4),
            "mean_seconds": round(sum(group["seconds"]) / runs_n, 1),
            "mean_waste": (round(sum(group["wastes"]) / len(group["wastes"]), 4)
                           if group["wastes"] else None),
            "waste_runs": len(group["wastes"]),
            "stalled": group["stalled"],
            "stall_rate": round(group["stalled"] / float(runs_n), 2),
            "rescued": group["rescued"],
            "errors": group["errors"],
        })
    if not rows:
        return {"runs": 0, "best_count": None, "best": None,
                "groups": [], "target": float(target),
                "mean_waste": None, "waste_runs": 0}
    waste_runs = sum(row["waste_runs"] for row in rows)
    mean_waste = (round(sum((row["mean_waste"] or 0.0) * row["waste_runs"]
                            for row in rows) / float(waste_runs), 4)
                  if waste_runs else None)
    best = min(rows, key=lambda row: (-row["mean_utility"], row["mean_cost"],
                                      row["mean_seconds"], row["count"]))
    return {"runs": total, "best_count": best["count"], "best": best,
            "groups": rows, "target": float(target),
            "mean_waste": mean_waste, "waste_runs": waste_runs}


def choose_agents(prompt, files=None, explicit=None, soft_cap=DEFAULT_SOFT_CAP,
                  default_subagents=DEFAULT_SUBAGENTS,
                  concurrency=DEFAULT_CONCURRENCY, runs=None):
    """Choose the starting subagent count for one builder run.

    Explicit ``subagents`` wins exactly (including 0 and values above the
    concurrency; the wave plan still caps each wave).  Otherwise the task is
    classified as simple/code/creative/research, sized, and started at the
    per-type baseline.  Past runs of the same type may nudge that baseline by
    at most one small step (2 agents), based on a stall-penalised utility.
    The result never exceeds the detected concurrency in one wave.
    """
    files = files or []
    classification = classify_task(prompt, files)
    task_type = classification["task_type"]
    size = classification["size"]
    signals = classification["signals"]
    try:
        concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY

    if explicit is not None:
        count = max(0, int(explicit))
        if count == 0:
            reason = ("Caller explicitly requested a single agent (subagents=0). "
                      "Exact count kept; task type %s." % classification["label"])
            complexity = "single"
        else:
            waves = _wave_sizes(count, concurrency)
            reason = (
                "Caller explicitly requested %d subagent(s); exact count kept. "
                "Task type %s (%s size: %d words, %d deliverable(s)). Waves at "
                "concurrency %d: %s." % (
                    count, classification["label"], size["size"], size["words"],
                    size["deliverables"], concurrency,
                    " + ".join(str(item) for item in waves))
            )
            complexity = "explicit"
        return {
            "subagents": count, "reason": reason, "complexity": complexity,
            "task_type": task_type, "size": size, "start_subagents": count,
            "concurrency": concurrency, "signals": signals,
            "waves": _wave_sizes(count, concurrency),
            "history": {"runs": 0, "best_count": None, "best": None,
                        "groups": [], "target": DEFAULT_QUALITY_TARGET},
            "agent_choice_reason": reason,
        }

    try:
        soft = max(0, int(soft_cap if soft_cap is not None else DEFAULT_SOFT_CAP))
    except (TypeError, ValueError):
        soft = DEFAULT_SOFT_CAP
    try:
        default = max(0, int(default_subagents
                             if default_subagents is not None else DEFAULT_SUBAGENTS))
    except (TypeError, ValueError):
        default = DEFAULT_SUBAGENTS

    if task_type == "simple":
        reason = ("Simple single-step question (%d words, no deliverable or file): "
                  "use one agent, no subagents. Task type simple." % signals["words"])
        return {
            "subagents": 0, "reason": reason, "complexity": "simple",
            "task_type": "simple", "size": size, "start_subagents": 0,
            "concurrency": concurrency, "signals": signals, "waves": [],
            "history": {"runs": 0, "best_count": None, "best": None,
                        "groups": [], "target": DEFAULT_QUALITY_TARGET},
            "agent_choice_reason": reason,
        }

    if soft == 0 or default == 0:
        reason = ("Automatic subagents are disabled (soft cap %d, "
                  "default_subagents %d); this run uses one agent. Task type %s."
                  % (soft, default, classification["label"]))
        return {
            "subagents": 0, "reason": reason, "complexity": "real_work",
            "task_type": task_type, "size": size, "start_subagents": 0,
            "concurrency": concurrency, "signals": signals, "waves": [],
            "history": {"runs": 0, "best_count": None, "best": None,
                        "groups": [], "target": DEFAULT_QUALITY_TARGET},
            "agent_choice_reason": reason,
        }

    cap = max(1, min(default, soft))
    start = AUTO_START[task_type][size["size"]]
    start = max(1, min(start, cap))

    floor = 2 if task_type == "code" else 4
    floor = min(floor, cap)
    history = history_stats(runs or [], task_type, concurrency=concurrency)
    adjusted = start
    history_note = None
    matched_smaller = False
    if history["runs"] and history["best_count"] is not None:
        best = int(history["best_count"])
        best_row = history.get("best") or {}
        target = float(history.get("target") or DEFAULT_QUALITY_TARGET)
        bucket_runs = int(best_row.get("runs") or 0)
        try:
            mean_utility = float(best_row.get("mean_utility") or 0.0)
        except (TypeError, ValueError):
            mean_utility = 0.0
        try:
            mean_score = (float(best_row["mean_score"])
                          if best_row.get("mean_score") is not None else None)
        except (TypeError, ValueError):
            mean_score = None
        strong = (bucket_runs >= 1 and mean_utility >= target
                  and (mean_score is None or mean_score >= target)
                  and not best_row.get("stalled")
                  and not best_row.get("errors")
                  and not best_row.get("rescued"))
        if best < start and strong:
            # A smaller bucket with a target-clearing score is strong evidence
            # of over-provisioning; one comparable run is enough to match it.
            # This is the live VIP-ad case: 12 agents chosen while a 6-agent run
            # scored 9.00/10 with zero stalls.
            adjusted = max(floor, min(cap, best))
            matched_smaller = True
            history_note = (
                "History matched the smaller best count %d (%.2f utility, "
                "%s score, %d%% stalled)." % (
                    best, mean_utility,
                    "n/a" if mean_score is None else "%.2f" % mean_score,
                    int(round(float(best_row.get("stall_rate") or 0.0) * 100))))
        elif best > start:
            # Raising the count is riskier: require two comparable runs in the
            # larger bucket and a target-clearing utility.
            if bucket_runs >= 2 and mean_utility >= target:
                adjusted = max(floor, min(cap, start + min(2, best - start)))
                history_note = "History nudged the start %+d to %d." % (
                    adjusted - start, adjusted)
            elif mean_utility < target:
                history_note = (
                    "History's best count %d has a mean utility below the %.1f "
                    "target (%.2f), so the start was not raised." % (
                        best, target, mean_utility))
            else:
                history_note = (
                    "History did not change the start (the larger best count "
                    "has only %d run)." % bucket_runs)
        elif best < start and bucket_runs >= 2:
            # Not strong enough to match, but two runs still justify a small
            # downward nudge rather than ignoring the signal entirely.
            adjusted = max(floor, min(cap, start - min(2, start - best)))
            history_note = "History nudged the start %+d to %d." % (
                adjusted - start, adjusted)

    waste_note = None
    mean_waste = history.get("mean_waste")
    if mean_waste is not None:
        try:
            waste_value = float(mean_waste)
        except (TypeError, ValueError):
            waste_value = None
        if (waste_value is not None
                and waste_value >= WASTE_NUDGE_THRESHOLD
                and adjusted > floor):
            waste_step = min(WASTE_NUDGE_STEP, adjusted - floor)
            adjusted -= waste_step
            waste_note = ("Waste ratio %.2f nudged the start -%d to %d."
                          % (waste_value, waste_step, adjusted))

    waves = _wave_sizes(adjusted, concurrency)
    pieces = [
        "Task type %s (%s size: %d words, %d deliverable(s), %d file(s)). "
        "Baseline start %d." % (
            classification["label"], size["size"], size["words"],
            size["deliverables"], size["files"], start),
    ]
    if history["runs"]:
        best_row = history["best"]
        mean_score = ("%.2f" % best_row["mean_score"]
                      if best_row["mean_score"] is not None else "n/a")
        pieces.append(
            "History: %d comparable run(s); best count %d (mean utility %.2f, "
            "mean score %s, mean cost $%.4f, mean %.0f s, %d%% stalled)." % (
                history["runs"], history["best_count"], best_row["mean_utility"],
                mean_score, best_row["mean_cost"], best_row["mean_seconds"],
                int(round(best_row["stall_rate"] * 100))))
        if matched_smaller:
            pieces.append(history_note)
        elif adjusted != start:
            pieces.append("History nudged the start %+d to %d." % (
                adjusted - start, adjusted))
        elif history_note:
            pieces.append(history_note)
        else:
            pieces.append("History did not change the start.")
    else:
        pieces.append("No comparable history yet.")
    if waste_note:
        pieces.append(waste_note)
    pieces.append("Waves at concurrency %d: %s." % (
        concurrency, " + ".join(str(item) for item in waves)))
    reason = " ".join(pieces)
    return {
        "subagents": adjusted, "reason": reason, "complexity": task_type,
        "task_type": task_type, "size": size, "start_subagents": start,
        "concurrency": concurrency, "signals": signals, "waves": waves,
        "history": history, "agent_choice_reason": reason,
    }


def choose_subagents(prompt, files=None, explicit=None, soft_cap=DEFAULT_SOFT_CAP,
                     default_subagents=DEFAULT_SUBAGENTS,
                     concurrency=DEFAULT_CONCURRENCY):
    """Backward-compatible wrapper around :func:`choose_agents`."""
    return choose_agents(prompt, files=files, explicit=explicit, soft_cap=soft_cap,
                         default_subagents=default_subagents,
                         concurrency=concurrency)


def quality_default(subagents, cfg):
    """Quality loop defaults on for real work (subagents > 0)."""
    return bool(subagents) and bool(cfg.quality)


def _wave_sizes(count, concurrency):
    waves = []
    remaining = max(0, int(count))
    while remaining > 0:
        size = min(max(1, int(concurrency)), remaining)
        waves.append(size)
        remaining -= size
    return waves


def plan_escalation(task_type, current_subagents, score, blocking_defects=0,
                    round_no=1, concurrency=DEFAULT_CONCURRENCY,
                    target=DEFAULT_QUALITY_TARGET, explicit=False,
                    soft_cap=None, default_subagents=None):
    """Decide whether the next revision round should add agents/change approach.

    Returns ``None`` when the review already meets the target with no blocking
    defects, otherwise a small JSON-serialisable record with the reason.  An
    explicit caller count is never increased (it is kept exactly); in that case
    the escalation is a different approach at the same count.  Automatic
    escalation is bounded by the smallest of the detected concurrency, the
    operator's ``subagent_soft_cap`` and the ``default_subagents`` ceiling.
    """
    try:
        blocking = int(blocking_defects or 0)
    except (TypeError, ValueError):
        blocking = 0
    try:
        score_value = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_value = None
    if score_value is not None and score_value >= float(target) and blocking == 0:
        return None

    try:
        current = max(0, int(current_subagents or 0))
    except (TypeError, ValueError):
        current = 0
    try:
        cap = max(1, int(concurrency or DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        cap = DEFAULT_CONCURRENCY
    ceilings = [cap]
    for raw in (soft_cap, default_subagents):
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            ceilings.append(value)
    limit = min(ceilings)

    limit_label = "concurrency cap" if limit == cap else "effective agent ceiling"
    if explicit:
        proposed = current
        approach = "different_approach"
        change = ("the caller fixed the count at %d, so keep it and use a "
                  "different approach" % current)
    elif current >= limit:
        proposed = current
        approach = "different_approach"
        change = ("already at the %s of %d, so use a different approach "
                  "rather than more agents" % (limit_label, limit))
    else:
        floor = 4 if task_type in ("creative", "research") else 2
        proposed = min(limit, max(current + 2, floor))
        approach = "more_agents" if proposed > current else "different_approach"
        if proposed > current:
            change = ("escalate from %d to at most %d agents (%s %d)"
                      % (current, proposed, limit_label, limit))
        else:
            change = ("keep %d agents (%s %d) and change the approach"
                      % (current, limit_label, limit))

    score_text = ("%.2f/10" % score_value) if score_value is not None else "no score"
    reason = ("Review round %d scored %s with %d blocking defect(s); %s."
              % (int(round_no), score_text, blocking, change))
    return {
        "round": int(round_no),
        "from": current,
        "to": proposed,
        "score": score_value,
        "blocking_defects": blocking,
        "approach": approach,
        "reason": reason,
        "explicit": bool(explicit),
        "concurrency": cap,
        "soft_cap": soft_cap,
        "default_subagents": default_subagents,
        "agent_ceiling": limit,
    }


_DEFECT_BUCKETS = (
    (("overlap", "collision", "superimpos"), "overlap"),
    (("missing", "absent", "omitted", "not present"), "missing required fact"),
    (("qr", "qrcode", "barcode"), "qr code"),
    (("overflow", "clipp", "cut off", "cut-off"), "overflow or clipping"),
    (("font size", "too small", "illegible", "contrast", "minimum"), "type size or contrast"),
    (("crash", "exception", "traceback", "unhandled"), "crash"),
    (("empty", "zero-byte", "zero byte", "blank"), "empty deliverable"),
    (("failing test", "test failure", "test fails", "test failed", "unit test",
      "build failed", "build fails", "lint"), "failing test or build"),
    (("broken", "unreadable", "corrupt", "cannot open", "does not open"), "broken artifact"),
)

_DEFECT_ADVICE = {
    "overlap": "reserve separate vertical bands and inspect a rendered screenshot for collisions",
    "missing required fact": "copy every required fact verbatim from the brief and grep the final deliverable",
    "qr code": "decode the QR from the final rendered file, not just visually",
    "overflow or clipping": "check scrollWidth/scrollHeight and render at the exact target size",
    "type size or contrast": "keep body text at least 10 pt and legal text at least 7 pt at final print size",
    "empty deliverable": "verify every deliverable exists and is non-empty before returning",
    "failing test or build": "run the documented build/test command and paste the result in the final answer",
    "crash": "reproduce the failing input and handle the error path explicitly",
    "broken artifact": "open the final artifact with the target tool, not only the source file",
}


def defect_kind(text):
    """Return a stable short kind for a reviewer defect sentence."""
    lower = str(text or "").lower()
    for terms, label in _DEFECT_BUCKETS:
        for term in terms:
            if term in lower:
                return label
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", lower)
    return " ".join(tokens[:6]) or "unspecified"


def _run_blocking_kinds(run):
    kinds = set()
    for entry in run.get("review_rounds") or []:
        if entry.get("kind") != "review":
            continue
        try:
            blocking = int(entry.get("blocking_defects") or 0)
        except (TypeError, ValueError):
            blocking = 0
        if blocking <= 0:
            continue
        for defect in entry.get("defects") or []:
            if str(defect).strip():
                kinds.add(defect_kind(defect))
    return kinds


def _skill_for_run(run, task_type):
    skill = run.get("skill")
    entry = _registry_entry(skill)
    if entry:
        key = entry.get("key")
        if key:
            return key
    seed = _seed_skill_key(skill)
    if seed:
        return seed
    if skill in ("poster", "web", "document", "code", "data"):
        return skill
    return {"code": "code", "research": "data",
            "creative": "poster"}.get(task_type, "document")


def recurring_lessons(run, past_runs=None, target=DEFAULT_QUALITY_TARGET):
    """Append one lesson per defect kind seen in >= 2 runs of this task type.

    ``run`` is the current (unsaved) run; ``past_runs`` are earlier records
    from the run log.  The caller writes the returned lessons through
    ``skills.append_lesson``, which sanitises and de-duplicates them.
    """
    task_type = _run_task_type(run)
    current = _run_blocking_kinds(run)
    if not current:
        return []
    counts = dict((kind, 1) for kind in current)
    for past in past_runs or []:
        if not isinstance(past, dict):
            continue
        if past is run:
            continue
        if run.get("id") and past.get("id") == run.get("id"):
            continue
        if _run_task_type(past) != task_type:
            continue
        for kind in _run_blocking_kinds(past):
            counts[kind] = counts.get(kind, 0) + 1
    skill = _skill_for_run(run, task_type)
    lessons = []
    for kind in sorted(counts):
        if counts[kind] < 2:
            continue
        advice = _DEFECT_ADVICE.get(
            kind, "add a pre-flight check for this defect kind and verify the rendered result")
        lessons.append({
            "task_type": task_type,
            "kind": kind,
            "runs": counts[kind],
            "skill": skill,
            "lesson": ("Recurring %s defect (%s): %s." % (task_type, kind, advice)),
        })
    return lessons


def _creative_builder_contract():
    return (
        "\n\n## Visual-impact contract (creative/design)\n"
        "Accuracy and readability are the floor, not the finish line. A previous ad "
        "scored 8.8/10 for accuracy/readability and was still rejected by the user as "
        "visually plain. Design for impact before polishing details.\n"
        "- Before building, choose 2-3 genuinely distinct directions (for example "
        "editorial, Swiss grid, art nouveau, bold print). Do not spend the wave on "
        "eight variants of one layout.\n"
        "- In one line per direction state: focal point, 2-3 colour system, type "
        "pairing, and the single image/graphic device.\n"
        "- Build the direction with the strongest visual idea first. Use a deliberate "
        "focal point, layered composition (type/image/negative space), generous "
        "whitespace, and display type with character. Avoid the default-template look: "
        "centered headline over a stock hero, one flat colour block, generic sans.\n"
        "- If the result could be mistaken for a default template, change composition, "
        "type or colour before rendering.\n"
        "- Render the chosen visual at the exact target size and inspect it with "
        "read_image at phone scale. Ask: would this stop a scroll? Does it look "
        "designed for this brand, not generated?\n"
        "- Add `Visual impact: X/10` and one sentence naming the strongest and weakest "
        "visual element to QUALITY_REPORT.md.\n"
    )


def _creative_review_contract():
    return (
        "\n\n## Creative review: visual impact is scored\n"
        "If the deliverable is visual/creative, score visual impact separately and "
        "weight it about 30% of the overall score. Accuracy and readability are "
        "necessary, not sufficient. A correct, readable but plain or generic result - "
        "a default-template layout, weak focal point, flat hierarchy, or no distinctive "
        "type/image treatment - must receive overall 7.5/10 or lower and a defect "
        "`visually plain`. Ask: would this stop a scroll? Does it look designed for "
        "this brand? Name the strongest and weakest visual element with a file or crop "
        "path. In REVIEW.md include `Visual impact: X/10`, `Overall: X/10` and "
        "`Final quality score: X/10`; the final-score line is the one the connector "
        "reads.\n"
    )


def _creative_revision_contract():
    return (
        "\n\n## Creative revision\n"
        "If a blocking or non-blocking defect is visual impact, change composition, "
        "type or colour treatment. Do not only rewrite copy or nudge spacing; a plain "
        "layout needs a different visual idea, and the revision must say which one it "
        "chose.\n"
    )


def build_harness_task(prompt, subagents, reason, skill=None, quality=True,
                       quality_rounds=DEFAULT_QUALITY_ROUNDS,
                       quality_target=DEFAULT_QUALITY_TARGET,
                       concurrency=DEFAULT_CONCURRENCY,
                       independent_review=True, render_hint=None,
                       task_type=None, builder_practices=True,
                       project_map_hint=None, plan_block=None,
                       failure_note=None, extra_sections=None):
    """Return the full task text sent to ``dsh --profile headless``."""
    parts = [prompt.strip()]
    if plan_block:
        parts.append("\n\n%s\n" % str(plan_block).strip())
    if project_map_hint:
        parts.append("\n\n## Persisted project map (connector)\n\n%s\n"
                     % str(project_map_hint).strip())
    try:
        concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY

    count = int(subagents) if subagents else 0
    if count > 0:
        waves = _wave_sizes(count, concurrency)
        wave_text = " + ".join(str(size) for size in waves)
        noun = "subagent" if count == 1 else "subagents"
        parts.append(
            "\n\nWork method (concurrency limit first): use %d %s in total, split into "
            "waves of AT MOST %d (%s). Harness allows at most %d children alive at once: never "
            "put more than %d agents in a single parallel() block and never start a new child "
            "while %d are still running, or Harness rejects it (ACTIVATION_LIMIT_REACHED) or "
            "hangs. Each subagent takes a different approach or part of the task; compare and "
            "combine their results into the single best final answer. Chosen automatically: %s. "
            "Save final deliverables as files in the working folder and list their names in "
            "your final answer. Run each wave in its own `workflow` tool call; never put two "
            "waves in one call."
            % (count, noun, concurrency, wave_text, concurrency, concurrency, concurrency, reason)
        )
        if count > 1:
            first_wave = waves[0] if waves else count
            section = (
                "\n\n## Pipelined, learning waves\n"
                "Run the waves strictly one after another: each wave gets its own `workflow` "
                "tool call with exactly one `parallel()` block, and a wave is finished only "
                "when its result file is on disk. Do not run the same wave again; never put "
                "two waves in one `workflow` script. Every wave after the first must improve "
                "on the previous one; a later wave is a new `workflow` call, not another async "
                "task in the same script.\n"
                "Time budget: each builder gets 6 minutes of wall-clock time and at most 12 "
                "tool calls; the whole wave must return within 7 minutes (under the default "
                "480 s stall watchdog). The first write "
                "must be the candidate file (a rough draft is fine); then write the result and "
                "return. A `parallel()` barrier waits for every child, so one slow child stalls "
                "the whole wave: never let a single browser/render command or retry loop run "
                "unbounded, and do not wait for a perfect candidate before saving it.\n"
                "1. Wave 1: make ONE `workflow` call containing exactly one `parallel()` block "
                "of %d builder `agent()` calls (never more than %d children alive at once, or "
                "Harness rejects it with ACTIVATION_LIMIT_REACHED or hangs). Each builder writes "
                "a distinct candidate file (or part) FIRST and returns a one-paragraph result "
                "with its file path. Every builder must return within its 6-minute budget even "
                "if QA is incomplete; a builder that cannot finish must still write its "
                "candidate file and report the limitation. Save every builder's result and "
                "path to `wave-1-results.md` as soon as the wave returns, before starting the "
                "next wave.\n"
                % (first_wave, concurrency)
            )
            section += (
                "2. When wave 1 returns and `wave-1-results.md` exists, spawn a SUB-LEADER "
                "subagent in its own call. Give it the wave-1 file paths (not contents) and ask "
                "for a compact structured summary in `wave-1-summary.md`: `{wave, "
                "best_candidate_path, strengths, defects, discarded_approaches, next_seed}`. "
                "The lead reads only summaries, never all drafts.\n"
            )
            if len(waves) > 1:
                section += (
                    "3. Wave 2: after `wave-1-summary.md` is saved, make a NEW `workflow` call "
                    "for wave 2 only, seeded with the summary's `best_candidate_path` plus its "
                    "defect list. Tell every wave-2 builder to improve that candidate and fix "
                    "those defects, not start over. Use one `parallel()` block of at most %d "
                    "builders and never more than %d children alive at once; save each wave's "
                    "results to `wave-2-results.md` before starting the next wave.\n"
                    % (waves[1], concurrency)
                ) + (
                    "4. Repeat for each later wave: one new `workflow` call; seed it with the "
                    "best candidate and defect list; keep the best candidate forward; save "
                    "`wave-<n>-results.md` before the next call.\n"
                    "5. If a wave hangs or errors, do not restart it and do not re-run an "
                    "earlier wave. Record it as hung, keep the files it already wrote, and "
                    "continue with the waves already finished. If a wave hangs, continue with "
                    "the waves already finished and build the final deliverables from their "
                    "best candidate; a hung wave must never block the run.\n"
                )
            else:
                section += (
                    "3. This run has a single wave: use the ONE `workflow` call above, then the "
                    "SUB-LEADER summary. If that call hangs, do not restart it; keep any "
                    "candidate files it wrote, and use the summary to consolidate the best "
                    "candidate and fix every listed defect before writing the final deliverables. "
                    "Save the wave's results to `wave-1-results.md`.\n"
                )
            section += (
                "Finally, keep each summary to roughly 200 words. The final answer must name "
                "the best candidate path and list every deliverable file."
            )
            parts.append(section)
            parts.append(
                "\n\n## Team artifacts (connector-owned overlap/waste checks)\n"
                "Before building, write `SLICES.md` in the working folder: one section "
                "per wave and, under it, one entry per slice naming its exact `Writes:` "
                "list of files. Keep write-sets disjoint inside a wave; shared files are "
                "reported as a warning, never a block.\n"
                "At the end, write `WASTE_REPORT.md` with `Agents launched: N`, "
                "`Agents used: N` and `Agents discarded: N` so the connector can size "
                "the next team.\n")
    else:
        parts.append(
            "\n\nWork method: this is a simple task. Do it directly with a single agent; do not "
            "start subagents."
        )

    if task_type == "creative":
        parts.append(_creative_builder_contract())

    # Fixed builder practices, after the work method/creative section and
    # before the render hint and execution-safety contract.
    if builder_practices:
        parts.append(builder_practice_block(task_type))

    if failure_note:
        parts.append("\n\n%s\n" % str(failure_note).strip())
    if extra_sections:
        for section in extra_sections:
            text = str(section or "").strip()
            if text:
                parts.append("\n\n%s\n" % text)

    if render_hint:
        parts.append("\n\n## Render environment (connector probe)\n%s\n" % render_hint)

    # Every builder task, including 0- and 1-agent runs, gets the same hard
    # execution-safety contract: the live-run hang came from unwrapped Chrome
    # processes, not from the number of agents.
    parts.append(
        "\n\n## Execution safety (hard requirement)\n"
        "- Every headless Chrome, browser, server, network or other potentially blocking "
        "command MUST run under a hard shell timeout, e.g. `timeout 60 chrome "
        "--headless=new ... --screenshot=...` (on macOS without coreutils, use "
        "`gtimeout 60 ...` or `perl -e 'alarm 60; exec @ARGV' -- ...`). A tool-level "
        "timeout (`timeoutMs`) is not enough: it does not reliably end the process "
        "group, so the command can keep the child alive after the tool reports a "
        "timeout.\n"
        "- Use a FRESH `--user-data-dir` inside the workspace and set `HOME` and `TMPDIR` "
        "inside the workspace. Chrome cannot create its profile or process-singleton "
        "socket under the sandbox's Application Support/default temp directory, so it can "
        "hang instead of exiting. Kill leftover Chrome processes for that profile and "
        "delete the profile when done.\n"
        "- Make at most 2 Chrome attempts in total. If both fail, stop trying, keep the "
        "HTML candidate and record `render unavailable: <reason>` in the result file. A "
        "failed render is a reported limitation, not a reason to loop.\n"
        "- Do not render every exploratory candidate. Write source/HTML candidates first; "
        "render only the chosen candidate once, after the wave returns, "
        "under the timeout above.\n"
        "- Write the candidate file FIRST, then do optional QA. No unbounded retry loops.\n"
    )
    if count <= 1:
        parts.append(
            "\n\n## Time budget (hard requirement)\n"
            "Finish within 6 minutes of wall-clock time and at most 12 tool calls. Write the "
            "deliverable file FIRST (a rough draft is fine), then run "
            "only the checks needed to make it correct, and return. Do not wait for a "
            "perfect result and do not retry a failed command more than twice: if a render "
            "fails, keep the source file and record `render unavailable: <reason>`.\n"
        )

    if skill and skill.get("content"):
        parts.append(
            "\n\n## Skill checklist: %s\n"
            "Apply this checklist explicitly. Check every item and report anything that cannot "
            "be met.\n\n%s" % (skill.get("title") or skill.get("key"), skill["content"].strip())
        )

    try:
        rounds = max(1, int(quality_rounds))
    except Exception:
        rounds = DEFAULT_QUALITY_ROUNDS

    if quality:
        parts.append(
            "\n\n## Mandatory quality loop (builder self-check)\n"
            "Do not stop at the first plausible draft. Run this loop:\n"
            "(a) Restate the brief as a numbered checklist of requirements and acceptance "
            "criteria before doing the work.\n"
            "(b) Have subagents produce independent candidates (different approaches or parts).\n"
            "(c) Run automatic checks appropriate to the deliverable. For HTML/visuals: render "
            "with headless Chrome under a hard timeout (e.g. `timeout 60 chrome --headless=new "
            "... --screenshot=...`, or `gtimeout`/`perl -e 'alarm 60; exec @ARGV' -- ...` "
            "when `timeout` is absent) with a fresh `--user-data-dir` under the workspace, kill "
            "leftovers, and try at most twice; if the render fails, record the limitation and "
            "keep the HTML instead of retrying variants. Then check overflow and clipping, "
            "overlapping text, minimum font size for print (>= 10 pt body, >= 7 pt legal), "
            "large empty areas, every fact from the brief present, and decode any QR codes. "
            "For code: build, tests and lint. For documents: completeness against the brief, "
            "length and tone. For data: reproduce the pipeline, validate schema and row counts, "
            "and check chart axes, units and uncertainty.\n"
            "(d) Use a separate REVIEWER subagent that did not create the candidate. It must "
            "inspect the rendered output with read_image and score it 0-10 against the "
            "checklist, listing concrete defects with evidence file paths. A blocking defect "
            "is any missing required fact, broken artifact, incorrect result or safety "
            "violation; it cannot be averaged away by a high score.\n"
            "(e) Revise and repeat until the score is >= %.1f/10 and there are zero blocking "
            "defects, or until %d revision %s have been used. Do not let the reviewer "
            "review its own work.\n"
            "(f) Write QUALITY_REPORT.md in the working folder with: the checklist, scores per "
            "round, defects found and fixed, evidence file paths, and the final score. Return "
            "the final score in your answer as 'Final quality score: X/10'. This is your "
            "self-score only; the connector will run an independent reviewer afterwards.\n"
            % (quality_target, rounds, "round" if rounds == 1 else "rounds")
        )

    if independent_review and subagents and int(subagents) > 0:
        parts.append(
            "\n\n## Independent verification by the connector\n"
            "After you finish, a separate fresh Harness agent (new session, same workspace, "
            "read-only intent) will review the deliverable files against the original brief "
            "and this skill checklist. It will not trust your QUALITY_REPORT.md conclusions. "
            "It may run tests and `read_image`, then return its own 0-10 score, blocking "
            "defects with evidence, and a fix list; the connector persists that report as "
            "REVIEW.md. If its score is below "
            "%.1f or it finds a blocking defect, a revision run will address only that fix "
            "list and the connector will review again (up to %d review round(s)). Therefore: "
            "make the deliverables self-contained, put evidence paths in QUALITY_REPORT.md, "
            "and list every output file in your final answer.\n"
            % (quality_target, rounds)
        )

    if quality:
        parts.append(
            "\n\nSave all deliverables (including QUALITY_REPORT.md) in the working folder. "
            "In the final answer list every file written and include the final quality score."
        )
    else:
        parts.append(
            "\n\nSave all deliverables in the working folder. In the final answer list every "
            "file written."
        )
    return "".join(parts)


def build_finisher_task(prompt, candidate_files, stop_reason="stalled",
                        stopped_after=None, skill=None, quality=True,
                        quality_target=DEFAULT_QUALITY_TARGET, limit_seconds=None):
    """Task text for the short rescue run after a stalled/timed-out builder.

    The finisher is deliberately a single agent with a hard call budget: the
    previous attempt already spent its subagent budget and its time, and the
    workspace already holds the candidates.  The task must pick one existing
    candidate, make only minimal fixes, produce and verify the deliverables,
    and stop -- not re-design, re-audit or create new browser profiles.
    """
    files = [str(name) for name in (candidate_files or [])][:40]
    if stop_reason == "stalled":
        reason = "stalled with no progress events"
    elif stop_reason == "time_limit":
        reason = "hit its time limit"
    else:
        reason = "stopped early"
    if stopped_after is not None:
        reason += " after %s s" % stopped_after
    limit_note = ""
    try:
        if limit_seconds is not None and float(limit_seconds) > 0:
            limit_note = (" (the connector may stop you at %d s; write the final "
                          "answer before then)" % int(float(limit_seconds)))
    except (TypeError, ValueError):
        limit_note = ""
    parts = [
        "You are the FINISHER. This is a rescue, not a normal run. The previous builder "
        "attempt stopped early: it %s. Its process is no longer running, but its files "
        "are still in this workspace.\n\n"
        "## Hard budget: finish, do not perfect\n"
        "- Work as one agent: 0 subagents, no `workflow`, no `parallel()` and no "
        "delegation. This is a rescue, not a rebuild.\n"
        "- Hard cap: at most 12 tool calls in total. Every read, bash, write, edit and "
        "read_image call counts.\n"
        "- Hard cap: at most 8 minutes of wall-clock time%s. Stop at 12 calls or minute "
        "8, even if the result is imperfect, and write the final answer.\n"
        "- No network: do not download browsers, fonts, packages or assets. Use only what "
        "is already installed.\n"
        "- **No redesign.** Do not redesign anything. Do not change the chosen concept, "
        "layout, palette or type system, and do not start over.\n"
        "- **No new browser profiles.** Do not create, inspect or repair persistent "
        "browser profile directories. If Chrome is unavoidable, use one fresh throwaway "
        "`--user-data-dir` for a single command, kill the process you started, and delete "
        "the directory in that same command. Never leave a browser process running.\n"
        "- **No new audits.** Do not write or run audit scripts, DOM-metric scripts, "
        "contrast checkers, screenshot matrices or new evidence files. One `read_image` of "
        "the final visual is the only inspection allowed.\n"
        "- Do not write `REVIEW.md`; the independent reviewer owns that file.\n\n"
        "## Original brief\n\n%s\n" % (reason, limit_note, (prompt or "").strip())
    ]
    if files:
        parts.append(
            "\n\n## Existing candidate files (relative to this workspace)\n\n%s\n"
            "\nUse the existing files. Read at most the 2 most promising candidates, not all "
            "of them; skip `BRIEF.md`, helper scripts, logs and reports. They may be partial, "
            "duplicated or contradictory. Pick the single best existing candidate, or the "
            "most complete one if none is clearly best. Do not merge candidates and leave "
            "unused candidates untouched.\n"
            % "\n".join("- `%s`" % name for name in files)
        )
    else:
        parts.append(
            "\n\n## Existing candidate files\n\nNone were listed. Do not search the whole "
            "disk: inspect this workspace once. If no candidate exists, write the simplest "
            "correct deliverable directly from the brief with the fewest tool calls, then "
            "verify it as in step 4.\n"
        )
    if skill and skill.get("content"):
        parts.append(
            "\n\n## Skill checklist: %s\n\n"
            "Use this only as a final sanity list for the deliverable you picked. It is "
            "not a licence to audit or rebuild.\n\n%s\n"
            % (skill.get("title") or skill.get("key"), skill["content"].strip())
        )
    parts.append(
        "\n\n## Method (time-boxed, minimal)\n"
        "1. Read the brief and note the exact deliverable paths it names in one short "
        "list. Do not add deliverables it does not name. If no candidate exists, skip "
        "steps 2 and 3 and build the simplest correct deliverable directly.\n"
        "2. Pick the single best existing candidate (or the most complete one) and keep "
        "its file name where it is already correct. Do not spend calls looking for a "
        "better starting point.\n"
        "3. Make at most three minimal fixes: a missing required fact, a wrong path or "
        "name, or an obvious break that stops the deliverable working. Do not add a new "
        "section, style, illustration, font or feature that the brief did not ask for.\n"
        "4. Produce every final deliverable the brief asks for, in this workspace, using "
        "its exact file names and formats. Put every potentially blocking command under "
        "a hard shell timeout, e.g. `timeout 60 chrome --headless=new ... "
        "--screenshot=...` (or a `gtimeout`/Perl alarm wrapper when `timeout` is absent); "
        "use a fresh `--user-data-dir` under the workspace, kill leftovers, and try at "
        "most twice. If the render still fails, keep the existing image/PDF or ship the "
        "HTML and state the exact error in the final answer; do not loop on browser "
        "variants.\n"
        "5. Verify every deliverable exists and is non-empty with `test -s <file>` (or "
        "`wc -c <file>`). A deliverable must exist and have size > 0; do not finish with "
        "a zero-byte or missing file. Fix a missing or empty file with at most two calls; "
        "if it is still missing, say so plainly.\n"
        "6. Stop. Do not run a second audit and do not create another profile; the "
        "connector's independent reviewer owns the review file.\n"
    )
    if quality:
        parts.append(
            "7. Write or refresh `QUALITY_REPORT.md` once, in one write, as your final "
            "file write. Keep it short: what you changed, which files you checked, and "
            "one line `Final quality score: X/10` (target %.1f/10). This is the builder "
            "self-score only; do not revise it or walk the checklist.\n"
            % float(quality_target)
        )
        final_step = 8
    else:
        final_step = 7
    parts.append(
        "%d. End your final answer with exactly this shape: `This is a rescue of an "
        "earlier interrupted attempt.` then `Deliverables:` and list the file names as "
        "relative paths, one per line with its byte size, then `Tool calls used: N of "
        "12.` Do not claim checks you did not run, and do not omit a deliverable that "
        "exists.\n" % final_step
    )
    return "".join(parts)


def build_review_task(prompt, deliverable_files, skill=None,
                      quality_target=DEFAULT_QUALITY_TARGET, round_no=1,
                      task_type=None, browser_evidence=None,
                      review_context=None, prior_defects=None,
                      test_evidence=None, prior_signatures=None,
                      partial=None):
    """Task text for the connector-owned, independent REVIEWER run.

    ``review_context`` is the connector diff (R2), ``prior_defects`` the
    previously flagged blocking defects (R3), and ``test_evidence`` the
    signed connector test result for this exact tree (R1).
    """
    files = list(deliverable_files or [])
    parts = [
        "You are the INDEPENDENT REVIEWER. You did not build these deliverables. A separate "
        "builder agent produced them in this workspace. Do not trust the builder's "
        "QUALITY_REPORT.md, self-score, final answer, or any claim of success; judge only the "
        "files and the original brief below.\n\n"
        "## Original brief\n\n%s\n" % (prompt or "").strip(),
    ]
    _scope = _review_scope_module()
    if _scope is not None:
        try:
            parts.append("\n\n" + _scope.prompt_block().strip() + "\n")
        except Exception:
            pass
    if skill and skill.get("content"):
        parts.append(
            "\n\n## Skill checklist to review against: %s\n\n%s\n"
            % (skill.get("title") or skill.get("key"), skill["content"].strip())
        )
    if files:
        parts.append(
            "\n\n## Deliverable files to inspect (relative to this workspace)\n\n%s\n"
            % "\n".join("- `%s`" % name for name in files)
        )
    else:
        parts.append(
            "\n\n## Deliverable files to inspect\n\nNone were reported. Treat a missing "
            "deliverable as a blocking defect unless the brief explicitly asked for a chat-only "
            "answer.\n"
        )
    if browser_evidence:
        parts.append("\n\n## Browser evidence captured by the connector\n\n%s\n"
                     % str(browser_evidence).strip())
    if task_type == "creative":
        parts.append(_creative_review_contract())
    if test_evidence:
        parts.append("\n\n%s\n" % str(test_evidence).strip())
    if review_context:
        parts.append("\n\n## Connector review context\n\n%s\n"
                     % str(review_context).strip())
    if isinstance(partial, dict):
        areas = [item for item in (partial.get("areas") or [])
                 if isinstance(item, dict)]
        remaining = [item for item in areas
                     if str(item.get("status") or "pending") != "checked"]
        checked = [item for item in areas
                   if str(item.get("status") or "pending") == "checked"]
        carried = [item for item in (partial.get("defects") or [])
                   if isinstance(item, dict)
                   and str(item.get("text") or "").strip()]
        completed = str(partial.get("status") or "") == "completed"
        if completed:
            resumed = (
                "\n\n## Resumed review checkpoint\n\n"
                "A previous reviewer finished round %d but its result was not "
                "durably recorded. Do not discard its findings. Verify the "
                "report and the carried-over findings below, check anything "
                "still uncertain, and return ONE complete `REVIEW.md` that "
                "keeps every carried finding (with its evidence) and adds any "
                "new defects.\n" % int(round_no))
        else:
            resumed = (
                "\n\n## Resumed review checkpoint\n\n"
                "You are resuming review round %d after the connector "
                "interrupted the previous reviewer. Do not restart a full "
                "review and do not redo areas already checked. Re-verify the "
                "carried-over findings, finish the remaining areas, and return "
                "ONE complete `REVIEW.md` that keeps every carried finding "
                "(with its evidence) and adds any new defects.\n" % int(round_no))
        if remaining:
            resumed += "\nRemaining areas to check:\n\n%s\n" % "\n".join(
                "- `%s`" % (item.get("label") or item.get("key") or "area")
                for item in remaining)
        if checked:
            resumed += "\nAlready checked (do not redo): %s\n" % ", ".join(
                "`%s`" % (item.get("label") or item.get("key") or "area")
                for item in checked)
        if carried:
            resumed += (
                "\nEvery carried finding must stay under its original severity "
                "heading: `[blocking]` findings under `## Blocking defects` and "
                "`[non-blocking]` findings under `## Non-blocking defects`. Do not "
                "move a carried finding to the other heading.\n")
            resumed += "\nCarried-over findings to keep and re-verify:\n\n%s\n" % (
                "\n".join("- [%s] %s%s" % (
                    str(item.get("severity") or "blocking").strip(),
                    str(item.get("text")).strip(),
                    (" (evidence: %s)" % item["evidence"])
                    if item.get("evidence") else "")
                    for item in carried))
        parts.append(resumed)
    if int(round_no) > 1:
        defects = [str(item).strip() for item in (prior_defects or [])
                   if str(item).strip()]
        focused = (
            "\n\n## Focused round %d\n"
            "This is a targeted re-review, not another full review. Start from the "
            "connector diff above and verify the previously flagged defects first. "
            "Read surrounding context only for changed files; do not read the whole "
            "repo. Re-run only new or defect-related tests, unless the signed evidence "
            "looks suspicious.\n" % int(round_no)
        )
        if defects:
            focused += "\nPreviously flagged defects to verify:\n\n%s\n" % (
                "\n".join("- %s" % item for item in defects))
        if prior_signatures and not review_context:
            focused += ("\nP3-2 defect signatures from the prior round: %s\n"
                        % ", ".join("`%s`" % str(item)[:12]
                                    for item in prior_signatures
                                    if str(item).strip()))
        parts.append(focused)
    evidence_text = str(test_evidence or "")
    evidence_lower = evidence_text.lower()
    status_match = re.search(r"(?im)^\s*-\s*status:\s*([a-z_]+)\s*$",
                             evidence_text)
    evidence_status = (status_match.group(1).lower()
                       if status_match else "")
    evidence_skipped = evidence_status == "skipped"
    tree_changed = ("tree changed while the suite ran: yes" in evidence_lower
                    or "tree changed while the suite ran: yes (rerun"
                    in evidence_lower)
    evidence_ok = evidence_status == "ok" and not tree_changed
    if evidence_skipped:
        test_step = (
            "0. The connector did not run a test suite for this workspace "
            "(signed evidence status: skipped). Do not claim tests passed. Run only "
            "the relevant or new tests for the changed files; do not run a whole-repo "
            "test discovery unless a changed file has no focused test. "
        )
    elif evidence_ok:
        test_step = (
            "0. The connector already ran the suite for this exact tree (signed "
            "evidence below). "
            "Trust that signed result for the tree hash it names; do not run the full "
            "unittest discovery over the whole repo. Rerun only a new test or a test "
            "directly related to a suspected defect, unless the evidence looks "
            "suspicious. Start from the diff (connector diff below); read only changed files and "
            "immediate dependencies; target <=10 minutes. Round 1 is a full review; "
            "later rounds focus on the flagged defects and the new diff. Do not read "
            "the whole repository when a diff is provided. "
        )
    else:
        test_step = (
            "0. Start from the connector diff and any signed test evidence provided. "
            "If no usable evidence is present, run only the relevant or new tests for "
            "the changed files; do not run a whole-repo test discovery. Read only "
            "changed files and immediate dependencies when a diff is provided. "
        )
    parts.append(
        "\n\n## Live progress checkpoint (connector-owned)\n\n"
        "While you work, print one progress line per event on its own line so the "
        "connector can save incremental evidence even if this review is killed:\n\n"
        "`@@REVIEW_PROGRESS@@ area=<key> | status=<checked|in_progress>`\n\n"
        "`@@REVIEW_PROGRESS@@ defect=<blocking|non-blocking> | area=<key> | "
        "text=<one-line finding> | evidence=<file:line or concrete evidence>`\n\n"
        "`@@REVIEW_PROGRESS@@ test=<command> | status=<ok|failed> | seconds=<number>`\n\n"
        "Area keys: `brief`, `deliverables`, `correctness`, `tests`, `safety`, "
        "and `answer` (chat-only answers). Emit `area=<key> | status=checked` "
        "immediately after you finish an area, and emit a defect line as soon as "
        "you find it. These marker lines are checkpoint data, not part of "
        "`REVIEW.md`: do not include them in the final report.\n\n"
    )
    parts.append(
        "\n\n## Review method\n"
        + test_step + "\n"
        "1. Open the listed deliverable files and the files named in the connector diff. "
        "Do not read the whole repository when a diff is provided. "
        "For images, PDFs and visual output, render or convert as "
        "needed and inspect with `read_image` at the target size; check overflow, clipping, "
        "overlap, minimum type size, empty areas, required facts and QR codes. For code, run "
        "the build/tests and inspect the entry point. For documents, check completeness, facts, "
        "length and tone. For data, reproduce or validate the pipeline, schema, row counts, "
        "units and charts. When browser evidence is provided, check its console errors, failed "
        "network requests and DOM summary as part of the review.\n"
        "2. Score 0-10 against the brief and checklist. A high score cannot average away a "
        "blocking defect. A blocking defect is a missing required fact, a broken or unreadable "
        "artifact, an incorrect result, a failed test that the brief required, or a safety/"
        "privacy violation.\n"
        "3. Do not modify any deliverable and do not write any files with tools. This run is "
        "read-only, so file mutations are rejected by the sandbox. Return the complete "
        "`REVIEW.md` document as your final answer; the connector writes that one file for "
        "you. Do not edit QUALITY_REPORT.md or any builder output. Do not start subagents or a "
        "workflow; you are a single reviewer agent.\n"
        "4. The complete `REVIEW.md` document in your final answer must use exactly these "
        "headings:\n"
        "   `# Independent review`\n"
        "   `## Score` with `Final quality score: X/10`\n"
        "   `## Blocking defects` (bullets, each with a file path and concrete evidence; write "
        "`None` if there are none)\n"
        "   `## Non-blocking defects` (bullets or `None`)\n"
        "   `## Follow-ups (pre-existing, non-blocking)` (bullets for pre-existing "
        "issues in code this change did not touch; every bullet MUST start with "
        "exactly one tag: `[security]`, `[data-loss]` or `[minor]`; `[security]` "
        "and `[data-loss]` are blocking, `[minor]` is non-blocking regardless of "
        "wording; tags must be first on the line; an untagged bullet containing a "
        "security/data-loss keyword is blocking (fail safe); do not repeat "
        "follow-ups under Blocking or Non-blocking defects)\n"
        "   `## Fix list` (ordered, concrete, smallest changes that would pass)\n"
        "5. End your final answer with `Final quality score: X/10` and `Blocking defects: N`. "
        "N counts this change's blocking defects plus every follow-up tagged `[security]` or "
        "`[data-loss]`; do not count `[minor]` follow-ups. An untagged follow-up containing a "
        "security or data-loss keyword is blocking (fail safe) and must be counted, as security "
        "when both kinds of keyword appear.\n"
        "This is review round %d. The target is %.1f/10 with zero blocking defects.\n"
        % (int(round_no), float(quality_target))
    )
    return "".join(parts)



REPROMPT_REPORT_CHARS = 12000


def build_score_reprompt_task(report_text, round_no=1, include_report=True,
                              fresh=False):
    """Ask the same reviewer for the two contract lines it omitted.

    The reply contract is deliberately rigid: ``Final quality score: X/10`` and
    ``Blocking defects: N`` - two lines, no new review.  ``fresh`` is used only
    when the original reviewer session is gone and a short follow-up call must
    carry the report text instead.  The report is head+tail truncated so the
    score/blocking lines and the defect list both survive.
    """
    try:
        number = int(round_no)
    except (TypeError, ValueError):
        number = 1
    report = str(report_text or "").strip()
    if len(report) > REPROMPT_REPORT_CHARS:
        half = REPROMPT_REPORT_CHARS // 2
        omitted = len(report) - (2 * half)
        report = (report[:half].rstrip()
                  + "\n\n[... %d characters of the review report omitted by "
                    "the connector for this follow-up ...]\n\n" % omitted
                  + report[-half:].lstrip())
    if fresh:
        head = (
            "SCORE REPROMPT - fresh reviewer fallback - review round %d.\n\n"
            "The reviewer session that produced the report below is no longer "
            "available. You are a fresh reviewer asked for ONE thing only: read "
            "the report and state the 0-10 score its own findings support and "
            "the blocking-defect count it lists. Do not review the repository, "
            "open files, run tests, start subagents or rewrite the report.\n\n")
    else:
        head = (
            "SCORE REPROMPT (connector follow-up) - review round %d.\n\n"
            "You already finished the independent review below. This is NOT a "
            "new review: do not re-read files, do not re-run tests, do not "
            "inspect the workspace, do not start subagents and do not repeat "
            "the report. Do not change your findings.\n\n")
    task = head % number + (
        "Reply with EXACTLY these two lines and nothing else (no heading, no "
        "bullets, no code fence, no commentary):\n\n"
        "Final quality score: X/10\n"
        "Blocking defects: N\n\n"
        "X must be the 0-10 score supported by the review below and must be "
        "consistent with its findings. N must be the number of blocking "
        "findings: the bullets actually listed under '## Blocking defects' "
        "plus every follow-up tagged `[security]` or `[data-loss]` (0 if there "
        "are none). Do not count `[minor]` follow-ups or other pre-existing "
        "non-blocking follow-ups under '## Follow-ups (pre-existing, "
        "non-blocking)'. An untagged follow-up "
        "that contains a security or data-loss keyword is blocking (fail safe) "
        "and must be counted, as security when both kinds of keyword appear. "
        "Do not invent a defect to explain a score."
    )
    if fresh:
        task += (" If the report does not state an explicit number, derive X "
                 "from the findings it reports (verdict, severity and evidence) "
                 "without re-reviewing the repository. Do not default to 0 or "
                 "10, and do not guess a number the report does not support.")
    task += "\n"
    if include_report and report:
        task += ("\n## Your review report (verbatim, already complete)\n\n"
                 "<<<REVIEW\n%s\nREVIEW>>>\n" % report)
    return task


def build_revision_task(prompt, deliverable_files, fix_list, blocking_defects,
                        skill=None, quality_target=DEFAULT_QUALITY_TARGET,
                        round_no=1, escalation=None, task_type=None,
                        scope_note=None, score_missing=False,
                        builder_practices=True, plan_block=None,
                        failure_note=None, extra_sections=None):
    """Task text for a connector-owned REVISION run after a failed review.

    ``scope_note`` is the operator's manual ``resume_with_scope`` note after a
    P3-2 convergence stop; it narrows the resumed revision instead of letting
    the same historical defect signature stop the cycle before it starts.

    ``score_missing`` marks a review that finished without a parsable
    ``Final quality score: X/10``.  The brief then says the review was
    unscored, lists only the reviewer's real defects, and tells the next round
    to re-review instead of fixing a defect that does not exist.
    """
    files = list(deliverable_files or [])
    parts = [
        "You are the REVISION agent. An independent reviewer (not you) inspected the "
        "deliverables and found blocking defects. Fix exactly those defects and re-run the "
        "relevant checks. Do not rewrite from scratch unless a fix requires it. Do not write "
        "REVIEW.md; the reviewer owns that file.\n\n"
        "## Original brief\n\n%s\n" % (prompt or "").strip(),
    ]
    if plan_block:
        parts.append("\n\n%s\n" % str(plan_block).strip())
    if builder_practices:
        parts.append(builder_practice_block(task_type))
    if failure_note:
        parts.append("\n\n%s\n" % str(failure_note).strip())
    if extra_sections:
        for section in extra_sections:
            text = str(section or "").strip()
            if text:
                parts.append("\n\n%s\n" % text)
    if score_missing:
        parts.append(
            "\n\n## Unscored review (score missing)\n\n"
            "The independent reviewer's report for round %d did not contain a "
            "parseable `Final quality score: X/10` line, so this round has no "
            "numeric quality score. The missing score is a reviewer reporting "
            "failure, not a 0/10 and not a defect in the deliverables. Fix only "
            "the real blocking defects listed below; do not invent or fix a "
            "defect just to explain the missing score. If no real blocking "
            "defect is listed, change nothing and say the deliverables already "
            "passed. The next round runs a fresh independent review, which must "
            "report `Final quality score: X/10` and `Blocking defects: N` "
            "before the run can be accepted.\n" % int(round_no)
        )
    if skill and skill.get("content"):
        parts.append(
            "\n\n## Skill checklist\n\n%s\n" % skill["content"].strip()
        )
    if task_type == "creative":
        parts.append(_creative_revision_contract())
    if blocking_defects:
        parts.append(
            "\n\n## Blocking defects (with reviewer evidence)\n\n%s\n"
            % ("\n".join("- %s" % item for item in blocking_defects))
        )
    if fix_list:
        # Suppress fixes that belong only to non-blocking findings. Keep the
        # full list when no fix can be matched (never drop the actionable
        # list merely because the reviewer phrased it differently).
        selected = list(fix_list)
        if blocking_defects:
            anchors = []
            for defect in blocking_defects:
                anchor = _file_anchor(defect)
                if anchor:
                    anchors.append(anchor)
            if anchors:
                matched = [fix for fix in selected
                           if any(anchor in str(fix) for anchor in anchors)]
                if matched:
                    selected = matched
        parts.append(
            "\n\n## Fix list (address in order)\n\n%s\n"
            % ("\n".join("%d. %s" % (index, item)
                         for index, item in enumerate(selected, 1)))
        )
    if scope_note:
        parts.append(
            "\n\n## Manual resume scope (operator)\n\n"
            "This revision was requested manually after a convergence stop. "
            "Keep the work inside this scope while fixing the blocking "
            "defects above:\n\n%s\n" % str(scope_note).strip()
        )
    if files:
        parts.append(
            "\n\n## Deliverable files to update\n\n%s\n"
            % "\n".join("- `%s`" % name for name in files)
        )
    if escalation:
        count = int(escalation.get("to") or 0)
        from_count = int(escalation.get("from") or 0)
        limit = int(escalation.get("concurrency") or count)
        if count > from_count:
            choice = ("Escalate from %d to at most %d subagents, in waves of at most "
                      "%d children alive at once (never more than %d in one "
                      "`parallel()` block), and/or use a different approach."
                      % (from_count, count, limit, limit))
        else:
            choice = ("Do not add more agents; use a different approach (new layout, "
                      "concept or verification path) at the same count of %d." % count)
        parts.append(
            "\n\n## Escalation for this round\n"
            "%s %s If you use subagents, each must write its file first and finish "
            "within 5 minutes; never exceed the detected concurrency. Record in your "
            "final answer whether you used more agents or a different approach, and why.\n"
            % (escalation.get("reason") or "", choice)
        )
    parts.append(
        "\n\n## Revision method\n"
        "1. Reproduce each blocking defect from the evidence.\n"
        "2. Make the smallest correct change. Keep everything that already passed.\n"
        "3. Re-run the checks the brief and skill checklist require (tests, render, read_image, "
        "data validation).\n"
        "4. Update QUALITY_REPORT.md with what changed and evidence paths; keep it factual.\n"
        "5. Save all deliverables in this workspace and list every changed file in your final "
        "answer. The connector will run another independent review (target %.1f/10, zero "
        "blocking defects) after this revision (round %d).\n"
        % (float(quality_target), int(round_no))
    )
    return "".join(parts)


def _read_report(workspace):
    if not workspace:
        return ""
    path = os.path.join(str(workspace), "QUALITY_REPORT.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return ""


_TARGET_LINE_RE = re.compile(
    r"target|threshold|minimum|at least|required|goal|>=\s*\d|\u2265", re.IGNORECASE)

_VISUAL_IMPACT_RE = re.compile(r"visual\s+impact", re.IGNORECASE)
_SCORE_NUM = r"(\d{1,2}(?:[.,]\d+)?)"
_SCORE_DENOM = r"(?:/\s*10(?!\d)|out\s+of\s+10\b)"
_SCORE_DECOR = " \t*_`|:=\u2013\u2014-"
_SCORE_BULLET_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")

# Words that may introduce a score label without turning it into a category
# score ("The final quality score", "Reviewer's score").  A prefix with any
# other word is a sub-score label ("Test coverage score", "Code quality
# score") or quoted/borrowed text and must not become the official score.
_SCORE_LABEL_LEAD_WORDS = frozenset({
    "a", "an", "the", "this", "that", "my", "our", "your", "their", "its",
    "i", "we", "review", "review's", "reviews", "reviewer", "reviewer's",
    "reviewers", "reviewers'", "report", "report's", "independent",
    "final", "overall",
})
_SCORE_QUOTE_RE = re.compile(r"[\"\u201c\u201d>]")

# Generic table column headers do not turn a row into a category row
# ("| Criterion | Score | 9/10 |").  Any other preceding word ("Test
# coverage", "Code quality") does.
_SCORE_TABLE_HEADER_WORDS = frozenset({
    "metric", "criterion", "criteria", "item", "label", "name", "dimension",
    "measure", "field", "value", "category", "area", "aspect",
})

# A closing bare ``N/10`` is the official score only in an overall-score or
# summary context.  Under ``## Tests``/``## Fix list``/... it is a sub-score
# or an item value, never the review score.
_SCORE_SECTION_FORMS = (
    r"(?:(?:final|overall)\s+)?quality(?:\s+score)?",
    r"(?:(?:final|overall)\s+)?score",
    r"(?:final|overall)",
    r"rating",
    r"scoring",
)

# A parenthetical qualifier may only be dropped when it explicitly says the
# label is the overall 0-10 score.  ``Score (test coverage)`` is a different
# metric and must not collapse to ``Score``.
_SCORE_OVERALL_QUALIFIER_RE = re.compile(
    r"(?:overall|final|"
    r"out\s+of\s+(?:10|ten)|"
    r"[01]\s*[-\u2013\u2014]\s*10|"
    r"[01]\s+to\s+10|"
    r"10[-\s]point(?:\s+scale)?|"
    r"scale\s*[01]\s*[-\u2013\u2014]\s*10)",
    re.IGNORECASE,
)


def _strip_overall_qualifiers(text):
    """Drop ``(...)`` groups only when each names the overall score.

    Returns ``None`` when any group is a category/unknown qualifier so callers
    reject ``Score (test coverage)`` instead of reading it as ``Score``.
    """
    value = text or ""
    for body in re.findall(r"\(([^)]*)\)", value):
        body = re.sub(r"[*_`]", "", body).strip()
        body = re.sub(r"\s+", " ", body)
        if not _SCORE_OVERALL_QUALIFIER_RE.fullmatch(body):
            return None
    cleaned = re.sub(r"\([^)]*\)", " ", value)
    return re.sub(r"\s+", " ", cleaned).strip()


def _score_section_is_overall(section, allow_summary=False):
    """True when a heading names the overall score, not a category sub-score."""
    text = re.sub(r"[*_`]", "", section or "").strip().lower()
    text = text.strip(" :=\t-\u2013\u2014|")
    text = _strip_overall_qualifiers(text)
    if text is None:
        return False
    text = text.strip(" :=\t-\u2013\u2014|")
    if not text:
        return False
    forms = list(_SCORE_SECTION_FORMS)
    if allow_summary:
        forms.extend([
            r"(?:(?:final|overall)\s+)?summary",
            r"(?:final\s+)?verdict",
            r"(?:final\s+)?conclusion",
            r"(?:overall\s+)?assessment",
            r"(?:overall\s+)?evaluation",
            r"bottom\s+line",
            r"review\s+summary",
        ])
    return any(re.fullmatch(form, text) for form in forms)

# Strongest label first.  Strength 3 = explicit final/overall quality score,
# 2 = final/overall score (or table label "Final"), 1 = bare "Score:".
_SCORE_LABEL_PATTERNS = (
    (3, re.compile(r"\b(?:final|overall)\s+quality\s+score\b", re.IGNORECASE)),
    (3, re.compile(r"\bquality\s+score\b", re.IGNORECASE)),
    (2, re.compile(r"\b(?:final|overall)\s+score\b", re.IGNORECASE)),
    (2, re.compile(r"\b(?:final|overall)\b", re.IGNORECASE)),
    (1, re.compile(r"\bscore\b", re.IGNORECASE)),
)


def _line_at(text, position):
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end < 0:
        end = len(text)
    return text[start:end]


def _last_score(pattern, text, skip_targets=False, first_on_final_line=False):
    """3.9.3 helper, unchanged: used by ``visual_impact_score``.

    Keep this behaviour byte-compatible; the stricter official-score logic
    lives in ``extract_quality_score`` below.
    """
    matches = []
    for match in re.finditer(pattern, text, re.IGNORECASE):
        try:
            value = float(match.group(1))
        except Exception:
            continue
        if not (0.0 <= value <= 10.0):
            continue
        line = _line_at(text, match.start())
        if skip_targets and _TARGET_LINE_RE.search(line):
            continue
        matches.append((match.start(), value, line))
    if not matches:
        return None
    if first_on_final_line:
        final_lines = [item for item in matches if re.search(r"\bfinal\b", item[2], re.IGNORECASE)]
        if final_lines:
            return final_lines[0][1]
    matches.sort()
    return matches[-1][1]


def _score_coerce(raw):
    """Return a 0-10 float, or None when the token is out of range."""
    try:
        value = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if 0.0 <= value <= 10.0:
        return value
    return None


def _score_rest_ok(rest):
    """True when the text after a value is decoration/qualifier, not prose.

    ``"9/10 near the end"`` and ``"9.0/10 pt"`` fail; ``"9/10"``,
    ``"9/10."``, ``"9/10 (required: 8.5)"``, ``"9/10 — excellent"`` and
    ``"9/10 Target: 9.5/10"`` pass.
    """
    text = (rest or "").strip()
    stripped = text.strip(_SCORE_DECOR + ".;,!")
    if not stripped:
        return True
    if _TARGET_LINE_RE.search(stripped):
        return True
    if text[:1] in ("(", "["):
        return True
    if text[:1] in ("-", "\u2013", "\u2014", ",", ";", ":"):
        return True
    if re.match(r"^(?:overall|final|total|rating)\b", stripped, re.IGNORECASE):
        return True
    return False


def _score_after_label(line, label_match):
    """Parse the first value that follows a score label on ``line``."""
    tail = (line or "")[label_match.end():]
    # A signed value is never a valid 0-10 score ("-1/10" must not read as
    # 1/10).  A spaced dash remains usable as a label/value separator.
    if re.match(r"^[ \t]*[:=][ \t]*[\u2013\u2014-]\s*\d", tail):
        return None
    if re.match(r"^[ \t]*[\u2013\u2014-]\d", tail):
        return None
    tail = tail.lstrip(_SCORE_DECOR)
    connector = re.match(
        r"(?:is|was|were|of|at|rated|scored)\b[\s*_`:=]*", tail, re.IGNORECASE)
    if connector:
        tail = tail[connector.end():]
    # ``Final quality score (out of 10): 9/10`` is an overall label; a
    # category qualifier in the same position ("(test coverage)") is not.
    qualifier = re.match(r"^[ \t]*\(([^)]*)\)", tail)
    if qualifier:
        body = re.sub(r"[*_`]", "", qualifier.group(1)).strip()
        body = re.sub(r"\s+", " ", body)
        if not _SCORE_OVERALL_QUALIFIER_RE.fullmatch(body):
            return None
        tail = tail[qualifier.end():].lstrip(_SCORE_DECOR)
    match = re.match(r"\s*" + _SCORE_NUM + r"\s*" + _SCORE_DENOM + r"?", tail,
                     re.IGNORECASE)
    if not match:
        return None
    rest = tail[match.end():]
    if re.match(r"\s*/\s*\d", rest):  # e.g. 9.5/100
        return None
    value = _score_coerce(match.group(1))
    if value is None or not _score_rest_ok(rest):
        return None
    return value


def _score_label_allowed(line, match):
    """True when ``match`` starts a whole score label, not a category sub-score.

    ``Final quality score``, ``The final score`` and ``**Score:**`` are whole
    labels.  ``Test coverage score``, ``Code quality score`` and quoted text
    such as ``> Final quality score: 9/10`` are not: their prefix contains a
    category word, a quote or a blockquote marker.
    """
    prefix = (line or "")[:match.start()]
    if _SCORE_QUOTE_RE.search(prefix):
        return False
    cleaned = prefix.strip()
    cleaned = _SCORE_BULLET_RE.sub("", cleaned)
    cleaned = cleaned.strip(_SCORE_DECOR + "()[]{}")
    cleaned = cleaned.lstrip("#").strip()
    if not cleaned:
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", cleaned)
    if not words or len(words) > 3:
        return False
    return all(word.lower() in _SCORE_LABEL_LEAD_WORDS for word in words)


def _score_label_strength(cell):
    """Classify an exact table label cell; 0 means "not a score label"."""
    value = re.sub(r"[*_`]", "", cell or "").strip().lower()
    value = _strip_overall_qualifiers(value)
    if value is None:
        return 0
    value = value.strip(" :=\t-\u2013\u2014|")
    if not value or _VISUAL_IMPACT_RE.search(value):
        return 0
    if re.fullmatch(r"(?:final|overall)\s+quality\s+score", value):
        return 3
    if re.fullmatch(r"quality\s+score", value):
        return 3
    if re.fullmatch(r"quality", value):
        # Two-row tables ("| Criterion | Score |" then "| Quality | 8.5/10 |").
        return 2
    if re.fullmatch(r"(?:final|overall)\s+score", value):
        return 2
    if re.fullmatch(r"(?:final|overall)", value):
        return 2
    if re.fullmatch(r"score", value):
        return 1
    return 0


def _score_cell_value(cell):
    """Parse one table cell; a bare number counts only in a pure number cell."""
    value = (cell or "").strip().strip("*_` ").strip()
    if not value or _VISUAL_IMPACT_RE.search(value):
        return None
    match = re.match(_SCORE_NUM + r"\s*" + _SCORE_DENOM + r"?", value,
                     re.IGNORECASE)
    if not match:
        return None
    rest = value[match.end():]
    if re.match(r"\s*/\s*\d", rest):
        return None
    number = _score_coerce(match.group(1))
    if number is None or not _score_rest_ok(rest):
        return None
    if not re.search(r"/\s*10|out\s+of\s+10", match.group(0), re.IGNORECASE):
        if not re.fullmatch(r"\d{1,2}(?:\.\d+)?", value):
            return None
    return number


def _score_table_context_cell(cell):
    """True when a table cell names a category, target or visual sub-score.

    A score that follows such a cell belongs to that metric, not to the overall
    review ("| Visual impact | Score: 6/10 |").  Empty/decorative cells,
    overall lead words ("Final", "Reviewer") and generic column headers
    ("Criterion", "Metric") do not create category context.
    """
    text = re.sub(r"[*_`]", "", cell or "").strip()
    text = text.strip(" :=\t-\u2013\u2014|")
    if not text:
        return False
    if _VISUAL_IMPACT_RE.search(text) or _TARGET_LINE_RE.search(text):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    if not words:
        return False
    if len(words) > 3:
        return True
    return not all(word.lower() in _SCORE_LABEL_LEAD_WORDS
                   or word.lower() in _SCORE_TABLE_HEADER_WORDS
                   for word in words)


def _score_table_candidate(line):
    """Return ``(value, strength)`` from a pipe-table row, else None."""
    body = (line or "").strip()
    if body.count("|") < 2:
        return None
    cells = [cell.strip() for cell in body.strip("|").split("|")]
    for index, cell in enumerate(cells):
        # Row context is part of the label: a preceding category/target cell
        # ("| Visual impact | Score: 6/10 |") means this is a sub-score, not
        # the official review score.  This applies to both branches below.
        if any(_score_table_context_cell(prev) for prev in cells[:index]):
            continue
        strength = _score_label_strength(cell)
        if strength:
            for other in cells[index + 1:index + 3]:
                if re.match(r"^[\s*_`]*\d", other):
                    # A numeric cell is the value even when it carries an
                    # annotation ("8.8/10 (target: 9.0)").
                    value = _score_cell_value(other)
                    if value is not None:
                        return value, strength
                    continue
                # A category/target cell between the label and the value
                # redefines the metric; a later number is not the overall score.
                if _score_table_context_cell(other):
                    break
                value = _score_cell_value(other)
                if value is not None:
                    return value, strength
        # Label and value in the same cell (e.g. "| Score: 9/10 |").
        for pattern_strength, pattern in _SCORE_LABEL_PATTERNS:
            match = pattern.search(cell)
            if not match:
                continue
            prefix = cell[:match.start()]
            if _VISUAL_IMPACT_RE.search(prefix) or _TARGET_LINE_RE.search(prefix):
                break
            if not _score_label_allowed(cell, match):
                break
            value = _score_after_label(cell, match)
            if value is not None:
                return value, pattern_strength
            break
    return None


def _score_line_candidate(line):
    """Return ``(value, strength)`` from a non-table line, else None."""
    for strength, pattern in _SCORE_LABEL_PATTERNS:
        for match in pattern.finditer(line):
            prefix = line[:match.start()]
            if _VISUAL_IMPACT_RE.search(prefix) or _TARGET_LINE_RE.search(prefix):
                continue
            if not _score_label_allowed(line, match):
                continue
            value = _score_after_label(line, match)
            if value is not None:
                return value, strength
    return None


def _score_is_score_heading(section):
    heading = (section or "").lower()
    if not heading:
        return False
    if _VISUAL_IMPACT_RE.search(heading):
        return False
    if re.search(r"blocking|defect|fix\s*list|\bissues\b|target|threshold|minimum",
                 heading):
        return False
    return _score_section_is_overall(heading)


def _score_standalone_value(line):
    """A bare ``N/10`` value: no prose before or after it."""
    value = (line or "").strip()
    value = _SCORE_BULLET_RE.sub("", value).strip()
    value = value.strip("*_`| ").strip()
    value = value.strip(".;:!,").strip()
    if not value:
        return None
    match = re.match(_SCORE_NUM + r"\s*" + _SCORE_DENOM, value, re.IGNORECASE)
    if not match:
        return None
    if not _score_rest_ok(value[match.end():]):
        return None
    return _score_coerce(match.group(1))


def _score_heading_text(line):
    """Return a heading's text for Markdown or bold pseudo-headings.

    ``## Score`` and a value-less ``**Score:**`` both open a section.  A bold
    line that contains a digit is a score statement, not a heading.
    """
    atx = re.match(r"^#{1,6}\s*(.*?)\s*#*$", line)
    if atx:
        return atx.group(1).strip()
    bold = re.fullmatch(r"\*{1,2}([^*]+?)\*{1,2}:?", line)
    if not bold:
        bold = re.fullmatch(r"_{1,2}([^_]+?)_{1,2}:?", line)
    if bold and not re.search(r"\d", bold.group(1)):
        return bold.group(1).strip()
    return None


def _score_final_line_allowed(section):
    """Whether a bare final ``N/10`` belongs to an overall-score section.

    A report can close with `9.0/10` under `## Score`, `## Summary`, `## Verdict`
    or no heading at all.  A value under `## Visual impact`, `## Target`,
    `## Threshold`, `## Tests`, `## Fix list` or another item heading is a
    sub-score/target/item value, never the official score.
    """
    heading = (section or "").lower().strip()
    if not heading:
        return True
    if _VISUAL_IMPACT_RE.search(heading):
        return False
    if re.search(r"target|threshold|minimum|at\s+least|required|goal", heading):
        return False
    return _score_section_is_overall(heading, allow_summary=True)


def _score_scan_text(text):
    """Classify one text into labelled candidates and safe fallbacks."""
    labelled = {3: [], 2: [], 1: []}
    fallback = []
    section = ""
    last_line = ""
    last_section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _score_heading_text(line)
        if heading is not None:
            # A heading can itself carry the score (``## Score: 9/10``).
            # Whole-label checks still reject category headings.
            if re.match(r"^#{1,6}\s", line):
                candidate = _score_line_candidate(line)
                if candidate is not None:
                    labelled[candidate[1]].append(candidate[0])
                    last_line = line
                    last_section = section
                    continue
            section = heading.lower()
            continue
        if re.fullmatch(r"[-*_=]{3,}", line):
            continue
        last_line = line
        last_section = section
        if line.startswith("|") and line.count("|") >= 2:
            table = _score_table_candidate(line)
            if table is not None:
                labelled[table[1]].append(table[0])
            continue
        candidate = _score_line_candidate(line)
        if candidate is not None:
            labelled[candidate[1]].append(candidate[0])
            continue
        if _score_is_score_heading(section):
            value = _score_standalone_value(line)
            if value is not None:
                fallback.append(value)
    if last_line and _score_final_line_allowed(last_section):
        # A final standalone `N/10` is the reviewer's closing score line, but
        # not when it sits under a visual-impact/target/threshold heading.
        value = _score_standalone_value(last_line)
        if value is not None:
            fallback.append(value)
    return labelled, fallback


def extract_quality_score(workspace=None, final_text=None):
    """Read the final quality score from QUALITY_REPORT.md, then the final text.

    Explicit "final quality score"/"final score"/"Score:" statements win, with
    the last one in document order.  A bare ``N/10`` is trusted only inside a
    Score/Quality section or on the final standalone line, so unrelated text
    ("checked 3/10 images", "font is 9.0/10 pt") and visual-impact sub-scores
    cannot become the official score.  Target/threshold lines are skipped.
    """
    texts = [text for text in (_read_report(workspace), final_text or "") if text]
    if not texts:
        return None
    scans = [_score_scan_text(text) for text in texts]
    for strength in (3, 2, 1):
        for labelled, _fallback in scans:
            values = labelled.get(strength) or []
            if values:
                return round(values[-1], 2)
    for _labelled, fallback in scans:
        if fallback:
            return round(fallback[-1], 2)
    return None


def _section_bullets(text, names):
    """Return bullet lines under the first heading containing one of ``names``."""
    lines = (text or "").splitlines()
    collecting = False
    bullets = []
    for raw in lines:
        line = raw.strip()
        if re.match(r"^#{1,6}\s", line):
            heading = line.lstrip("#").strip().lower()
            is_non_blocking = "non-blocking" in heading or "non blocking" in heading
            if any(name in heading for name in names):
                if is_non_blocking and any("blocking" in name for name in names):
                    if collecting:
                        break
                    continue
                collecting = True
                continue
            if collecting:
                break
            continue
        if not collecting:
            continue
        if re.match(r"^(?:[-*+]|\d+[.)])\s+", line):
            item = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line).strip()
            if item and not re.match(r"^(?:none|no blocking|n/?a|0)\b", item, re.IGNORECASE):
                bullets.append(item)
        elif line and not line.startswith("```"):
            # A plain "None" or "Blocking defects: 0" line closes the section.
            if re.match(r"^(?:none|no blocking|n/?a|0)\b", line, re.IGNORECASE):
                continue
    return bullets


CREATIVE_PLAIN_CAP = 7.5
CREATIVE_VISUAL_FLOOR = 6.0
_PLAIN_NEGATION_RE = re.compile(
    r"(?:\bnot|\bnever|\bno|\bwithout|\bisn['’]?t|\baren['’]?t|"
    r"\bdoesn['’]?t|\bdon['’]?t|\bwasn['’]?t|\bavoid(?:s|ed)?|"
    r"\bnot\s+(?:at\s+all|particularly|especially))\s+(?:\w+\s+){0,3}$",
    re.IGNORECASE)
_PLAIN_ASSERTION_RE = re.compile(
    r"\b(?:is|are|was|were|looks?|appears?|feels?|remains?|seems?|too|"
    r"quite|rather|very|still|scored?|gets?|got|renders?|reads?)\b"
    r"[^.;\n]{0,80}$",
    re.IGNORECASE)


def _plain_occurrences(text):
    """Yield unnegated ``visually plain`` occurrences from ``text``."""
    for match in re.finditer(r"visually\s+plain", text or "", re.IGNORECASE):
        prefix = (text or "")[max(0, match.start() - 50):match.start()]
        if _PLAIN_NEGATION_RE.search(prefix):
            continue
        yield match


def _defect_asserts_plain(defects):
    """A defect bullet that names plainness is already a positive assertion."""
    for item in defects or []:
        if any(True for _ in _plain_occurrences(str(item))):
            return True
    return False


def _review_asserts_plain(text, defects=()):
    """True only for a positive plainness assertion, never for a denial.

    A bullet under a defects heading is positive by placement.  Free prose must
    put a verb near the phrase ("the ad is visually plain"); otherwise a
    sentence such as "the result is not visually plain" would trigger a cap.
    """
    if _defect_asserts_plain(defects):
        return True
    for match in _plain_occurrences(text):
        clause = (text or "")[max(0, match.start() - 90):match.start()]
        if _PLAIN_ASSERTION_RE.search(clause):
            return True
    return False


def visual_impact_score(text):
    """Return the review's separate Visual impact score, if reported."""
    return _last_score(
        r"visual\s+impact\s*(?:score)?\s*[:=\-|]?\s*(\d+(?:\.\d+)?)\s*/\s*10",
        text or "", skip_targets=True)


_LEGACY_SYNTHETIC_SCORE_RE = re.compile(
    r"independent\s+reviewer\s+did\s+not\s+report\s+a\s+0\s*[-\u2013\u2014]\s*10\s+score",
    re.IGNORECASE)


def _is_legacy_synthetic_score_defect(text):
    """True for the 3.9.3 connector-invented blocking defect, not reviewer text."""
    return bool(_LEGACY_SYNTHETIC_SCORE_RE.search(str(text or "")))


def _merge_review_texts(report_text, final_text):
    """Join the report file and the reviewer's answer without doubling it.

    The read-only review flow persists the reviewer's complete final answer as
    ``REVIEW.md`` and then passes both that file text and the same answer to
    this parser.  Concatenating identical/contained copies would count every
    finding twice, so keep only the fuller text in that case.
    """
    first = str(report_text or "").strip()
    second = str(final_text or "").strip()
    if not first:
        return second
    if not second or second == first or second in first:
        return first
    if first in second:
        return second
    return first + "\n" + second


def _dedupe_follow_ups(items):
    """De-duplicate follow-ups by normalised ``(text, blocking, kind)``."""
    seen = set()
    unique = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").lower().split())
        key = (text, bool(item.get("blocking")), str(item.get("kind") or ""))
        if not text or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def parse_review(report_text="", final_text="", task_type=None,
                 prefer_sections=False):
    """Parse an independent REVIEW.md/final answer.

    Returns ``{"score", "blocking_defects", "defects", "fixes"}`` plus the
    visual-impact sub-score and whether the creative cap was applied.  The
    count is explicit when reported, otherwise the number of bullets under the
    "Blocking defects" heading.  On a resumed review the caller passes
    ``prefer_sections=True``: the merged report's heading bullets are then
    authoritative over the reviewer's summary count, so a carried finding the
    merge moved back under its original heading cannot be hidden by a stale
    ``Blocking defects: 0`` line (or promoted by a stale ``1``).

    The prompt asks a creative reviewer to cap an accurate-but-plain result at
    7.5, but a model can ignore that instruction.  The connector therefore
    enforces the cap here: for a creative task, a Visual impact score of 6.0 or
    lower, or a positive "visually plain" assertion/defect, clamps the parsed
    official score to 7.5 so the normal 8.5 acceptance gate starts a revision.
    A denial such as "not visually plain" does not trigger the cap.
    """
    combined = _merge_review_texts(report_text, final_text)
    _scope = _review_scope_module()
    follow_ups = []
    section_text = combined
    if _scope is not None:
        try:
            # The persisted report is authoritative when present: in the
            # read-only flow the final answer was materialised into REVIEW.md,
            # so parsing both texts would count every follow-up twice.  Use the
            # final answer only when there is no report text, and de-duplicate
            # as a second guard for partially overlapping copies.
            follow_source = (report_text
                             if str(report_text or "").strip() else final_text)
            follow_ups = _dedupe_follow_ups(
                _scope.follow_up_items(follow_source or ""))
            section_text = _scope.strip_follow_ups(combined)
        except Exception:
            follow_ups = []
            section_text = combined
    blocking_follow_ups = [item for item in follow_ups
                           if isinstance(item, dict) and item.get("blocking")]
    non_blocking_follow_ups = [item for item in follow_ups
                               if not (isinstance(item, dict) and item.get("blocking"))]
    score = extract_quality_score(None, report_text) if report_text else None
    if score is None:
        score = extract_quality_score(None, final_text)
    if score is None:
        score = extract_quality_score(None, combined)

    blocking = None
    for pattern in (
            r"blocking\s+defects?\s*(?:count)?\s*[:=\-|]\s*(\d+)",
            r"(\d+)\s+blocking\s+defect"):
        # Follow-up sections are removed first: a pre-existing finding that
        # mentions "2 blocking defects" must never raise this change's count.
        match = re.search(pattern, section_text, re.IGNORECASE)
        if match:
            try:
                blocking = int(match.group(1))
                break
            except ValueError:
                pass
    blocking_lines = _section_bullets(section_text, ("blocking defect", "blocking"))
    # 3.9.3 persisted a connector-invented blocking defect when the score line
    # was missing.  It is not reviewer evidence: drop it (and its contribution
    # to an explicit count) so an old partial/report cannot keep a workspace
    # permanently blocked.
    legacy_blocking = [item for item in blocking_lines
                       if _is_legacy_synthetic_score_defect(item)]
    if legacy_blocking:
        blocking_lines = [item for item in blocking_lines
                          if item not in legacy_blocking]
        if blocking is not None and not prefer_sections:
            blocking = max(0, int(blocking) - len(legacy_blocking))
    if prefer_sections:
        # The merged report is authoritative on resume.  The reviewer's
        # trailing "Blocking defects: N" summary may still reflect the
        # pre-merge headings (for example 0 after repeating a carried blocking
        # finding under Non-blocking), so do not let it override the sections.
        blocking = len(blocking_lines)
    elif blocking is None:
        blocking = len(blocking_lines)
    defects = blocking_lines or _section_bullets(section_text, ("defect", "issues"))
    defects = [item for item in defects
               if not _is_legacy_synthetic_score_defect(item)]
    fixes = _section_bullets(section_text, ("fix list", "fixes", "required changes"))
    if not fixes:
        fixes = defects
    if blocking_follow_ups:
        # A security/data-loss follow-up is blocking and must be visibly
        # marked as such wherever the connector repeats it (defects, fix list,
        # run record, result).  The original wording is kept.
        follow_texts = []
        for item in blocking_follow_ups:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            # The parser keeps the reviewer's own tag in ``text``; do not
            # repeat it when the connector adds its blocking marker.
            text = re.sub(
                r"^\s*\[\s*(?:security|data[\s\-_]*loss)\s*\]\s*", "", text,
                flags=re.IGNORECASE).strip()
            if not text:
                continue
            kind = str(item.get("kind") or "security/data-loss").strip()
            follow_texts.append("[blocking: %s] %s" % (kind, text))
        defects = list(defects) + [text for text in follow_texts
                                   if text not in defects]
        fixes = list(fixes) + [text for text in follow_texts
                               if text not in fixes]
    blocking = max(int(blocking or 0), len(blocking_follow_ups))
    visual = visual_impact_score(combined)
    plain = _review_asserts_plain(combined, defects)
    capped = False
    if (task_type == "creative" and score is not None
            and (plain or (visual is not None and visual <= CREATIVE_VISUAL_FLOOR))):
        capped_score = min(float(score), CREATIVE_PLAIN_CAP)
        capped = capped_score < float(score)
        score = capped_score
        if capped and not any("visually plain" in str(item).lower() for item in defects):
            defects = list(defects) + [
                "Creative score capped at 7.5: accurate but visually plain/generic result."]
            if not fixes or fixes == defects[:-1]:
                fixes = list(fixes) + [defects[-1]]
    return {
        "score": round(float(score), 2) if score is not None else None,
        "blocking_defects": int(blocking or 0),
        "defects": defects,
        "fixes": fixes,
        "visual_impact": round(float(visual), 2) if visual is not None else None,
        "creative_cap": bool(capped),
        "follow_ups": follow_ups,
        "blocking_follow_ups": blocking_follow_ups,
        "non_blocking_follow_ups": non_blocking_follow_ups,
    }


def quality_verdict(score, blocking_defects=0, round_no=1, max_rounds=DEFAULT_QUALITY_ROUNDS,
                    target=DEFAULT_QUALITY_TARGET):
    """Pure control-flow helper used by tests and by the review loop."""
    try:
        score = float(score)
    except Exception:
        score = 0.0
    blocking = int(blocking_defects or 0)
    if score >= float(target) and blocking == 0:
        return "accept"
    if int(round_no) >= int(max_rounds):
        return "stop"
    return "revise"


# -- P3-2: review convergence and one-question handoff ----------------------
# Pure helpers used by the Lead's review loop.  Signatures are deliberately
# coarse (kind + basename/line + evidence type) so the same defect carried
# across rounds converges without making cosmetic polish look substantive.
_COSMETIC_RE = re.compile(
    r"\b(?:cosmetic|polish|aesthetic|spacing|alignment|colour palette|"
    r"color palette|palette|wording)\b", re.IGNORECASE)
_SUBSTANTIVE_RE = re.compile(
    r"\b(?:font[\s-]?size|contrast|accessib\w*|missing|fact|crash\w*|"
    r"tests?|broken|qr|overflow\w*)\b", re.IGNORECASE)
_EVIDENCE_TEST_RE = re.compile(
    r"\b(?:tests?|pytest|unittest|assert\w*|build (?:failed|fails)|"
    r"failing test)\b", re.IGNORECASE)
_EVIDENCE_SHOT_RE = re.compile(
    r"\b(?:screenshots?|screen shots?|images?|png|jpe?g|render\w*|visual)\b",
    re.IGNORECASE)
_EVIDENCE_LOG_RE = re.compile(
    r"\b(?:logs?|traceback|stderr|stdout|console|error output)\b", re.IGNORECASE)
_EVIDENCE_CODE_RE = re.compile(
    r"\b(?:code|function|module|import|syntax|api|class|variable)\b",
    re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/])?(?:[.~\w-]+[\\/])*"
    r"[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,9}(?::\d+)?", re.IGNORECASE)


def normalise_defect(text):
    """Lowercase and flatten a defect sentence for conservative comparison."""
    value = str(text or "").lower()
    value = re.sub(r"(?m)^\s*(?:[-*\u2022\u00b7]+|\d+[.)])\s*", " ", value)
    value = value.replace("\u2010", "-").replace("\u2011", "-")
    value = value.replace("\u2012", "-").replace("\u2013", "-")
    value = value.replace("\u2014", "-").replace("\u2015", "-")
    value = value.replace("colour", "color").replace("centre", "center")
    value = re.sub(r"[^a-z0-9./:_-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .,;:-")
    return value


def _file_anchor(text):
    """First path-like token as basename[:line]; never an absolute path."""
    for match in _PATH_TOKEN_RE.finditer(str(text or "")):
        token = match.group(0).replace("\\", "/").rstrip(".,;:)")
        line = ""
        if ":" in token:
            base, maybe_line = token.rsplit(":", 1)
            if maybe_line.isdigit():
                token, line = base, ":" + maybe_line
        name = token.rsplit("/", 1)[-1]
        if not name or name in (".", "..") or ".." in name:
            continue
        if name.rsplit(".", 1)[-1].isdigit():
            continue
        return name + line
    return ""


def _evidence_type(text):
    value = str(text or "")
    if _EVIDENCE_TEST_RE.search(value):
        return "test"
    if _EVIDENCE_SHOT_RE.search(value):
        return "screenshot"
    if _EVIDENCE_LOG_RE.search(value):
        return "log"
    if _EVIDENCE_CODE_RE.search(value):
        return "code"
    return "text"


def defect_signature(text):
    """Stable 64-hex SHA-256 of kind | file anchor | evidence type."""
    kind = defect_kind(text)
    anchor = _file_anchor(text)
    evidence = _evidence_type(text)
    seed = "%s|%s|%s" % (kind, anchor, evidence)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def defect_signatures(defects):
    """Sorted unique signatures for a list of defect strings."""
    return sorted({defect_signature(item) for item in (defects or [])
                   if str(item).strip()})


def defect_class(text):
    """Return ``cosmetic`` only for explicitly cosmetic/polish wording."""
    value = normalise_defect(text)
    if not value:
        return "substantive"
    if _SUBSTANTIVE_RE.search(value):
        return "substantive"
    if _COSMETIC_RE.search(value):
        return "cosmetic"
    return "substantive"


def _round_no(entry, index):
    try:
        value = int(entry.get("round"))
        return value if value > 0 else index + 1
    except (AttributeError, TypeError, ValueError):
        return index + 1


def _blocking_signatures(entry):
    """Signature list saved with a review entry, else derived from defects."""
    saved = entry.get("signatures")
    if isinstance(saved, list):
        return sorted({str(item) for item in saved if str(item).strip()})
    try:
        blocking = int(entry.get("blocking_defects") or 0)
    except (TypeError, ValueError):
        blocking = 0
    if blocking <= 0:
        return []
    return defect_signatures(entry.get("defects") or [])


def _review_entries(review_rounds):
    entries = []
    for index, entry in enumerate(review_rounds or []):
        if not isinstance(entry, dict) or entry.get("kind") == "revision":
            continue
        entries.append((index, entry))
    return entries


def _first_seen(review_rounds):
    seen = {}
    for index, entry in _review_entries(review_rounds):
        round_no = _round_no(entry, index)
        for sig in _blocking_signatures(entry):
            if sig not in seen:
                seen[sig] = round_no
    return seen


def repeated_signatures(review_rounds, k=2):
    """Blocking signatures present in each of the last ``k`` review rounds."""
    reviews = _review_entries(review_rounds)
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 2
    if k < 1 or len(reviews) < k:
        return {}
    recent = reviews[-k:]
    common = set(_blocking_signatures(recent[0][1]))
    for _index, entry in recent[1:]:
        common &= set(_blocking_signatures(entry))
    first = _first_seen(review_rounds)
    result = {}
    for sig in sorted(common):
        history = []
        defect = ""
        for index, entry in _review_entries(review_rounds):
            if sig in _blocking_signatures(entry):
                round_no = _round_no(entry, index)
                if round_no not in history:
                    history.append(round_no)
                if not defect:
                    for item in entry.get("defects") or []:
                        if str(item).strip() and defect_signature(item) == sig:
                            defect = str(item).strip()
                            break
                    if not defect and entry.get("defect"):
                        defect = str(entry["defect"]).strip()
        result[sig] = {
            "first_seen_round": first.get(sig, history[0] if history else None),
            "rounds": sorted(history),
            "defect": defect,
        }
    return result


def _signature_label(info, sig):
    raw = str(info.get("defect") or "").replace("?", "").strip()
    if raw:
        anchor = _file_anchor(raw)
        kind = defect_kind(raw)
        return "%s in %s" % (kind, anchor) if anchor else kind
    return "signature " + sig[:12]


def _stop_action(mode):
    return "stop" if str(mode or "").strip().lower() == "auto" else "observe_stop"


def _base_decision(**overrides):
    decision = {
        "action": "continue",
        "reason": "No repeated blocking signature in the last review rounds.",
        "signatures": [],
        "question": "",
        "evidence": [],
        "options": [],
        "first_seen_round": None,
        "cosmetic_only": False,
    }
    decision.update(overrides)
    return decision


def convergence_decision(review_rounds, mode="observe", target=8.5, k=2,
                         tolerance=0.5):
    """Decide continue/stop from repeated blocking signatures or polish."""
    rounds = list(review_rounds or [])
    reviews = _review_entries(rounds)
    if not reviews:
        return _base_decision(reason="No review round is available yet.")
    index, current = reviews[-1]
    current_round = _round_no(current, index)
    defects = [str(item).strip() for item in current.get("defects") or []
               if str(item).strip()]
    try:
        blocking = int(current.get("blocking_defects") or 0)
    except (TypeError, ValueError):
        blocking = 0
    classes = [defect_class(item) for item in defects]
    cosmetic_only = bool(defects) and blocking > 0 and all(
        item == "cosmetic" for item in classes)
    try:
        score = float(current.get("score"))
    except (TypeError, ValueError):
        score = None
    prior_revision = any(isinstance(item, dict) and item.get("kind") == "revision"
                         for item in rounds)
    repeated = repeated_signatures(rounds, k=k)

    if repeated:
        sig = sorted(repeated)[0]
        info = repeated[sig]
        label = _signature_label(info, sig)
        history = info.get("rounds") or [current_round]
        reason = ("Blocking defect repeated across review rounds %s (%s)."
                  % (", ".join(str(item) for item in history), sig[:12]))
        question = (
            "Round %s repeated the same blocking defect (%s). Resume with a "
            "narrower fix scope, accept below the gate, or stop?"
            % (current_round, label))
        return _base_decision(
            action=_stop_action(mode),
            reason=reason,
            signatures=sorted(repeated),
            question=question,
            evidence=["round %s: %s" % (item, label) for item in history],
            options=["resume_with_scope", "accept_below_gate", "stop"],
            first_seen_round=info.get("first_seen_round"),
            cosmetic_only=cosmetic_only,
        )

    if (cosmetic_only and score is not None
            and score >= float(target) - float(tolerance) and prior_revision):
        sigs = defect_signatures(defects)
        first = _first_seen(rounds)
        label = _signature_label({"defect": defects[0]}, sigs[0] if sigs else "")
        question = (
            "Round %s was cosmetic-only (%s) at %s against the %s gate after "
            "one revision. Resume with a narrower polish scope, accept below "
            "the gate, or stop?"
            % (current_round, label, score, float(target)))
        return _base_decision(
            action=_stop_action(mode),
            reason=("Cosmetic-only review at %s meets the %s gate after a "
                    "prior revision." % (score, float(target) - float(tolerance))),
            signatures=sigs,
            question=question,
            evidence=["round %s: cosmetic-only defect (%s)"
                      % (current_round, label),
                      "score %s >= %s" % (score, float(target) - float(tolerance))],
            options=["resume_with_scope", "accept_below_gate", "stop"],
            first_seen_round=min((first.get(sig, current_round) for sig in sigs),
                                 default=current_round),
            cosmetic_only=True,
        )

    return _base_decision(
        reason=("No blocking signature repeated in the last %s review rounds; "
                "the normal review loop should continue." % k),
        cosmetic_only=cosmetic_only,
    )


def persist_signatures(review_rounds, entry):
    """Annotate a review entry with signatures and sig -> first round."""
    if not isinstance(entry, dict):
        return entry
    sigs = _blocking_signatures(entry)
    entry["signatures"] = sigs
    first = _first_seen([item for item in (review_rounds or [])
                         if item is not entry])
    index = len(list(review_rounds or []))
    current_round = _round_no(entry, index)
    entry["first_seen_round"] = {
        sig: first.get(sig, current_round) for sig in sigs}
    return entry
