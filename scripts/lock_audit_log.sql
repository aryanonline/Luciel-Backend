-- Audit-log immutability lock (Architecture §5.3 + §5.11, Decision §25,
-- Commitment §12 + §23).
--
-- WHY THIS EXISTS (audit finding Axis 9 / B1 / B4): the append-only guarantee cannot
-- rest on `REVOKE UPDATE, DELETE ... FROM luciel_app` alone. Migrations run AS
-- luciel_app, so luciel_app OWNS the audit tables, and a table owner can always
-- `GRANT UPDATE ... TO luciel_app` to itself and `DROP` its own triggers. A REVOKE is
-- therefore a speed-bump, not a lock. TRUNCATE adds two holes of its own: it is a
-- SEPARATE privilege (a `REVOKE UPDATE, DELETE` does not touch it) that is inherent to
-- the table owner, and a row-level `BEFORE UPDATE OR DELETE` trigger never fires on
-- it — blocking TRUNCATE needs a STATEMENT-level `BEFORE TRUNCATE` trigger.
--
-- THE LOCK, applied to BOTH append-only artifacts — admin_audit_log (§5.3) and
-- internal_access_log (§5.11 operator wall / Commitment C23), which is held to the
-- same standard rather than the row-only guard migration 0012 gave it:
--   1. ownership moves to luciel_audit_owner — a NOLOGIN role luciel_app is NOT a
--      member of — which is what actually removes TRUNCATE and DDL from luciel_app;
--   2. luciel_app is left with INSERT + SELECT only, so it can append and read but
--      cannot mutate rows, re-grant itself UPDATE/DELETE/TRUNCATE (not the owner),
--      nor drop the guards (not the owner);
--   3. defense in depth, with every guard function owned by luciel_audit_owner: a
--      row-level BEFORE UPDATE OR DELETE guard AND a statement-level BEFORE TRUNCATE
--      guard on each table, so a future grant slip-up is still rejected.
--
-- The guards deliberately do NOT exempt superusers: the RDS master is the documented
-- break-glass path and must leave an obvious trace, so mutating an audit table takes
-- an explicit `ALTER TABLE ... DISABLE TRIGGER <name>` first.
--
-- RUN THIS AS A SUPERUSER, ONCE, AFTER `alembic upgrade head`:
--     psql -U postgres -d luciel_dev -f scripts/lock_audit_log.sql
-- It is idempotent and safe to re-run (e.g. after a migration that recreates a table).
-- It CANNOT be folded into the Docker init (docker-entrypoint-initdb.d runs before the
-- tables exist) nor into a migration (alembic runs as luciel_app, which is not a member
-- of luciel_audit_owner and so cannot transfer ownership) — it is a post-migrate
-- superuser step in every environment. In production it is step 3 of
-- scripts/migrate.sh; locally, run the psql line above after your first migration.
-- Migration 0019 installs the same guards as far as the app role can, so an
-- alembic-only database is guarded but not owner-locked.

-- 1) Shared append-only guard function, owned by the audit owner role (luciel_app
--    cannot CREATE OR REPLACE it — it is not the owner). Serves the row-level
--    UPDATE/DELETE triggers and the statement-level TRUNCATE triggers on both tables.
CREATE OR REPLACE FUNCTION _audit_append_only_guard() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  -- The ONE authorized deletion (ruling 6 / P1-15): the published 1-year audit
  -- retention. Without an exemption that commitment cannot be kept at all, and
  -- "append-only forever" is not what §B5 promises.
  --
  -- Exempted on IDENTITY, not on a flag. current_user is luciel_audit_owner only
  -- inside a SECURITY DEFINER function that role owns, and luciel_app is deliberately
  -- not a member of it (scripts/bootstrap_roles.sql). A GUC or a session setting would
  -- be settable by the app role and so would be no lock at all.
  --
  -- The age test is repeated HERE rather than trusted from the procedure, so what
  -- makes "only rows older than 365 days" true is the trigger — the same object that
  -- makes append-only true — and not a function body that could later be replaced.
  IF TG_OP = 'DELETE' AND current_user = 'luciel_audit_owner' THEN
    IF OLD.created_at < now() - interval '365 days' THEN
      RETURN OLD;
    END IF;
  END IF;

  RAISE EXCEPTION
    '% is append-only (Architecture §5.3/§5.11): % is not permitted',
    TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$;
ALTER FUNCTION _audit_append_only_guard() OWNER TO luciel_audit_owner;

