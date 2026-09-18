-- 0002_add_accounts.sql
CREATE TABLE accounts (
    id       bigserial PRIMARY KEY,
    owner_id bigint NOT NULL,
    balance  numeric(12,2) NOT NULL DEFAULT 0
);
