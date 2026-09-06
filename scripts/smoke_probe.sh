#!/usr/bin/env bash
# Post-deploy smoke probe (2026-09-05 audit F008): the live API must answer the way
# the deploy intended BEFORE the deploy is called done — and if it does not, the
# workflow rolls the services back to the task definitions that were serving.
#
#   scripts/smoke_probe.sh <api-base-url> <expected-integration-mode>
#
# Public probes only (the runner is outside the VPC):
#   1. /healthz reports status ok, the expected integrationMode and an EMPTY
#      degraded list — a non-empty list in live mode means sends are fabricated.
#   2. An unauthenticated control-plane call (/api/v1/auth/me) is answered by the
#      APP with 401 — never a 502/503/504 from the load balancer, which is what a
#      broken listener rule or an unhealthy control-plane target looks like (F067).
#   3. The data-plane widget bootstrap refuses an unknown embed key with a 4xx from
#      the app — proof the default target group serves.
set -euo pipefail

BASE="${1:?api base url, e.g. https://api.vantagemind.ai}"
MODE="${2:?expected integration mode: mock|live}"
TMP="$(mktemp -d)"

probe() {
  # probe <METHOD> <path> <out-file> [json-body] → prints the HTTP status code
  local method="$1" path="$2" out="$3" body="${4:-}"
  if [ -n "$body" ]; then
    curl -sS -m 20 -o "$out" -w '%{http_code}' -X "$method" "$BASE$path" \
      -H 'content-type: application/json' -d "$body" || echo 000
  else
    curl -sS -m 20 -o "$out" -w '%{http_code}' -X "$method" "$BASE$path" || echo 000
  fi
}

# 1) healthz — retried: a rolling deployment shifts targets for a minute or two.
ok=false
for i in $(seq 1 24); do
  code=$(probe GET /healthz "$TMP/healthz.json")
  if [ "$code" = "200" ] && python3 - "$MODE" "$TMP/healthz.json" <<'PY'
import json, sys
mode, path = sys.argv[1], sys.argv[2]
d = json.load(open(path))
status, got_mode, degraded = d.get("status"), d.get("integrationMode"), d.get("degraded")
print(f"[smoke] healthz status={status} integrationMode={got_mode} degraded={degraded}")
sys.exit(0 if (status == "ok" and got_mode == mode and degraded == []) else 1)
PY
  then ok=true; break; fi
  echo "[smoke] healthz not ready or not healthy (attempt $i/24, http $code) - waiting 15s"
  sleep 15
done
$ok || { echo "::error::/healthz never reported status=ok, integrationMode=$MODE, degraded=[]"; exit 1; }

# 2) control plane answers (401 from the app, not a load-balancer error).
code=$(probe GET /api/v1/auth/me "$TMP/me.json")
if [ "$code" != "401" ]; then
  echo "::error::control-plane route /api/v1/auth/me answered HTTP $code (expected 401 from the app)"
  head -c 300 "$TMP/me.json" || true
  exit 1
fi

# 3) data plane answers (a 4xx from the app for an unknown embed key).
code=$(probe POST /api/v1/chat-widget/bootstrap "$TMP/boot.json" '{"embedKey":"vm_live_smoke_probe_invalid"}')
case "$code" in
  4??) ;;
  *) echo "::error::data-plane route /api/v1/chat-widget/bootstrap answered HTTP $code (expected a 4xx from the app)"; exit 1 ;;
esac

echo "[smoke] PASS: healthz ok/$MODE with no degraded bindings; control plane answered 401; data plane answered $code."
