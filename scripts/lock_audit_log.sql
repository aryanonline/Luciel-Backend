-- Audit-log immutability lock (Architecture §5.3, Decision §25, Commitment §12).
--
-- WHY THIS EXISTS (audit finding Axis 9 / B1): the append-only guarantee cannot
-- rest on `REVOKE UPDATE, DELETE ... FROM luciel_app` alone. Migrations run AS
-- luciel_app, so luciel_app OWNS admin_audit_log, and a table owner can always
-- `GRANT UPDATE ON admin_audit_log TO luciel_app` to itself and `DROP` its own
-- triggers. A REVOKE is therefore a speed-bump, not a lock.
--
-- THE LOCK: hand ownership of admin_audit_log (and its append-only guard) to
-- luciel_audit_owner — a NOLOGIN role that luciel_app is NOT a member of — and
-- grant luciel_app only INSERT + SELECT. Now luciel_app can append and read but
-- can neither mutate rows, re-grant itself UPDATE/DELETE (not the owner), nor drop
-- the guard trigger (not the owner). Defense in depth: a BEFORE UPDATE OR DELETE
-- guard trigger owned by luciel_audit_owner rejects any mutation even if a future
-- grant slip-up occurs.
--
-- RUN THIS AS A SUPERUSER, ONCE, AFTER `alembic upgrade head`:
--     psql -d luciel_dev -f scripts/lock_audit_log.sql
-- It is idempotent and safe to re-run (e.g. after a migration that recreates the
-- table). The Docker init runs it automatically (scripts/docker/20_lock_audit_log.sql).
-- In production this is part of the post-migrate deploy step (ops runbook).

-- 1) Append-only guard function, owned by the audit owner role (luciel_app cannot
--    CREATE OR REPLACE it — it is not the owner).
CREATE OR REPLACE FUNCTION _audit_append_only_guard() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION
    'admin_audit_log is append-only (Architecture §5.3): % is not permitted', TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$;
ALTER FUNCTION _audit_append_only_guard() OWNER TO luciel_audit_owner;

-- 2) Install the guard trigger (drop+recreate for idempotency; done as superuser
--    so it succeeds regardless of current ownership).
DROP TRIGGER IF EXISTS trg_audit_append_only ON admin_audit_log;
CREATE TRIGGER trg_audit_append_only
  BEFORE UPDATE OR DELETE ON admin_audit_log
  FOR EACH ROW EXECUTE FUNCTION _audit_append_only_guard();

-- 3) Transfer table ownership to the dedicated owner role and lock grants down to
--    append + read for the application role. (RLS ENABLE+FORCE set by the migration
--    is preserved across an owner change.)
ALTER TABLE admin_audit_log OWNER TO luciel_audit_owner;
REVOKE ALL ON admin_audit_log FROM luciel_app;
GRANT SELECT, INSERT ON admin_audit_log TO luciel_app;

-- 4) The owner role must be able to evaluate the RLS policies on the table it owns
--    when luciel_app inserts (RLS is FORCE'd). The existing tenant policy uses
--    current_setting('app.admin_id'); ownership does not change that. No extra grant
--    to luciel_audit_owner is needed beyond owning the relation.
