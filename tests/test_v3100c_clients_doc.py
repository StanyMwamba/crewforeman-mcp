"""Contract tests for docs/CLIENTS.md (crewforeman-mcp 3.10.0, Part C).

Standard library only, Python 3.9 compatible. Intended to live at
``repo/tests/test_v3100c_clients_doc.py`` and run with::

    python3 -m unittest discover -s tests -q

The document path is derived from this file (``../docs/CLIENTS.md``).  For
local QA against a candidate copy, override it with the environment variable
``CREWFOREMAN_CLIENTS_DOC``.
"""

import json
import os
import re
import unittest
from pathlib import Path


DOC_ENV = "CREWFOREMAN_CLIENTS_DOC"
DEFAULT_DOC = Path(__file__).resolve().parent.parent / "docs" / "CLIENTS.md"

# (label used in failure messages, accepted heading prefixes)
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

HEADING_RE = re.compile(r"^(#{2})(?!#)[ \t]*(.+?)[ \t]*$", re.M)
FENCE_RE = re.compile(r"^```[ \t]*([^\n`]*)[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
MARKERS = ("Config", "Confirm", "Pitfall")
SECTION_SNIPPET_LANGS = {"json", "toml", "bash", "sh", "shell", "zsh"}
KEY_RE = re.compile(r"DEEPSEEK_API_KEY\s*[\"']?\s*[:=]")


# ---------------------------------------------------------------------------
# Minimal strict TOML fallback (subset used by the documentation snippets)
# ---------------------------------------------------------------------------


class TomlFallbackError(ValueError):
    """Raised when the fallback TOML parser sees unsupported/malformed TOML."""


_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_INT_RE = re.compile(r"[+-]?(?:0|[1-9](?:_?\d)*)")
_ESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _strip_toml_comment(line):
    """Remove a # comment that is outside a basic string."""
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "#":
            return line[:index]
    return line


def _bracket_delta(line):
    """Net []/{} depth of a line, ignoring brackets inside basic strings."""
    delta = 0
    in_string = False
    escaped = False
    for char in line:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            delta += 1
        elif char in "]}":
            delta -= 1
    return delta


def _toml_logical_lines(text):
    """Yield (first line number, logical line), joining multiline values."""
    pending = []
    depth = 0
    start = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_toml_comment(raw)
        if start is None:
            if not line.strip():
                continue
            start = lineno
        pending.append(line)
        depth += _bracket_delta(line)
        if depth <= 0:
            yield start, "\n".join(pending)
            pending = []
            depth = 0
            start = None
    if pending:
        yield start, "\n".join(pending)


def _split_assignment(line, lineno):
    """Split ``key = value`` at the first = outside strings/tables/arrays."""
    in_string = False
    escaped = False
    depth = 0
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "=" and depth == 0:
            return line[:index], line[index + 1:]
    raise TomlFallbackError("line %d: expected 'key = value'" % lineno)


class _TomlValueParser(object):
    def __init__(self, text, lineno):
        self.text = text
        self.pos = 0
        self.lineno = lineno

    def error(self, message):
        snippet = self.text[max(0, self.pos - 10):self.pos + 20]
        raise TomlFallbackError(
            "line %d: %s near %r" % (self.lineno, message, snippet)
        )

    def skip_ws(self):
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def parse(self):
        self.skip_ws()
        value = self.parse_value()
        self.skip_ws()
        if self.pos != len(self.text):
            self.error("trailing characters after value")
        return value

    def _boundary(self, index):
        return index >= len(self.text) or self.text[index] in " \t\r\n,]}"

    def parse_value(self):
        self.skip_ws()
        if self.pos >= len(self.text):
            self.error("missing value")
        char = self.text[self.pos]
        if char == '"':
            return self.parse_basic_string()
        if char == "[":
            return self.parse_array()
        if char == "{":
            return self.parse_inline_table()
        if self.text.startswith("true", self.pos) and self._boundary(self.pos + 4):
            self.pos += 4
            return True
        if self.text.startswith("false", self.pos) and self._boundary(self.pos + 5):
            self.pos += 5
            return False
        match = _INT_RE.match(self.text, self.pos)
        if match:
            end = match.end()
            if end < len(self.text) and self.text[end] not in " \t\r\n,]}":
                self.error("invalid integer")
            self.pos = end
            return int(match.group(0).replace("_", ""))
        self.error("unsupported value (expected string, array, inline table, boolean or integer)")

    def parse_basic_string(self):
        if self.text[self.pos] != '"':
            self.error("expected basic string")
        self.pos += 1
        out = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '"':
                self.pos += 1
                return "".join(out)
            if char == "\n":
                self.error("newline in basic string")
            if char != "\\":
                out.append(char)
                self.pos += 1
                continue
            self.pos += 1
            if self.pos >= len(self.text):
                self.error("unterminated escape")
            escape = self.text[self.pos]
            if escape in _ESCAPES:
                out.append(_ESCAPES[escape])
                self.pos += 1
            elif escape in ("u", "U"):
                width = 4 if escape == "u" else 8
                digits = self.text[self.pos + 1:self.pos + 1 + width]
                if len(digits) != width or not re.match(
                    r"^[0-9A-Fa-f]{%d}$" % width, digits
                ):
                    self.error("invalid unicode escape")
                out.append(chr(int(digits, 16)))
                self.pos += 1 + width
            else:
                self.error("unknown escape \\%s" % escape)
        self.error("unterminated basic string")

    def parse_array(self):
        self.pos += 1
        items = []
        self.skip_ws()
        if self.pos < len(self.text) and self.text[self.pos] == "]":
            self.pos += 1
            return items
        while True:
            items.append(self.parse_value())
            self.skip_ws()
            if self.pos >= len(self.text):
                self.error("unterminated array")
            char = self.text[self.pos]
            if char == ",":
                self.pos += 1
                self.skip_ws()
                if self.pos < len(self.text) and self.text[self.pos] == "]":
                    self.pos += 1
                    return items
                continue
            if char == "]":
                self.pos += 1
                return items
            self.error("expected ',' or ']' in array")

    def parse_inline_table(self):
        self.pos += 1
        table = {}
        self.skip_ws()
        if self.pos < len(self.text) and self.text[self.pos] == "}":
            self.pos += 1
            return table
        while True:
            parts = self.parse_key_path()
            self.skip_ws()
            if self.pos >= len(self.text) or self.text[self.pos] != "=":
                self.error("expected '=' in inline table")
            self.pos += 1
            value = self.parse_value()
            _assign_value(table, parts, value, self.lineno)
            self.skip_ws()
            if self.pos >= len(self.text):
                self.error("unterminated inline table")
            char = self.text[self.pos]
            if char == ",":
                self.pos += 1
                continue
            if char == "}":
                self.pos += 1
                return table
            self.error("expected ',' or '}' in inline table")

    def parse_key_path(self):
        parts = []
        while True:
            self.skip_ws()
            if self.pos >= len(self.text):
                self.error("missing key")
            if self.text[self.pos] == '"':
                parts.append(self.parse_basic_string())
            else:
                match = _BARE_KEY_RE.match(self.text, self.pos)
                if not match:
                    self.error("invalid bare key")
                parts.append(match.group(0))
                self.pos = match.end()
            self.skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ".":
                self.pos += 1
                continue
            return parts


def _parse_key_path_text(text, lineno):
    parser = _TomlValueParser(text, lineno)
    parts = parser.parse_key_path()
    parser.skip_ws()
    if parser.pos != len(parser.text):
        parser.error("trailing characters after key")
    return parts


def _assign_value(table, parts, value, lineno):
    node = table
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            raise TomlFallbackError(
                "line %d: key %r is not a table" % (lineno, part)
            )
        node = child
    last = parts[-1]
    if last in node:
        raise TomlFallbackError("line %d: duplicate key %r" % (lineno, last))
    node[last] = value


def parse_toml_fallback(text):
    """Parse the documented TOML subset into a dict; reject anything else."""
    root = {}
    current = root
    defined_tables = set()
    for lineno, logical in _toml_logical_lines(text):
        line = logical.strip()
        if not line:
            continue
        if line.startswith("[["):
            raise TomlFallbackError(
                "line %d: arrays of tables are outside the supported subset" % lineno
            )
        if line.startswith("["):
            if not line.endswith("]"):
                raise TomlFallbackError("line %d: malformed table header" % lineno)
            inner = line[1:-1].strip()
            if not inner:
                raise TomlFallbackError("line %d: empty table header" % lineno)
            parts = tuple(_parse_key_path_text(inner, lineno))
            if parts in defined_tables:
                raise TomlFallbackError(
                    "line %d: duplicate table header %r" % (lineno, ".".join(parts))
                )
            defined_tables.add(parts)
            table = root
            for part in parts:
                child = table.get(part)
                if child is None:
                    child = {}
                    table[part] = child
                elif not isinstance(child, dict):
                    raise TomlFallbackError(
                        "line %d: %r is not a table" % (lineno, part)
                    )
                table = child
            current = table
            continue
        key_text, value_text = _split_assignment(logical, lineno)
        parts = _parse_key_path_text(key_text, lineno)
        value = _TomlValueParser(value_text, lineno).parse()
        _assign_value(current, parts, value, lineno)
    return root


def parse_toml(text):
    """Use tomllib when present (3.11+), otherwise the checked-in fallback."""
    try:
        import tomllib  # noqa: F401  (Python 3.11+)
    except ImportError:
        return parse_toml_fallback(text)
    return tomllib.loads(text)


# ---------------------------------------------------------------------------
# Documentation helpers
# ---------------------------------------------------------------------------


def _clean_heading(title):
    title = title.strip()
    title = re.sub(r"[ \t]+#+[ \t]*$", "", title)
    return title.strip()


def _normalise(text):
    return re.sub(r"\s+", " ", text.replace("`", "").strip().lower())


def _heading_matches(title, expected):
    actual = _normalise(title)
    wanted = _normalise(expected)
    if actual == wanted:
        return True
    if actual.startswith(wanted):
        rest = actual[len(wanted):]
        return bool(rest) and rest[0] in " (:,/-+\u2014&"
    return False


def _level2_headings(text):
    headings = []
    for match in HEADING_RE.finditer(text):
        headings.append((match.start(), _clean_heading(match.group(2))))
    return headings


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
            }
        )
    return found


