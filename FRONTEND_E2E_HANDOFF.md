# Luciel Backend → Frontend E2E Handoff

This document hands the assembled, locally-tested backend to the owner for the final frontend end-to-end pass. The backend implements every subsystem the four business documents specify, built against the frozen frontend contract (`Luciel-Frontend/packages/api-client`). It is **not** deployed to AWS — that waits for explicit owner approval (per the build doctrine).

**State at handoff:** arcs 9–18 complete, merged to `main`. **238 tests passing**, stable across repeated runs. All 11 migrations round-trip `base → head` cleanly. Doctrine-sync gate green.

---

## 1. What "done" means here (and what it doesn't)

**Done — verified:**
- All **45 frozen `http-admin.ts` endpoints** have a matching backend route (1:1; verified by enumerating both sides — see §5). No frozen endpoint is missing.
- Wire contract holds: camelCase via `WireModel` (`alias_generator=to_camel`, `populate_by_name`); error envelope is `{message, retryAfterSeconds?}` with `ApiErrorCode` derived from HTTP status exactly as `transport.ts` expects (401/403→unauthorized, 402→payment_required, 404→not_found, 409→conflict, 422→validation_error, 429→rate_limited, else server_error); 204 for void methods; `GET /api/v1/admin/luciel` returns JSON `null` (200) when no Luciel exists.
- Plane separation enforced: the widget data plane (`/api/v1/chat-widget/*`) exposes **zero** admin surface; AI-identity disclosure + powered-by chrome always present; `paused`→empty; `at_cap`→graceful server-driven no-LLM reply.
- Three-layer tenant isolation (TenantMixin + Postgres RLS ENABLE+FORCE + per-request `SET LOCAL app.admin_id`) — the isolation suite is a **non-overridable** merge gate.
- Audit log is append-only and SHA-256 hash-chained per admin; `verify_chain` is deterministic (the chain orders by a monotonic `seq`; see §7 note).

**NOT done — deferred behind swappable interfaces (bind on owner approval):**
- **AWS:** no ECS/RDS/ECR/Secrets Manager/SSM provisioning. `SecretStore` is a local impl; the AWS Secrets Manager backend binds behind the same Protocol later.
- **Stripe:** `StripeGateway` is a deterministic fake in CI. Real **test-mode** Stripe wiring (Checkout, metered usage, webhook secret) binds behind the same Protocol; no live/production Stripe.
- **External providers:** OAuth authorize/callback, real connection health probes + token refresh, real LLM providers (Anthropic/OpenAI), real STT/TTS, real Twilio/SNS/Meta delivery, real embedding model, and the LLM-backed graph extractor + intent classifier — all behind Protocols with deterministic local impls used for tests.

These deferrals are intentional: the local, document-aligned, fully-tested system comes first; infra and paid vendors wait for your go-ahead.

---

## 2. Prerequisites (owner's machine)

- Python 3.12, PostgreSQL 18 with `vector` (pgvector) + `uuid-ossp` extensions, Redis.
- Docker Compose is provided for Postgres+pgvector and Redis (see `docker-compose.yml`). If you run native Postgres/Redis instead, the connection defaults are `127.0.0.1:5432` / `127.0.0.1:6379`.

### DB roles (this is what makes RLS real)
- `luciel_app` / `luciel_dev_pw` — app role, **NOSUPERUSER, NOBYPASSRLS** (so row-level security actually constrains it).
- `luciel_auth` — **NOLOGIN, BYPASSRLS** — owns the `SECURITY DEFINER` pre-subject lookup functions (`auth_lookup_account`, `channel_resolve_embed_key`, `channel_resolve_destination`).
- DB `luciel_dev`. After any `DROP SCHEMA public CASCADE` reset, recreate:
  ```sql
  CREATE SCHEMA public AUTHORIZATION luciel_app;
  GRANT USAGE, CREATE ON SCHEMA public TO luciel_auth;
  CREATE EXTENSION vector; CREATE EXTENSION "uuid-ossp";
  ```

---

## 3. Run the backend locally

```bash
cd Luciel-Backend
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]            # or: pip install -r requirements (see pyproject)

# start infra (compose) OR native:
docker compose up -d             # postgres+pgvector, redis
#   native alt: pg_ctlcluster 18 main start ; redis-server --daemonize yes --port 6379

alembic upgrade head             # apply all 11 migrations

# run the API
uvicorn app.main:app --reload --port 8000
# OpenAPI: http://localhost:8000/docs   health: http://localhost:8000/healthz
```

For local frontend dev over plain HTTP, set `LUCIEL_COOKIE_SECURE=false` so the httpOnly session cookie is issued without the Secure flag.

---

## 4. Run the full test + quality gate

```bash
. .venv/bin/activate
python -m pytest tests/ -q                 # 238 passing
ruff check app tests
ruff format --check
mypy app                                    # strict
bandit -q -r app -c pyproject.toml
python scripts/check_doctrine_sync.py
# migration round-trip — run on an IDLE db, not interleaved with a live pytest run:
alembic downgrade base && alembic upgrade head
detect-secrets scan > .secrets.baseline
```
The test DB (`luciel_test`) is auto-provisioned by `tests/conftest.py`.

---

## 5. Frozen-contract → backend route map (all 45, verified 1:1)

`${id}` (client) ⇄ `{id}` (server) is templating only, not a mismatch.

