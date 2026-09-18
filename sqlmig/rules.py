"""Rule catalogue: ids, risk ranks, and the wording of every finding.

Keeping the text in one table means the analyzer stays readable and the README
can quote output that is generated from the same strings.  Nothing here is
invented at runtime.
"""

from __future__ import annotations

from typing import Optional

# Ordered worst-first: a lower rank means a more dangerous operation.
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
RISK_LEVELS = ("high", "medium", "low")

# Confidence is a separate axis from risk and is what makes the tool honest:
#   * "verified" -- a purely syntactic fact (there is no CONCURRENTLY keyword),
#                   so the finding holds regardless of data or version.
#   * "likely"   -- depends on the Postgres version being >= some release.
#   * "judgement"-- depends on data volume / row distribution / deployment.
CONFIDENCE_ORDER = {"verified": 0, "likely": 1, "judgement": 2}

# Categories are used only for grouping in the report.
CATEGORIES = (
    "destructive",
    "index-concurrency",
    "type-change",
    "column",
    "constraint",
    "privilege",
    "lock",
    "transaction",
    "order",
)

RULE_TITLES = {
    "destructive-statement": "irreversible data loss",
    "index-not-concurrent": "index build blocks writes",
    "index-drop-not-concurrent": "index drop blocks reads and writes",
    "type-rewrite": "table rewrite under ACCESS EXCLUSIVE",
    "type-safe": "type change is metadata-only",
    "type-unknown": "type change needs manual review",
    "add-column-not-null": "ADD COLUMN NOT NULL without DEFAULT",
    "add-column-default": "ADD COLUMN DEFAULT rewrote the whole table (PG < 11)",
    "set-not-null": "SET NOT NULL scans the table under ACCESS EXCLUSIVE",
    "constraint-not-valid": "constraint validated while holding a strong lock",
    "constraint-never-validated": "NOT VALID constraint was never validated",
    "unbounded-dml": "unbounded UPDATE/DELETE",
    "reindex-locking": "REINDEX blocks the table",
    "vacuum-full": "VACUUM FULL rewrites the table under ACCESS EXCLUSIVE",
    "lock-churn": "many ALTER TABLE statements on one table",
    "no-transaction-control": "no explicit transaction control",
    "transaction-illegal": "statement cannot run inside a transaction block",
    "transaction-illegal-risk": "CREATE DATABASE cannot be transactional",
    "transaction-concurrently": "CREATE INDEX CONCURRENTLY inside a transaction",
    "lock-timeout-missing": "ACCESS EXCLUSIVE lock taken without lock_timeout",
    "lock-timeout-disabled": "lock_timeout explicitly disabled",
    "migration-order-gap": "gap in the migration sequence",
    "migration-order-duplicate": "duplicate migration number",
    "migration-order-unnumbered": "file does not follow the numbering convention",
    "migration-order-single-file": "order check needs the whole directory",
    "add-column-safe": "nullable ADD COLUMN is safe",
    "constraint-not-valid-ok": "NOT VALID constraint is the safe half",
}

# Categories that take an ACCESS EXCLUSIVE lock on at least one table, and
# therefore should be preceded by an explicit lock_timeout budget.
_TITLE_BY_ID = RULE_TITLES


#: Rules that describe a step the tool checked and found *acceptable*.  They are
#: emitted so the JSON output records that the pattern was recognised, but they
#: are not problems, so they never appear as findings in the text report and
#: never affect the exit code.  Otherwise a correctly written migration would
#: "fail" the check simply for being recognised.
NOTE_ONLY_RULES = frozenset(
    {
        "add-column-safe",
        "constraint-not-valid-ok",
        "type-safe",
        "vacuum-plain",
    }
)


def is_note(rule_id: str) -> bool:
    return rule_id in NOTE_ONLY_RULES


def rank(risk: str) -> int:
    return RISK_ORDER.get(risk, 99)


def at_or_above(risk: str, threshold: str) -> bool:
    """True when ``risk`` is at least as dangerous as ``threshold``."""
    return rank(risk) <= rank(threshold)


def worse(a: str, b: str) -> str:
    return a if rank(a) <= rank(b) else b


def normalize_risk(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    if low in RISK_ORDER:
        return low
    # Accept a numeric-ish alias used by some CI configs.
    aliases = {"h": "high", "m": "medium", "l": "low", "3": "high", "2": "medium", "1": "low"}
    return aliases.get(low)


def title_for(rule_id: str) -> str:
    return _TITLE_BY_ID.get(rule_id, rule_id)
