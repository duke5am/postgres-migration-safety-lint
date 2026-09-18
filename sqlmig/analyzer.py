"""The analysis engine: turns lexed migration statements into findings.

Design constraints, stated up front so the code stays honest:

* It is **static**.  No database is contacted, no schema is introspected, and
  no Postgres version is assumed beyond a documented default.
* It never claims a lock *will* be taken on *your* database.  It reports the
  operation it can see, the lock that operation classically requires, and the
  rewrite that removes the risk.
* Comments, string literals and dollar-quoted function bodies are skipped by
  the lexer, so a ``DROP TABLE`` inside a comment or a ``CREATE FUNCTION`` body
  is not a finding.  This is tested.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import rules
from .actions import (
    Cursor,
    find_keyword,
    find_sequence,
    has_keyword_sequence,
)
from .lexer import Statement, split_statements, leading_verb, tokenize

#: Operations that take an ACCESS EXCLUSIVE lock on at least one table and for
#: which a lock_timeout budget is therefore worth setting.
ACCESS_EXCLUSIVE_KINDS = frozenset(
    {
        "alter-table",
        "drop-table",
        "drop-column",
        "add-column",
        "alter-column-type",
        "set-not-null",
        "add-constraint",
        "validate-constraint",
        "drop-constraint",
        "rename",
        "truncate",
        "create-index",
        "reindex",
    }
)

#: Statements that PostgreSQL refuses to run inside a transaction block.
ILLEGAL_IN_TRANSACTION = {
    "VACUUM": "VACUUM cannot run inside a transaction block",
    "CREATE DATABASE": "CREATE DATABASE cannot run inside a transaction block",
    "DROP DATABASE": "DROP DATABASE cannot run inside a transaction block",
}

#: ``REINDEX`` is refused in a transaction block only for the whole-database
#: form; ``REINDEX TABLE`` works but takes a strong lock, so it is reported as a
#: caution rather than as an error.
REINDEX_CAUTION = (
    "REINDEX takes an ACCESS EXCLUSIVE lock for the whole rebuild and "
    "cancels concurrent queries; on PostgreSQL 12+ prefer "
    "REINDEX ... CONCURRENTLY, which cannot run inside a transaction block "
    "and is not supported for every index type."
)

DEFAULT_LOCK_TIMEOUT = "5s"

#: Types that PostgreSQL converts in place (no table rewrite) when widening.
_SAFE_TARGETS = {"TEXT", "VARCHAR", "CHARACTER VARYING", "NUMERIC", "DECIMAL"}

_RE_NUMERIC = re.compile(r"^(NUMERIC|DECIMAL)\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\)$")
_RE_VARCHAR = re.compile(r"^(VARCHAR|CHARACTER VARYING)\(\s*(\d+)\s*\)$")


@dataclass
class Finding:
    line: int
    rule_id: str
    risk: str
    confidence: str
    category: str
    statement: str
    message: str
    suggestion: str = ""
    table: Optional[str] = None
    #: Optional machine-readable subject (used for constraint names, so a later
    #: ``VALIDATE CONSTRAINT`` can retire a ``constraint-never-validated`` note).
    detail: Optional[str] = None

    @property
    def title(self) -> str:
        return rules.title_for(self.rule_id)

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "rule": self.rule_id,
            "title": self.title,
            "risk": self.risk,
            "confidence": self.confidence,
            "category": self.category,
            "table": self.table,
            "detail": self.detail,
            "statement": self.statement,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class Operation:
    """A single classified statement, with its lock-risk estimate."""

    line: int
    kind: str
    table: Optional[str]
    risk: str
    confidence: str
    statement: str
    note: str = ""

    @property
    def requires_access_exclusive(self) -> bool:
        return self.kind in ACCESS_EXCLUSIVE_KINDS

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "kind": self.kind,
            "table": self.table,
            "risk": self.risk,
            "confidence": self.confidence,
            "statement": self.statement,
            "note": self.note,
            "access_exclusive": self.requires_access_exclusive,
        }


@dataclass
class FileReport:
    path: str
    findings: list = field(default_factory=list)
    operations: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)
    statement_count: int = 0
    total_lines: int = 0
    in_transaction: bool = False
    #: ``NOT VALID`` constraints this file adds and does not validate itself, as
    #: ``{"name", "line", "table"}`` dicts.  Used by :func:`analyse_path` to tell
    #: "validate it in the next migration" apart from "never validated".
    unvalidated_constraints: list = field(default_factory=list)
    #: Constraint names (lower-cased) this file validates with
    #: ``VALIDATE CONSTRAINT``, consumed by :func:`analyse_path`.
    validated_constraints: set = field(default_factory=set)

    @property
    def problems(self) -> list:
        """Findings that are actually problems (notes excluded)."""
        return [f for f in self.findings if not rules.is_note(f.rule_id)]

    @property
    def notes(self) -> list:
        """Emissions that recorded a checked-and-fine pattern."""
        return [f for f in self.findings if rules.is_note(f.rule_id)]

    @property
    def worst_risk(self) -> Optional[str]:
        if not self.problems:
            return None
        return min((f.risk for f in self.problems), key=rules.rank)

    def filtered(self, threshold: str) -> list:
        return [f for f in self.problems if rules.at_or_above(f.risk, threshold)]

    def to_dict(self, threshold: str = "low") -> dict:
        shown = self.filtered(threshold)
        return {
            "path": self.path,
            "statements": self.statement_count,
            "lines": self.total_lines,
            "in_transaction": self.in_transaction,
            "worst_risk": self.worst_risk,
            "finding_count": len(self.problems),
            "note_count": len(self.notes),
            "reported_finding_count": len(shown),
            "parse_errors": self.parse_errors,
            "findings": [f.to_dict() for f in shown],
            "checked_and_ok": [f.to_dict() for f in self.notes],
            "operations": [o.to_dict() for o in self.operations],
        }


# --------------------------------------------------------------------------
# token helpers
# --------------------------------------------------------------------------

def _code(tokens: list) -> list:
    return [t for t in tokens if t.kind != "comment"]


def _is_word(tok, *words: str) -> bool:
    return tok.kind == "word" and tok.text.upper() in words


def _is_punct(tok, *chars: str) -> bool:
    return tok.kind == "punct" and tok.text in chars


def _words_after(code: list, i: int, *words: str) -> bool:
    """True when ``words`` appear in order starting at index ``i``."""
    for k, w in enumerate(words):
        j = i + k
        if j >= len(code) or not _is_word(code[j], w):
            return False
    return True


def _find_word(code: list, word: str, start: int = 0, top_level: bool = False) -> int:
    """Index of the first matching keyword at/after ``start``, else -1."""
    depth = 0
    for i in range(start, len(code)):
        tok = code[i]
        if _is_punct(tok, "("):
            depth += 1
            continue
        if _is_punct(tok, ")"):
            depth -= 1
            continue
        if top_level and depth != 0:
            continue
        if _is_word(tok, word):
            return i
    return -1


def _matching_paren(code: list, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(code)):
        if _is_punct(code[i], "("):
            depth += 1
        elif _is_punct(code[i], ")"):
            depth -= 1
            if depth == 0:
                return i
    return len(code) - 1


def _read_name(code: list, i: int) -> tuple:
    """Read a possibly schema-qualified name.

    Returns ``(display_name, next_index)``.  Display names use the token text
    as written, minus quotes, e.g. ``public.users``.
    """
    parts: list = []
    depth = 0
    while i < len(code):
        tok = code[i]
        if _is_punct(tok, "("):
            depth += 1
        elif _is_punct(tok, ")"):
            depth = max(0, depth - 1)
        if depth == 0 and tok.kind in ("word", "quoted_ident"):
            parts.append(tok.text.strip('"'))
            i += 1
            if i < len(code) and _is_punct(code[i], "."):
                i += 1
                continue
            break
        if depth == 0 and parts:
            break
        i += 1
    return (".".join(parts) if parts else None, i)


def _ident_after(code: list, i: int) -> tuple:
    """Read a single identifier at ``i`` (skipping no keywords).

    Returns ``(name, next_index)``; ``name`` is ``None`` when no identifier sits
    at ``i``.  Quotes are stripped so that a quoted identifier compares equal to
    its unquoted spelling.
    """
    if i >= len(code):
        return (None, i)
    tok = code[i]
    if tok.kind in ("word", "quoted_ident"):
        return (tok.text.strip('"'), i + 1)
    return (None, i)


def _ident_at(code: list, i: int) -> tuple:
    """Alias for :func:`_ident_after` (explicitly reads the identifier at ``i``)."""
    return _ident_after(code, i)


def _type_text(code: list, i: int) -> tuple:
    """Read a column type starting at ``i``, stopping at USING/COLLATE/;/etc."""
    stops = {
        "USING", "COLLATE", "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE",
        "REFERENCES", "CHECK", "CONSTRAINT", "GENERATED", "IDENTITY", "STORAGE",
        "COMPRESSION", "STATISTICS",
    }
    parts: list = []
    depth = 0
    while i < len(code):
        tok = code[i]
        if _is_punct(tok, "("):
            depth += 1
        elif _is_punct(tok, ")"):
            if depth == 0:
                break
            depth -= 1
        if depth == 0 and tok.kind == "word" and tok.text.upper() in stops and parts:
            break
        parts.append(tok.text)
        i += 1
    text = " ".join(parts).strip()
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*\)\s*", ")", text)
    text = re.sub(r"\s*,\s*", ",", text)
    return (text, i)


def _split_top_level_commas(code: list) -> list:
    """Split a token list on commas that are not inside parentheses."""
    groups: list = [[]]
    depth = 0
    for tok in code:
        if _is_punct(tok, "("):
            depth += 1
        elif _is_punct(tok, ")"):
            depth -= 1
        if _is_punct(tok, ",") and depth == 0:
            groups.append([])
            continue
        groups[-1].append(tok)
    return [g for g in groups if g]


def _join_text(tokens: list, limit: int = 72) -> str:
    flat = " ".join(t.text for t in tokens)
    flat = re.sub(r"\s+([,)])", r"\1", flat)
    flat = re.sub(r"\(\s+", "(", flat)
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# --------------------------------------------------------------------------
# type-change classification
# --------------------------------------------------------------------------

def _parse_numeric_precision(text: str) -> Optional[tuple]:
    m = _RE_NUMERIC.match(text.strip().upper())
    if not m:
        return None
    return (int(m.group(2)), int(m.group(3)) if m.group(3) else 0)


def _parse_varchar_len(text: str) -> Optional[int]:
    up = text.strip().upper()
    if up in ("TEXT", "VARCHAR", "CHARACTER VARYING", "BPCHAR", "CHARACTER"):
        return None  # unbounded: widening target
    m = _RE_VARCHAR.match(up)
    if not m:
        return None
    return int(m.group(2))


def classify_type_change(old_type: str, new_type: str) -> tuple:
    """Return ``(verdict, detail)`` with verdict in safe/unsafe/unknown.

    Only conversions that PostgreSQL performs as a catalog-only change when
    *widening* are called safe, and only when the direction is provably a
    widening.  Everything else is reported as a rewrite, or as unknown when the
    two type names do not match a pattern this tool understands.
    """
    old = (old_type or "").strip().upper()
    new = (new_type or "").strip().upper()
    if not old or not new:
        return ("unknown", "could not read one side of the type change")

    if old == new:
        return ("safe", f"{old} -> {new} is a no-op")

    old_vc = _parse_varchar_len(old)
    new_vc = _parse_varchar_len(new)
    old_is_textish = old in ("TEXT", "VARCHAR", "CHARACTER VARYING") or old_vc is not None
    new_is_textish = new in ("TEXT", "VARCHAR", "CHARACTER VARYING") or new_vc is not None

    if old_is_textish and new_is_textish:
        if new in ("TEXT", "VARCHAR", "CHARACTER VARYING") and old_vc is not None:
            return ("safe", f"removing the length limit ({old} -> {new}) does not rewrite the table")
        if new_vc is not None and old_vc is not None and new_vc > old_vc:
            return ("safe", f"widening the length limit ({old_vc} -> {new_vc}) does not rewrite the table")
        if new_vc is not None and old_vc is not None and new_vc == old_vc:
            return ("safe", "same length limit")
        return (
            "unsafe",
            f"narrowing {old} -> {new} requires a length check on every row, so the table is rewritten",
        )

    old_num = _parse_numeric_precision(old)
    new_num = _parse_numeric_precision(new)
    if old_num and new_num:
        if new_num[0] >= old_num[0] and new_num[1] >= old_num[1]:
            return (
                "safe",
                f"increasing NUMERIC precision ({old} -> {new}) is metadata-only on PostgreSQL 9.2+",
            )
        return ("unsafe", f"reducing NUMERIC precision ({old} -> {new}) rewrites the table")

    if new in _SAFE_TARGETS and old in _SAFE_TARGETS:
        return ("safe", f"{old} -> {new} is metadata-only on PostgreSQL 9.2+")

    known_rewrites = {
        ("INTEGER", "BIGINT"), ("INT", "BIGINT"), ("INT4", "INT8"),
        ("SMALLINT", "INTEGER"), ("TIMESTAMP", "TIMESTAMPTZ"),
        ("TIMESTAMPTZ", "TIMESTAMP"), ("TIMESTAMP", "DATE"), ("TEXT", "UUID"),
        ("VARCHAR", "UUID"), ("JSON", "JSONB"), ("INTEGER", "TEXT"),
        ("BIGINT", "INTEGER"), ("TEXT", "BYTEA"), ("CHARACTER", "TEXT"),
    }
    if (old, new) in known_rewrites:
        return (
            "unsafe",
            f"{old} -> {new} is not a binary-coercible widening, so PostgreSQL "
            "rewrites every row of the table",
        )
    return (
        "unknown",
        f"{old} -> {new} is not a conversion this tool can classify; check "
        "whether PostgreSQL can use a binary-coercible cast for it",
    )


# --------------------------------------------------------------------------
# analyzer
# --------------------------------------------------------------------------

class _Context:
    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self.report = FileReport(
            path=path, total_lines=text.count("\n") + 1,
        )
        self.tokens: list = []
        self.statements: list = []
        self.alters_by_table: dict = {}
        self.validated_constraints: set = set()
        self.added_not_valid: list = []
        self.lock_timeout_statements: list = []
        self.lock_timeout_disabled_at: list = []
        self.explicit_begin = False
        self.explicit_commit = False
        self.has_create_index_concurrently = False
        self.has_non_transactional_statement = False
        self.has_transactional_ddl = False
        self.has_dml = False

    # -- finding helpers ---------------------------------------------------
    def add(
        self,
        stmt: Statement,
        rule_id: str,
        risk: str,
        confidence: str,
        category: str,
        message: str,
        suggestion: str = "",
        table: Optional[str] = None,
        line: Optional[int] = None,
        statement: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> Finding:
        finding = Finding(
            line=line if line is not None else stmt.start_line,
            rule_id=rule_id,
            risk=risk,
            confidence=confidence,
            category=category,
            statement=statement if statement is not None else stmt.truncated(),
            message=message,
            suggestion=suggestion,
            table=table,
            detail=detail,
        )
        self.report.findings.append(finding)
        return finding

    def operation(
        self,
        stmt: Statement,
        kind: str,
        table: Optional[str],
        risk: str,
        confidence: str,
        note: str = "",
    ) -> None:
        self.report.operations.append(
            Operation(
                line=stmt.start_line,
                kind=kind,
                table=table,
                risk=risk,
                confidence=confidence,
                statement=stmt.truncated(),
                note=note,
            )
        )


def _find_top_level_words(stmt: Statement) -> list:
    """Top-level ALTER TABLE actions, split on commas.

    ``ALTER TABLE t ADD COLUMN a int, DROP COLUMN b;`` yields two action groups.
    """
    code = _code(stmt.tokens)
    try:
        start = code.index(next(t for t in code if _is_word(t, "TABLE"))) + 1
    except (StopIteration, ValueError):
        return []
    i = start
    if _words_after(code, i, "IF", "EXISTS"):
        i += 3
    if _words_after(code, i, "ONLY"):
        i += 1
    _, i = _read_name(code, i)
    action_tokens = code[i:]
    return _split_top_level_commas(action_tokens)


def _analyse_alter_table(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    i = 1  # past ALTER
    if not _words_after(code, i, "TABLE"):
        return
    i += 1
    if _words_after(code, i, "IF", "EXISTS"):
        i += 3
    if _words_after(code, i, "ONLY"):
        i += 1
    table, i = _read_name(code, i)
    ctx.alters_by_table.setdefault(table or "?", []).append(stmt.start_line)

    actions = _find_top_level_words(stmt)
    if not actions:
        ctx.operation(stmt, "alter-table", table, "medium", "judgement", "could not read the action list")
        return

    for action in actions:
        _analyse_alter_action(stmt, ctx, table, action)


def _analyse_alter_action(stmt: Statement, ctx: _Context, table: Optional[str], action: list) -> None:
    """Classify one top-level ``ALTER TABLE`` action.

    ``action`` is the token list of a single comma-separated action, starting at
    its keyword (``ADD``, ``ALTER COLUMN``, ``DROP COLUMN``, ``VALIDATE`` ...).
    All reading goes through a :class:`~sqlmig.actions.Cursor`, so no token list
    is ever sliced.
    """
    if not action:
        return
    cursor = Cursor(action)
    head = cursor.keyword()

    # ALTER [COLUMN] <column> ...
    if head == "ALTER":
        cursor.take()
        cursor.accept("COLUMN")
        column = cursor.raw_name()
        target = f"{table}.{column}" if (table and column) else (column or table)
        if cursor.accept("SET", "NOT", "NULL"):
            _flag_set_not_null(stmt, ctx, table, target)
            return
        if cursor.accept("DROP", "NOT", "NULL"):
            ctx.operation(stmt, "drop-not-null", table, "low", "judgement",
                          "dropping NOT NULL is catalog-only and never blocks for long")
            return
        if cursor.accept("TYPE") or cursor.accept("SET", "DATA", "TYPE"):
            _flag_type_change(stmt, ctx, table, column, cursor)
            return
        if cursor.accept("SET", "DEFAULT"):
            ctx.operation(stmt, "set-default", table, "low", "verified",
                          "SET DEFAULT is metadata-only on PostgreSQL")
            return
        if cursor.accept("DROP", "DEFAULT"):
            ctx.operation(stmt, "drop-default", table, "low", "verified")
            return
        if cursor.accept("ADD", "GENERATED"):
            ctx.operation(stmt, "add-generated", table, "high", "likely",
                          "adding an identity or generated column rewrites the table")
            return
        ctx.operation(stmt, "alter-column", table, "medium", "judgement")
        return

    # ADD [COLUMN] <column> ...
    if head == "ADD":
        cursor.take()
        cursor.accept("COLUMN")
        if cursor.keyword() == "CONSTRAINT":
            cursor.take()
            name = cursor.raw_name()
            _flag_add_constraint(stmt, ctx, table, cursor, name)
            return
        if cursor.keyword() in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE"):
            _flag_add_constraint(stmt, ctx, table, cursor, None)
            return
        cursor.accept_if_not_exists()
        column = cursor.raw_name()
        if column is None:
            ctx.operation(stmt, "add", table, "medium", "judgement")
            return
        _flag_add_column(stmt, ctx, table, column, cursor)
        return

    # DROP COLUMN / DROP CONSTRAINT
    if head == "DROP":
        cursor.take()
        is_column = cursor.accept("COLUMN")
        cursor.accept_if_exists()
        name = cursor.raw_name()
        if not is_column and _is_constraint_drop_verb(action):
            ctx.operation(stmt, "drop-constraint", table, "medium", "judgement",
                          "constraint removal is metadata-only")
            return
        target = f"{table}.{name}" if (table and name) else (name or table)
        cascade = find_keyword(action, "CASCADE", top_level=True) != -1
        ctx.operation(stmt, "drop-column", table, "high", "judgement")
        ctx.add(
            stmt, "destructive-statement", "high", "judgement", "destructive",
            f"`DROP COLUMN` is irreversible: {target} and every value in it are gone "
            "on commit, and any code still selecting that column starts failing "
            "immediately.",
            "Use the contract half of expand/contract: stop writing and reading the "
            "column in one deploy, wait at least one release, then drop it in a "
            "separate migration so the drop can be reverted from a backup if needed."
            + (" CASCADE is present, so dependent views, constraints and indexes are "
               "dropped too: list them with `\\d+` before shipping this." if cascade else ""),
            table=table,
        )
        return

    if head == "VALIDATE":
        cursor.take()
        cursor.accept("CONSTRAINT")
        name = cursor.raw_name()
        if name:
            ctx.validated_constraints.add(name.lower())
        ctx.operation(stmt, "validate-constraint", table, "medium", "judgement",
                      "SHARE UPDATE EXCLUSIVE: writes continue, but the table is scanned")
        return

    if head == "RENAME":
        ctx.operation(stmt, "rename", table, "high", "judgement",
                      "a rename invalidates code that still uses the old name")
        return

    if head in ("ENABLE", "DISABLE"):
        ctx.operation(stmt, "toggle-trigger", table, "medium", "judgement")
        return

    if head in ("SET", "RESET", "OWNER", "CLUSTER"):
        ctx.operation(stmt, "alter-table-misc", table, "medium", "judgement")
        return

    if head in ("ATTACH", "DETACH"):
        ctx.operation(stmt, "partition-move", table, "high", "judgement",
                      "partition attach/detach takes a strong lock on the partition")
        return

    ctx.operation(stmt, "alter-table", table, "medium", "judgement",
                  "unrecognised ALTER TABLE action")


def _is_constraint_drop_verb(action: list) -> bool:
    """True when a ``DROP ...`` action targets ``CONSTRAINT`` rather than a column."""
    for token in action:
        if token.kind == "word" and token.text.upper() == "CONSTRAINT":
            return True
        if token.kind == "word" and token.text.upper() == "COLUMN":
            return False
    return False


def _flag_type_change(stmt: Statement, ctx: _Context, table: Optional[str],
                      column: Optional[str], cursor: Cursor) -> None:
    """Report ``ALTER COLUMN ... TYPE`` and decide rewrite vs metadata-only."""
    stops = frozenset({
        "USING", "COLLATE", "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE",
        "REFERENCES", "CHECK", "CONSTRAINT", "GENERATED", "IDENTITY", "STORAGE",
        "COMPRESSION", "STATISTICS",
    })
    new_type = cursor.type_text(stops)
    old_type = _read_declared_type(ctx, table, column)
    verdict, detail = classify_type_change(old_type, new_type)
    target = f"{table}.{column}" if (table and column) else (column or table)

    if verdict == "safe":
        ctx.operation(stmt, "alter-column-type", table, "low", "likely", detail)
        ctx.add(
            stmt, "type-safe", "low", "likely", "type-change",
            f"`ALTER COLUMN {target} TYPE {new_type}` is metadata-only: {detail}.",
            "No rewrite is needed, so this is safe to run directly. It still takes an "
            "ACCESS EXCLUSIVE lock for the moment the catalog row is updated: set "
            "`lock_timeout` first so a queued query cannot stall the migration.",
            table=table,
        )
        return

    if verdict == "unknown":
        seen = (f"{old_type} -> {new_type}" if old_type
                else f"<type not declared in this file> -> {new_type}")
        ctx.operation(stmt, "alter-column-type", table, "high", "judgement", detail)
        ctx.add(
            stmt, "type-unknown", "high", "judgement", "type-change",
            f"`ALTER COLUMN {target} TYPE {new_type}` could not be classified "
            f"statically ({seen}): {detail}.",
            "Check whether PostgreSQL can use a binary-coercible cast for this pair "
            "(`SELECT castsource::regtype, casttarget::regtype FROM pg_cast WHERE "
            "castmethod = 'b'`). If it cannot, treat it as a rewrite and use the "
            "expand/contract pattern. Naming the current type in a comment such as "
            "`-- was integer` lets this tool tell the two cases apart next time.",
            table=table,
        )
        return

    ctx.operation(stmt, "alter-column-type", table, "high", "judgement", detail)
    ctx.add(
        stmt, "type-rewrite", "high", "judgement", "type-change",
        f"`ALTER COLUMN {target} TYPE {new_type}` rewrites every row of `{table}` "
        "while holding an ACCESS EXCLUSIVE lock"
        + (f" ({detail})" if old_type else "")
        + ". Reads and writes to the table are blocked for the whole rewrite, which "
        "grows with table size.",
        "Expand/contract instead: (1) add a new column with the target type, "
        "(2) backfill it in batches with a trigger keeping both columns in sync, "
        "(3) swap the columns inside one short transaction, (4) drop the old column "
        "in a later migration. Doing it in one rewrite needs a maintenance window and "
        "a disk-space check, because the rewrite needs room for a second copy of the "
        "table.",
        table=table,
    )


def _read_declared_type(ctx: _Context, table: Optional[str], column: Optional[str]) -> str:
    """Best-effort lookup of a column type declared earlier in the same file.

    Static analysis cannot see the live schema, so this only ever finds types
    that the migration itself declares.  When nothing is found the type-change
    verdict degrades to what the two type names alone can prove.
    """
    if not table or not column:
        return ""
    needle = (table.split(".")[-1] or "").lower()
    column_low = column.lower()
    for stmt in ctx.statements:
        code = _code(stmt.tokens)
        if not code or not _is_word(code[0], "CREATE"):
            continue
        index = _find_word(code, "TABLE", 0, top_level=True)
        if index == -1:
            continue
        cursor = Cursor(code, index + 1)
        cursor.accept_if_not_exists()
        created = cursor.name()
        if not created or created.split(".")[-1].lower() != needle:
            continue
        opening = cursor.peek()
        if opening is None or opening.kind != "punct" or opening.text != "(":
            continue
        body, _closing = _paren_body(code, cursor.pos)
        for group in _split_top_level_commas(body):
            if not group:
                continue
            first = group[0]
            if first.kind == "word" and first.text.upper() in (
                "CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE",
            ):
                continue
            group_cursor = Cursor(group)
            declared = group_cursor.raw_name()
            if declared and declared.replace('"', "").lower() == column_low:
                return group_cursor.type_text(frozenset({
                    "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE", "REFERENCES",
                    "CHECK", "CONSTRAINT", "GENERATED", "COLLATE",
                }))
    return ""


def _paren_body(code: list, open_index: int) -> tuple:
    """Tokens between the parentheses opened at ``open_index`` and their match."""
    depth = 0
    for index in range(open_index, len(code)):
        token = code[index]
        if token.kind == "punct" and token.text == "(":
            depth += 1
            if depth == 1:
                start = index + 1
        elif token.kind == "punct" and token.text == ")":
            depth -= 1
            if depth == 0:
                return (code[start:index], index)
    return (code[open_index + 1:], len(code) - 1)


def _flag_add_column(stmt: Statement, ctx: _Context, table: Optional[str],
                     column: str, cursor: Cursor) -> None:
    """Classify ``ADD COLUMN``: nullable, DEFAULT, or NOT NULL without DEFAULT."""
    target = f"{table}.{column}" if table else column
    remainder = cursor.tokens[cursor.pos:]
    has_default = find_keyword(remainder, "DEFAULT", top_level=True) != -1
    not_null_at = find_keyword(remainder, "NOT", top_level=True)
    has_not_null = not_null_at != -1 and has_keyword_sequence(
        remainder, not_null_at, "NOT", "NULL"
    )
    generated = (find_keyword(remainder, "GENERATED", top_level=True) != -1
                 or find_keyword(remainder, "IDENTITY", top_level=True) != -1)

    if has_not_null and not has_default:
        ctx.operation(stmt, "add-column", table, "high", "judgement",
                      "NOT NULL without DEFAULT")
        ctx.add(
            stmt, "add-column-not-null", "high", "verified", "column",
            f"`ADD COLUMN {target} ... NOT NULL` with no DEFAULT cannot satisfy the "
            "constraint for rows that already exist, so the statement fails outright "
            "on a non-empty table (`column contains null values`).",
            "Three steps instead: (1) `ADD COLUMN ...` nullable and deploy, "
            "(2) backfill in batches, (3) either `ALTER TABLE ... ALTER COLUMN ... "
            "SET DEFAULT <v>` plus `SET NOT NULL`, or add "
            "`CHECK (col IS NOT NULL) NOT VALID` and `VALIDATE CONSTRAINT` before "
            "`SET NOT NULL` (PostgreSQL 12+ can then skip the validation scan).",
            table=table,
        )
        return

    if generated:
        ctx.operation(stmt, "add-column", table, "high", "likely",
                      "generated/identity column: PostgreSQL 11+ avoids a rewrite")
        ctx.add(
            stmt, "add-column-default", "medium", "likely", "column",
            f"`ADD COLUMN {target} GENERATED ... AS IDENTITY` is metadata-only on "
            "PostgreSQL 11+, but on older servers adding a populated column rewrites "
            "the table.",
            "On PostgreSQL 11+ this is fine; still set `lock_timeout` before it "
            "because the ACCESS EXCLUSIVE lock is taken even for a metadata-only "
            "change.",
            table=table,
        )
        return

    if has_default:
        ctx.operation(stmt, "add-column", table, "high", "likely",
                      "DEFAULT present: metadata-only on PostgreSQL 11+")
        ctx.add(
            stmt, "add-column-default", "medium", "likely", "column",
            f"`ADD COLUMN {target} ... DEFAULT` rewrites the entire table while "
            "holding an ACCESS EXCLUSIVE lock on PostgreSQL 10 and older. On "
            "PostgreSQL 11+ the default is stored in the catalog and no rewrite "
            "happens, so the risk depends on the server version.",
            "If any environment is older than PostgreSQL 11: `ADD COLUMN` with no "
            "DEFAULT, backfill in batches, then `SET DEFAULT` for future inserts. "
            "On PostgreSQL 11+ keep the DEFAULT but still set `lock_timeout`, because "
            "the ACCESS EXCLUSIVE lock is taken even for a metadata-only change.",
            table=table,
        )
        return

    ctx.operation(stmt, "add-column", table, "low", "likely",
                  "nullable column, metadata-only on PostgreSQL 11+")
    ctx.add(
        stmt, "add-column-safe", "low", "likely", "column",
        f"`ADD COLUMN {target}` is nullable with no default, so it is a metadata-only "
        "change and does not touch existing rows.",
        "Keep it this way: still set `lock_timeout` before the statement, because even "
        "a metadata-only `ALTER TABLE` needs ACCESS EXCLUSIVE for the instant it runs.",
        table=table,
    )


def _flag_add_constraint(stmt: Statement, ctx: _Context, table: Optional[str],
                         cursor: Cursor, name: Optional[str]) -> None:
    """Classify ``ADD [CONSTRAINT name] <kind>``.

    ``cursor`` is positioned on the constraint kind (``FOREIGN``, ``CHECK``,
    ``UNIQUE``, ``PRIMARY``, ``EXCLUDE``) and ``name`` is the declared name, if
    the statement gave one.
    """
    kind_start = cursor.mark()
    kind = cursor.keyword() or "CONSTRAINT"
    is_fk = cursor.keyword() == "FOREIGN" and cursor.keyword(1) == "KEY"
    remainder = cursor.tokens[kind_start:]
    not_valid = find_sequence(remainder, "NOT", "VALID") != -1

    subject = f"ADD CONSTRAINT {name}" if name else f"ADD {kind}"
    display = name or kind
    # "ADD FOREIGN", "ADD PRIMARY" ... name the constraint kind in full so the
    # message reads like the SQL the user wrote.
    kind_phrase = {"FOREIGN": "FOREIGN KEY", "PRIMARY": "PRIMARY KEY"}.get(kind, kind)

    if not_valid:
        ctx.operation(stmt, "add-constraint", table, "low", "verified",
                      "NOT VALID: existing rows are not checked now")
        if name:
            ctx.added_not_valid.append((name.lower(), stmt.start_line, table))
        ctx.add(
            stmt, "constraint-not-valid-ok", "low", "verified", "constraint",
            f"`{subject} ... NOT VALID` takes only a brief lock and does not scan "
            "existing rows, which is the safe half of the pattern.",
            "Follow it with `VALIDATE CONSTRAINT` in a later migration to actually "
            "enforce the constraint for existing rows.",
            table=table,
        )
        return

    if is_fk:
        ctx.operation(stmt, "add-constraint", table, "high", "judgement",
                      "foreign key validated immediately")
        ctx.add(
            stmt, "constraint-not-valid", "high", "judgement", "constraint",
            f"`{subject} FOREIGN KEY` without `NOT VALID` validates every existing "
            f"row of `{table}` under a lock that also blocks writes on the "
            "referenced table, and it fails outright if any existing row violates it.",
            "Two migrations: (1) `ALTER TABLE ... ADD CONSTRAINT "
            f"{name or 'fk_name'} FOREIGN KEY (...) REFERENCES ... NOT VALID;` "
            "(brief lock, no scan) and deploy; (2) `ALTER TABLE ... VALIDATE CONSTRAINT "
            f"{name or 'fk_name'};` which takes SHARE UPDATE EXCLUSIVE and lets writes "
            "continue while it checks.",
            table=table,
        )
        return

    if kind == "CHECK":
        ctx.operation(stmt, "add-constraint", table, "high", "judgement",
                      "CHECK is validated for all rows immediately")
        ctx.add(
            stmt, "constraint-not-valid", "high", "judgement", "constraint",
            f"`{subject} CHECK` without `NOT VALID` validates every existing row of "
            f"`{table}` while holding a lock that blocks writes; on a large table "
            "that is a long outage, and it fails if any row violates it.",
            "Add it as `... CHECK (...) NOT VALID` first (brief lock, no scan), deploy, "
            "then `ALTER TABLE ... VALIDATE CONSTRAINT ...` (SHARE UPDATE EXCLUSIVE, "
            "writes continue).",
            table=table,
        )
        return

    ctx.operation(stmt, "add-constraint", table, "high", "judgement",
                  f"{kind} constraint validated immediately")
    ctx.add(
        stmt, "constraint-not-valid", "high", "judgement", "constraint",
        f"`{subject}` builds or validates an index over the whole table while "
        f"blocking writes on `{table}`"
        + ("" if name else f" (constraint kind: {display})") + ".",
        "Build the supporting index with `CREATE INDEX CONCURRENTLY`, then attach the "
        "constraint with `ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY USING INDEX "
        "index_name` (or `UNIQUE USING INDEX`), which only takes a brief lock.",
        table=table,
    )
def _read_declared_type(ctx: _Context, table: Optional[str], col: Optional[str]) -> str:
    """Best-effort lookup of a column type declared earlier in the same file.

    Static analysis cannot see the live schema, so this only ever finds types
    that the migration itself declares.  When nothing is found the type-change
    verdict degrades to what the two type names alone can prove.
    """
    if not table or not col:
        return ""
    needle = (table.split(".")[-1] or "").lower()
    col_low = col.lower()
    for stmt in ctx.statements:
        code = _code(stmt.tokens)
        if not code or not _is_word(code[0], "CREATE"):
            continue
        k = _find_word(code, "TABLE", 0, top_level=True)
        if k == -1:
            continue
        j = k + 1
        if _words_after(code, j, "IF", "NOT", "EXISTS"):
            j += 4
        name, j = _read_name(code, j)
        if not name or name.split(".")[-1].lower() != needle:
            continue
        if j < len(code) and _is_punct(code[j], "("):
            end = _matching_paren(code, j)
            body = code[j + 1:end]
            for group in _split_top_level_commas(body):
                if not group:
                    continue
                if _is_word(group[0], "CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE"):
                    continue
                cname, m = _ident_after(group, 0)
                if cname and cname.lower() == col_low:
                    t, _ = _type_text(group, m)
                    return t
    return ""


def _flag_set_not_null(stmt: Statement, ctx: _Context, table: Optional[str], target: str) -> None:
    ctx.operation(stmt, "set-not-null", table, "high", "likely",
                  "full table scan under ACCESS EXCLUSIVE")
    ctx.add(
        stmt, "set-not-null", "high", "likely", "column",
        f"`ALTER COLUMN {target} SET NOT NULL` scans the whole table to prove no row "
        "is null, and it does so while holding ACCESS EXCLUSIVE, so reads and writes "
        "are blocked for the duration of the scan.",
        "Use the CHECK ... NOT VALID pattern: `ALTER TABLE ... ADD CONSTRAINT "
        "col_not_null CHECK (col IS NOT NULL) NOT VALID;` then "
        "`ALTER TABLE ... VALIDATE CONSTRAINT col_not_null;` (SHARE UPDATE EXCLUSIVE, "
        "writes continue). On PostgreSQL 12+ a following `SET NOT NULL` can then reuse "
        "the validated constraint instead of scanning again. Then drop the CHECK if "
        "you do not want it permanently.",
        table=table,
    )


def _analyse_create_index(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    i = 1
    if _words_after(code, i, "UNIQUE"):
        i += 1
    if not _words_after(code, i, "INDEX"):
        return
    i += 1
    concurrent = _words_after(code, i, "CONCURRENTLY")
    if concurrent:
        i += 1
    if _words_after(code, i, "IF", "NOT", "EXISTS"):
        i += 4
    name, i = _read_name(code, i)
    table, _ = _read_name(code, i + 1) if i < len(code) and _is_word(code[i], "ON") else (None, i)

    if concurrent:
        ctx.has_create_index_concurrently = True
        ctx.operation(stmt, "create-index-concurrently", table, "medium", "verified",
                      "does not block writes, cannot run inside a transaction")
        return

    ctx.operation(stmt, "create-index", table, "high", "judgement",
                  "blocks writes for the whole build")
    ctx.add(
        stmt, "index-not-concurrent", "high", "verified", "index-concurrency",
        f"`CREATE INDEX` (index `{name}` on `{table}`) takes a SHARE lock that blocks "
        "INSERT, UPDATE and DELETE for the entire build, which grows with table size; "
        "reads continue.",
        "Use `CREATE INDEX CONCURRENTLY` (or `CREATE UNIQUE INDEX CONCURRENTLY`). It "
        "takes longer and can leave an INVALID index behind if it fails, so run it "
        "outside a transaction and check `pg_index.indisvalid` afterwards, dropping "
        "and recreating any INVALID index.",
        table=table,
    )


def _analyse_drop_index(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    i = 1
    if not _words_after(code, i, "INDEX"):
        return
    i += 1
    concurrent = _words_after(code, i, "CONCURRENTLY")
    if concurrent:
        i += 1
    if _words_after(code, i, "IF", "EXISTS"):
        i += 4
    name, _ = _read_name(code, i)
    cascade = _find_word(code, "CASCADE", top_level=True) != -1

    if concurrent:
        ctx.operation(stmt, "drop-index-concurrently", None, "medium", "verified",
                      "does not block reads or writes; cannot run in a transaction")
        return

    ctx.operation(stmt, "drop-index", None, "high", "judgement")
    ctx.add(
        stmt, "index-drop-not-concurrent", "high", "verified", "index-concurrency",
        f"`DROP INDEX` (index `{name}`) takes an ACCESS EXCLUSIVE lock, blocking reads "
        "and writes on the table that uses the index until the catalog change commits"
        + ("; CASCADE also drops every object that depends on the index" if cascade else "")
        + ".",
        "Use `DROP INDEX CONCURRENTLY` (PostgreSQL 9.2+): it does not block reads or "
        "writes. It cannot run inside a transaction block, and like all CONCURRENTLY "
        "operations it can leave the index INVALID if it fails, so verify and retry.",
    )


def _analyse_drop_table(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    i = 1
    if _words_after(code, i, "TABLE"):
        i += 1
    elif _words_after(code, i, "MATERIALIZED", "VIEW"):
        i += 2
    else:
        return
    if _words_after(code, i, "IF", "EXISTS"):
        i += 3
    name, _ = _read_name(code, i)
    cascade = _find_word(code, "CASCADE", top_level=True) != -1
    ctx.operation(stmt, "drop-table", name, "high", "judgement")
    ctx.add(
        stmt, "destructive-statement", "high", "judgement", "destructive",
        f"`DROP TABLE {name}` destroys the table and all of its rows; the data is not "
        "recoverable from the database afterwards"
        + (" and CASCADE drops every view, foreign key and index that depends on it"
           if cascade else "")
        + ".",
        "Treat the drop as the contract step of expand/contract: ship the code that "
        "stops using the table first, keep the table read-only for at least one "
        "release, and take a verified logical dump (`pg_dump -t`) or rename it to "
        f"`{name}_deprecated_<date>` before dropping, so a rollback is still possible.",
        table=name,
    )


def _analyse_truncate(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    if not _words_after(code, 0, "TRUNCATE"):
        return
    i = 1
    if _words_after(code, i, "TABLE"):
        i += 1
    if _words_after(code, i, "ONLY"):
        i += 1
    name, _ = _read_name(code, i)
    ctx.operation(stmt, "truncate", name, "high", "judgement")
    ctx.add(
        stmt, "destructive-statement", "high", "verified", "destructive",
        f"`TRUNCATE {name}` deletes every row immediately and takes an ACCESS "
        "EXCLUSIVE lock on the table; it is not reversible by rollback of your "
        "application code.",
        "If the intent is to clear a large table, prefer batched "
        "`DELETE FROM ... WHERE id BETWEEN ...` calls so locks stay short, or only "
        "run TRUNCATE in a scripted maintenance window with the application stopped.",
        table=name,
    )


def _analyse_dml(stmt: Statement, ctx: _Context, verb: str) -> None:
    code = _code(stmt.tokens)
    has_where = _find_word(code, "WHERE", top_level=True) != -1
    ctx.has_dml = True
    # Only an unbounded DML statement is a finding.
    if has_where:
        ctx.operation(stmt, "dml-bounded", None, "medium", "judgement",
                      "row count still unknown statically")
        return
    ctx.operation(stmt, "dml-unbounded", None, "high", "judgement",
                  "no WHERE clause")
    ctx.add(
        stmt, "unbounded-dml", "high", "verified", "lock",
        f"`{verb}` has no `WHERE` clause, so it touches every row in the table. That "
        "is either a forgotten filter (a common way to lose production data) or an "
        "intentional bulk change that will hold row locks for a long time and bloat "
        "the table.",
        "Wrap the statement in `BEGIN`/`COMMIT` so the row count can be checked before "
        "`COMMIT`, and run `EXPLAIN` on the exact statement first. For a bulk change, "
        "loop in batches on the primary key (for example 10,000 rows per batch) with a "
        "short sleep between batches so autovacuum and replicas can keep up.",
    )


def _analyse_vacuum(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    full = _find_word(code, "FULL", top_level=True) != -1
    analyze_only = _find_word(code, "ANALYZE", top_level=True) != -1
    ctx.has_non_transactional_statement = True
    if not full:
        ctx.operation(stmt, "vacuum", None, "medium", "verified",
                      "cannot run in a transaction block; does not block ordinary DML")
        return
    name, _ = _read_name(code, 2) if len(code) > 2 else (None, 2)
    ctx.operation(stmt, "vacuum-full", name, "high", "judgement",
                  "rewrites the table under ACCESS EXCLUSIVE")
    ctx.add(
        stmt, "vacuum-full", "high", "judgement", "lock",
        "`VACUUM FULL` rewrites the whole table into a new file under an ACCESS "
        "EXCLUSIVE lock, so every read and write on that table is blocked for the "
        "entire rewrite. It also needs free disk space for a second copy of the table"
        + (" (ANALYZE is requested as well)" if analyze_only else "") + ".",
        "Use plain `VACUUM (ANALYZE)` instead, which runs alongside normal traffic. "
        "`VACUUM FULL` belongs in a maintenance window with the table quiesced, or "
        "replace it with `pg_repack`, which rebuilds the table online.",
        table=name,
    )


def _analyse_reindex(stmt: Statement, ctx: _Context) -> None:
    code = _code(stmt.tokens)
    if not _words_after(code, 0, "REINDEX"):
        return
    verbose = "VERBOSE" in [t.text.upper() for t in code if t.kind == "word"]
    concurrent = _find_word(code, "CONCURRENTLY", top_level=True) != -1
    i = 1
    while i < len(code) and code[i].normalized in ("INDEX", "TABLE", "SCHEMA", "DATABASE",
                                                   "SYSTEM", "VERBOSE", "CONCURRENTLY"):
        i += 1
    name, _ = _read_name(code, i)
    ctx.has_non_transactional_statement = True
    risk = "medium" if concurrent else "high"
    ctx.operation(stmt, "reindex", name, risk, "judgement",
                  "CONCURRENTLY rebuild" if concurrent else "ACCESS EXCLUSIVE rebuild")
    subject = f"`REINDEX {name}`" if name else "`REINDEX`"
    ctx.add(
        stmt, "reindex-locking", risk, "judgement", "lock",
        subject
        + (" uses REINDEX CONCURRENTLY, which still takes SHARE UPDATE EXCLUSIVE on "
           "the table for the whole rebuild and cannot run inside a transaction block."
           if concurrent else
           " rebuilds the index while holding ACCESS EXCLUSIVE, blocking reads and "
           "writes on the table for the whole rebuild"
           + (" (VERBOSE also adds progress output)" if verbose else "") + "."),
        "On PostgreSQL 12+ prefer `REINDEX INDEX CONCURRENTLY <index>` in a "
        "non-transactional migration (it cannot run inside a transaction block and is "
        "unsupported for some index types). Otherwise schedule the rebuild in a "
        "maintenance window and set `lock_timeout` so it fails fast instead of "
        "queueing behind other queries.",
        table=name,
    )


# --------------------------------------------------------------------------
# file-level rules
# --------------------------------------------------------------------------

#: Comment markers that tell a migration runner (and this tool) that the file
#: must not be wrapped in an implicit transaction.  Recognising them is the
#: difference between "you forgot to say" and "you said it".
NON_TRANSACTIONAL_MARKERS = (
    "migrate: no-transaction",
    "migrate:no-transaction",
    "no-transaction",
    "notransaction",
    "transactional: false",
    "transaction: false",
    "disable_ddl_transaction",
    "executeintransaction=false",
    "executeintransaction = false",
    "autocommit",
)


def declares_non_transactional(text: str, tokens: Optional[list] = None) -> bool:
    """True when a *comment* in ``text`` declares the file non-transactional.

    Markers such as ``-- migrate: no-transaction`` are what migration runners
    (and this tool) use to say "do not wrap this file in a transaction", so the
    check has to be comment-aware: a ``-- no-transaction`` inside a string
    literal is not a declaration.
    """
    if tokens is None:
        tokens = tokenize(text)
    for token in tokens:
        if token.kind != "comment":
            continue
        lowered = token.text.lower()
        for marker in NON_TRANSACTIONAL_MARKERS:
            if marker in lowered:
                return True
    return False


def _check_transaction_control(ctx: _Context) -> None:
    if not ctx.statements:
        return
    if ctx.explicit_begin or ctx.explicit_commit:
        return
    if declares_non_transactional(ctx.text, ctx.tokens):
        return
    stmt = ctx.statements[0]
    has_concurrently = any(
        op.kind in ("create-index-concurrently", "drop-index-concurrently")
        for op in ctx.report.operations
    )
    if has_concurrently and ctx.has_transactional_ddl:
        ctx.add(
            stmt, "no-transaction-control", "medium", "judgement", "transaction",
            "This file mixes `CONCURRENTLY` statements with ordinary DDL and declares "
            "no transaction control either way. `CREATE INDEX CONCURRENTLY` cannot run "
            "inside a transaction block, and it is the one statement that cannot be "
            "rolled back, so if the runner wraps this file in a transaction the index "
            "build fails outright, and if it does not, a later failure leaves the "
            "index built while the rest of the file is not applied.",
            "Split the file: keep `CONCURRENTLY` builds alone in a migration marked "
            "`-- migrate: no-transaction` (Flyway `executeInTransaction=false`, Rails "
            "`disable_ddl_transaction!`, Django `atomic = False`), and put the ordinary "
            "DDL in a transactional file with explicit `BEGIN`/`COMMIT`.",
            line=1,
            statement="<whole file>",
        )
    elif ctx.has_non_transactional_statement and len(ctx.statements) > 1:
        ctx.add(
            stmt, "no-transaction-control", "medium", "judgement", "transaction",
            "This file contains a statement that cannot be rolled back with the rest of "
            "the file (`VACUUM`, `CREATE DATABASE`, `DROP DATABASE` or a CONCURRENTLY "
            "build) but declares no transaction control, so whether the file is atomic "
            "depends on the migration runner rather than on the migration. A failure "
            "after that statement leaves the schema half-applied with no way to roll it "
            "back automatically.",
            "Split the non-transactional statement into its own migration marked "
            "`-- migrate: no-transaction`, and wrap the remaining transactional work in "
            "explicit `BEGIN`/`COMMIT` so a failure rolls back cleanly.",
            line=1,
            statement="<whole file>",
        )


def _analyse_set(stmt: Statement, ctx: _Context, code: list) -> None:
    """Track ``SET lock_timeout`` (and friends) for the lock-timeout rule."""
    if "LOCK_TIMEOUT" not in [t.normalized for t in code if t.kind == "word"]:
        return
    # Consume the optional LOCAL / SESSION / TRANSACTION scope keyword.
    i = 1
    while i < len(code) and code[i].kind == "word" and code[i].normalized in (
        "LOCAL", "SESSION", "TRANSACTION",
    ):
        i += 1
    if i >= len(code) or not _is_word(code[i], "LOCK_TIMEOUT"):
        return
    i += 1
    if i < len(code) and code[i].kind == "operator" and code[i].text in ("=", "TO", ":="):
        i += 1
    elif i < len(code) and _is_word(code[i], "TO"):
        i += 1
    if i < len(code) and code[i].kind == "number" and code[i].text.strip("0.") == "":
        ctx.lock_timeout_disabled_at.append(stmt.start_line)
        return
    if i < len(code) and code[i].kind == "string":
        raw = code[i].text.strip("'").strip()
        if re.fullmatch(r"0+(\.0+)?\s*(ms|s|min|h|d)?", raw):
            ctx.lock_timeout_disabled_at.append(stmt.start_line)
            return
    ctx.lock_timeout_statements.append(stmt.start_line)


def _check_transaction_concurrently(ctx: _Context) -> None:
    """``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction block."""
    if not (ctx.explicit_begin or ctx.explicit_commit):
        return
    first = None
    for stmt in ctx.statements:
        for op in ctx.report.operations:
            if op.line == stmt.start_line and op.kind in (
                "create-index-concurrently", "drop-index-concurrently",
            ):
                first = (stmt, op)
                break
        if first:
            break
    if not first:
        return
    stmt, op = first
    ctx.add(
        stmt, "transaction-concurrently", "high", "verified", "transaction",
        f"`{op.statement}` uses CONCURRENTLY, but this file also contains explicit "
        "transaction control (`BEGIN`/`COMMIT`). PostgreSQL refuses this outright: "
        "`CREATE INDEX CONCURRENTLY cannot run inside a transaction block` (same for "
        "`DROP INDEX CONCURRENTLY`), because CONCURRENTLY needs to commit intermediate "
        "states between its internal passes.",
        "Take the CONCURRENTLY statements out of the transaction: keep `BEGIN`/`COMMIT` "
        "around the ordinary DDL, and put each CONCURRENTLY build in its own migration "
        "that the runner does not wrap (Flyway `executeInTransaction=false`, Rails "
        "`disable_ddl_transaction!`, Django `atomic = False`, sqitch "
        "`-- no-transaction`).",
        line=op.line,
        statement=op.statement,
    )


def _check_lock_timeout(ctx: _Context) -> None:
    """ACCESS EXCLUSIVE without a preceding (and enabled) lock_timeout budget."""
    first_lock_stmt = None
    first_lock_line = None
    for op in ctx.report.operations:
        if op.requires_access_exclusive:
            first_lock_stmt, first_lock_line = op, op.line
            break
    if first_lock_stmt is None:
        return

    if ctx.lock_timeout_disabled_at and not ctx.lock_timeout_statements:
        line = ctx.lock_timeout_disabled_at[0]
        stmt = next(
            (s for s in ctx.statements if s.start_line == line), ctx.statements[0]
        )
        ctx.add(
            stmt, "lock-timeout-disabled", "medium", "verified", "lock",
            "`lock_timeout = 0` disables the lock timeout, so the ACCESS EXCLUSIVE "
            "lock this file needs is waited for forever: the migration blocks every "
            "query that queues behind it instead of failing fast.",
            f"Use a finite budget instead: `SET lock_timeout = '{DEFAULT_LOCK_TIMEOUT}';` "
            "before the locking statements, and `RESET lock_timeout;` after them.",
            line=line,
            statement="<SET lock_timeout = 0>",
        )
        return

    if not ctx.lock_timeout_statements:
        stmt = next(
            (s for s in ctx.statements if s.start_line == first_lock_line), ctx.statements[0]
        )
        ctx.add(
            stmt, "lock-timeout-missing", "medium", "judgement", "lock",
            "No `lock_timeout` is set anywhere in this file, yet it contains at least "
            "one statement that needs an ACCESS EXCLUSIVE lock. That lock request "
            "queues behind every open transaction touching the table, and once it is "
            "queued it blocks every later query on that table too, including plain "
            "SELECTs. An idle transaction in a connection pool is enough to stall the "
            "migration and then the application.",
            f"Put `SET lock_timeout = '{DEFAULT_LOCK_TIMEOUT}';` before the locking "
            "statements (and `RESET lock_timeout;` after them) so the migration fails "
            "fast and can be retried during a quieter moment instead of freezing the "
            "table.",
            line=first_lock_line,
            statement=first_lock_stmt.statement,
        )


def _check_lock_churn(ctx: _Context) -> None:
    for table, lines in sorted(ctx.alters_by_table.items(), key=lambda kv: kv[1][0]):
        if len(lines) > 1:
            stmt = next((s for s in ctx.statements if s.start_line == lines[0]), ctx.statements[0])
            listed = ", ".join(str(n) for n in lines)
            ctx.add(
                stmt, "lock-churn", "medium", "verified", "lock",
                f"`{table}` is altered {len(lines)} times in this file (lines "
                f"{listed}). Each `ALTER TABLE` queues for its own ACCESS EXCLUSIVE "
                "lock, so the table is briefly frozen several times and there are more "
                "chances to queue behind a long-running query.",
                f"Combine the actions into one statement: `ALTER TABLE {table} "
                "ADD COLUMN ..., ADD COLUMN ..., ALTER COLUMN ...;` A single "
                "ALTER TABLE takes one lock for all of its actions, so the total lock "
                "time drops even though the work is the same.",
                table=table,
                line=lines[0],
            )


def _check_never_validated(ctx: _Context) -> None:
    for name, line, table in ctx.added_not_valid:
        if name in ctx.validated_constraints:
            continue
        # Record it so a directory-wide run can retire the finding when a later
        # migration validates the constraint; otherwise the reminder is real.
        ctx.report.unvalidated_constraints.append(
            {"name": name, "line": line, "table": table}
        )
        stmt = next((s for s in ctx.statements if s.start_line == line), ctx.statements[0])
        ctx.add(
            stmt, "constraint-never-validated", "medium", "verified", "constraint",
            f"Constraint `{name}` was added `NOT VALID` and this file never runs "
            "`VALIDATE CONSTRAINT`, so PostgreSQL still checks it for new rows but "
            "existing rows are unverified. The constraint is not actually enforced "
            "for the data you already have.",
            f"Add `ALTER TABLE ... VALIDATE CONSTRAINT {name};` in this migration (it "
            "takes SHARE UPDATE EXCLUSIVE and lets writes continue) or in the next one, "
            "and record why if you deliberately leave it unvalidated.",
            table=table,
            line=line,
            detail=name,
        )


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------

def analyse_text(text: str, path: str = "<string>") -> FileReport:
    """Analyse one migration file's contents."""
    ctx = _Context(path, text)
    ctx.tokens = tokenize(text)
    ctx.statements = split_statements(text, ctx.tokens)
    ctx.report.statement_count = len(ctx.statements)

    for stmt in ctx.statements:
        if stmt.errors:
            ctx.report.parse_errors.extend(stmt.errors)
        if not stmt.terminated:
            ctx.report.parse_errors.append(
                f"line {stmt.start_line}: statement is not terminated by a semicolon"
            )

    in_transaction = False

    for stmt in ctx.statements:
        code = _code(stmt.tokens)
        if not code:
            continue
        verb, verb_index = leading_verb(stmt.tokens)
        if verb_index < 0:
            continue

        # ---- transaction control -----------------------------------------
        if verb in ("BEGIN", "START"):
            in_transaction = True
            ctx.explicit_begin = True
            continue
        if verb in ("COMMIT", "END"):
            in_transaction = False
            ctx.explicit_commit = True
            continue
        if verb == "ROLLBACK":
            in_transaction = False
            ctx.explicit_commit = True
            continue
        if verb == "SET":
            _analyse_set(stmt, ctx, code)
            continue
        if verb in ("SHOW", "RESET", "DISCARD", "ANALYZE", "EXPLAIN", "GRANT", "REVOKE",
                    "COMMENT", "SECURITY", "LISTEN", "NOTIFY", "UNLISTEN", "DO", "CALL",
                    "LOCK"):
            if verb == "ANALYZE":
                ctx.operation(stmt, "analyze", None, "low", "judgement")
            continue

        # ---- statements that cannot run in a transaction block ------------
        if in_transaction:
            illegal = None
            if verb == "VACUUM":
                illegal = ILLEGAL_IN_TRANSACTION["VACUUM"]
            elif verb == "CREATE" and _words_after(code, 1, "DATABASE"):
                illegal = ILLEGAL_IN_TRANSACTION["CREATE DATABASE"]
            elif verb == "DROP" and _words_after(code, 1, "DATABASE"):
                illegal = ILLEGAL_IN_TRANSACTION["DROP DATABASE"]
            if illegal:
                ctx.add(
                    stmt, "transaction-illegal", "high", "verified", "transaction",
                    f"{illegal}, but this file opens a transaction with `BEGIN`/`START` "
                    "before it. PostgreSQL will abort the whole transaction with "
                    "`ERROR: ... cannot run inside a transaction block`, so the "
                    "migration fails after whatever ran before it.",
                    "Move the statement into its own file that has no `BEGIN`, mark that "
                    "file as non-transactional for the runner, and keep it last in the "
                    "release so a failed run does not leave the schema half-migrated.",
                )
                ctx.has_non_transactional_statement = True

        # ---- dispatch ------------------------------------------------------
        if verb in ("CREATE", "ALTER", "DROP", "TRUNCATE") and not _words_after(
            code, 1, "DATABASE"
        ):
            # Ordinary catalog DDL: rolls back cleanly inside a transaction.
            ctx.has_transactional_ddl = True
        if verb == "ALTER" and _words_after(code, 1, "TABLE"):
            _analyse_alter_table(stmt, ctx)
        elif verb == "ALTER":
            ctx.operation(stmt, "alter-other", None, "medium", "judgement")
        elif verb == "CREATE" and (
            _words_after(code, 1, "INDEX")
            or _words_after(code, 1, "UNIQUE")
            or _words_after(code, 1, "CONCURRENTLY")
        ):
            _analyse_create_index(stmt, ctx)
        elif verb == "DROP" and _words_after(code, 1, "INDEX"):
            _analyse_drop_index(stmt, ctx)
        elif verb == "DROP" and (_words_after(code, 1, "TABLE")
                                 or _words_after(code, 1, "MATERIALIZED", "VIEW")):
            _analyse_drop_table(stmt, ctx)
        elif verb == "TRUNCATE":
            _analyse_truncate(stmt, ctx)
        elif verb in ("UPDATE", "DELETE") or (verb == "WITH" and verb_index >= 0
                                              and code[verb_index].normalized in ("UPDATE", "DELETE")):
            _analyse_dml(stmt, ctx, code[verb_index].normalized)
        elif verb == "REINDEX":
            _analyse_reindex(stmt, ctx)
        elif verb == "VACUUM":
            _analyse_vacuum(stmt, ctx)
        elif verb == "CREATE" and _words_after(code, 1, "DATABASE"):
            name, _ = _read_name(code, 2)
            ctx.operation(stmt, "create-database", name, "high", "verified",
                          "cannot run inside a transaction block")
            ctx.add(
                stmt, "transaction-illegal-risk", "medium", "verified", "transaction",
                f"`CREATE DATABASE {name}` cannot run inside a transaction block, so a "
                "migration file that wraps its statements in `BEGIN`/`COMMIT` will fail "
                "here, and it cannot be rolled back with the rest of the file.",
                "Keep `CREATE DATABASE` in a separate non-transactional migration and "
                "make it idempotent (check `pg_database` first) so a retry does not "
                "fail on an existing database.",
                table=name,
            )
        elif verb == "DROP" and _words_after(code, 1, "DATABASE"):
            ctx.has_transactional_ddl = True
            ctx.has_non_transactional_statement = True
            ctx.add(
                stmt, "destructive-statement", "high", "judgement", "destructive",
                "`DROP DATABASE` destroys every table in the database and cannot be "
                "undone, and it cannot run inside a transaction block.",
                "Prefer `ALTER DATABASE ... RENAME TO ..._deprecated_<date>` plus a "
                "verified backup, and drop it in a separate, manually approved step.",
            )
            ctx.operation(stmt, "drop-database", None, "high", "verified")
        elif verb in ("CREATE", "INSERT", "SELECT", "COMMENT", "DO"):
            ctx.operation(stmt, "other", None, "low", "judgement")
        else:
            ctx.operation(stmt, "other", None, "low", "judgement")

    ctx.report.in_transaction = in_transaction
    ctx.report.validated_constraints = set(ctx.validated_constraints)
    _check_lock_churn(ctx)
    _check_never_validated(ctx)
    _check_transaction_control(ctx)
    _check_transaction_concurrently(ctx)
    _check_lock_timeout(ctx)
    ctx.report.findings.sort(key=lambda f: (f.line, rules.rank(f.risk), f.rule_id))
    return ctx.report


