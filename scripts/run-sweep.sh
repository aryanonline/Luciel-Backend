#!/usr/bin/env bash
# Scheduled-sweep entrypoint (Legal §B5 / §A3). Invoked by EventBridge Scheduler via
# ECS RunTask with the sweep name as the only argument (infra/lib/compute-stack.ts).
#
# Derives LUCIEL_DATABASE_URL for the NON-superuser luciel_app role exactly as
# scripts/app-entrypoint.sh does, so the sweeps run under RLS like the app rather
# than as master — the per-tenant sweep transactions depend on that.
set -euo pipefail

SWEEP="${1:?usage: run-sweep.sh <retention|grace_expiry|close_purge|dunning>}"

# Absolute venv interpreter, never a bare `python3`: the scheduler runs this script
# through a LOGIN shell (`bash -lc`), and /etc/profile resets PATH to the system
# default, dropping the /opt/venv/bin that the image's ENV puts in front. A bare
# `python3` would resolve to the system interpreter, which has none of the app deps.
VENV_PY=/opt/venv/bin/python3

eval "$("$VENV_PY" - <<'PY'
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

exec "$VENV_PY" -m app.jobs "$SWEEP"
