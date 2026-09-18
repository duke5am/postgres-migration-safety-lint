-- 0007_add_email.sql
-- A small migration that looks harmless and is not.

ALTER TABLE accounts ADD COLUMN email text NOT NULL;

SET lock_timeout = '5s';
CREATE INDEX idx_accounts_email ON accounts (email);

ALTER TABLE accounts ADD CONSTRAINT accounts_email_key UNIQUE (email);
