-- 0001_add_email.sql
-- NEGATIVE CONTROL for the test suite: this file must produce ZERO findings.
--
-- Every step here is safe on a large live PostgreSQL table:
--   * ONE ALTER TABLE taking one lock, with a metadata-only action list: a
--     nullable column (no rewrite) and a NOT VALID rule that does not scan or
--     lock existing rows;
--   * a unique index built CONCURRENTLY in a file that is explicitly marked as
--     non-transactional, because CONCURRENTLY cannot run inside a transaction;
--   * a lock_timeout budget set before the locking statement, so it fails fast
--     instead of queueing behind another transaction.
--
-- The matching VALIDATE CONSTRAINT lives in a later migration (see
-- tests/fixtures/expand_contract/0006_validate_phase.sql for that half).

-- migrate: no-transaction

SET lock_timeout = '5s';

ALTER TABLE users
    ADD COLUMN email text,
    ADD CONSTRAINT users_email_not_null CHECK (email IS NOT NULL) NOT VALID;

RESET lock_timeout;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_users_email ON users (email);
