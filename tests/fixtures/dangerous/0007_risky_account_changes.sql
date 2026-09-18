-- 0007_risky_account_changes.sql
-- Fixture: every pattern this tool is meant to catch, on one small schema.
-- Not meant to be run anywhere; it exists to be linted.

ALTER TABLE accounts ADD COLUMN email text NOT NULL;
ALTER TABLE accounts ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE accounts ALTER COLUMN nickname SET NOT NULL;
ALTER TABLE accounts ALTER COLUMN balance TYPE varchar(40);
ALTER TABLE accounts ALTER COLUMN note TYPE varchar(400);
ALTER TABLE accounts ALTER COLUMN note TYPE text;

CREATE INDEX idx_accounts_email ON accounts (email);
DROP INDEX idx_accounts_legacy;
DROP INDEX CONCURRENTLY idx_accounts_old_email;

ALTER TABLE accounts ADD CONSTRAINT accounts_email_key UNIQUE (email);
ALTER TABLE accounts
    ADD CONSTRAINT accounts_owner_fk FOREIGN KEY (owner_id) REFERENCES users (id);

ALTER TABLE accounts DROP COLUMN legacy_code;

UPDATE accounts SET verified = true;
DELETE FROM sessions;

VACUUM FULL accounts;
CREATE DATABASE reporting;
REINDEX TABLE accounts;
DROP TABLE accounts_archive;
