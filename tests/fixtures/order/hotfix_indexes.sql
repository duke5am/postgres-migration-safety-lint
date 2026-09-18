-- hotfix_indexes.sql
-- Deliberately numbered-free file inside a numbered directory: its position in
-- the sequence is undefined, which is what the unnumbered rule reports.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sessions_user_id ON sessions (user_id);
