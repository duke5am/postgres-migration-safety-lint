"""A deliberately small, honest lexer for PostgreSQL migration files.

This is *not* a SQL parser and does not pretend to be one.  Its only job is to
turn a migration file into a list of tokens while correctly skipping the places
where a naive regular expression would report nonsense:

* ``-- line comments`` and ``/* block comments */`` (PostgreSQL block comments
  nest, so they are tracked with a depth counter),
* ``'single quoted strings'`` including the doubled ``''`` escape,
* ``E'escape strings'`` and the ``U&'...'`` / ``B'...'`` / ``X'...'`` prefixes,
* ``"double quoted identifiers"``,
* ``$tag$ dollar quoted strings $tag$`` -- the mechanism used by
  ``CREATE FUNCTION`` bodies, which is the single biggest source of false
  positives in regex-based SQL linters.

Everything else is passed through as a token, and statements are split on
semicolons that are outside of any of the constructs above.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["Token", "Statement", "tokenize", "split_statements", "LineIndex"]

_IDENT_START = re.compile(r"[A-Za-z_\u0080-\uffff]")
_IDENT_CONT = re.compile(r"[A-Za-z0-9_$\u0080-\uffff]")
# A dollar-quote tag: $ followed by an optional identifier, e.g. $$ or $body$.
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff]*)?\$")
# A string / bit-string / unicode-escape prefix that may sit before a quote.
_STRING_PREFIX = re.compile(r"[eEbBxXnN]", re.ASCII)

# Longest first, so that '->>' wins over '->'.
_OPERATORS = (
    "->>", "#>>", "!~~*", "!~~", "~~*", "<=>",
    "::", "->", "#>", "<=", ">=", "<>", "!=", "||", ":=", "=>", "~~",
)


_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,;)\]])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[])\s+")
# "numeric(12, 2)" -> "numeric(12,2)": a numeric argument list reads better
# without the space, while "REFERENCES users (id)" keeps its own.
_DIGIT_COMMA_SPACE = re.compile(r"(\d),\s+(\d)")
# "varchar (40)" -> "varchar(40)", but "UNIQUE (a)" keeps its space because the
# preceding word is a keyword, not a type name.
_SPACE_BEFORE_OPEN_AFTER_TYPE = re.compile(
    r"\b(varchar|character varying|char|character|numeric|decimal|bit|varbit|"
    r"timestamp|time|interval|float|double precision|real)\s+\(",
    re.IGNORECASE,
)


def normalise_sql_spacing(text: str) -> str:
    """Tidy whitespace in a token-joined SQL preview.

    Joining tokens with spaces turns ``varchar(40)`` into ``varchar ( 40 )``;
    this puts the punctuation back where a reader expects it.
    """
    text = re.sub(r"\s+", " ", text).strip()
    text = _SPACE_BEFORE_OPEN_AFTER_TYPE.sub(r"\1(", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _DIGIT_COMMA_SPACE.sub(r"\1,\2", text)
    return text


class LineIndex:
    """Maps character offsets to 1-based line numbers."""
    def __init__(self, text: str) -> None:
        self._starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._starts.append(i + 1)

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self._starts, offset)


@dataclass
class Token:
    """One lexed token.

    ``kind`` is one of ``word``, ``quoted_ident``, ``string``, ``number``,
    ``operator``, ``punct``, ``comment``, ``error``.

    ``normalized`` uppercases only *word* tokens (unquoted identifiers and
    keywords) so that keyword tests can never match inside a string literal or
    a quoted identifier.
    """

    kind: str
    text: str
    start: int
    end: int
    line: int

    @property
    def normalized(self) -> str:
        return self.text.upper() if self.kind == "word" else self.text

    @property
    def is_comment(self) -> bool:
        return self.kind == "comment"

    @property
    def is_code(self) -> bool:
        return self.kind not in ("comment", "error")

    def is_word(self, *words: str) -> bool:
        return self.kind == "word" and self.text.upper() in words

    def is_punct(self, *chars: str) -> bool:
        return self.kind == "punct" and self.text in chars


@dataclass
class Statement:
    """A semicolon-delimited slice of a migration file."""

    text: str
    tokens: list
    start_line: int
    end_line: int
    terminated: bool = True
    errors: list = field(default_factory=list)
    #: Line of the first *comment* token belonging to this statement.  Findings
    #: are reported against :attr:`start_line` (the first line of actual SQL),
    #: never against a leading comment block's line.
    comment_line: int = 0
    #: Character offset of the statement, including any leading comments.
    start_offset: int = 0
    end_offset: int = 0

    @property
    def verb(self) -> str:
        """The leading keyword, ignoring a leading ``WITH ... AS (...)`` clause."""
        return leading_verb(self.tokens)[0]

    def code_tokens(self) -> list:
        return [t for t in self.tokens if t.kind != "comment"]

    def truncated(self, limit: int = 72) -> str:
        """A one-line preview of the SQL, with leading comments stripped.

        Token text is joined and then normalised so the preview reads like the
        SQL that was written (``varchar(40)``, not ``varchar ( 40 )``).
        """
        code = self.code_tokens()
        if not code:
            flat = " ".join(self.text.split())
        else:
            flat = normalise_sql_spacing(" ".join(t.text for t in code))
        return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _read_dollar_quoted(text: str, i: int) -> Optional[tuple]:
    """If a dollar-quoted string starts at ``i``, return ``(end, tag)``."""
    m = _DOLLAR_TAG.match(text, i)
    if not m:
        return None
    tag = m.group(0)
    end = text.find(tag, m.end())
    if end == -1:
        return (len(text), tag)  # unterminated: consume the rest of the file
    return (end + len(tag), tag)


def tokenize(text: str) -> list:
    """Tokenize ``text``, skipping comments and all string-literal forms."""
    tokens: list = []
    index = LineIndex(text)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        start = i

        # -- line comment ---------------------------------------------------
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            tokens.append(Token("comment", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- block comment (PostgreSQL block comments nest) -----------------
        if ch == "/" and text.startswith("/*", i):
            depth = 0
            j = i
            while j < n:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    j += 1
            tokens.append(Token("comment", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- whitespace -----------------------------------------------------
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            i = j
            continue

        # -- dollar quoted string -------------------------------------------
        if ch == "$":
            dq = _read_dollar_quoted(text, i)
            if dq is not None:
                end, _tag = dq
                tokens.append(Token("string", text[i:end], i, end, index.line_of(i)))
                i = end
                continue

        # -- string literals -------------------------------------------------
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                if text[j] == "\\" and i > 0 and text[i - 1] in "eE":
                    j += 2
                    continue
                j += 1
            tokens.append(Token("string", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- E'...' / U&'...' / B'...' / X'...' prefixes ---------------------
        if (
            _STRING_PREFIX.match(ch)
            and i + 1 < n
            and text[i + 1] == "'"
            and not (i > 0 and _IDENT_CONT.match(text[i - 1]))
        ):
            j = i + 2
            prefix_is_escape = ch in "eE"
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                if text[j] == "\\" and prefix_is_escape:
                    j += 2
                    continue
                j += 1
            tokens.append(Token("string", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- quoted identifier ----------------------------------------------
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            tokens.append(Token("quoted_ident", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- word (keyword / identifier) -------------------------------------
        if _IDENT_START.match(ch):
            j = i + 1
            while j < n and _IDENT_CONT.match(text[j]):
                j += 1
            tokens.append(Token("word", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- number ----------------------------------------------------------
        if ch.isdigit():
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] in "._eE" or
                             (text[j] in "+-" and j > i and text[j - 1] in "eE")):
                j += 1
            tokens.append(Token("number", text[i:j], i, j, index.line_of(i)))
            i = j
            continue

        # -- operators / punctuation -----------------------------------------
        matched = None
        for op in _OPERATORS:
            if text.startswith(op, i):
                matched = op
                break
        if matched:
            tokens.append(Token("operator", matched, i, i + len(matched), index.line_of(i)))
            i += len(matched)
            continue

        kind = "punct" if ch in "(),;[]." else "operator"
        tokens.append(Token(kind, ch, start, i + 1, index.line_of(start)))
        i += 1
        continue

    return tokens


def split_statements(text: str, tokens: Optional[list] = None) -> list:
    """Split a migration file into semicolon-delimited :class:`Statement` objects.

    Semicolons inside strings, dollar-quoted bodies, comments and parentheses
    do not split a statement.  Pass pre-computed ``tokens`` to avoid lexing the
    same text twice.
    """
    if tokens is None:
        tokens = tokenize(text)
    statements: list = []
    buf: list = []
    depth = 0

    def flush(terminated: bool) -> None:
        code = [t for t in buf if t.kind != "comment"]
        if not code:
            buf.clear()
            return
        start = buf[0].start
        end = buf[-1].end
        statements.append(
            Statement(
                text=text[start:end],
                tokens=list(buf),
                # start_line is the first line of real SQL, so a leading comment
                # block never shifts where a finding points.
                start_line=code[0].line,
                end_line=code[-1].line,
                terminated=terminated,
                errors=[t.text for t in buf if t.kind == "error"],
                comment_line=buf[0].line if buf[0].kind == "comment" else 0,
                start_offset=start,
                end_offset=end,
            )
        )
        buf.clear()

    for tok in tokens:
        if tok.kind == "punct" and tok.text == "(":
            depth += 1
        elif tok.kind == "punct" and tok.text == ")":
            depth = max(0, depth - 1)
        elif tok.kind == "punct" and tok.text == ";" and depth == 0:
            flush(True)
            continue
        buf.append(tok)

    flush(False)
    return statements


def leading_verb(tokens: list) -> tuple:
    """Return ``(verb, index)`` for a statement, skipping a leading WITH clause.

    ``WITH recent AS (SELECT ...) DELETE FROM t WHERE ...`` has verb ``DELETE``.
    Returns ``("", -1)`` when no verb can be determined.
    """
    code = [t for t in tokens if t.kind != "comment"]
    if not code:
        return ("", -1)

    def word_at(k: int) -> str:
        return code[k].normalized if k < len(code) and code[k].kind == "word" else ""

    if word_at(0) != "WITH":
        return (word_at(0), 0 if code[0].kind == "word" else -1)

    k = 1
    if word_at(k) == "RECURSIVE":
        k += 1
    depth = 0
    while k < len(code):
        t = code[k]
        if t.kind == "punct" and t.text == "(":
            depth += 1
        elif t.kind == "punct" and t.text == ")":
            depth -= 1
            if depth < 0:
                break
        elif depth == 0 and t.kind == "word" and t.normalized in (
            "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "VALUES", "TABLE",
        ):
            return (t.normalized, k)
        k += 1
    return ("WITH", 0)
