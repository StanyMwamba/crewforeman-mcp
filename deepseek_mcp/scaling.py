"""Live scaling helpers for builder waves (v3.6.0 candidate).

Pure, deterministic functions used by the harness after a builder wave:

* :func:`score_candidates` turns a wave's raw output into a 0..10 quality
  score, with candidate-level reasons.
* :func:`plan_extra_wave` decides whether another builder wave is worth
  starting, applying time and cost margins and the extra-wave cap.
* :func:`extra_wave_instructions` builds the prompt section for that wave and
  explicitly names the approaches that must not be repeated.

No filesystem, clock, network or randomness is touched here, so the same
inputs always produce the same outputs.  The module is Python 3.9 compatible
and standard-library only.
"""

import math
import re

DEFAULT_THRESHOLD = 7.0
MIN_TIME_MARGIN_SECONDS = 120.0
BUDGET_MARGIN_FRACTION = 0.10
ERROR_PENALTY = 2.5

_STOPWORDS = frozenset(
    """a about above after again against all also an and any are as at be
    because been before being below between both but by can could did do does
    doing down during each few for from further had has have having he her here
    hers herself him himself his how i if in into is it its itself just me more
    most my myself no nor not now of off on once only or other our ours
    ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why
    will with would you your yours yourself yourselves file files
    """.split()
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*")
_FACT_MARKER = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:required\s+facts?|requirements?|must[\s-]+include|"
    r"must[\s-]+have|facts?)\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


def _clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def _optional_float(value):
    """Return ``value`` as a finite float, or None when it is not usable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _non_negative_float(value):
    number = _optional_float(value)
    if number is None:
        return 0.0
    return max(0.0, number)


def _label(value, fallback):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:80] if text else fallback


def _answer_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _files(value):
    """Normalise a candidate's deliverable list without touching disk."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]
    result = []
    for item in items:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _tokens(text):
    return _WORD_RE.findall(str(text or "").lower())


def _keywords(text):
    """Ordered unique content words from the brief."""
    seen = set()
    result = []
    for token in _tokens(text):
        if len(token) < 3 and not token.isdigit():
            continue
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _required_facts(brief):
    """Pull explicit required-fact phrases out of a brief.

    Recognises ``Required facts: a, b``, ``Must include: a; b`` and bullet
    lists below those markers.  Returns normalised, de-duplicated strings.
    """
    facts = []
    capture = False
    for raw_line in str(brief or "").splitlines():
        line = raw_line.strip()
        match = _FACT_MARKER.match(line)
        if match:
            rest = (match.group(1) or "").strip()
            capture = not rest
            if rest:
                for part in re.split(r"[;,]", rest):
                    part = part.strip(" .-")
                    if part:
                        facts.append(part)
            continue
        if not capture:
            continue
        if not line:
            capture = False
            continue
        bullet = _BULLET.match(line)
        if bullet:
            facts.append(bullet.group(1).strip(" .-"))
        elif line.endswith(":"):
            capture = False
        else:
            capture = False
    seen = set()
    unique = []
    for fact in facts:
        key = " ".join(_tokens(fact))
        if key and key not in seen:
            seen.add(key)
            unique.append(fact)
    return unique


def _coverage(fact, candidate_tokens, candidate_text):
    """Fraction of a required fact's content words found in the candidate."""
    words = [word for word in _tokens(fact)
             if (len(word) >= 3 or word.isdigit()) and word not in _STOPWORDS]
    if not words:
        return 0.0
    if " ".join(words) in " ".join(candidate_tokens):
        return 1.0
    matched = sum(1 for word in words if word in candidate_tokens)
    return float(matched) / float(len(words))


def _negative_reasons(reasons):
    markers = ("missing", "empty", "low ", "error", "no ", "below")
    picked = []
    for reason in reasons:
        low = reason.lower()
        if any(marker in low for marker in markers):
            picked.append(reason)
    return picked[:2]


def _score_one(index, candidate, brief_keywords, facts, threshold):
    candidate = candidate if isinstance(candidate, dict) else {"answer": candidate}
    name = _label(candidate.get("name"), "candidate-%d" % (index + 1))
    answer = _answer_text(candidate.get("answer"))
    files = _files(candidate.get("files"))
    error = _answer_text(candidate.get("error"))
    review = _optional_float(candidate.get("review_score"))
    if review is not None:
        review = _clamp(review, 0.0, 10.0)

    text = " ".join([name, answer] + files)
    candidate_tokens = set(_tokens(text))
    reasons = []

    if review is not None:
        score = review
        reasons.append("review_score override (%.2f/10)" % review)
    else:
        score = 1.0
        if files:
            score += 3.0
            reasons.append("deliverable files present (%d)" % len(files))
        else:
            reasons.append("missing deliverable files")

        answer_length = len(answer)
        if answer_length:
            score += min(2.0, (2.0 * answer_length) / 600.0)
            if answer_length < 120:
                reasons.append("low answer length (%d chars)" % answer_length)
            else:
                reasons.append("answer length %d chars" % answer_length)
        else:
            reasons.append("empty answer")

        if brief_keywords:
            matched = sum(1 for word in brief_keywords if word in candidate_tokens)
            ratio = float(matched) / float(len(brief_keywords))
            # Full credit at 30% coverage: briefs are intentionally noisy.
            score += 2.0 * min(1.0, ratio / 0.30)
            if ratio < 0.10:
                reasons.append(
                    "low brief keyword overlap (%d/%d)"
                    % (matched, len(brief_keywords))
                )
            else:
                reasons.append(
                    "brief keyword overlap %d/%d" % (matched, len(brief_keywords))
                )
        else:
            score += 1.0
            reasons.append("brief has no keyword signal")

        if facts:
            covered = sum(1 for fact in facts
                          if _coverage(fact, candidate_tokens, text) >= 0.60)
            fact_ratio = float(covered) / float(len(facts))
            score += 2.0 * fact_ratio
            if covered < len(facts):
                reasons.append(
                    "required facts %d/%d covered" % (covered, len(facts))
                )
            else:
                reasons.append("all required facts covered")
        else:
            score += 1.0
            reasons.append("brief has no explicit required facts")

    if error:
        score -= ERROR_PENALTY
        reasons.append("error: %s" % _label(error, "builder error"))

    score = round(_clamp(score, 0.0, 10.0), 2)
    return {
        "name": name,
        "score": score,
        "weak": bool(score < threshold),
        "reasons": reasons,
        "answer_chars": len(answer),
        "files": len(files),
        "error": bool(error),
        "review_score": review,
    }


def score_candidates(candidates, brief, threshold=DEFAULT_THRESHOLD):
    """Score one builder wave on a deterministic 0..10 scale.

    A candidate is a dict with ``name``, ``answer``, ``files``, ``error`` and
    an optional ``review_score``.  A numeric ``review_score`` (0..10) replaces
    the heuristic for that candidate but an error still incurs its penalty.

    The heuristic combines deliverable-file presence (+3), answer length
    (+2), brief keyword overlap (+2) and explicit required-fact coverage (+2)
    on top of a 1-point baseline, then subtracts 2.5 for a builder error.
    """
    threshold = _optional_float(threshold)
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    threshold = _clamp(threshold, 0.0, 10.0)
    if not isinstance(candidates, (list, tuple)):
        candidates = []

    brief_keywords = _keywords(brief)
    facts = _required_facts(brief)
    per_candidate = [
        _score_one(index, candidate, brief_keywords, facts, threshold)
        for index, candidate in enumerate(candidates)
    ]

    reasons = []
    if not per_candidate:
        score = 0.0
        reasons.append("no candidates were produced")
    else:
        score = round(
            sum(item["score"] for item in per_candidate) / float(len(per_candidate)),
            2,
        )
        failed = [item for item in per_candidate if item["weak"]]
        if failed:
            reasons.append(
                "mean score %.2f/10 is below threshold %.2f"
                % (score, threshold)
            )
            names = ", ".join(item["name"] for item in failed[:4])
            reasons.append("candidates below threshold: %s" % names)
        else:
            reasons.append(
                "mean score %.2f/10 meets threshold %.2f" % (score, threshold)
            )
            best = max(per_candidate, key=lambda item: item["score"])
            reasons.append("best candidate: %s (%.2f/10)" % (best["name"], best["score"]))
        for item in per_candidate[:4]:
            negatives = _negative_reasons(item["reasons"])
            if negatives:
                reasons.append("%s: %s" % (item["name"], "; ".join(negatives)))

    return {
        "score": score,
        "weak": bool(score < threshold),
        "per_candidate": per_candidate,
        "reasons": reasons,
    }


def _budget_state(elapsed_seconds, deadline_seconds, cost_usd, budget_usd,
                  cost_known=True):
    elapsed = _non_negative_float(elapsed_seconds)
    deadline = _optional_float(deadline_seconds)
    if deadline is None or deadline <= 0:
        time_ok = True
        time_remaining = None
        time_margin = MIN_TIME_MARGIN_SECONDS
    else:
        time_remaining = deadline - elapsed
        time_margin = MIN_TIME_MARGIN_SECONDS
        time_ok = time_remaining >= time_margin

    known = bool(cost_known) and cost_usd is not None
    spent = _non_negative_float(cost_usd)
    budget = _optional_float(budget_usd)
    if budget is None:
        cost_ok = True
        cost_remaining = None
        cost_margin = None
    elif not known:
        # An unpriced wave cannot be proven under a cost ceiling. Mirror
        # budget.JobBudget.cost_exceeded: stop instead of treating None as $0.
        cost_ok = False
        cost_remaining = None
        cost_margin = 0.0
    elif budget <= 0:
        cost_ok = False
        cost_remaining = round(budget - spent, 4)
        cost_margin = 0.0
    else:
        cost_remaining = budget - spent
        cost_margin = BUDGET_MARGIN_FRACTION * budget
        cost_ok = cost_remaining > 0 and cost_remaining + 1e-9 >= cost_margin

    return {
        "time_remaining_seconds": (None if time_remaining is None
                                   else round(time_remaining, 2)),
        "time_margin_seconds": round(time_margin, 2),
        "time_ok": bool(time_ok),
        "cost_remaining_usd": (None if cost_remaining is None
                               else round(cost_remaining, 4)),
        "cost_margin_usd": (None if cost_margin is None else round(cost_margin, 4)),
        "cost_known": bool(known),
        "cost_ok": bool(cost_ok),
        "elapsed_seconds": round(elapsed, 2),
        "budget_usd": (None if budget is None else round(budget, 4)),
    }


def _approaches(scored, candidates, concurrency):
    """Build at most ``max(1, concurrency)`` distinct next-wave approaches."""
    try:
        cap = int(concurrency)
    except (TypeError, ValueError):
        cap = 1
    cap = max(1, cap)

    failed = [item for item in scored["per_candidate"] if item["weak"]]
    ideas = [
        "Re-plan from the brief and discard the previous wave's partial outputs."
    ]
    for item in failed[:3]:
        ideas.append(
            "Replace %s's approach with a materially different design; "
            "do not edit or reuse its files." % item["name"]
        )
    if scored["weak"] and not failed and candidates:
        ideas.append("Use a different implementation strategy from every previous attempt.")
    ideas.append(
        "Change the implementation strategy, produce the required deliverable "
        "files, and verify them before answering."
    )

    unique = []
    seen = set()
    for idea in ideas:
        if idea not in seen:
            seen.add(idea)
            unique.append(idea)
    return unique[:cap]


def plan_extra_wave(
    wave_number,
    max_extra_waves,
    candidates,
    brief,
    threshold=DEFAULT_THRESHOLD,
    elapsed_seconds=0.0,
    deadline_seconds=None,
    cost_usd=0.0,
    budget_usd=None,
    cost_known=True,
    concurrency=1,
):
    """Decide whether a weak builder wave should get another wave.

    A wave is added only when the scored candidates are weak, the
    ``max_extra_waves`` cap has room, at least 120 seconds remain before the
    deadline, and the remaining cost budget can be proven: when a budget is set
    at least 10% of it must still be unspent, and an unknown/``None`` cost is
    treated as over budget (mirroring ``budget.JobBudget.cost_exceeded``).
    ``concurrency`` caps the number of proposed approaches and is never lower
    than one.
    """
    try:
        wave = int(wave_number)
    except (TypeError, ValueError):
        wave = 1
    if wave < 0:
        wave = 0
    try:
        max_extra = int(max_extra_waves)
    except (TypeError, ValueError):
        max_extra = 0
    max_extra = max(0, max_extra)
    threshold_value = _optional_float(threshold)
    if threshold_value is None:
        threshold_value = DEFAULT_THRESHOLD
    threshold_value = _clamp(threshold_value, 0.0, 10.0)

    scored = score_candidates(candidates, brief, threshold=threshold_value)
    budget = _budget_state(
        elapsed_seconds, deadline_seconds, cost_usd, budget_usd,
        cost_known=cost_known)
    used = max(0, wave - 1)
    waves_remaining = max(0, max_extra - used)
    next_wave = wave + 1
    budget["waves_remaining"] = waves_remaining
    budget["max_extra_waves"] = max_extra

    if not scored["weak"]:
        add = False
        reason = ("score %.2f/10 meets threshold %.2f; no extra wave needed"
                  % (scored["score"], threshold_value))
    elif waves_remaining < 1:
        add = False
        reason = ("extra wave limit reached (max_extra_waves=%d)" % max_extra)
    elif not budget["time_ok"]:
        add = False
        reason = ("only %.0fs remain; need at least %.0fs before starting another wave"
                  % (budget["time_remaining_seconds"] or 0.0,
                     budget["time_margin_seconds"]))
    elif not budget["cost_ok"]:
        add = False
        if not budget["cost_known"]:
            reason = ("estimated cost is unknown; cannot prove the remaining "
                      "budget, so no extra wave")
        elif budget["cost_margin_usd"] is None:
            reason = "no budget remains for another wave"
        else:
            reason = ("only $%.2f remains; need the 10%% budget margin ($%.2f)"
                      % (budget["cost_remaining_usd"] or 0.0,
                         budget["cost_margin_usd"]))
    else:
        add = True
        reason = ("score %.2f/10 is below threshold %.2f; extra wave %d approved"
                  % (scored["score"], threshold_value, next_wave))

    return {
        "add": bool(add),
        "wave": next_wave if add else None,
        "reason": reason,
        "approaches": _approaches(scored, candidates, concurrency) if add else [],
        "budget": budget,
    }


def extra_wave_instructions(brief, candidates, wave_number):
    """Return the prompt section for a fresh, differently-approached wave.

    The section names the weak candidates and their concrete failure signals,
    forbids repeating them, and restates the brief excerpt that the new wave
    must satisfy.
    """
    try:
        wave = int(wave_number)
    except (TypeError, ValueError):
        wave = 2
    wave = max(1, wave)
    scored = score_candidates(candidates, brief, threshold=DEFAULT_THRESHOLD)

    lines = [
        "## Extra wave %d: new approaches required" % wave,
        "",
        "The previous builder wave did not meet the quality bar "
        "(mean score %.2f/10; target %.2f/10)." % (
            scored["score"], DEFAULT_THRESHOLD),
        "",
        "Do not repeat or lightly revise these failed approaches:",
    ]
    failed = [item for item in scored["per_candidate"] if item["weak"]]
    if not failed:
        failed = scored["per_candidate"][:5]
    if not failed:
        lines.append("- No usable candidate output was produced; start from the brief.")
    else:
        for item in failed[:5]:
            negatives = _negative_reasons(item["reasons"])
            detail = "; ".join(negatives) if negatives else "did not reach the bar"
            lines.append("- %s: %s (%.2f/10)" % (item["name"], detail, item["score"]))

    excerpt = re.sub(r"\s+", " ", str(brief or "")).strip()
    if len(excerpt) > 320:
        excerpt = excerpt[:317].rstrip() + "..."
    lines.extend([
        "",
        "Choose a materially different design, algorithm, or structure from "
        "the approaches above. Produce the required deliverable files and "
        "verify they exist before answering.",
    ])
    if excerpt:
        lines.extend(["", "Brief excerpt:", excerpt])
    return "\n".join(lines)
