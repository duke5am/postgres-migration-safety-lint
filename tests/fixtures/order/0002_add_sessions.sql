-- 0002_add_sessions.sql
-- Duplicate migration number on purpose: two files claim slot 0002, so the apply
-- order between them is undefined. This file must trigger the duplicate rule.

CREATE TABLE sessions (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
