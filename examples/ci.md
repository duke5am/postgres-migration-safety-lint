# Using sql-migration-lint in CI

Two commands, two purposes: gate the build on the dangerous stuff, and post the
full report so a reviewer sees everything.

## GitHub Actions

```yaml
name: migration-safety

on:
  pull_request:
    paths:
      - 'migrations/**'

jobs:
  lint-migrations:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Lint changed migrations (fails the job on high-risk findings)
        run: python3 sql-migration-lint/sql_migration_lint.py migrations/ --min-risk high --no-color

      - name: Full report (never fails the job)
        if: always()
        run: python3 sql-migration-lint/sql_migration_lint.py migrations/ --min-risk low --json > migration-lint.json

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: migration-lint-report
          path: migration-lint.json
```

`--min-risk high` exits `1` only for findings that are dangerous on any
PostgreSQL version; `--min-risk low` reports everything for the reviewer. Because
a usage error exits `2`, a mistyped path cannot masquerade as a clean run.

## Reading the JSON

```console
python3 sql_migration_lint.py migrations/ --json --min-risk medium \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['totals'])"
```

```json
{"files": 6, "statements": 42, "findings": 9, "notes": 14, "reported_findings": 9}
```

Each entry in `files[].findings` carries `line`, `rule`, `title`, `risk`,
`confidence`, `category`, `table`, `statement`, `message` and `suggestion`, so a
bot comment can be generated without re-implementing any of the wording.

## Pre-commit hook

```bash
#!/bin/sh
# .git/hooks/pre-commit - block commits that add a high-risk migration step.
changed=$(git diff --cached --name-only --diff-filter=ACM | grep '^migrations/.*\.sql$' || true)
[ -z "$changed" ] && exit 0
python3 sql-migration-lint/sql_migration_lint.py migrations/ --min-risk high --no-color
```

Note that this lints the whole `migrations/` directory rather than only the
staged files: the two-migration `NOT VALID` + `VALIDATE` pattern only reads
correctly when both files are visible to the tool.
