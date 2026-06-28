#!/usr/bin/env bash
# Phase B migration runner (DEVOPS §5.3/§5.9). Runs as a one-off ECS task INSIDE
# the VPC (RDS is private). Executes the exact ordered sequence the README/CI use:
#   1. bootstrap_roles.sql   (SUPERUSER / master) - creates luciel_app, audit owner
#   2. alembic upgrade head  (as luciel_app)       - schema + RLS + grants
#   3. lock_audit_log.sql    (SUPERUSER / master)  - immutable audit log (§5.3)
#
# Env required (injected from Secrets Manager by the task definition):
#   PGHOST, PGPORT, PGDATABASE           - RDS endpoint/db
#   MASTER_USER, MASTER_PASSWORD         - RDS master (superuser-equivalent on RDS)
#   APP_PASSWORD                         - password to set for luciel_app
set -euo pipefail

export PGPASSWORD="$MASTER_PASSWORD"
PSQL="psql -h $PGHOST -p ${PGPORT:-5432} -U $MASTER_USER -d $PGDATABASE -v ON_ERROR_STOP=1"

echo "[migrate] 1/3 bootstrap roles (master)"
# Pass the desired app password into the bootstrap script.
$PSQL -v app_password="$APP_PASSWORD" -f scripts/bootstrap_roles.sql

echo "[migrate] 2/3 alembic upgrade head (as luciel_app)"
export LUCIEL_DATABASE_URL_SYNC="postgresql+psycopg://luciel_app:${APP_PASSWORD}@${PGHOST}:${PGPORT:-5432}/${PGDATABASE}"
export LUCIEL_DATABASE_URL="postgresql+asyncpg://luciel_app:${APP_PASSWORD}@${PGHOST}:${PGPORT:-5432}/${PGDATABASE}"
alembic upgrade head

echo "[migrate] 3/3 lock audit log (master, post-migrate)"
$PSQL -f scripts/lock_audit_log.sql

echo "[migrate] done — schema, RLS, grants, and audit-log lock applied."
