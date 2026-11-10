"""Contract tests for the crewforeman-mcp 3.14.1 INSTALL and CLIENTS docs.

Standard library only and Python 3.9 compatible.  Intended to live at
``repo/tests/test_v3121_install_docs.py`` and run with::

    python3 -m unittest discover -s tests -q

The repository root is derived from this file, so the test reads
``docs/INSTALL.md`` and ``docs/CLIENTS.md`` from the checkout it belongs to.
JSON snippets are parsed with :func:`json.loads`; TOML snippets use the
existing test-suite TOML parser (the local fallback on Python 3.9).
"""

import json
import re
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_v3100c_clients_doc import parse_toml  # noqa: E402


DOCS = {
    "docs/INSTALL.md": REPO_ROOT / "docs" / "INSTALL.md",
    "docs/CLIENTS.md": REPO_ROOT / "docs" / "CLIENTS.md",
}
DOC_ORDER = ("docs/INSTALL.md", "docs/CLIENTS.md")

CLIENTS = (
    ("Cursor", ("Cursor",)),
    (
        "VS Code with GitHub Copilot",
        (
            "VS Code with GitHub Copilot",
            "VS Code (GitHub Copilot)",
            "VS Code + GitHub Copilot",
            "VS Code & GitHub Copilot",
        ),
    ),
    ("Claude Desktop", ("Claude Desktop",)),
    ("Claude Code", ("Claude Code",)),
    ("OpenAI Codex CLI", ("OpenAI Codex CLI", "Codex CLI")),
)

INSTALL_HEADINGS = (
    "Requirements",
    "Install",
    "Keys",
    "Choose models",
    "Reviewer from another vendor",
)

JOB_OVERRIDE_NAMES = (
    "builder_model",
    "builder_provider",
    "slice_models",
    "model_candidates",
    "reviewer_model",
    "reviewer_provider",
)

CLIENT_LAUNCH_TERMS = (
    "/path/to/crewforeman-mcp",
    "DEEPSEEK_API_KEY",
    "Harness",
)

INSTALL_TERMS = (
    ("3.14.1", "3.14.1"),
    ("~/.dsh/.credentials.yaml", "~/.dsh/.credentials.yaml"),
    ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    ("api_key_env", "api_key_env"),
    ("backup_builder_model", "backup_builder_model"),
    ("reviewer_same_vendor_policy", "reviewer_same_vendor_policy"),
    ("dsh_bin", "dsh_bin"),
) + tuple((name, name) for name in JOB_OVERRIDE_NAMES)

