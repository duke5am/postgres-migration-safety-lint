-- 0010_edge_cases.sql
-- Fixture: statements that stress the lexer rather than the rules.
-- Note the nested block comment, the escape string holding a doubled quote and a
-- decoy statement, and the numeric widening that must be reported as safe.

SET lock_timeout = 0;

/* A block comment /* with a nested block comment */ inside it. */
DROP DATABASE legacy_reporting;

-- An escape string holding a doubled quote and a decoy statement:
INSERT INTO audit_log (message) VALUES (E'it''s fine; DROP TABLE users;');

ALTER TABLE accounts ALTER COLUMN balance TYPE bigint;

UPDATE accounts SET balance = 0 WHERE id IN (SELECT id FROM accounts WHERE balance < 0);

UPDATE accounts SET balance = 1
WHERE id = 42

