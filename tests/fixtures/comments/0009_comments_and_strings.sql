-- 0009_comments_and_strings.sql
/*
   This block comment contains SQL that must never be reported:
   DROP TABLE accounts;
   ALTER TABLE accounts ALTER COLUMN balance TYPE text;
   CREATE INDEX idx_fake ON accounts (email);
*/

-- A line comment with more decoys:
--   DROP COLUMN legacy_code; CREATE DATABASE reporting; UPDATE accounts SET x = 1;

INSERT INTO audit_log (message)
VALUES ('DROP TABLE accounts; ALTER TABLE accounts ALTER COLUMN note TYPE text;');

UPDATE notes SET body = 'DELETE FROM sessions;' WHERE id = 7;

CREATE TABLE audit_notes (
    id      bigserial PRIMARY KEY,
    note    text DEFAULT 'VACUUM FULL accounts;',
    enabled boolean NOT NULL DEFAULT true
);

CREATE OR REPLACE FUNCTION notify_audit() RETURNS trigger AS $body$
DECLARE
    note text := 'REINDEX TABLE orders;';
BEGIN
    -- DROP TABLE orders;
    INSERT INTO audit_log (message)
    VALUES ('ALTER TABLE orders ADD COLUMN x integer NOT NULL;');
    RETURN NEW;
END;
$body$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mark_empty() RETURNS integer AS $fn$
BEGIN
    UPDATE orders SET seen = true;
    RETURN 1;
END;
$fn$ LANGUAGE plpgsql;
