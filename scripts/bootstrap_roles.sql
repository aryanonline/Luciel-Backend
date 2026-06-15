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

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'luciel_app') THEN
    CREATE ROLE luciel_app LOGIN PASSWORD 'luciel_dev_pw'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'luciel_auth') THEN
    CREATE ROLE luciel_auth NOLOGIN NOSUPERUSER BYPASSRLS;
  END IF;
END $$;

GRANT luciel_auth TO luciel_app;
