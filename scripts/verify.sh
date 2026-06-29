#!/usr/bin/env bash
# §10 live verification runner (one-off ECS task INSIDE the VPC; RDS is private).
# Connects as luciel_app (non-superuser, RLS-bound) — the SAME role the app uses —
# and runs scripts/verify_probe.sql. Greps the NOTICEs: any ROW*-FAIL => exit 1.
set -euo pipefail

eval "$(python3 - <<'PY'
import json, os, hashlib
s = json.loads(os.environ["LUCIEL_DB_SECRET"])
host = s["host"]; port = s.get("port", 5432)
pw = s["password"]
db = s.get("dbname") or "luciel"
app_pw = hashlib.sha256(("luciel_app:" + pw).encode()).hexdigest()[:32]
print(f'export PGHOST={host!r}')
print(f'export PGPORT={port}')
print(f'export PGDATABASE={db!r}')
print(f'export APP_PASSWORD={app_pw!r}')
PY
)"

# Connect AS luciel_app (not master) so RLS + the audit guard apply exactly as in prod.
APP_DSN="host=$PGHOST port=${PGPORT:-5432} dbname=$PGDATABASE user=luciel_app password=$APP_PASSWORD"

echo "[verify] running §10 live probe as luciel_app..."
OUT=$(python3 scripts/run_sql.py --dsn "$APP_DSN" --file scripts/verify_probe.sql 2>&1)
echo "$OUT"

if echo "$OUT" | grep -q "FAIL"; then
  echo "::error::§10 live probe found FAIL(s)"; exit 1
fi
echo "[verify] §10 live probe: all checks PASS."
