-- Runs as the postgres superuser on first container init.
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