def analyse_path(path: str) -> list:
    """Analyse a file or a directory of ``.sql`` files.

    Returns a list of :class:`FileReport`, one per file, sorted by path.

    When a directory is given, migrations are analysed in filename order so that
    a ``VALIDATE CONSTRAINT`` in a later migration retires the
    ``constraint-never-validated`` reminder raised by an earlier one.  Without
    that, the two-migration ``NOT VALID`` + ``VALIDATE`` pattern could never come
    out clean -- and that pattern is exactly what this tool recommends.
    """
    if not os.path.isdir(path):
        return [analyse_file(path)]

    names = sorted(
        n for n in os.listdir(path)
        if n.lower().endswith(".sql") and os.path.isfile(os.path.join(path, n))
    )
    reports = [analyse_file(os.path.join(path, name)) for name in names]

    # Second pass: a constraint added NOT VALID in one migration and validated in
    # a later one is the documented two-step pattern, so the reminder raised by
    # the earlier file is retired once a later file validates it.  Without this,
    # the pattern this tool recommends could never come out clean.
    validated_later: set = set()
    for report in reversed(reports):
        if validated_later:
            report.findings = [
                f for f in report.findings
                if not (f.rule_id == "constraint-never-validated"
                        and f.detail in validated_later)
            ]
        validated_later |= set(report.validated_constraints)
    return reports


