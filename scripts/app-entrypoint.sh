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
# never matches. Keep in sync with the Dockerfile CMD.
# --forwarded-allow-ips: ONLY the VPC (LUCIEL_TRUSTED_PROXY_CIDRS, injected by CDK).
# With "*" uvicorn takes the LEFTMOST X-Forwarded-For entry — the one the CLIENT
# wrote — so a spoofed header became request.client.host and every per-IP abuse
# ceiling keyed on it. Trusting only the ALB's CIDR makes uvicorn walk the chain
# from the right and stop at the first untrusted hop: the address the ALB appended.
# --timeout-keep-alive 75: MUST exceed the ALB idle timeout (60s default). With
# uvicorn's 5s default the ALB reuses connections uvicorn has already closed ->
# instant 502 with no app-side log (live-caught 2026-08-18: Twilio error 11200 on
# /voice/turn — the caller's question died between the ALB and us).
# --timeout-graceful-shutdown 22: bounds the wait for in-flight requests (a POST in its
# 20 s inline sync wait), so the app's own drain still fits ECS's stopTimeout of 60 s.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${LUCIEL_TRUSTED_PROXY_CIDRS:-*}" --timeout-keep-alive 75 --timeout-graceful-shutdown 22