def _section(text, headings, start_index):
    end = len(text)
    for position, _title in headings:
        if position > start_index:
            end = position
            break
    return text[start_index:end]


def _display_path(path):
    """Keep the user's home directory out of unittest failure output."""
    text = str(path)
    home = str(Path.home()).rstrip("/\\")
    if home and home not in ("/", "\\") and text.startswith(home):
        return "~" + text[len(home):]
    return text


def _privacy_patterns():
    patterns = [
        ("macOS home path", re.compile(r"/Users/[^\s\"'`]+")),
        ("Linux home path", re.compile(r"/home/[^\s\"'`]+")),
        (
            "Windows home path",
            re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'`]+"),
        ),
        (
            "email address",
            re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        ),
    ]
    home = Path.home()
    home_text = str(home).rstrip("/\\")
    if home_text and home_text not in ("/", "\\"):
        patterns.append(
            (
                "current home directory",
                re.compile(re.escape(home_text), re.I),
            )
        )
    basename = home.name
    generic = {"user", "users", "home", "root", "runner", "admin", "username"}
    if len(basename) >= 3 and basename.lower() not in generic:
        patterns.append(
            (
                "current home-directory basename",
                re.compile(
                    r"(?<![A-Za-z0-9._-])"
                    + re.escape(basename)
                    + r"(?![A-Za-z0-9._-])",
                    re.I,
                ),
            )
        )
    return patterns


