-- Run by scripts/migrate.sh on EVERY exit (trap), as the master user.
-- bootstrap_roles.sql grants luciel_app membership in the BYPASSRLS luciel_auth role
-- for the migration's ownership transfers only; lock_audit_log.sql revokes it on the
-- success path. This file is the revoke for the failure path, so a refused migration
-- never leaves the live app role able to SET ROLE past RLS (audit F085).
REVOKE luciel_auth FROM luciel_app;
