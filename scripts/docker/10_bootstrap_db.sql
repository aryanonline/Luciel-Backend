-- Runs as postgres against POSTGRES_DB (luciel_dev) on first container init.
ALTER DATABASE luciel_dev OWNER TO luciel_app;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
ALTER SCHEMA public OWNER TO luciel_app;
GRANT ALL ON SCHEMA public TO luciel_app;
GRANT USAGE, CREATE ON SCHEMA public TO luciel_auth;
GRANT USAGE ON SCHEMA public TO luciel_audit_owner;
