# Luciel Backend (VantageMind)

The Python backend for the Luciel platform, built to the four business documents
(Vision > Architecture > Customer Journey > Legal) and the frozen frontend contract
in `aryanonline/Luciel-Frontend` (`packages/api-client`). The documents win on any
conflict; `BUILD_SPEC.md` is the engineering distillation and cites doc sections.

Stack (Architecture Locked Decisions §7): FastAPI + SQLAlchemy 2 async + asyncpg,
Alembic, Pydantic v2, Redis, pgvector. Built arc-by-arc (Architecture §6).

## Architecture at a glance
- **Two planes, one auth wall** (§1): the Control Plane authenticates as an admin
  JWT (cookie); the Data Plane (`/api/v1/chat-widget/*`) authenticates as an embed
  key / signed webhook. They never share an auth subject and communicate only
  through persistence.
- **Three-layer tenant isolation** (§3.7.2b) on every tenant table:
  1. app-layer `WHERE admin_id` (TenantMixin + scoped repositories),
  2. Postgres RLS (`ENABLE` + `FORCE`; policy `admin_id = current_setting('app.admin_id')`),
  3. per-request `app.admin_id` set once from the authenticated subject.
  Proven by `tests/isolation/` — the non-overridable CI gate.
- **Append-only, hash-chained audit log** (§5.2/§5.3): INSERT-only grant, SHA-256
  `prev_hash` per `admin_id`.

## Local development

### Option A — Docker Compose (recommended on your machine)
```bash
docker compose up -d db redis          # Postgres+pgvector + Redis, roles auto-bootstrapped
uv venv && . .venv/bin/activate && uv pip install -e ".[dev]" "psycopg[binary]" "pydantic[email]"
alembic upgrade head                   # create schema + RLS + grants
LUCIEL_COOKIE_SECURE=false uvicorn app.main:app --reload --port 8080
```

### Option B — native Postgres + Redis
```bash
# As a superuser, once per environment:
psql -f scripts/bootstrap_roles.sql
createdb luciel_dev -O luciel_app
psql -d luciel_dev -f scripts/bootstrap_db.sql
# Then:
alembic upgrade head
LUCIEL_COOKIE_SECURE=false uvicorn app.main:app --reload --port 8080
```

`luciel_app` is intentionally **NON-superuser, NON-BYPASSRLS** — that is what makes
RLS apply. `luciel_auth` is a narrow `NOLOGIN BYPASSRLS` role that owns only the
`auth_lookup_account()` function (the one pre-subject email lookup).

## Point the frontend at this backend
In the frontend repo's `.env.local`:
```
NEXT_PUBLIC_API_ADAPTER=http
NEXT_PUBLIC_API_BASE_URL=http://localhost:8080
```
The backend sets an httpOnly session cookie and allows the dashboard origin with
credentials (CORS). For http localhost, run the backend with `LUCIEL_COOKIE_SECURE=false`
so the browser sends the cookie; production keeps `Secure=true`.

## Wire-contract notes (reconciliation, Space Instructions §7)
- The frontend transport derives the `ApiErrorCode` from the **HTTP status** (it
  does not read a `code` field). So the backend returns the correct status and a
  `{message, retryAfterSeconds?}` body. Status map: 401/403→unauthorized,
  402→payment_required, 404→not_found, 409→conflict, 422→validation_error,
  429→rate_limited.
- **Flagged:** there is no status mapping to `verification_required` or `at_cap` in
  `transport.ts`. Unverified gating is therefore expressed as documented — `me()`
  returns 200 with `state='unverified'` + `nextRoute='verify_wall'`, and gated
  writes return 403. `at_cap` is surfaced as billing data (`budget.atCap`). Carrying
  those two codes over the wire would require adding them to the frontend transport's
  `STATUS_TO_CODE` (a coordinated frontend-repo change) — noted, not invented.

## Tests & CI gate (§5.14/§5.15)
```bash
pytest tests/                  # isolation + contract + unit + integration
ruff check app tests && ruff format --check app tests
mypy app
bandit -q -r app -c pyproject.toml
python scripts/check_doctrine_sync.py
```
The CI workflow (`.github/workflows/ci.yml`) runs all of the above plus migration
apply/rollback and secret scanning. The tenant-isolation suite failing blocks merge
with no override.

## Build sequence
Arc 9 (foundation/RLS) → 10 (lifecycle+export) → 11 (knowledge) → 11b (live-sync)
→ 12 (tools+sandbox) → 13 (email+SMS) → 14 (runtime+escalation, 14b voice)
→ 15 (config UX) → 16 (graph+hybrid) → 17 (Connections Layer) → 18 (metering+Stripe).
No AWS provisioning until explicit owner approval (local-first).
