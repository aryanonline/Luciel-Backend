-- Infra bootstrap (run once per environment as a superuser, BEFORE migrations).
-- Provisions the application roles the three-layer isolation model depends on.
--
-- WHY two roles:
--   luciel_app  — the application connects as THIS role. It is NON-superuser and
--                 NON-BYPASSRLS, which is exactly what makes Postgres RLS apply
--                 to it (Architecture §3.7.2b). If the app connected as a
--                 superuser/BYPASSRLS role, RLS would be silently inert.
--   luciel_auth — a NOLOGIN, BYPASSRLS role that owns ONLY the narrow
--                 auth_lookup_account() SECURITY DEFINER function (created in the
--                 first migration). It exists so the single pre-subject email
--                 lookup (signup/login/reset) can resolve an email to its account
--                 before app.admin_id is known. It logs in nowhere and owns
--                 nothing else.
--   luciel_audit_owner — a NOLOGIN, NON-BYPASSRLS role that OWNS admin_audit_log
--                 and its append-only guard trigger (Architecture §5.3). It is
--                 deliberately NOT granted to luciel_app: the audit log's
--                 immutability cannot rest on a REVOKE alone, because a table's
--                 OWNER can re-GRANT UPDATE/DELETE to itself and DROP its own
--                 triggers. By owning admin_audit_log with a role the application
--                 role is not a member of, luciel_app can INSERT/SELECT but can
--                 neither mutate rows, restore the grant, nor remove the guard.
--                 The ownership transfer + guard install is scripts/lock_audit_log.sql,
--                 run as a superuser AFTER `alembic upgrade head`.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'luciel_app') THEN
    CREATE ROLE luciel_app LOGIN PASSWORD 'luciel_dev_pw'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'luciel_auth') THEN
    CREATE ROLE luciel_auth NOLOGIN NOSUPERUSER BYPASSRLS;
  END IF;
  -- Owner of the append-only audit log. NOLOGIN, NON-BYPASSRLS, and intentionally
  -- NOT granted to luciel_app (see header). This is what makes §5.3 immutability
  -- a true lock rather than a revocable grant.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'luciel_audit_owner') THEN
    CREATE ROLE luciel_audit_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

GRANT luciel_auth TO luciel_app;
-- NB: luciel_audit_owner is NOT granted to luciel_app — that non-membership is
-- the security property. Do not add `GRANT luciel_audit_owner TO luciel_app`.

-- Cloud deploys (Phase B) inject a real generated password via -v app_password=...
-- so the well-known dev password 'luciel_dev_pw' is NEVER used outside local dev.
-- Local/CI runs that don't pass the var keep the dev default (psql substitutes the
-- fallback). This keeps the local docker loop unchanged while making cloud secure.
\if :{?app_password}
  ALTER ROLE luciel_app PASSWORD :'app_password';
\endif

-- Postgres 15+ revokes CREATE on the public schema from non-owner roles by default,
-- so on RDS the app role cannot create tables (alembic fails: 'permission denied for
-- schema public'). Grant the app role create+usage on public and make future objects
-- it doesn't own still usable. Run as the database owner/master (migrate.sh connects
-- as master to the target DB before alembic runs as luciel_app).
GRANT CREATE, USAGE ON SCHEMA public TO luciel_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO luciel_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO luciel_app;
-- Migrations reassign object ownership to luciel_auth (e.g. ALTER FUNCTION ... OWNER
-- TO luciel_auth). A role can only assign ownership to a role it belongs to AND on a
-- schema it owns. Make luciel_app own the public schema (this is a dedicated
-- single-platform DB) so it can create objects and reassign their ownership. Run as
-- master/owner.
ALTER SCHEMA public OWNER TO luciel_app;
-- The OWNER-transfer targets (luciel_auth, luciel_audit_owner) must ALSO hold
-- CREATE/USAGE on public, or 'ALTER ... OWNER TO <role>' fails with 'permission
-- denied for schema public' (the new owner role needs schema rights to hold the
-- object). Grant both target roles schema privileges.
GRANT CREATE, USAGE ON SCHEMA public TO luciel_auth;
GRANT CREATE, USAGE ON SCHEMA public TO luciel_audit_owner;

-- pgvector: migration 0004 adds a vector column, which needs the extension. On RDS
-- only the master/rds_superuser can CREATE EXTENSION, so do it here (bootstrap runs
-- as master) rather than in the app-role-run alembic step. Idempotent.
CREATE EXTENSION IF NOT EXISTS vector;
