"""Text and JSON rendering of a lint run."""

from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from . import rules

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
GREEN = "\033[32m"
CYAN = "\033[36m"

RISK_COLOR = {"high": RED, "medium": YELLOW, "low": BLUE}
CONFIDENCE_LABEL = {
    "verified": "verified by syntax",
    "likely": "depends on version",
    "judgement": "depends on data/scale",
}


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "".join(codes) + text + RESET


def _rule_slug(rule_id: str) -> str:
    return rule_id


def _wrap(text: str, width: int, indent: str) -> list:
    """Wrap ``text`` at ``width`` columns, preserving explicit newlines."""
    if width < 20:
        width = 20
    out: list = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        if not words:
            out.append("")
            continue
        line = words[0]
        for word in words[1:]:
            if len(line) + 1 + len(word) > width:
                out.append(line)
                line = indent + word
            else:
                line += " " + word
        out.append(line)
    return out


def render_file(report, paint: Palette, threshold: str = "low", width: Optional[int] = None,
                show_operations: bool = True) -> str:
    """Render one :class:`~sqlmig.analyzer.FileReport` as text."""
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    width = max(60, min(width, 120))
    body = max(40, width - 6)

    lines: list = []
    shown = report.filtered(threshold)
    hidden = len(report.problems) - len(shown)

    header = f"{report.path}"
    counts = {}
    for finding in report.problems:
        counts[finding.risk] = counts.get(finding.risk, 0) + 1
    summary_bits = ", ".join(
        f"{counts[r]} {r}" for r in rules.RISK_LEVELS if counts.get(r)
    ) or "no findings"
    lines.append(paint(header, BOLD))
    lines.append(
        paint(
            f"  {report.statement_count} statement(s), {report.total_lines} line(s) - "
            f"{summary_bits}",
            DIM,
        )
    )

    if report.parse_errors:
        lines.append("")
        for err in report.parse_errors:
            lines.append(paint(f"  ! {err}", YELLOW))

    for finding in shown:
        color = RISK_COLOR.get(finding.risk, "")
        tag = f"[{finding.risk.upper()}]"
        lines.append("")
        lines.append(
            "  "
            + paint(tag.ljust(9), color, BOLD)
            + paint(finding.title, BOLD)
            + paint(f"  ({finding.category})", DIM)
        )
        lines.append(paint(f"    line {finding.line}", DIM) + "  "
                     + paint(finding.statement, CYAN))
        lines.append("    " + paint("why:", DIM) + f" {_rule_slug(finding.rule_id)}"
                     + paint(f" - confidence: {CONFIDENCE_LABEL.get(finding.confidence, finding.confidence)}", DIM))
        for chunk in _wrap(finding.message, body, "      "):
            lines.append("      " + chunk)
        if finding.suggestion:
            lines.append("      " + paint("safer:", GREEN, BOLD))
            for chunk in _wrap(finding.suggestion, body, "      "):
                lines.append("      " + chunk)

    if show_operations and report.operations:
        lines.append("")
        lines.append(paint("  lock risk by statement", BOLD))
        for op in report.operations:
            color = RISK_COLOR.get(op.risk, "")
            suffix = f"  {op.note}" if op.note else ""
            table = f" on {op.table}" if op.table else ""
            lines.append(
                "    "
                + paint(op.risk.upper().ljust(6), color)
                + f" line {str(op.line).ljust(5)}"
                + paint(f"{op.kind}{table}", DIM)
                + paint(suffix, DIM)
            )

    if hidden:
        lines.append("")
        lines.append(
            paint(
                f"  {hidden} finding(s) below --min-risk {threshold} not shown "
                "(use --min-risk low to see everything)",
                DIM,
            )
        )
    return "\n".join(lines)


def render_summary(reports: list, threshold: str, paint: Palette) -> str:
    total = sum(len(r.problems) for r in reports)
    reported = sum(len(r.filtered(threshold)) for r in reports)
    counts: dict = {}
    for report in reports:
        for finding in report.problems:
            counts[finding.risk] = counts.get(finding.risk, 0) + 1
    bits = ", ".join(f"{counts[r]} {r}" for r in rules.RISK_LEVELS if counts.get(r))
    if not bits:
        bits = "no findings"
    files = len(reports)
    text = f"{files} file(s) checked, {total} finding(s): {bits}"
    if reported != total:
        text += f" ({reported} at or above --min-risk {threshold})"
    color = GREEN if total == 0 else RED
    return paint(text, color, BOLD)


def render_json(reports: list, threshold: str) -> str:
    payload = {
        "tool": "sql-migration-lint",
        "version": _version(),
        "threshold": threshold,
        "totals": {
            "files": len(reports),
            "statements": sum(r.statement_count for r in reports),
            "findings": sum(len(r.problems) for r in reports),
            "notes": sum(len(r.notes) for r in reports),
            "reported_findings": sum(len(r.filtered(threshold)) for r in reports),
        },
        "files": [r.to_dict(threshold) for r in reports],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _version() -> str:
    from . import __version__
    return __version__


def color_enabled(no_color: bool) -> bool:
    if no_color:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return os.isatty(1)
    except (OSError, ValueError):
        return False
