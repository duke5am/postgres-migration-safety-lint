-- 0002_validate_email.sql
-- Companion to 0001_add_email.sql: the NOT VALID constraint added there is
-- validated here, which is the two-step pattern this tool recommends. Running
-- the whole tests/fixtures/safe directory must therefore produce ZERO findings.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE users VALIDATE CONSTRAINT users_email_not_null;

COMMIT;
