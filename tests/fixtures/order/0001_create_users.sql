-- 0001_create_users.sql
CREATE TABLE users (
    id    bigserial PRIMARY KEY,
    email text NOT NULL
);
