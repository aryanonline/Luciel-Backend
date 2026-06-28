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

# The task injects the RDS master secret as LUCIEL_DB_SECRET (JSON blob with
# host/port/username/password/dbname). Parse it into PG* vars. The app-role
# password is derived deterministically from the master secret (a stable secret
# not stored elsewhere) so bootstrap and the app agree without a separate secret.
eval "$(python3 - <<'PY'
import json, os, hashlib
s = json.loads(os.environ["LUCIEL_DB_SECRET"])
host = s["host"]; port = s.get("port", 5432)
user = s["username"]; pw = s["password"]
db = s.get("dbname") or "luciel"
# Deterministic app password derived from the master password (not logged).
app_pw = hashlib.sha256(("luciel_app:" + pw).encode()).hexdigest()[:32]
print(f'export PGHOST={host!r}')
print(f'export PGPORT={port}')
print(f'export MASTER_USER={user!r}')
print(f'export MASTER_PASSWORD={pw!r}')
print(f'export PGDATABASE={db!r}')
print(f'export APP_PASSWORD={app_pw!r}')
PY
)"

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
