#!/usr/bin/env bash
# App container entrypoint. Constructs LUCIEL_DATABASE_URL(_SYNC) for the
# NON-superuser luciel_app role from the injected RDS secret, then execs the app.
# The app connects as luciel_app (NOT master) so RLS tenant isolation binds (LD 18).
# The app-role password is derived from the master password the same way migrate.sh
# derives it, so they always agree without a separate stored secret.
set -euo pipefail

eval "$(python3 - <<'PY'
import json, os, hashlib
s = json.loads(os.environ["LUCIEL_DB_SECRET"])
host = s["host"]; port = s.get("port", 5432)
pw = s["password"]; db = s.get("dbname") or "luciel"
app_pw = hashlib.sha256(("luciel_app:" + pw).encode()).hexdigest()[:32]
base = f"luciel_app:{app_pw}@{host}:{port}/{db}"
print(f'export LUCIEL_DATABASE_URL="postgresql+asyncpg://{base}"')
print(f'export LUCIEL_DATABASE_URL_SYNC="postgresql+psycopg://{base}"')
PY
)"

# --proxy-headers: the ALB terminates TLS, so without it request.url reconstructs
# as http:// and Twilio's X-Twilio-Signature (signed over the https:// webhook URL)
# never matches. Trusting XFF from any peer is safe here because tasks are only
# reachable from the ALB security group. Keep in sync with the Dockerfile CMD.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
