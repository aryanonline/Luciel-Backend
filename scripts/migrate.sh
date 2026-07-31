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

# SQL is executed via run_sql.py (psycopg), NOT psql, so the image needs no
# postgresql-client (which dragged in perl + CVE-2026-12087).
MASTER_DSN="host=$PGHOST port=${PGPORT:-5432} dbname=$PGDATABASE user=$MASTER_USER password=$MASTER_PASSWORD"

echo "[migrate] 1/3 bootstrap roles (master)"
python3 scripts/run_sql.py --dsn "$MASTER_DSN" --file scripts/bootstrap_roles.sql --app-password "$APP_PASSWORD"

echo "[migrate] 2/3 alembic upgrade head (as luciel_app)"
export LUCIEL_DATABASE_URL_SYNC="postgresql+psycopg://luciel_app:${APP_PASSWORD}@${PGHOST}:${PGPORT:-5432}/${PGDATABASE}"
export LUCIEL_DATABASE_URL="postgresql+asyncpg://luciel_app:${APP_PASSWORD}@${PGHOST}:${PGPORT:-5432}/${PGDATABASE}"
alembic upgrade head

echo "[migrate] 3/4 lock audit log (master, post-migrate)"
python3 scripts/run_sql.py --dsn "$MASTER_DSN" --file scripts/lock_audit_log.sql

# The lock is the one part of the schema alembic cannot install (it must be owned by
# luciel_audit_owner, and alembic runs as luciel_app). Without this gate a deploy that
# skipped or half-applied step 3 still reports success, and the omission surfaces days
# later as `undefined_function` in the nightly audit_retention sweep.
echo "[migrate] 4/4 verify the audit-log lock"
python3 scripts/verify_audit_lock.py --dsn "$MASTER_DSN"

echo "[migrate] done — schema, RLS, grants, and audit-log lock applied and verified."
