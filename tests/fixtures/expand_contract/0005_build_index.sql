-- 0005_build_index.sql
-- Non-transactional half of expand/contract: the CONCURRENTLY index build.
-- Must produce ZERO findings.
--
-- There is no BEGIN/COMMIT here on purpose. CREATE INDEX CONCURRENTLY cannot run
-- inside a transaction block, and the file says so explicitly for whoever runs
-- it, which is what the no-transaction-control rule asks for.

-- migrate: no-transaction

SET lock_timeout = '5s';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_customers_email_normalized
    ON customers (email_normalized);
