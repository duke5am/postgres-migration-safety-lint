# postgres-migration-safety-lint
A static analyser for SQL migration files. Point it at a `.sql` file (or a
directory of them) and it reports the operations that are dangerous to run
against a live PostgreSQL database, why they are dangerous, and what to write
instead.

It needs **no database connection**, makes **no network calls**, and installs
**nothing**: one Python 3 file plus a small package, standard library only.

```console
$ python3 sql_migration_lint.py examples/0007_add_email.sql
```

```
examples/0007_add_email.sql
  4 statement(s), 10 line(s) - 3 high, 1 medium

  [HIGH]   ADD COLUMN NOT NULL without DEFAULT  (column)
    line 4  ALTER TABLE accounts ADD COLUMN email text NOT NULL
    why: add-column-not-null - confidence: verified by syntax
      `ADD COLUMN accounts.email ... NOT NULL` with no DEFAULT cannot satisfy the constraint for
            rows that already exist, so the statement fails outright on a non-empty table (`column
            contains null values`).
      safer:
      Three steps instead: (1) `ADD COLUMN ...` nullable and deploy, (2) backfill in batches, (3)
            either `ALTER TABLE ... ALTER COLUMN ... SET DEFAULT <v>` plus `SET NOT NULL`, or add
            `CHECK (col IS NOT NULL) NOT VALID` and `VALIDATE CONSTRAINT` before `SET NOT NULL`
            (PostgreSQL 12+ can then skip the validation scan).

  [MEDIUM] many ALTER TABLE statements on one table  (lock)
    line 4  ALTER TABLE accounts ADD COLUMN email text NOT NULL
    why: lock-churn - confidence: verified by syntax
      `accounts` is altered 2 times in this file (lines 4, 9). Each `ALTER TABLE` queues for its own
            ACCESS EXCLUSIVE lock, so the table is briefly frozen several times and there are more
            chances to queue behind a long-running query.
      safer:
      Combine the actions into one statement: `ALTER TABLE accounts ADD COLUMN ..., ADD COLUMN ...,
            ALTER COLUMN ...;` A single ALTER TABLE takes one lock for all of its actions, so the
            total lock time drops even though the work is the same.

  [HIGH]   index build blocks writes  (index-concurrency)
    line 7  CREATE INDEX idx_accounts_email ON accounts (email)
    why: index-not-concurrent - confidence: verified by syntax
      `CREATE INDEX` (index `idx_accounts_email` on `accounts`) takes a SHARE lock that blocks
            INSERT, UPDATE and DELETE for the entire build, which grows with table size; reads
            continue.
      safer:
      Use `CREATE INDEX CONCURRENTLY` (or `CREATE UNIQUE INDEX CONCURRENTLY`). It takes longer and
            can leave an INVALID index behind if it fails, so run it outside a transaction and check
            `pg_index.indisvalid` afterwards, dropping and recreating any INVALID index.

  [HIGH]   constraint validated while holding a strong lock  (constraint)
    line 9  ALTER TABLE accounts ADD CONSTRAINT accounts_email_key UNIQUE (email)
    why: constraint-not-valid - confidence: depends on data/scale
      `ADD CONSTRAINT accounts_email_key` builds or validates an index over the whole table while
            blocking writes on `accounts`.
      safer:
      Build the supporting index with `CREATE INDEX CONCURRENTLY`, then attach the constraint with
            `ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY USING INDEX index_name` (or `UNIQUE
            USING INDEX`), which only takes a brief lock.

  lock risk by statement
    HIGH   line 4    add-column on accounts  NOT NULL without DEFAULT
    HIGH   line 7    create-index on accounts  blocks writes for the whole build
    HIGH   line 9    add-constraint on accounts  UNIQUE constraint validated immediately

1 file(s) checked, 4 finding(s): 3 high, 1 medium
```

## Why this exists

Most "dangerous migration" mistakes are not subtle. They are `ADD COLUMN ...
NOT NULL` with no default, `CREATE INDEX` without `CONCURRENTLY`, a bare
`UPDATE` with no `WHERE`, a foreign key added without `NOT VALID`, a
`SET NOT NULL` that scans the whole table while holding an `ACCESS EXCLUSIVE`
lock, or a `VACUUM` inside a transaction block that PostgreSQL refuses to run at
all. Each of those has a known safe rewrite, and each one is visible in the
migration text without asking the server anything.

`sql-migration-lint` is the free companion to the **SQL Migration Safety Pack**.
It is deliberately narrow: it knows PostgreSQL, it reads text, and it says what
it can prove.

## Install

Copy the directory and run it. There is nothing to install.

```console
git clone <this repository>
cd sql-migration-lint
python3 sql_migration_lint.py --help
```

Requires Python 3.8 or newer (developed and tested on CPython 3.13). No third
party packages, no database driver, no network access.

## Usage

```console
python3 sql_migration_lint.py <path> [--json] [--min-risk high|medium|low] [--no-color]
```

