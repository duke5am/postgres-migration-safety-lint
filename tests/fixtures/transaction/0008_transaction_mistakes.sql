-- 0008_transaction_mistakes.sql
-- Fixture: CONCURRENTLY and non-transactional statements inside a transaction.
-- Every one of these is rejected by PostgreSQL, not merely slow.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX CONCURRENTLY idx_orders_customer_id ON orders (customer_id);

DROP INDEX CONCURRENTLY idx_orders_legacy;

VACUUM ANALYZE orders;

REINDEX TABLE orders;

COMMIT;
