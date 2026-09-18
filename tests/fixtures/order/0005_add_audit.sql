-- 0005_add_audit.sql
CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