| Argument | Meaning |
| --- | --- |
| `<path>` | a single `.sql` migration file, or a directory containing `.sql` files |
| `--json` | machine-readable output instead of text; every finding carries `line`, `rule`, `title`, `risk`, `confidence`, `category`, `table`, `detail`, `statement`, `message` and `suggestion`, and `files[].operations` lists the per-statement lock risk |
| `--min-risk` | `high`, `medium` or `low` (default `low`). Only findings at this level or worse are printed, and only those decide the exit code |
| `--no-color` | disable ANSI colour (also honours the `NO_COLOR` environment variable) |
| `--check-order` | additionally check the numbered filename convention of a migration directory |
| `--no-lock-summary` | hide the per-statement lock risk list |
| `--version` | print the version |

When a directory is given, every `.sql` file is analysed in filename order, each
file gets its own block, and a summary closes the run:

```console
$ python3 sql_migration_lint.py tests/fixtures/order --min-risk medium --no-color --no-lock-summary
```
```
tests/fixtures/order/0001_create_users.sql
  1 statement(s), 6 line(s) - no findings

tests/fixtures/order/hotfix_indexes.sql
  1 statement(s), 6 line(s) - 1 medium

  [MEDIUM] no explicit transaction control  (transaction)
    line 1  <whole file>
    why: no-transaction-control - confidence: depends on data/scale
      This file mixes `CONCURRENTLY` statements with ordinary DDL and declares no transaction
            control either way. `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block,
            and it is the one statement that cannot be rolled back, so if the runner wraps this file
            in a transaction the index build fails outright, and if it does not, a later failure
            leaves the index built while the rest of the file is not applied.
      safer:
      Split the file: keep `CONCURRENTLY` builds alone in a migration marked `-- migrate:
            no-transaction` (Flyway `executeInTransaction=false`, Rails `disable_ddl_transaction!`,
            Django `atomic = False`), and put the ordinary DDL in a transactional file with explicit
            `BEGIN`/`COMMIT`.

5 file(s) checked, 1 finding(s): 1 medium
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | nothing at or above `--min-risk`; the file or directory is clean by these rules |
| `1` | at least one finding at or above `--min-risk` |
| `2` | usage error: unknown flag, bad `--min-risk` value, or an unreadable path |

That makes the tool usable as a CI gate with one command per severity:

```console
python3 sql_migration_lint.py migrations/ --min-risk high       # blocks the build
python3 sql_migration_lint.py migrations/ --min-risk low --json # full report for the PR comment
```

A file with no `.sql` files in it, a mistyped path, or an unknown flag all exit
`2`, so a green build can never mean "the tool did not run".

### Checking migration numbering

```console
$ python3 sql_migration_lint.py migrations/ --check-order --min-risk medium
```

```
migrations
  5 statement(s), 0 line(s) - 1 high, 1 medium, 1 low

  [HIGH]   duplicate migration number  (order)
    line 0  0002_add_accounts.sql, 0002_add_sessions.sql
    why: migration-order-duplicate - confidence: verified by syntax
      Migration number 2 is used by 2 files (0002_add_accounts.sql,
            0002_add_sessions.sql). Two migrations claiming the same slot have no
            defined order between them, and runners that key on the number (or on a
            version table) will apply whichever they read first and then silently skip
            the other.
      safer:
      Renumber one of them into the next free slot and keep the sequence strictly
            increasing.
```

Duplicates are reported as `high` (two migrations with no defined order between
them is a real bug); a gap in the sequence is `low`, because a gap is usually
harmless and only matters if it means a deployed migration was deleted; a file
with no numeric prefix inside a numbered directory is `medium`, because its
position depends entirely on the runner. `--check-order` needs a directory:
gaps and duplicates only exist relative to the files next to them.

## What it detects

Everything in this table is implemented, exercised by the test suite, and
mentioned in the report with a concrete reason and a concrete rewrite.

