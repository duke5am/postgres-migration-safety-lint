#!/usr/bin/env python3
"""sql-migration-lint - flag the dangerous steps in a SQL migration file.

Static analysis only: no database connection, no schema access, no network.
Point it at a single ``.sql`` file or at a directory of migrations.

Exit codes
----------
0   nothing at or above the ``--min-risk`` threshold
1   at least one finding at or above the threshold
2   usage error, unreadable path, or a file that could not be parsed

Examples
--------
postgres-migration-safety-lint migrations/0007_add_email.sql
postgres-migration-safety-lint migrations/ --min-risk medium --json
postgres-migration-safety-lint migrations/ --check-order

The implementation lives here, inside the importable package, so that the
installed console script (``postgres-migration-safety-lint``) and the repo-root
wrapper ``sql_migration_lint.py`` run exactly the same code.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlmig import __version__, analyzer, report as reporting, rules

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

EPILOG = """\
exit codes:
  0  nothing at or above --min-risk
  1  findings at or above --min-risk
  2  usage error or unreadable/unparsable input

Only findings at or above --min-risk are printed and only those affect the exit
code, so the same command can gate a build (--min-risk high) and produce a full
report (--min-risk low) from one tool.

This tool never connects to a database. It reads .sql text, skips comments,
string literals and dollar-quoted function bodies, and reports what it can prove
about the statements it sees.
"""


class _Parser(argparse.ArgumentParser):
    """argparse that exits with the documented usage code (2)."""

    def error(self, message: str):  # pragma: no cover - exercised via subprocess
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        # No hard-coded prog: usage and error messages then name the command the
        # user actually ran -- "sql_migration_lint.py" from a clone,
        # "postgres-migration-safety-lint" from the installed console script.
        description="Static safety analyser for SQL migration files (PostgreSQL focus).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        help="a .sql migration file, or a directory containing .sql migrations",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of text",
    )
    parser.add_argument(
        "--min-risk",
        default="low",
        metavar="high|medium|low",
        help="only report findings at this risk level or worse (default: low, "
             "meaning report everything)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colour (also honours the NO_COLOR environment variable)",
    )
    parser.add_argument(
        "--check-order",
        action="store_true",
        help="also check the numbered filename convention (duplicates, gaps) of a "
             "migration directory",
    )
    parser.add_argument(
        "--no-lock-summary",
        action="store_true",
        help="hide the per-statement lock risk list",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"sql-migration-lint {__version__}",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    threshold = rules.normalize_risk(args.min_risk)
    if threshold is None:
        sys.stderr.write(
            f"{parser.prog}: error: --min-risk must be one of "
            f"{', '.join(rules.RISK_LEVELS)} (got {args.min_risk!r})\n"
        )
        return EXIT_USAGE

    path = args.path
    if not os.path.exists(path):
        sys.stderr.write(f"{parser.prog}: error: no such file or directory: {path}\n")
        return EXIT_USAGE
    if os.path.isdir(path):
        if not any(
            n.lower().endswith(".sql") and os.path.isfile(os.path.join(path, n))
            for n in os.listdir(path)
        ):
            sys.stderr.write(
                f"{parser.prog}: error: no .sql files in directory: {path}\n"
            )
            return EXIT_USAGE
    elif not os.path.isfile(path):
        sys.stderr.write(f"{parser.prog}: error: not a regular file: {path}\n")
        return EXIT_USAGE

    try:
        reports = analyzer.analyse_path(path)
    except OSError as exc:
        sys.stderr.write(f"{parser.prog}: error: cannot read {path}: {exc}\n")
        return EXIT_USAGE

    if args.check_order:
        reports = reports + [analyzer.check_order(path)]

    for rep in reports:
        if rep.parse_errors:
            sys.stderr.write(
                f"{parser.prog}: warning: {rep.path}: "
                + "; ".join(rep.parse_errors)
                + "\n"
            )

    paint = reporting.Palette(False if args.json else reporting.color_enabled(args.no_color))

    if args.json:
        sys.stdout.write(reporting.render_json(reports, threshold) + "\n")
    else:
        blocks = []
        for rep in reports:
            blocks.append(
                reporting.render_file(
                    rep,
                    paint,
                    threshold=threshold,
                    show_operations=not args.no_lock_summary,
                )
            )
        sys.stdout.write("\n\n".join(blocks) + "\n\n")
        sys.stdout.write(reporting.render_summary(reports, threshold, paint) + "\n")
        if not any(rep.filtered(threshold) for rep in reports):
            sys.stdout.write(
                paint(
                    "No findings at or above --min-risk "
                    f"{threshold}. This is a static check: it says nothing about "
                    "whether the statements are correct, only that none of the "
                    "patterns this tool knows about matched.\n",
                    reporting.DIM,
                )
            )

    reported = sum(len(rep.filtered(threshold)) for rep in reports)
    return EXIT_FINDINGS if reported else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