def analyse_file(path: str) -> FileReport:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return analyse_text(text, path)


# --------------------------------------------------------------------------
# directory-level ordering check
# --------------------------------------------------------------------------

_ORDER_PREFIX = re.compile(r"^(\d+)[-_]")
_ORDER_EXACT = re.compile(r"^(\d+)\.sql$", re.IGNORECASE)


def check_order(path: str) -> FileReport:
    """Check the ``0001_name.sql`` numbering convention of a migration directory.

    Reports duplicate numbers (two migrations claiming the same slot), gaps in
    the sequence, and files that carry no number at all.  This is a convention
    check, not a correctness check: several runners are happy with gaps, which
    is why a gap is only a low-risk finding and duplicates are not.
    """
    report = FileReport(path=path)
    if not os.path.isdir(path):
        report.findings.append(
            Finding(
                line=0,
                rule_id="migration-order-single-file",
                risk="low",
                confidence="verified",
                category="order",
                statement=os.path.basename(path),
                message=(
                    "`--check-order` needs a directory: gaps and duplicates can only "
                    "be seen by comparing the migrations that sit next to each other."
                ),
                suggestion="Run the check against the migrations directory instead, "
                           "for example `sql-migration-lint migrations --check-order`.",
            )
        )
        return report

    names = sorted(
        n for n in os.listdir(path)
        if n.lower().endswith(".sql") and os.path.isfile(os.path.join(path, n))
    )
    report.statement_count = len(names)
    if not names:
        return report

    numbered: dict = {}
    unnumbered: list = []
    for name in names:
        m = _ORDER_PREFIX.match(name) or _ORDER_EXACT.match(name)
        if not m:
            unnumbered.append(name)
            continue
        numbered.setdefault(int(m.group(1)), []).append(name)

    for number in sorted(numbered):
        group = sorted(numbered[number])
        if len(group) > 1:
            report.findings.append(
                Finding(
                    line=0,
                    rule_id="migration-order-duplicate",
                    risk="high",
                    confidence="verified",
                    category="order",
                    statement=", ".join(group),
                    message=(
                        f"Migration number {number} is used by {len(group)} files "
                        f"({', '.join(group)}). Two migrations claiming the same slot "
                        "have no defined order between them, and runners that key on the "
                        "number (or on a version table) will apply whichever they read "
                        "first and then silently skip the other."
                    ),
                    suggestion="Renumber one of them into the next free slot and keep "
                               "the sequence strictly increasing.",
                )
            )

    numbers = sorted(numbered)
    gaps = [
        (a, b) for a, b in zip(numbers, numbers[1:]) if b - a > 1
    ]
    for a, b in gaps:
        missing = ", ".join(str(n) for n in range(a + 1, min(b, a + 6)))
        if b - a > 5:
            missing += ", ..."
        report.findings.append(
            Finding(
                line=0,
                rule_id="migration-order-gap",
                risk="low",
                confidence="verified",
                category="order",
                statement=f"{a} -> {b}",
                message=(
                    f"No migration has numbers between {a} and {b} (missing: {missing}). "
                    "A gap is usually harmless, but it can also mean a migration was "
                    "deleted after being deployed, which leaves environments that already "
                    "ran it out of step with the repository."
                ),
                suggestion="If the missing migrations were deployed and then deleted, "
                           "record that somewhere durable instead of relying on the "
                           "numbering; otherwise renumber to keep the sequence dense.",
            )
        )

    if unnumbered and numbered:
        report.findings.append(
            Finding(
                line=0,
                rule_id="migration-order-unnumbered",
                risk="medium",
                confidence="verified",
                category="order",
                statement=", ".join(sorted(unnumbered)),
                message=(
                    "These files in an otherwise numbered directory have no numeric "
                    "prefix, so their position in the sequence is undefined and they may "
                    "run before or after the numbered migrations depending on the runner."
                ),
                suggestion="Rename them to `<next-number>_<description>.sql`.",
            )
        )
    elif unnumbered and not numbered:
        report.findings.append(
            Finding(
                line=0,
                rule_id="migration-order-unnumbered",
                risk="low",
                confidence="verified",
                category="order",
                statement=", ".join(sorted(unnumbered)),
                message=(
                    "No file in this directory starts with a number, so the "
                    "`0001_name.sql` convention is not in use here. Order then depends "
                    "entirely on the runner's own ordering rules."
                ),
                suggestion="Adopt zero-padded sequential names "
                           "(`0001_add_users.sql`, `0002_add_index.sql`) to make the "
                           "apply order visible in the repository.",
            )
        )

    report.findings.sort(key=lambda f: (rules.rank(f.risk), f.statement))
    return report