| Rule | Risk | What it catches |
| --- | --- | --- |
| `destructive-statement` | high | `DROP COLUMN`, `DROP TABLE`, `TRUNCATE`, `DROP DATABASE` — irreversible data loss |
| `index-not-concurrent` | high | `CREATE INDEX` / `CREATE UNIQUE INDEX` without `CONCURRENTLY` |
| `index-drop-not-concurrent` | high | `DROP INDEX` without `CONCURRENTLY` |
| `type-rewrite` | high | `ALTER COLUMN ... TYPE` that is not a binary-coercible widening, so the table is rewritten under `ACCESS EXCLUSIVE` |
| `type-safe` | low (note) | the type change *is* metadata-only (widening `varchar(50)` to `varchar(120)`, `TEXT`, increasing `NUMERIC` precision) |
| `type-unknown` | high | a type pair that cannot be classified from the text alone, with the `pg_cast` query to check it |
| `add-column-not-null` | high | `ADD COLUMN ... NOT NULL` with no `DEFAULT` — fails on a non-empty table |
| `add-column-default` | medium | `ADD COLUMN ... DEFAULT` — a full table rewrite on PostgreSQL 10 and older |
| `add-column-safe` | low (note) | a nullable `ADD COLUMN` with no default, which is metadata-only |
| `set-not-null` | high | `SET NOT NULL` instead of `CHECK ... NOT VALID` + `VALIDATE CONSTRAINT` |
| `constraint-not-valid` | high | `ADD CONSTRAINT` / `ADD FOREIGN KEY` / `ADD UNIQUE` / `ADD CHECK` without `NOT VALID` |
| `constraint-not-valid-ok` | low (note) | a `NOT VALID` constraint, i.e. the safe half of that pattern |
| `constraint-never-validated` | medium | a `NOT VALID` constraint that no later migration validates |
| `unbounded-dml` | high | `UPDATE` or `DELETE` with no `WHERE` clause |
| `lock-churn` | medium | more than one `ALTER TABLE` on the same table in one file |
| `no-transaction-control` | medium | `CONCURRENTLY` mixed with ordinary DDL, or a non-transactional statement, in a file with no declared transaction control |
| `transaction-concurrently` | high | `CREATE INDEX CONCURRENTLY` inside a `BEGIN`/`COMMIT` block (PostgreSQL refuses it) |
| `transaction-illegal` | high | `VACUUM` / `CREATE DATABASE` / `DROP DATABASE` inside a transaction block |
| `transaction-illegal-risk` | medium | `CREATE DATABASE` (or `DROP DATABASE`) in a file without transaction control, where the runner's implicit transaction would break it |
| `vacuum-full` | high | `VACUUM FULL`, which rewrites the table under `ACCESS EXCLUSIVE` |
| `reindex-locking` | high / medium | `REINDEX`, high for the blocking form, medium with `CONCURRENTLY` |
| `lock-timeout-missing` | medium | an `ACCESS EXCLUSIVE` operation with no `lock_timeout` set anywhere in the file |
| `lock-timeout-disabled` | medium | `SET lock_timeout = 0`, i.e. "wait forever" |
| `migration-order-duplicate` | high | two files claiming the same migration number |
| `migration-order-gap` | low | a missing number in the sequence |
| `migration-order-unnumbered` | medium | a `.sql` file without a numeric prefix in a numbered directory |
| `migration-order-single-file` | low | `--check-order` was given a file, not a directory, so no ordering check was possible |

### Lock risk, and why confidence is reported separately

Every statement also gets a lock risk estimate — `high`, `medium` or `low` — with
the reasoning shown in the `lock risk by statement` block. The estimate is about
*lock behaviour*: how long a table is unavailable and how many other statements
queue behind it. A nullable `ADD COLUMN` is `low` (metadata-only), a
`CREATE INDEX CONCURRENTLY` is `medium` (no write blocking, but a long scan and
an invalid-index risk if it fails), a `CREATE INDEX` is `high` (writes blocked
for the whole build).

Each finding also carries a confidence, because risk and certainty are different
questions:

- **verified by syntax** — the fact is in the text: there is no `CONCURRENTLY`
  keyword, there is no `WHERE` clause, the `NOT NULL` has no `DEFAULT`. This
  holds regardless of your data or version.
- **depends on version** — true on some PostgreSQL versions and not others, e.g.
  `ADD COLUMN ... DEFAULT` since PostgreSQL 11 stores the default in the catalog
  and does not rewrite the table.
- **depends on data/scale** — true in practice because of how much data you have
  or whether a lock is contended, e.g. whether an idle transaction in a
  connection pool happens to be holding the table when your migration runs.

The tool never claims a lock *will* be taken on your server. It reports the
operation it can see, the lock that operation classically requires, and the
rewrite that removes the risk.

## Real output

Two runs against the fixtures in this repository. Both are pasted verbatim.

### A dangerous migration

`tests/fixtures/dangerous/0007_risky_account_changes.sql` is a short file that
contains almost every mistake at once.

```console
$ python3 sql_migration_lint.py tests/fixtures/dangerous/0007_risky_account_changes.sql --no-color
```