| Frozen client call | Backend route |
|---|---|
| `auth.signup/login/logout/verifyEmail/resendVerification/forgotPassword/resetPassword` | `POST /api/v1/auth/{signup,login,logout,verify-email,resend-verification,forgot-password,reset-password}` |
| `auth.me` | `GET /api/v1/auth/me` |
| `luciel.get / create` | `GET` / `POST /api/v1/admin/luciel` |
| `luciel.pause/resume/delete/restore` | `POST /api/v1/admin/luciel/{pause,resume,delete,restore}` |
| `luciel.voiceConsent` | `POST /api/v1/admin/luciel/voice-consent` |
| `luciel.setChannels/setTools/setEscalation/setPersonality` | `PUT /api/v1/admin/luciel/{channels,tools,escalation,personality}` |
| `knowledge.quota/listSources/getChunks` | `GET /api/v1/admin/knowledge/{quota,sources,sources/{id}/chunks}` |
| `knowledge.deleteSource/resync` | `DELETE` / `POST /api/v1/admin/knowledge/sources/{id}[/resync]` |
| `connections.list/start/reconnect/disconnect` | `GET`/`POST`/`POST .../{id}/reconnect`/`DELETE .../{id}` under `/api/v1/admin/connections` |
| `billing.get/startCheckout/removePaymentMethod` | `GET /api/v1/billing`, `POST /api/v1/billing/checkout`, `DELETE /api/v1/billing/payment-method` |
| `usage.overview/auditLog` | `GET /api/v1/admin/usage/{overview,audit-log}` |
| `dashboard.conversations/messages/evidence/escalations` | `GET /api/v1/dashboard/conversations[...]`, `/escalations` |
| `dashboard.takeOver/handBack/flag` | `POST /api/v1/dashboard/conversations/{sid}/{take-over,hand-back,messages/{mid}/flag}` |
| `dashboard.leads/archive/prune/erase` | `GET /api/v1/dashboard/leads`, `POST .../{id}/archive`, `POST .../prune`, `DELETE .../{id}` |
| `account.close/export` | `POST /api/v1/admin/account/{close,export}` |

**Additive backend surfaces the contract expects to exist but doesn't call directly:** widget data plane (`POST /api/v1/chat-widget/bootstrap`, `POST .../messages`, `GET .../sessions/{sid}`), Stripe webhook (`POST /api/v1/webhooks/stripe`), knowledge ingest (`POST /api/v1/admin/knowledge/sources/{paste,upload}` — see §6 gap #2).

---

## 6. Flagged contract gaps (surfaced, never silently worked around)

1. **`verification_required` / `at_cap` have no HTTP status in `transport.ts`.** Implemented as documented routing, not thrown errors: `auth.me` returns 200 with `nextRoute='verify_wall'` for unverified; gated writes by an unverified account → 403; `at_cap` is surfaced as **data** (`budget.atCap=true`), not an exception. 402 `payment_required` is reserved for genuine payment-required rejections.
2. **Knowledge paste/upload ingest endpoints are additive** — the frozen client has no `knowledge.create`. The backend exposes `POST .../sources/{paste,upload}`; wire the frontend ingest UI to these (shapes are camelCase `WireModel`, documented in `/docs`).
3. **Per-message grounding evidence is not yet persisted.** The `/messages/{mid}/evidence` endpoint returns the uniform grounding floor (0.50) + empty chunks; the wire shape is frozen and correct, but evidence persistence is owned by a later knowledge/trace iteration. The endpoint will populate once evidence rows are stored.
4. **`channel_resolve_embed_key` resolves only active instances**, so the widget `paused` render branch is unreachable via that resolver in practice; it is retained defensively in case the resolver behavior changes.

---

## 7. Notes for the e2e session

- **8 mock states to exercise** (mirror `mock-data.ts`): verified, fresh, unverified, expired_session, at_cap, payg, paused, grace. The backend can be seeded into each; the at_cap/payg/paused/grace paths are the highest-value to verify against real wire responses.
- **Audit-chain determinism** was hardened mid-build: the chain orders strictly by a monotonic `seq` (BIGSERIAL), and `created_at` is app-assigned so the hashed value round-trips exactly. Tamper-evidence is preserved (mutating any hashed field breaks `verify_chain`); regression tests assert this. If you extend audit events, keep `seq` DB-assigned (don't send it explicitly in INSERTs).
- **Run pytest and the alembic round-trip separately** (don't interleave the migration round-trip with a live suite run against the same DB) — doing so previously left transient residue.
- **Cross-arc deferred bindings now closed:** the orchestrator/voice empty-retriever stubs (Arc 14/14b) are bound to the real hybrid retriever (Arc 16); the tool broker's connection gate (Arc 12) now delegates to the shared universal gate (Arc 17). No dangling stubs remain in the runtime path.

---

## 8. PR trail (reviewable per arc)

| Arc | PR | Scope |
|---|---|---|
| 14 | #6 | Agentic runtime + dashboard + widget data plane |
| 14b | #7 | Voice channel (Twilio Voice + STT/TTS + mandatory recording disclosure) |
| 15 | #8 | Config-surface invariant enforcement (5 pillars) |
| fix | #9 | Deterministic audit-chain verification (root-caused intermittent failures) |
| 16 | #10 | Graph store (Postgres recursive-CTE) + hybrid retrieval |
| 17 | #11 | Connections Layer (§3.8): admin API, health/refresh worker, universal gate, fail-safe queue |
| 18 | #12 | Budget metering + Stripe overage (test-mode gateway) + usage dashboard + dunning |

(Arcs 9–13 shipped in PRs #1–#5 earlier in the build.)

---

## 9. Next, on your approval

- Bind real Stripe **test mode** (keys + webhook secret) behind `StripeGateway`; run a live test-mode metering + dunning pass.
- Bind real LLM/STT/TTS/embedding providers behind their Protocols and validate grounding/voice end to end.
- Then — and only then — AWS provisioning (ECS/RDS/ECR/Secrets Manager/SSM) and Arc 19 (SOC 2), which were explicitly held back.
