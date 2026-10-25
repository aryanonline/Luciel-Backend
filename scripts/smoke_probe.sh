#!/usr/bin/env bash
# Post-deploy smoke probe (2026-09-05 audit F008): the live API must answer the way
# the deploy intended BEFORE the deploy is called done — and if it does not, the
# workflow rolls the services back to the task definitions that were serving.
#
#   scripts/smoke_probe.sh <env|api-base-url> <expected-integration-mode> [stripe-posture]
#
# The first argument is normally the ENVIRONMENT (dev|preprod|prod): the API origin
# and the Stripe posture default are then read from infra's hostnames helper
# (infra/bin/hostnames.ts — env-config.ts `hostnames`, round 7 WP-8b / owner decision
# D6), so dev is probed at https://api-dev.vantagemind.ai and prod at the public api
# host without a hostname written here. Needs infra's node_modules (deploy.yml runs
# `npm ci` there first). An explicit https:// origin is still accepted (an operator
# probing by hand); then the posture defaults to `test`, dev's.
#
# Public probes only (the runner is outside the VPC):
#   1. /healthz reports status ok, the expected integrationMode and EXACTLY the
#      degraded set that mode selects (app/runtime/integration_status.py
#      degraded_bindings): EMPTY in live mode — a non-empty list there means
#      sends are fabricated — and, in mock mode, precisely the in-process
#      stand-ins (Null email/SMS senders, the local secret store, the in-memory
#      calendar, the echo LLM) and nothing else. The old probe demanded an empty
#      list for ANY mode, which mock can never satisfy, so a mock deploy always
#      failed the probe and rolled back (round 7 WP-8). The platform SMS sender is
#      listed only where a platform Twilio account is configured (round 7 WP-7),
#      so in mock mode it may be present or absent. The optional third
#      argument is the environment's Stripe posture (default `test`, dev's):
#      a `mock` posture (prod) adds stripe_gateway:FakeStripeGateway to the
#      expected set in either mode, because that is the honest answer there.
#      An entry outside the expected set — a database:... or pgvector_too_old
#      report — still fails the probe in every mode.
#   2. An unauthenticated control-plane call (/api/v1/auth/me) is answered by the
#      APP with 401 — never a 502/503/504 from the load balancer, which is what a
#      broken listener rule or an unhealthy control-plane target looks like (F067).
#   3. The data-plane widget bootstrap refuses an unknown embed key with a 4xx from
#      the app — proof the default target group serves.
set -euo pipefail

TARGET="${1:?environment (dev|preprod|prod) or an https:// api base url}"
MODE="${2:?expected integration mode: mock|live}"
case "$TARGET" in
  dev|preprod|prod)
    # Resolved, never guessed: a failure here fails the probe (set -e) rather than
    # probing some other environment's API.
    INFRA_DIR="$(cd "$(dirname "$0")/../infra" && pwd)"
    hosts() { (cd "$INFRA_DIR" && npx --no-install ts-node --prefer-ts-exts bin/hostnames.ts "$TARGET" "$1"); }
    BASE="$(hosts apiBaseUrl)"
    STRIPE_POSTURE="${3:-$(hosts stripeIntegrationPosture)}"
    ;;
  https://*)
    BASE="${TARGET%/}"
    STRIPE_POSTURE="${3:-test}"
    ;;
  *)
    echo "::error::smoke_probe: '$TARGET' is neither an environment (dev|preprod|prod) nor an https:// origin"
    exit 2
    ;;
esac
echo "[smoke] probing $BASE (mode $MODE, stripe posture $STRIPE_POSTURE)"
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
  if [ "$code" = "200" ] && python3 - "$MODE" "$STRIPE_POSTURE" "$TMP/healthz.json" <<'PY'
import json, sys
mode, stripe_posture, path = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path))
status, got_mode, degraded = d.get("status"), d.get("integrationMode"), d.get("degraded")
# The EXACT set /healthz must list for this mode — the names are the
# `seam:ImplClass` strings app/runtime/integration_status.py degraded_bindings
# emits. Compared as a set equality, so an unexpected entry (database:...,
# pgvector_too_old:..., a live-mode Null sender) fails the probe.
expected = set()
# Entries the mode MAY list without failing the probe. platform_sms is a seam only when
# the environment's platform secret carries a platform Twilio account (round 7 WP-7):
# in mock mode it is then honestly Null, and an environment without one omits it. In
# live mode it is never optional — listed means half-configured, which fails.
optional = set()
if mode == "mock":
    expected = {
        "platform_email:NullEmailSender",
        "secret_store:LocalSecretStore",
        "calendar_api:InMemoryCalendarApi",
        "llm:EchoModel",
    }
    optional = {"platform_sms:NullSmsSender"}
if stripe_posture != "test":
    expected.add("stripe_gateway:FakeStripeGateway")
# `escalation_backlog:<n>` (round 7 WP-4b) is an OPERATIONS signal — some tenant has
# an escalation page still undelivered after a sweep — not a binding fault of this
# build. It alarms through CloudWatch (EscalationBacklogAlarm); letting it fail this
# probe would roll back a healthy release because one owner's contact is unreachable.
# It is reported here and left out of the set equality.
all_degraded = set(degraded or [])
# `embeddings:degraded` (round 7 WP-6) is the same kind of signal: the embeddings
# vendor refused the last calls of THIS process. It alarms on its own; a release
# unrelated to embeddings must not roll back over a vendor's bad minute.
operational = {
    d for d in all_degraded if d.startswith("escalation_backlog:") or d == "embeddings:degraded"
}
got = all_degraded - operational
if operational:
    print(f"[smoke] operations signal (not gating the deploy): {sorted(operational)}")
print(f"[smoke] healthz status={status} integrationMode={got_mode} degraded={sorted(got)} expected={sorted(expected)} optional={sorted(optional)}")
sys.exit(0 if (status == "ok" and got_mode == mode and expected <= got <= expected | optional) else 1)
PY
  then ok=true; break; fi
  echo "[smoke] healthz not ready or not healthy (attempt $i/24, http $code) - waiting 15s"
  sleep 15
done
$ok || { echo "::error::/healthz never reported status=ok, integrationMode=$MODE with the expected degraded set for that mode (stripe posture $STRIPE_POSTURE)"; exit 1; }

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

echo "[smoke] PASS: healthz ok/$MODE with exactly the expected degraded set; control plane answered 401; data plane answered $code."