```
tests/fixtures/dangerous/0007_risky_account_changes.sql
  18 statement(s), 29 line(s) - 15 high, 5 medium

  [MEDIUM] no explicit transaction control  (transaction)
    line 1  <whole file>
    why: no-transaction-control - confidence: depends on data/scale
      This file mixes `CONCURRENTLY` statements with ordinary DDL and declares no transaction
            control either way. `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block,
            and it is the one statement that cannot be rolled back, so if the runner wraps this file
            in a transaction the index build fails outright, and if it does not, a later failure
            leaves the index built while the rest of the file is not applied.
      safer:
      Split the file: keep `CONCURRENTLY` builds alone in a migration marked `-- migrate:
            no-transaction` (Flyway `executeInTransaction=false`, Rails `disable_ddl_transaction!`,
            Django `atomic = False`), and put the ordinary DDL in a transactional file with explicit
            `BEGIN`/`COMMIT`.

  [HIGH]   ADD COLUMN NOT NULL without DEFAULT  (column)
    line 5  ALTER TABLE accounts ADD COLUMN email text NOT NULL
    why: add-column-not-null - confidence: verified by syntax
      `ADD COLUMN accounts.email ... NOT NULL` with no DEFAULT cannot satisfy the constraint for
            rows that already exist, so the statement fails outright on a non-empty table (`column
            contains null values`).
      safer:
      Three steps instead: (1) `ADD COLUMN ...` nullable and deploy, (2) backfill in batches, (3)
            either `ALTER TABLE ... ALTER COLUMN ... SET DEFAULT <v>` plus `SET NOT NULL`, or add
            `CHECK (col IS NOT NULL) NOT VALID` and `VALIDATE CONSTRAINT` before `SET NOT NULL`
            (PostgreSQL 12+ can then skip the validation scan).

  [MEDIUM] many ALTER TABLE statements on one table  (lock)
    line 5  ALTER TABLE accounts ADD COLUMN email text NOT NULL
    why: lock-churn - confidence: verified by syntax
      `accounts` is altered 9 times in this file (lines 5, 6, 7, 8, 9, 10, 16, 17, 20). Each `ALTER
            TABLE` queues for its own ACCESS EXCLUSIVE lock, so the table is briefly frozen several
            times and there are more chances to queue behind a long-running query.
      safer:
      Combine the actions into one statement: `ALTER TABLE accounts ADD COLUMN ..., ADD COLUMN ...,
            ALTER COLUMN ...;` A single ALTER TABLE takes one lock for all of its actions, so the
            total lock time drops even though the work is the same.

  [MEDIUM] ACCESS EXCLUSIVE lock taken without lock_timeout  (lock)
    line 5  ALTER TABLE accounts ADD COLUMN email text NOT NULL
    why: lock-timeout-missing - confidence: depends on data/scale
      No `lock_timeout` is set anywhere in this file, yet it contains at least one statement that
            needs an ACCESS EXCLUSIVE lock. That lock request queues behind every open transaction
            touching the table, and once it is queued it blocks every later query on that table too,
            including plain SELECTs. An idle transaction in a connection pool is enough to stall the
            migration and then the application.
      safer:
      Put `SET lock_timeout = '5s';` before the locking statements (and `RESET lock_timeout;` after
            them) so the migration fails fast and can be retried during a quieter moment instead of
            freezing the table.

  [MEDIUM] ADD COLUMN DEFAULT rewrote the whole table (PG < 11)  (column)
    line 6  ALTER TABLE accounts ADD COLUMN created_at timestamptz DEFAULT now ()
    why: add-column-default - confidence: depends on version
      `ADD COLUMN accounts.created_at ... DEFAULT` rewrites the entire table while holding an ACCESS
            EXCLUSIVE lock on PostgreSQL 10 and older. On PostgreSQL 11+ the default is stored in
            the catalog and no rewrite happens, so the risk depends on the server version.
      safer:
      If any environment is older than PostgreSQL 11: `ADD COLUMN` with no DEFAULT, backfill in
            batches, then `SET DEFAULT` for future inserts. On PostgreSQL 11+ keep the DEFAULT but
            still set `lock_timeout`, because the ACCESS EXCLUSIVE lock is taken even for a
            metadata-only change.

  [HIGH]   SET NOT NULL scans the table under ACCESS EXCLUSIVE  (column)
    line 7  ALTER TABLE accounts ALTER COLUMN nickname SET NOT NULL
    why: set-not-null - confidence: depends on version
      `ALTER COLUMN accounts.nickname SET NOT NULL` scans the whole table to prove no row is null,
            and it does so while holding ACCESS EXCLUSIVE, so reads and writes are blocked for the
            duration of the scan.
      safer:
      Use the CHECK ... NOT VALID pattern: `ALTER TABLE ... ADD CONSTRAINT col_not_null CHECK (col
            IS NOT NULL) NOT VALID;` then `ALTER TABLE ... VALIDATE CONSTRAINT col_not_null;` (SHARE
            UPDATE EXCLUSIVE, writes continue). On PostgreSQL 12+ a following `SET NOT NULL` can
            then reuse the validated constraint instead of scanning again. Then drop the CHECK if
            you do not want it permanently.

  [HIGH]   type change needs manual review  (type-change)
    line 8  ALTER TABLE accounts ALTER COLUMN balance TYPE varchar(40)
    why: type-unknown - confidence: depends on data/scale
      `ALTER COLUMN accounts.balance TYPE varchar(40)` could not be classified statically (<type not
            declared in this file> -> varchar(40)): could not read one side of the type change.
      safer:
      Check whether PostgreSQL can use a binary-coercible cast for this pair (`SELECT
            castsource::regtype, casttarget::regtype FROM pg_cast WHERE castmethod = 'b'`). If it
            cannot, treat it as a rewrite and use the expand/contract pattern. Naming the current
            type in a comment such as `-- was integer` lets this tool tell the two cases apart next
            time.

  [HIGH]   type change needs manual review  (type-change)
    line 9  ALTER TABLE accounts ALTER COLUMN note TYPE varchar(400)
    why: type-unknown - confidence: depends on data/scale
      `ALTER COLUMN accounts.note TYPE varchar(400)` could not be classified statically (<type not
            declared in this file> -> varchar(400)): could not read one side of the type change.
      safer:
      Check whether PostgreSQL can use a binary-coercible cast for this pair (`SELECT
            castsource::regtype, casttarget::regtype FROM pg_cast WHERE castmethod = 'b'`). If it
            cannot, treat it as a rewrite and use the expand/contract pattern. Naming the current
            type in a comment such as `-- was integer` lets this tool tell the two cases apart next
            time.

  [HIGH]   type change needs manual review  (type-change)
    line 10  ALTER TABLE accounts ALTER COLUMN note TYPE text
    why: type-unknown - confidence: depends on data/scale
      `ALTER COLUMN accounts.note TYPE text` could not be classified statically (<type not declared
            in this file> -> text): could not read one side of the type change.
      safer:
      Check whether PostgreSQL can use a binary-coercible cast for this pair (`SELECT
            castsource::regtype, casttarget::regtype FROM pg_cast WHERE castmethod = 'b'`). If it
            cannot, treat it as a rewrite and use the expand/contract pattern. Naming the current
            type in a comment such as `-- was integer` lets this tool tell the two cases apart next
            time.

  [HIGH]   index build blocks writes  (index-concurrency)
    line 12  CREATE INDEX idx_accounts_email ON accounts (email)
    why: index-not-concurrent - confidence: verified by syntax
      `CREATE INDEX` (index `idx_accounts_email` on `accounts`) takes a SHARE lock that blocks
            INSERT, UPDATE and DELETE for the entire build, which grows with table size; reads
            continue.
      safer:
      Use `CREATE INDEX CONCURRENTLY` (or `CREATE UNIQUE INDEX CONCURRENTLY`). It takes longer and
            can leave an INVALID index behind if it fails, so run it outside a transaction and check
            `pg_index.indisvalid` afterwards, dropping and recreating any INVALID index.

  [HIGH]   index drop blocks reads and writes  (index-concurrency)
    line 13  DROP INDEX idx_accounts_legacy
    why: index-drop-not-concurrent - confidence: verified by syntax
      `DROP INDEX` (index `idx_accounts_legacy`) takes an ACCESS EXCLUSIVE lock, blocking reads and
            writes on the table that uses the index until the catalog change commits.
      safer:
      Use `DROP INDEX CONCURRENTLY` (PostgreSQL 9.2+): it does not block reads or writes. It cannot
            run inside a transaction block, and like all CONCURRENTLY operations it can leave the
            index INVALID if it fails, so verify and retry.

  [HIGH]   constraint validated while holding a strong lock  (constraint)
    line 16  ALTER TABLE accounts ADD CONSTRAINT accounts_email_key UNIQUE (email)
    why: constraint-not-valid - confidence: depends on data/scale
      `ADD CONSTRAINT accounts_email_key` builds or validates an index over the whole table while
            blocking writes on `accounts`.
      safer:
      Build the supporting index with `CREATE INDEX CONCURRENTLY`, then attach the constraint with
            `ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY USING INDEX index_name` (or `UNIQUE
            USING INDEX`), which only takes a brief lock.

  [HIGH]   constraint validated while holding a strong lock  (constraint)
    line 17  ALTER TABLE accounts ADD CONSTRAINT accounts_owner_fk FOREIGN KEY (ow...
    why: constraint-not-valid - confidence: depends on data/scale
      `ADD CONSTRAINT accounts_owner_fk FOREIGN KEY` without `NOT VALID` validates every existing
            row of `accounts` under a lock that also blocks writes on the referenced table, and it
            fails outright if any existing row violates it.
      safer:
      Two migrations: (1) `ALTER TABLE ... ADD CONSTRAINT accounts_owner_fk FOREIGN KEY (...)
            REFERENCES ... NOT VALID;` (brief lock, no scan) and deploy; (2) `ALTER TABLE ...
            VALIDATE CONSTRAINT accounts_owner_fk;` which takes SHARE UPDATE EXCLUSIVE and lets
            writes continue while it checks.

  [HIGH]   irreversible data loss  (destructive)
    line 20  ALTER TABLE accounts DROP COLUMN legacy_code
    why: destructive-statement - confidence: depends on data/scale
      `DROP COLUMN` is irreversible: accounts.legacy_code and every value in it are gone on commit,
            and any code still selecting that column starts failing immediately.
      safer:
      Use the contract half of expand/contract: stop writing and reading the column in one deploy,
            wait at least one release, then drop it in a separate migration so the drop can be
            reverted from a backup if needed.

  [HIGH]   unbounded UPDATE/DELETE  (lock)
    line 22  UPDATE accounts SET verified = true
    why: unbounded-dml - confidence: verified by syntax
      `UPDATE` has no `WHERE` clause, so it touches every row in the table. That is either a
            forgotten filter (a common way to lose production data) or an intentional bulk change
            that will hold row locks for a long time and bloat the table.
      safer:
      Wrap the statement in `BEGIN`/`COMMIT` so the row count can be checked before `COMMIT`, and
            run `EXPLAIN` on the exact statement first. For a bulk change, loop in batches on the
            primary key (for example 10,000 rows per batch) with a short sleep between batches so
            autovacuum and replicas can keep up.

  [HIGH]   unbounded UPDATE/DELETE  (lock)
    line 23  DELETE FROM sessions
    why: unbounded-dml - confidence: verified by syntax
      `DELETE` has no `WHERE` clause, so it touches every row in the table. That is either a
            forgotten filter (a common way to lose production data) or an intentional bulk change
            that will hold row locks for a long time and bloat the table.
      safer:
      Wrap the statement in `BEGIN`/`COMMIT` so the row count can be checked before `COMMIT`, and
            run `EXPLAIN` on the exact statement first. For a bulk change, loop in batches on the
            primary key (for example 10,000 rows per batch) with a short sleep between batches so
            autovacuum and replicas can keep up.

  [HIGH]   VACUUM FULL rewrites the table under ACCESS EXCLUSIVE  (lock)
    line 25  VACUUM FULL accounts
    why: vacuum-full - confidence: depends on data/scale
      `VACUUM FULL` rewrites the whole table into a new file under an ACCESS EXCLUSIVE lock, so
            every read and write on that table is blocked for the entire rewrite. It also needs free
            disk space for a second copy of the table.
      safer:
      Use plain `VACUUM (ANALYZE)` instead, which runs alongside normal traffic. `VACUUM FULL`
            belongs in a maintenance window with the table quiesced, or replace it with `pg_repack`,
            which rebuilds the table online.

  [MEDIUM] CREATE DATABASE cannot be transactional  (transaction)
    line 26  CREATE DATABASE reporting
    why: transaction-illegal-risk - confidence: verified by syntax
      `CREATE DATABASE reporting` cannot run inside a transaction block, so a migration file that
            wraps its statements in `BEGIN`/`COMMIT` will fail here, and it cannot be rolled back
            with the rest of the file.
      safer:
      Keep `CREATE DATABASE` in a separate non-transactional migration and make it idempotent (check
            `pg_database` first) so a retry does not fail on an existing database.

  [HIGH]   REINDEX blocks the table  (lock)
    line 27  REINDEX TABLE accounts
    why: reindex-locking - confidence: depends on data/scale
      `REINDEX accounts` rebuilds the index while holding ACCESS EXCLUSIVE, blocking reads and
            writes on the table for the whole rebuild.
      safer:
      On PostgreSQL 12+ prefer `REINDEX INDEX CONCURRENTLY <index>` in a non-transactional migration
            (it cannot run inside a transaction block and is unsupported for some index types).
            Otherwise schedule the rebuild in a maintenance window and set `lock_timeout` so it
            fails fast instead of queueing behind other queries.

  [HIGH]   irreversible data loss  (destructive)
    line 28  DROP TABLE accounts_archive
    why: destructive-statement - confidence: depends on data/scale
      `DROP TABLE accounts_archive` destroys the table and all of its rows; the data is not
            recoverable from the database afterwards.
      safer:
      Treat the drop as the contract step of expand/contract: ship the code that stops using the
            table first, keep the table read-only for at least one release, and take a verified
            logical dump (`pg_dump -t`) or rename it to `accounts_archive_deprecated_<date>` before
            dropping, so a rollback is still possible.

  lock risk by statement
    HIGH   line 5    add-column on accounts  NOT NULL without DEFAULT
    HIGH   line 6    add-column on accounts  DEFAULT present: metadata-only on PostgreSQL 11+
    HIGH   line 7    set-not-null on accounts  full table scan under ACCESS EXCLUSIVE
    HIGH   line 8    alter-column-type on accounts  could not read one side of the type change
    HIGH   line 9    alter-column-type on accounts  could not read one side of the type change
    HIGH   line 10   alter-column-type on accounts  could not read one side of the type change
    HIGH   line 12   create-index on accounts  blocks writes for the whole build
    HIGH   line 13   drop-index
    MEDIUM line 14   drop-index-concurrently  does not block reads or writes; cannot run in a transaction
    HIGH   line 16   add-constraint on accounts  UNIQUE constraint validated immediately
    HIGH   line 17   add-constraint on accounts  foreign key validated immediately
    HIGH   line 20   drop-column on accounts
    HIGH   line 22   dml-unbounded  no WHERE clause
    HIGH   line 23   dml-unbounded  no WHERE clause
    HIGH   line 25   vacuum-full on accounts  rewrites the table under ACCESS EXCLUSIVE
    HIGH   line 26   create-database on reporting  cannot run inside a transaction block
    HIGH   line 27   reindex on accounts  ACCESS EXCLUSIVE rebuild
    HIGH   line 28   drop-table on accounts_archive

1 file(s) checked, 20 finding(s): 15 high, 5 medium
```

Exit code `1`.

### A safe migration

`tests/fixtures/safe/` is the negative control used by the test suite: two
migrations that add a nullable column, add a `NOT VALID` rule, build a unique
index `CONCURRENTLY` in a file marked non-transactional, and then validate the
constraint in a second migration.

```console
$ python3 sql_migration_lint.py tests/fixtures/safe --no-color
```

```
tests/fixtures/safe/0001_add_email.sql
  4 statement(s), 27 line(s) - no findings

  lock risk by statement
    LOW    line 20   add-column on users  nullable column, metadata-only on PostgreSQL 11+
    LOW    line 20   add-constraint on users  NOT VALID: existing rows are not checked now
    MEDIUM line 26   create-index-concurrently  does not block writes, cannot run inside a transaction

tests/fixtures/safe/0002_validate_email.sql
  4 statement(s), 13 line(s) - no findings

  lock risk by statement
    MEDIUM line 10   validate-constraint on users  SHARE UPDATE EXCLUSIVE: writes continue, but the table is scanned

2 file(s) checked, 0 finding(s): no findings
No findings at or above --min-risk low. This is a static check: it says nothing about whether the statements are correct, only that none of the patterns this tool knows about matched.
```

Exit code `0`. Note that "no findings" still prints the lock risk of every
statement it classified: silence means "nothing matched a dangerous pattern",
not "nothing was read".

## The expand / contract pattern

Almost every safe rewrite in this tool is an instance of one idea: **never make
a change that requires rewriting or scanning the whole table while users are
writing to it**. Split the change into a step that only adds something (expand)
and a later step that removes something (contract), with the application
tolerating both shapes in between.

The fixtures in `tests/fixtures/expand_contract/` are a working example of
renaming a column's data into a new, uniquely constrained column:

1. **0004 — expand.** In one `ALTER TABLE` (one lock, one catalog change) add the
   new nullable column and attach the new rules as `NOT VALID`:
   `ADD COLUMN email_normalized text`, `ADD CONSTRAINT ... UNIQUE
   (email_normalized) NOT VALID`, `ADD CONSTRAINT ... CHECK (email_normalized IS
   NOT NULL) NOT VALID`. Nothing is scanned, nothing is rewritten, and the old
   application code keeps working because the new column is nullable.
2. **Backfill** in batches, outside the migration, with a trigger or dual-write
   keeping old and new columns consistent. Batches keep row locks short.
3. **0005 — build the index** with `CREATE INDEX CONCURRENTLY` in a migration
   explicitly marked `-- migrate: no-transaction`, because `CONCURRENTLY` cannot
   run inside a transaction block.
4. **0006 — validate.** `VALIDATE CONSTRAINT` for each rule. This takes only
   `SHARE UPDATE EXCLUSIVE`, so reads and writes continue while the table is
   scanned once. The index build happens *before* validation so the validation
   scan can use it.
5. **Later — contract.** Switch the application to the new column, deploy, wait
   at least one release, then `SET NOT NULL` (PostgreSQL 12+ can reuse the
   validated `CHECK` constraint instead of scanning again) and finally
   `DROP COLUMN` the old one in its own migration.

Every file in that directory lints clean as a set. Run one of them alone and the
tool tells you the next step is missing — for example, `0004` alone reports
`constraint-never-validated` for both `NOT VALID` constraints, because a `NOT
VALID` constraint that is never validated is not actually enforced for the rows
you already have.

## Parsing honesty: what the analyser skips

This is a text and token analyser. It is not a full SQL parser, and it says so.
The one thing it refuses to get wrong is reporting SQL that PostgreSQL would
never execute. The lexer understands and skips:

- `-- line comments`;
- `/* block comments */`, including PostgreSQL's nested block comments;
- `'string literals'` with the doubled `''` escape;
- `E'escape strings'`, `U&'...'`, `B'...'`, `X'...'`;
- `"quoted identifiers"`, so `SELECT "drop" FROM t` is not a `DROP`;
- `$tag$ dollar quoted strings $tag$`, which is how `CREATE FUNCTION` bodies are
  written — the single biggest source of false positives in regex-based SQL
  linters.

Statements are split on semicolons that are outside all of the above and outside
parentheses, so a `;` inside a string or a function body does not start a new
statement. A statement that is not terminated by a semicolon is reported as a
parse warning on stderr, because a truncated file is worth knowing about.

`tests/fixtures/comments/0009_comments_and_strings.sql` is a fixture full of
decoys — a `DROP TABLE`, a `CREATE DATABASE` and an `ALTER COLUMN ... TYPE`
inside comments, string literals and PL/pgSQL bodies — and it produces zero
findings. That behaviour is asserted in the test suite, not just described here.

What the analyser still cannot see: dynamic SQL built at runtime with `EXECUTE`
or `format()`, migrations generated by an ORM at run time, a `WHERE` clause whose
predicate matches every row anyway, and statements that are dangerous only in
combination with something outside the file. When the analyser cannot classify
something it says so (`type-unknown`) rather than guessing.

## Tests

```console
python3 -m unittest discover -s tests -v
```

The suite is standard library `unittest` — 176 tests, no pytest, no fixtures
library, no database. It includes:

- a **negative control**: `tests/fixtures/safe/` and
  `tests/fixtures/expand_contract/` must produce **zero** findings, so the tool
  cannot pass by reporting everything;
- one test per rule, plus tests that decoys inside comments, strings and
  dollar-quoted function bodies produce nothing;
- CLI tests for all three documented exit codes, JSON shape, `--min-risk`
  filtering, `--no-color` and `--check-order`, including one real subprocess run
  of the documented command line.

The fixtures are obviously synthetic: table names like `accounts`, string
literals like `'DROP TABLE accounts;'`, and no credentials or connection strings
anywhere.

## What this does not do

Stated plainly, because a linter that oversells itself is worse than none:

- **It is static analysis only.** It never connects to a database, never reads
  your schema, and never runs `EXPLAIN`. It reasons about the text of the
  migration.
- **It does not know your PostgreSQL version.** Lock behaviour, and what counts
  as a rewrite, depends on the version: `ADD COLUMN ... DEFAULT` stopped
  rewriting tables in PostgreSQL 11, `REINDEX CONCURRENTLY` arrived in 12,
  `SET NOT NULL` can reuse a validated `CHECK` constraint from 12 onwards, and
  `CREATE INDEX CONCURRENTLY` itself has changed details over the years. Where
  the answer depends on the version, the finding says so via its confidence
  rather than pretending to know.
- **It cannot tell you how long a lock will be held.** That depends on table
  size, row width, index count, disk speed, cache state, and what else is
  running. A `CREATE INDEX CONCURRENTLY` on a 50-row table and on a
  billion-row table are the same finding and a very different afternoon.
- **It cannot guarantee that a `CONCURRENTLY` build succeeds.** Concurrent index
  builds can fail and leave an `INVALID` index behind that still consumes writes
  on every insert. The tool reminds you to check `pg_index.indisvalid`; it
  cannot do it for you.
- **It does not know your data distribution.** A `CHECK` constraint added with
  `NOT VALID` will block new writes that violate it, and `VALIDATE CONSTRAINT`
  will fail if existing rows violate it. Whether they do is a question for your
  data.
- **It does not replace a review, a backup, or a staging run.** Treat the output
  as a checklist of things to think about, not as permission to ship.
- **It is PostgreSQL-focused.** Most of the rules are meaningless for MySQL,
  SQLite or SQL Server, where the locking model and DDL semantics are different.

The honest summary: this tool reliably catches *syntactic* patterns that are
known to be dangerous, and it is explicit about which claims are syntax,
which are version-dependent, and which depend on your data.

## Licence

MIT. See [LICENSE](LICENSE).

Copyright (c) 2026 duke5am

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

<!-- RELATED:START -->

## Related tools

- **[pg-perf-check](https://github.com/duke5am/pg-perf-check)** — PostgreSQL performance diagnostics: 24 read-only checks and 7 SQL files for bloat, missing indexes, slow queries, locks and autovacuum.
  *(if you were searching for "postgres performance tuning queries")*
- **[pg-restore-drill](https://github.com/duke5am/pg-restore-drill)** — Prove your PostgreSQL backup actually restores: a scripted point-in-time recovery drill with a measured RPO/RTO report and a negative control.
  *(if you were searching for "test postgres backup restore")*
- **[rls-policy-tester](https://github.com/duke5am/rls-policy-tester)** — Prove user A cannot read user B's rows in Postgres or Supabase with row level security, including negative controls that fail on a missing policy.
  *(if you were searching for "supabase rls test")*

All 28 tools in this set, grouped by what they check: **[dev-tools-index](https://duke5am.github.io/dev-tools-index/)**

If you arrived here searching for one of these, this is the tool: **postgres migration lock** · **alter table blocking migration** · **create index concurrently** · **safe database migration checker**

<!-- RELATED:END -->

→ **[SQL Migration Safety Pack](https://duke5am.gumroad.com/l/10-sql-migration-pack)** — $24 on Gumroad <!-- GUMROAD-LINK -->