-- 1b) The retention procedure the exemption above exists for (ruling 6 / P1-15).
--
-- Narrow on every axis: one table, one tenant per call, a floor of 365 days that a
-- caller cannot argue down, and no way to name the rows. It is SECURITY DEFINER so
-- that calling it is the ONLY way luciel_app can reach a DELETE on admin_audit_log —
-- the app role holds EXECUTE on this function and nothing else.
--
-- RLS is FORCE'd on admin_audit_log and the tenant_isolation policy is TO PUBLIC, so
-- it binds the audit owner too: the DELETE is scoped by app.admin_id as well as by
-- p_admin_id. The explicit predicate is the belt to RLS's braces, as everywhere else.
CREATE OR REPLACE FUNCTION audit_retention_prune(p_admin_id uuid, p_retention_days int DEFAULT 365)
  RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  -- A floor, not a default: passing 30 prunes at 365 anyway. The published commitment
  -- is a minimum retention, so the only direction a caller may move it is longer.
  v_days int := GREATEST(COALESCE(p_retention_days, 365), 365);
  v_deleted bigint;
BEGIN
  DELETE FROM admin_audit_log
   WHERE admin_id = p_admin_id
     AND created_at < now() - make_interval(days => v_days);
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  RETURN v_deleted;
END;
$$;
ALTER FUNCTION audit_retention_prune(uuid, int) OWNER TO luciel_audit_owner;
REVOKE ALL ON FUNCTION audit_retention_prune(uuid, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit_retention_prune(uuid, int) TO luciel_app;

-- 2) admin_audit_log (§5.3): row guard + TRUNCATE guard, then ownership + grants.
DROP TRIGGER IF EXISTS trg_audit_append_only ON admin_audit_log;
CREATE TRIGGER trg_audit_append_only
  BEFORE UPDATE OR DELETE ON admin_audit_log
  FOR EACH ROW EXECUTE FUNCTION _audit_append_only_guard();

DROP TRIGGER IF EXISTS trg_audit_no_truncate ON admin_audit_log;
CREATE TRIGGER trg_audit_no_truncate
  BEFORE TRUNCATE ON admin_audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION _audit_append_only_guard();

-- (RLS ENABLE+FORCE set by the migration is preserved across an owner change.)
ALTER TABLE admin_audit_log OWNER TO luciel_audit_owner;
REVOKE ALL ON admin_audit_log FROM luciel_app;
GRANT SELECT, INSERT ON admin_audit_log TO luciel_app;

-- Granted AFTER the ownership transfer so it cannot be re-attributed by it. The owner
-- needs DELETE for audit_retention_prune to do anything, and that is safe only because
-- of the row guard: with the trigger in place, the widest thing this grant can express
-- is "delete rows older than a year" — which is the retention commitment itself.
GRANT DELETE ON admin_audit_log TO luciel_audit_owner;

-- admin_audit_log.seq defaults to nextval('admin_audit_log_seq'), so the append path
-- still needs USAGE on that sequence after the grant reset above — the sequence is a
-- separate object and `REVOKE ALL ON` the table does not touch it.
GRANT USAGE ON SEQUENCE admin_audit_log_seq TO luciel_app;

-- 3) internal_access_log (§5.11 / C23): the same lock. Migration 0012 created this
--    table under the app-role DSN with a row-only guard, leaving luciel_app as OWNER —
--    free to TRUNCATE it, re-grant itself UPDATE/DELETE, or drop the guard. That is
--    the B4 gap; the ownership transfer below is what closes it.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'internal_access_log' AND c.relkind = 'r'
  ) THEN
    RAISE NOTICE 'lock_audit_log: internal_access_log absent (pre-0012 database), skipping';
    RETURN;
  END IF;

  DROP TRIGGER IF EXISTS trg_internal_access_log_no_update ON internal_access_log;
  CREATE TRIGGER trg_internal_access_log_no_update
    BEFORE UPDATE OR DELETE ON internal_access_log
    FOR EACH ROW EXECUTE FUNCTION _audit_append_only_guard();

  DROP TRIGGER IF EXISTS trg_internal_access_log_no_truncate ON internal_access_log;
  CREATE TRIGGER trg_internal_access_log_no_truncate
    BEFORE TRUNCATE ON internal_access_log
    FOR EACH STATEMENT EXECUTE FUNCTION _audit_append_only_guard();

  ALTER TABLE internal_access_log OWNER TO luciel_audit_owner;
  REVOKE ALL ON internal_access_log FROM luciel_app;
  GRANT SELECT, INSERT ON internal_access_log TO luciel_app;

  -- 0012's own guard function is left in place for its downgrade path, but re-owned:
  -- while luciel_app owned it, the app role could CREATE OR REPLACE the body into a
  -- no-op and quietly defeat the guard it is named after.
  IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'guard_internal_access_log_immutable') THEN
    ALTER FUNCTION guard_internal_access_log_immutable() OWNER TO luciel_audit_owner;
  END IF;
END $$;

-- 4) The owner role must be able to evaluate the RLS policies on the table it owns
--    when luciel_app inserts (RLS is FORCE'd). The existing tenant policy uses
--    current_setting('app.admin_id'); ownership does not change that. No extra grant
--    to luciel_audit_owner is needed beyond owning the relation.
