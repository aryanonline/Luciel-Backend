-- Per-database bootstrap (run as superuser against the target DB, after roles
-- exist and the DB is created OWNER luciel_app). Installs extensions and grants
-- the narrow auth role the schema privileges it needs to own its function.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
ALTER SCHEMA public OWNER TO luciel_app;
GRANT ALL ON SCHEMA public TO luciel_app;
GRANT USAGE, CREATE ON SCHEMA public TO luciel_auth;
-- Audit-log owner role needs schema usage to own admin_audit_log + its guard
-- (the ownership transfer itself is scripts/lock_audit_log.sql, run post-migrate).
GRANT USAGE ON SCHEMA public TO luciel_audit_owner;
