-- 0004_expand_phase.sql
-- The EXPAND half of expand/contract. Must produce ZERO findings.
--
-- One ALTER TABLE takes one lock for all of its actions, so the new nullable
-- columns and the NOT VALID rules are added together: metadata-only changes, no
-- scan of existing rows, and the old application code keeps working because
-- every new column is nullable.
--
-- The index build lives in 0005 because CREATE INDEX CONCURRENTLY cannot run
-- inside a transaction block, and this file is transactional.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE customers
    ADD COLUMN email_normalized text,
    ADD COLUMN email_verified_at timestamptz,
    ADD CONSTRAINT customers_email_normalized_unique
        UNIQUE (email_normalized) NOT VALID,
    ADD CONSTRAINT customers_email_normalized_not_null
        CHECK (email_normalized IS NOT NULL) NOT VALID;

CREATE TABLE customer_email_events (
    id          bigserial PRIMARY KEY,
    customer_id bigint NOT NULL,
    kind        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMIT;

-- The backfill of email_normalized runs in batches outside the migration, so no
-- single statement holds row locks for long.
