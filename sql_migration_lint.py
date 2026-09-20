#!/usr/bin/env python3
"""sql-migration-lint - flag the dangerous steps in a SQL migration file.

This wrapper exists so `python3 sql_migration_lint.py <path>` keeps working from
a clone. The same CLI is installed as the `postgres-migration-safety-lint`
console script; the implementation lives in `sqlmig/cli.py` so that the
installed package and the checkout are the same code, not two versions of it.

Exit codes
----------
0   nothing at or above the ``--min-risk`` threshold
1   at least one finding at or above the threshold
2   usage error, unreadable path, or a file that could not be parsed
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlmig.cli import (  # noqa: E402
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    main,
)

__all__ = ["EXIT_FINDINGS", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]

if __name__ == "__main__":
    sys.exit(main())