# ---------------------------------------------------------------------------
# Tests for the fallback parser itself (always run, including Python 3.9)
# ---------------------------------------------------------------------------


class TomlFallbackSelfTest(unittest.TestCase):
    VALID = """\
# comment
[mcp_servers.crewforeman-mcp]
command = "/path/to/crewforeman-mcp"
args = ["--config", "/path/to/config.json"]
env = { DEEPSEEK_API_KEY = "YOUR_DEEPSEEK_API_KEY" }
enabled = true
timeout = 30

[ui]
"quoted.key" = "value"
items = [
  "one",
  "two",
]
"""

    def test_valid_toml_subset_parses_to_dict(self):
        parsed = parse_toml_fallback(self.VALID)
        server = parsed["mcp_servers"]["crewforeman-mcp"]
        self.assertEqual(server["command"], "/path/to/crewforeman-mcp")
        self.assertEqual(server["args"], ["--config", "/path/to/config.json"])
        self.assertEqual(
            server["env"], {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}
        )
        self.assertIs(server["enabled"], True)
        self.assertEqual(server["timeout"], 30)
        self.assertEqual(parsed["ui"]["quoted.key"], "value")
        self.assertEqual(parsed["ui"]["items"], ["one", "two"])

    def test_malformed_toml_is_rejected(self):
        broken = (
            '[mcp_servers.demo]\ncommand = "/path/to/x"\n'
            'args = ["a", "b"\n'
        )
        with self.assertRaises(TomlFallbackError):
            parse_toml_fallback(broken)
        with self.assertRaises(TomlFallbackError):
            parse_toml_fallback('command = "bad\\q"\n')
        with self.assertRaises(TomlFallbackError):
            parse_toml_fallback("[unclosed\ncommand = 1\n")

    def test_dispatch_parses_same_valid_sample(self):
        self.assertEqual(parse_toml(self.VALID), parse_toml_fallback(self.VALID))


# ---------------------------------------------------------------------------
# Tests for docs/CLIENTS.md
# ---------------------------------------------------------------------------


class ClientsDocContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ.get(DOC_ENV, str(DEFAULT_DOC)))
        if not path.is_file():
            raise AssertionError(
                "CLIENTS document not found at %s (set %s to test a copy)"
                % (path, DOC_ENV)
            )
        cls.doc_path = path
        cls.text = path.read_text(encoding="utf-8")
        cls.headings = _level2_headings(cls.text)
        cls.snippets = _snippets(cls.text)

    def _matches(self, aliases):
        return [
            (position, title)
            for position, title in self.headings
            if any(_heading_matches(title, alias) for alias in aliases)
        ]

    def test_one_level2_heading_per_client(self):
        problems = []
        for label, aliases in CLIENTS:
            matches = self._matches(aliases)
            if len(matches) != 1:
                problems.append(
                    "%s: expected exactly 1 level-2 heading matching %s, found %d (%s)"
                    % (
                        label,
                        " / ".join(repr(alias) for alias in aliases),
                        len(matches),
                        ", ".join(repr(title) for _pos, title in matches) or "none",
                    )
                )
        if problems:
            self.fail("Heading problems in %s:\n  %s" % (_display_path(self.doc_path), "\n  ".join(problems)))

    def test_every_json_and_toml_snippet_parses(self):
        problems = []
        languages = []
        for index, snippet in enumerate(self.snippets, 1):
            language = snippet["language"]
            languages.append(language)
            try:
                if language == "json":
                    json.loads(snippet["body"])
                elif language == "toml":
                    parse_toml(snippet["body"])
            except Exception as exc:  # noqa: BLE001 - report every bad snippet
                problems.append(
                    "%s snippet #%d (offset %d): %s"
                    % (language.upper(), index, snippet["start"], exc)
                )
        for required in ("json", "toml"):
            if required not in languages:
                problems.append(
                    "no fenced %s snippet found (documented launch configs need one)"
                    % required.upper()
                )
        if problems:
            self.fail("Snippet problems in %s:\n  %s" % (_display_path(self.doc_path), "\n  ".join(problems)))

    def test_client_sections_have_required_markers(self):
        problems = []
        for label, aliases in CLIENTS:
            matches = self._matches(aliases)
            if len(matches) != 1:
                problems.append("%s: expected exactly one section, found %d" % (label, len(matches)))
                continue
            start = matches[0][0]
            body = _section(self.text, self.headings, start)
            for marker in MARKERS:
                if not re.search(r"\b" + marker, body):
                    problems.append("%s: missing %r marker" % (label, marker))
            section_snippets = [
                snippet
                for snippet in self.snippets
                if start <= snippet["start"] < start + len(body)
            ]
            if not any(
                snippet["language"] in SECTION_SNIPPET_LANGS
                for snippet in section_snippets
            ):
                problems.append(
                    "%s: no fenced json/toml/bash snippet" % label
                )
            if "crewforeman-mcp" not in body:
                problems.append("%s: does not mention crewforeman-mcp" % label)
        if problems:
            self.fail("Section problems in %s:\n  %s" % (_display_path(self.doc_path), "\n  ".join(problems)))

    def test_no_private_paths_or_email_addresses(self):
        problems = []
        for name, pattern in _privacy_patterns():
            for match in pattern.finditer(self.text):
                problems.append("%s: %r" % (name, match.group(0)))
        if problems:
            self.fail(
                "Privacy violations in %s:\n  %s"
                % (_display_path(self.doc_path), "\n  ".join(problems))
            )

    def test_launch_placeholder_key_and_no_real_home_in_snippets(self):
        problems = []
        launch_snippets = [
            snippet
            for snippet in self.snippets
            if snippet["language"] in {"json", "toml", "bash", "sh", "shell", "zsh"}
        ]
        if not any(
            "/path/to/crewforeman-mcp" in snippet["body"]
            for snippet in launch_snippets
        ):
            problems.append(
                "no snippet uses the placeholder /path/to/crewforeman-mcp"
            )
        if not any(KEY_RE.search(snippet["body"]) for snippet in launch_snippets):
            problems.append(
                "no snippet sets DEEPSEEK_API_KEY (key: value, key = value or key=value)"
            )
        home = str(Path.home()).rstrip("/\\")
        for index, snippet in enumerate(launch_snippets, 1):
            body = snippet["body"]
            if re.search(r"/Users/", body) or re.search(r"/home/", body):
                problems.append(
                    "snippet #%d (%s) contains an absolute real home path"
                    % (index, snippet["language"])
                )
            if re.search(r"[A-Za-z]:[\\/]+Users[\\/]", body):
                problems.append(
                    "snippet #%d (%s) contains a Windows real home path"
                    % (index, snippet["language"])
                )
            if home and home not in ("/", "\\") and home.lower() in body.lower():
                problems.append(
                    "snippet #%d (%s) contains the current home directory"
                    % (index, snippet["language"])
                )
        if problems:
            self.fail(
                "Launch snippet problems in %s:\n  %s"
                % (_display_path(self.doc_path), "\n  ".join(problems))
            )


if __name__ == "__main__":
    unittest.main()
