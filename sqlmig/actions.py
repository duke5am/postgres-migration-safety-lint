"""Action-level parsing for ``ALTER TABLE`` and constraint classification.

This module deliberately avoids slicing token lists: every routine walks the
statement's token list with an explicit cursor index. Slicing token lists was
the source of an off-by-one bug that made ``ADD CONSTRAINT fk`` look like an
unnamed constraint, so the cursor is now the only way tokens are read.
"""

from __future__ import annotations

from typing import Optional

from .lexer import normalise_sql_spacing


class Cursor:
    """A read cursor over a list of tokens.

    ``peek()``/``take()`` never modify the underlying list, and ``mark``/``reset``
    let a caller try a keyword sequence and back out of it.
    """

    __slots__ = ("tokens", "pos")

    def __init__(self, tokens: list, pos: int = 0) -> None:
        self.tokens = tokens
        self.pos = pos

    def __len__(self) -> int:
        return len(self.tokens) - self.pos

    @property
    def at_end(self) -> bool:
        return self.pos >= len(self.tokens)

    def peek(self, offset: int = 0):
        index = self.pos + offset
        if 0 <= index < len(self.tokens):
            return self.tokens[index]
        return None

    def take(self):
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    def mark(self) -> int:
        return self.pos

    def reset(self, mark: int) -> None:
        self.pos = mark

    def keyword(self, offset: int = 0) -> str:
        token = self.peek(offset)
        if token is None or token.kind != "word":
            return ""
        return token.text.upper()

    def accept(self, *words: str) -> bool:
        """Consume ``words`` in order only when all of them are present."""
        mark = self.pos
        for word in words:
            if self.keyword() != word:
                self.pos = mark
                return False
            self.pos += 1
        return True

    def accept_if_exists(self) -> bool:
        return self.accept("IF", "EXISTS")

    def accept_if_not_exists(self) -> bool:
        return self.accept("IF", "NOT", "EXISTS")

    def name(self) -> Optional[str]:
        """Read one possibly-qualified identifier, or ``None``."""
        token = self.peek()
        if token is None or token.kind not in ("word", "quoted_ident"):
            return None
        parts = [token.text.replace('"', "")]
        self.pos += 1
        while True:
            dot = self.peek()
            if dot is None or dot.kind != "punct" or dot.text != ".":
                break
            nxt = self.peek(1)
            if nxt is None or nxt.kind not in ("word", "quoted_ident"):
                break
            parts.append(nxt.text.replace('"', ""))
            self.pos += 2
        return ".".join(parts)

    def raw_name(self) -> Optional[str]:
        """Read one identifier exactly as written (quotes kept)."""
        token = self.peek()
        if token is None or token.kind not in ("word", "quoted_ident"):
            return None
        self.pos += 1
        return token.text

    def type_text(self, stops: frozenset) -> str:
        """Read a type expression up to (but not including) the first stop word.

        Parenthesised arguments are attached to the type name with no space, so
        ``varchar ( 40 )`` is reported as ``varchar(40)`` -- the spelling a
        reader expects.
        """
        parts: list = []
        depth = 0
        while not self.at_end:
            token = self.peek()
            if token.kind == "punct" and token.text == "(":
                if depth == 0 and parts:
                    parts[-1] = parts[-1] + "("
                    self.pos += 1
                    depth += 1
                    continue
                depth += 1
            elif token.kind == "punct" and token.text in (")", ","):
                if token.text == ")" and depth == 0:
                    break
                if token.text == ")":
                    depth -= 1
                if parts:
                    # Glue the separator to the previous part: "numeric(12,2)".
                    parts[-1] = parts[-1] + token.text
                    self.pos += 1
                    continue
            elif depth == 0 and token.kind == "word" and parts and token.text.upper() in stops:
                break
            parts.append(token.text.replace('"', ""))
            self.pos += 1
        return normalise_sql_spacing(" ".join(parts))


def find_keyword(tokens: list, word: str, start: int = 0, top_level: bool = False) -> int:
    """Index of the first matching keyword at/after ``start``, else ``-1``."""
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.kind == "punct" and token.text == "(":
            depth += 1
            continue
        if token.kind == "punct" and token.text == ")":
            depth -= 1
            continue
        if top_level and depth != 0:
            continue
        if token.kind == "word" and token.text.upper() == word:
            return index
    return -1


def has_keyword_sequence(tokens: list, start: int, *words: str) -> bool:
    """True when ``words`` appear consecutively starting at ``start``."""
    for offset, word in enumerate(words):
        index = start + offset
        if index >= len(tokens):
            return False
        token = tokens[index]
        if token.kind != "word" or token.text.upper() != word:
            return False
    return True


def find_sequence(tokens: list, *words: str) -> int:
    """Index of any position where ``words`` appear consecutively, else ``-1``."""
    if not words:
        return -1
    last = len(tokens) - len(words)
    for index in range(0, last + 1):
        if has_keyword_sequence(tokens, index, *words):
            return index
    return -1