HEADING_RE = re.compile(r"^##(?!#)[ \t]*(.+?)[ \t]*$", re.M)
FENCE_RE = re.compile(
    r"^```[ \t]*([^\n`]*)[ \t]*\r?\n(.*?)^```[ \t]*\r?$",
    re.M | re.S,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
SPACE_RE = re.compile(r"\s+")

ENV_FIRST_RE = re.compile(
    r"\benvironment\b.{0,260}?\b(?:before|first|then|precedence|prior to|"
    r"ahead of|fallback|falling back)\b.{0,260}?\bcredentials?\b",
    re.I | re.S,
)
CREDENTIALS_AFTER_RE = re.compile(
    r"\bcredentials?\b.{0,220}?\b(?:after|second|following|fallback)\b"
    r".{0,180}?\benvironment\b",
    re.I | re.S,
)
NAMES_NOT_VALUES_RE = re.compile(
    r"\b(?:variable\s+)?names?\b.{0,180}?\b(?:not|never|no|instead of|"
    r"rather than)\b.{0,120}?\bvalues?\b",
    re.I | re.S,
)
VALUES_NOT_NAMES_RE = re.compile(
    r"\bvalues?\b.{0,160}?\b(?:not|never|no)\b.{0,100}?\bnames?\b",
    re.I | re.S,
)
ONLY_NAMES_RE = re.compile(
    r"\b(?:only|just)\b.{0,40}?\b(?:variable\s+)?names?\b"
    r"|\b(?:variable\s+)?names?\b.{0,40}?\b(?:only|alone)\b",
    re.I | re.S,
)
NEGATED_VALUES_RE = re.compile(
    r"\b(?:not|never|no)\b.{0,100}?\bvalues?\b", re.I | re.S
)


def _normalise(text):
    return SPACE_RE.sub(" ", text.replace("`", "")).strip().lower()


def _clean_heading(title):
    return re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()


def _heading_matches(title, expected):
    actual = _normalise(title)
    wanted = _normalise(expected)
    if actual == wanted:
        return True
    if actual.startswith(wanted):
        rest = actual[len(wanted):]
        return bool(rest) and rest[0] in " (:,/-+\u2014&"
    return False


def _snippets(text):
    found = []
    for match in FENCE_RE.finditer(text):
        language = match.group(1).strip().split(" ")[0].lower()
        found.append(
            {
                "language": language,
                "body": match.group(2),
                "start": match.start(),
                "end": match.end(),
                "line": text.count("\n", 0, match.start()) + 1,
            }
        )
    return found


def _level2_headings(text, snippets):
    code_spans = [(snippet["start"], snippet["end"]) for snippet in snippets]
    headings = []
    for match in HEADING_RE.finditer(text):
        if any(start <= match.start() < end for start, end in code_spans):
            continue
        headings.append((match.start(), _clean_heading(match.group(1))))
    return headings


def _section(text, headings, heading_index):
    start = headings[heading_index][0]
    end = len(text)
    for position, _title in headings[heading_index + 1:]:
        end = position
        break
    return text[start:end]


def _without_fences(text):
    return FENCE_RE.sub(" ", text)


def _matches(headings, aliases):
    return [
        (position, title)
        for position, title in headings
        if any(_heading_matches(title, alias) for alias in aliases)
    ]


def _mentions(text, term):
    if term == "3.14.1":
        return re.search(r"(?<![\d.])3\.14\.1(?![\d.])", text) is not None
    if term.startswith("~"):
        return term in text
    return re.search(
        r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
        text,
    ) is not None


def _states_environment_first(section):
    prose = _normalise(_without_fences(section))
    for sentence in SENTENCE_SPLIT_RE.split(prose):
        if "environment" not in sentence or "credential" not in sentence:
            continue
        if ENV_FIRST_RE.search(sentence) or CREDENTIALS_AFTER_RE.search(sentence):
            return True
    return False


def _states_names_not_values(section):
    prose = _normalise(_without_fences(section))
    for sentence in SENTENCE_SPLIT_RE.split(prose):
        has_name = re.search(r"\b(?:variable\s+)?names?\b", sentence) is not None
        has_value = re.search(r"\bvalues?\b", sentence) is not None
        if not has_name or not has_value:
            continue
        if NAMES_NOT_VALUES_RE.search(sentence):
            return True
        if VALUES_NOT_NAMES_RE.search(sentence):
            return True
        if ONLY_NAMES_RE.search(sentence) and NEGATED_VALUES_RE.search(sentence):
            return True
    return False


def _privacy_patterns():
    patterns = [
        (
            "macOS home path",
            re.compile(r"/Users/[A-Za-z0-9._-]+(?:/|$)"),
        ),
        (
            "Linux home path",
            re.compile(r"/home/[A-Za-z0-9._-]+(?:/|$)"),
        ),
        (
            "Windows home path",
            re.compile(
                r"[A-Za-z]:[\\/]+Users[\\/][A-Za-z0-9._-]+(?:[\\/]|$)",
                re.I,
            ),
        ),
        (
            "email address",
            re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        ),
        ("sk- style key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
        ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
        ("GitHub token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
        ("Slack token", re.compile(r"xox[baprs]-")),
        ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{20,}")),
    ]
    return patterns


class InstallDocsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = []
        cls.texts = {}
        for relative in DOC_ORDER:
            path = DOCS[relative]
            try:
                cls.texts[relative] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                missing.append(
                    "%s: required document could not be read (%s)"
                    % (relative, type(exc).__name__)
                )
        if missing:
            raise AssertionError(
                "Requirement [both documents are readable] failed:\n  %s"
                % "\n  ".join(missing)
            )
        cls.snippets = {
            relative: _snippets(cls.texts[relative]) for relative in DOC_ORDER
        }
        cls.headings = {
            relative: _level2_headings(
                cls.texts[relative], cls.snippets[relative]
            )
            for relative in DOC_ORDER
        }

    def _fail(self, requirement, problems):
        self.fail(
            "Requirement [%s] failed:\n  %s"
            % (requirement, "\n  ".join(problems))
        )

    def test_every_fenced_json_and_toml_snippet_parses(self):
        problems = []
        for relative in DOC_ORDER:
            for index, snippet in enumerate(self.snippets[relative], 1):
                language = snippet["language"]
                if language not in ("json", "toml"):
                    continue
                try:
                    if language == "json":
                        json.loads(snippet["body"])
                    else:
                        parse_toml(snippet["body"])
                except Exception as exc:  # noqa: BLE001 - report every snippet
                    if isinstance(exc, json.JSONDecodeError):
                        detail = "%s at line %d, column %d of the snippet" % (
                            exc.msg,
                            exc.lineno,
                            exc.colno,
                        )
                    else:
                        detail = type(exc).__name__
                    problems.append(
                        "%s: fenced %s snippet #%d (line %d) does not parse: %s"
                        % (
                            relative,
                            language.upper(),
                            index,
                            snippet["line"],
                            detail,
                        )
                    )
        if problems:
            self._fail("every fenced JSON and TOML snippet parses", problems)

    def test_clients_have_exactly_one_level2_heading_each(self):
        relative = "docs/CLIENTS.md"
        problems = []
        for label, aliases in CLIENTS:
            found = _matches(self.headings[relative], aliases)
            if len(found) != 1:
                problems.append(
                    "%s: client %r needs exactly one level-2 heading matching "
                    "%s; found %d (%s)"
                    % (
                        relative,
                        label,
                        " / ".join(repr(alias) for alias in aliases),
                        len(found),
                        ", ".join(repr(title) for _pos, title in found)
                        or "none",
                    )
                )
        if problems:
            self._fail("one level-2 heading per client in CLIENTS.md", problems)

    def test_each_client_section_has_config_confirm_pitfall(self):
        relative = "docs/CLIENTS.md"
        problems = []
        for label, aliases in CLIENTS:
            found = _matches(self.headings[relative], aliases)
            if len(found) != 1:
                problems.append(
                    "%s: cannot check client %r section markers until there is "
                    "exactly one matching level-2 heading; found %d"
                    % (relative, label, len(found))
                )
                continue
            heading_index = self.headings[relative].index(found[0])
            body = _section(
                self.texts[relative],
                self.headings[relative],
                heading_index,
            )
            for marker in ("Config", "Confirm", "Pitfall"):
                if not re.search(r"\b" + marker + r"\b", body, re.I):
                    problems.append(
                        "%s: client %r section must contain %r"
                        % (relative, label, marker)
                    )
            if "crewforeman-mcp" not in body:
                problems.append(
                    "%s: client %r section must contain 'crewforeman-mcp'"
                    % (relative, label)
                )
        if problems:
            self._fail("Config, Confirm, Pitfall and crewforeman-mcp per client", problems)

    def test_each_client_section_has_launch_env_and_harness_note(self):
        relative = "docs/CLIENTS.md"
        problems = []
        for label, aliases in CLIENTS:
            found = _matches(self.headings[relative], aliases)
            if len(found) != 1:
                problems.append(
                    "%s: cannot check client %r launch terms until there is "
                    "exactly one matching level-2 heading; found %d"
                    % (relative, label, len(found))
                )
                continue
            heading_index = self.headings[relative].index(found[0])
            body = _section(
                self.texts[relative],
                self.headings[relative],
                heading_index,
            )
            for term in CLIENT_LAUNCH_TERMS:
                if term not in body:
                    problems.append(
                        "%s: client %r section must contain %r"
                        % (relative, label, term)
                    )
            section_snippets = [
                snippet
                for snippet in self.snippets[relative]
                if found[0][0] <= snippet["start"] < found[0][0] + len(body)
            ]
            if not any(
                snippet["language"] in ("json", "toml", "bash", "sh",
                                        "shell", "zsh")
                for snippet in section_snippets
            ):
                problems.append(
                    "%s: client %r section must contain a fenced json, toml "
                    "or bash launch snippet" % (relative, label)
                )
        if problems:
            self._fail(
                "each client section has a launch command, an env key and "
                "the Harness-omit note",
                problems,
            )

    def test_install_has_required_level2_headings(self):
        relative = "docs/INSTALL.md"
        problems = []
        for required in INSTALL_HEADINGS:
            found = _matches(self.headings[relative], (required,))
            if len(found) != 1:
                problems.append(
                    "%s: expected exactly one level-2 heading matching %r; "
                    "found %d (%s)"
                    % (
                        relative,
                        required,
                        len(found),
                        ", ".join(repr(title) for _pos, title in found)
                        or "none",
                    )
                )
        if problems:
            self._fail("required INSTALL.md level-2 headings", problems)

    def test_install_mentions_release_credentials_and_model_keys(self):
        relative = "docs/INSTALL.md"
        text = self.texts[relative]
        problems = []
        for label, term in INSTALL_TERMS:
            if not _mentions(text, term):
                problems.append(
                    "%s: must mention %r" % (relative, label)
                )
        if problems:
            self._fail(
                "INSTALL.md release, credentials and model-key mentions",
                problems,
            )

    def test_keys_section_states_lookup_order_and_name_only_config(self):
        relative = "docs/INSTALL.md"
        found = _matches(self.headings[relative], ("Keys",))
        if len(found) != 1:
            self._fail(
                "Keys section lookup order and name-only config",
                [
                    "%s: expected exactly one level-2 Keys heading; found %d"
                    % (relative, len(found))
                ],
            )
        heading_index = self.headings[relative].index(found[0])
        body = _section(
            self.texts[relative], self.headings[relative], heading_index
        )
        problems = []
        if not _states_environment_first(body):
            problems.append(
                "%s: Keys section must state that the process environment is "
                "checked before the credentials file" % relative
            )
        if not _states_names_not_values(body):
            problems.append(
                "%s: Keys section must state that config lists variable "
                "names, not values" % relative
            )
        if problems:
            self._fail(
                "Keys section lookup order and name-only config", problems
            )

    def test_install_has_both_model_examples_and_provider_entry(self):
        relative = "docs/INSTALL.md"
        text = self.texts[relative]
        problems = []

        for marker in ("Example (a)", "Example (b)"):
            if marker not in text:
                problems.append("%s: must contain %r" % (relative, marker))

        provider_example = None
        for snippet in self.snippets[relative]:
            if snippet["language"] != "json":
                continue
            try:
                data = json.loads(snippet["body"])
            except ValueError:
                continue
            if (isinstance(data, dict) and "providers" in data
                    and "reviewer_provider" in data):
                provider_example = data
                break
        if provider_example is None:
            problems.append(
                "%s: no JSON example contains both 'providers' and "
                "'reviewer_provider'" % relative
            )
        else:
            providers = provider_example.get("providers")
            entry = providers[0] if isinstance(providers, list) and providers else {}
            for field in ("name", "vendor", "base_url", "api",
                          "api_key_env", "models"):
                if not entry.get(field):
                    problems.append(
                        "%s: example (a) providers[0] must set %r"
                        % (relative, field)
                    )
            if entry.get("api_key_env") != "ANTHROPIC_API_KEY":
                problems.append(
                    "%s: example (a) api_key_env must be the variable name "
                    "ANTHROPIC_API_KEY, not a key value" % relative
                )
            if "api_key" in entry:
                problems.append(
                    "%s: example (a) must not contain a provider key value"
                    % relative
                )

        override = None
        for snippet in self.snippets[relative]:
            if snippet["language"] != "json":
                continue
            try:
                data = json.loads(snippet["body"])
            except ValueError:
                continue
            if (isinstance(data, dict) and "builder_provider" in data
                    and "reviewer_provider" in data):
                override = data
                break
        if override is None:
            problems.append(
                "%s: no JSON example is a per-job override with "
                "builder_provider and reviewer_provider" % relative
            )
        else:
            for field in ("builder_provider", "builder_model",
                          "reviewer_provider", "reviewer_model"):
                if not override.get(field):
                    problems.append(
                        "%s: example (b) must set %r" % (relative, field)
                    )
            if "providers" in override:
                problems.append(
                    "%s: example (b) must be harness_deepseek arguments, not "
                    "a config.json body with providers" % relative
                )

        if "harness_deepseek" not in text:
            problems.append("%s: must name harness_deepseek" % relative)
        if problems:
            self._fail("INSTALL.md model examples", problems)

    def test_keys_section_lists_the_vendor_variables(self):
        relative = "docs/INSTALL.md"
        found = _matches(self.headings[relative], ("Keys",))
        if len(found) != 1:
            self._fail(
                "Keys section vendor variables",
                ["%s: expected exactly one level-2 Keys heading" % relative],
            )
        heading_index = self.headings[relative].index(found[0])
        body = _section(
            self.texts[relative], self.headings[relative], heading_index
        )
        problems = []
        for term in (
            "DEEPSEEK_API_KEY",
            "ANTHROPIC_API_KEY",
        ):
            if term not in body:
                problems.append(
                    "%s: Keys section must name %r" % (relative, term)
                )
        if ("MOONSHOT_API_KEY" not in body
                and "KIMI_API_KEY" not in body):
            problems.append(
                "%s: Keys section must name MOONSHOT_API_KEY or KIMI_API_KEY"
                % relative
            )
        if problems:
            self._fail("Keys section vendor variables", problems)

    def test_privacy_patterns_allow_bare_username_reject_home_paths(self):
        def labels(text):
            return [label for label, pattern in _privacy_patterns()
                    if pattern.search(text)]

        # A bare account name in prose (e.g. "repository root") is not a path.
        self.assertEqual(
            labels("Run this from the repository root as root."), [])
        self.assertIn("macOS home path",
                      labels("Open " + "/Users/" + "root/project."))
        self.assertIn("Linux home path",
                      labels("Open " + "/home/" + "root/project."))
        self.assertIn("Windows home path",
                      labels(r"Open C:\Users\root\project."))

    def test_no_private_paths_emails_usernames_or_real_keys(self):
        problems = []
        for relative in DOC_ORDER:
            text = self.texts[relative]
            seen = set()
            for label, pattern in _privacy_patterns():
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    key = (label, line)
                    if key in seen:
                        continue
                    seen.add(key)
                    problems.append(
                        "%s: line %d contains a prohibited %s"
                        % (relative, line, label)
                    )
        if problems:
            self._fail(
                "no private paths, usernames, emails or real-looking keys",
                problems,
            )


if __name__ == "__main__":
    unittest.main()
