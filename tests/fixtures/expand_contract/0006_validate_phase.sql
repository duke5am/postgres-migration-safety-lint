-- 0006_validate_phase.sql
-- The VALIDATE half of expand/contract, run after the batched backfill.
-- Must produce ZERO findings.
--
-- VALIDATE CONSTRAINT takes SHARE UPDATE EXCLUSIVE, so reads and writes carry on
-- while the table is scanned, and both validations fit in one ALTER TABLE.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE customers
    VALIDATE CONSTRAINT customers_email_normalized_unique,
    VALIDATE CONSTRAINT customers_email_normalized_not_null;

COMMIT;

-- `SET NOT NULL` is deliberately left to a later migration: the validated CHECK
-- constraint is what lets PostgreSQL 12+ prove the column is not null without
-- scanning the table a second time.
