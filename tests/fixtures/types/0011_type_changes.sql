-- 0011_type_changes.sql
-- Fixture: the type-change classifier. The narrowing change near the end is the
-- only one here that rewrites the table.

CREATE TABLE metrics (
    id        bigserial PRIMARY KEY,
    note      varchar(50),
    label     text,
    amount    numeric(10,2)
);

ALTER TABLE metrics ALTER COLUMN note TYPE varchar(120);
ALTER TABLE metrics ALTER COLUMN label TYPE varchar(80);
ALTER TABLE metrics ALTER COLUMN amount TYPE numeric(12,2);
