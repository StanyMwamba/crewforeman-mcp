"""Fixed, versioned builder-practices guidance for assembled build prompts.

The full block is for code builds and is injected regardless of skill
detection.  Research and creative tasks get a shorter variant; simple tasks
get no block at all.
"""

PRACTICE_SET = "builder-practices"
PRACTICE_VERSION = "1.0"
WORD_BUDGET = 350

CODE_BLOCK = """## Builder practices

1. **Restate the acceptance criteria as a numbered checklist.** Write every
   requirement into your working notes before touching code, and keep that
   list beside you while you build. If the brief is ambiguous, state the
   assumption you chose.
2. **Write tests first for behaviour changes.** Add or adjust a focused test
   that fails for the right reason, then make it pass. New modules need at
   least an import-and-exercise test.
3. **Keep the change minimal and in scope.** Choose the simplest solution
   that meets the checklist. Touch only what it requires; leave neighbouring
   code, public names and formatting alone.
4. **Do not hard-code values the brief marks as examples.** Sample names,
   paths, counts and URLs are illustrations; derive real values from input,
   arguments or configuration.
5. **Run the full suite once at the end.** Use the documented command and
   paste the exact counts (tests run, failures, errors) into your handoff.
6. **Before you finish, re-read the deliverables list.** The reviewer checks
   what the brief names; confirm every deliverable path exists and holds what
   you say it holds, tests and docs included.
7. **Back every statement with evidence; never claim something you did not
   verify.** If you did not run it or read it, say so; quote the command
   output, file path or line that supports each claim.
"""

LIGHT_BLOCK = """## Builder practices (light)

1. Restate the acceptance criteria as a short numbered checklist before you
   start work.
2. Keep the change minimal and in scope for the brief.
3. Treat sample values marked as examples as illustrations, not fixed
   requirements.
4. Confirm every deliverables item named in the brief exists at the path
   you will report.
5. Re-read your output and cite evidence (file, section or command output)
   for each claim.
"""

_TEXT_BY_VARIANT = {"code": CODE_BLOCK, "light": LIGHT_BLOCK, "none": ""}
_LIGHT_TASK_TYPES = frozenset({
    "research", "creative", "docs", "doc", "document", "documentation",
})
_NONE_TASK_TYPES = frozenset({"simple"})


def _task_type_key(task_type):
    if task_type is None:
        return ""
    if isinstance(task_type, str):
        return task_type.strip().lower()
    return ""


def variant_for(task_type):
    """Return the practice variant name for *task_type*."""
    key = _task_type_key(task_type)
    if key in _NONE_TASK_TYPES:
        return "none"
    if key in _LIGHT_TASK_TYPES:
        return "light"
    return "code"


def block(task_type, enabled=True):
    """Return the Markdown block, wrapped for prompt insertion, or ``""``."""
    if not enabled:
        return ""
    variant = variant_for(task_type)
    text = _TEXT_BY_VARIANT.get(variant, "")
    if not text:
        return ""
    return "\n\n" + text + "\n"


def word_count(text):
    """Return the whitespace-delimited word count of *text*."""
    if not text:
        return 0
    return len(str(text).split())


def token_estimate(text):
    """Rough token cost for English Markdown: about four characters per token."""
    return (len(text or "") + 3) // 4


def record(task_type, enabled=True):
    """Return JSON-safe metadata describing the block for a run record."""
    variant = variant_for(task_type) if enabled else "none"
    text = _TEXT_BY_VARIANT.get(variant, "")
    return {
        "set": PRACTICE_SET,
        "version": PRACTICE_VERSION,
        "variant": variant,
        "enabled": bool(enabled),
        "words": word_count(text),
        "chars": len(text),
        "tokens_est": token_estimate(text),
        "budget_words": WORD_BUDGET,
    }
