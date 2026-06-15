# Luciel Backend — Authoritative Build Spec (derived from the 4 docs + frozen contract)

This file is the engineering contract for the backend. It is downstream of the four
business documents and the frozen frontend contract; on any conflict, the documents win
(precedence: Vision > Architecture > Customer Journey > Legal) and this file is corrected.
Every product-behavior decision cites its doc section.

## 0. Non-negotiable environment facts (this sandbox)
- Python 3.12, `uv` for deps. No Docker in sandbox — Postgres 18 + Redis run natively.
- DB (dev/test): host `127.0.0.1:5432`, db `luciel_dev`, app role `luciel_app` / `luciel_dev_pw`.
  - CRITICAL: `luciel_app` is NOSUPERUSER + NOBYPASSRLS. RLS only works because of this.
  - Extensions installed: `vector` (pgvector), `uuid-ossp`.
- Redis: `127.0.0.1:6379`.
- A separate test DB `luciel_test` is created by the test harness.
- Docker Compose file (`docker-compose.yml`) is authored for the OWNER's machine, not run here.

## 1. Stack (Architecture Locked Decisions §7, Space Instr §2)
- FastAPI (ASGI), SQLAlchemy 2.x (async) + asyncpg, Alembic migrations, Pydantic v2.
- Redis via `redis.asyncio`. pgvector via `pgvector.sqlalchemy.Vector`.
- Lint/format: ruff. Type-check: mypy (strict on app/). Tests: pytest + pytest-asyncio.
- httpx for the contract-conformance harness (in-process ASGI transport).

## 2. The app/** layout — MUST match Architecture §8 doctrine anchors verbatim
```
app/
  __init__.py
  main.py                      # FastAPI app factory; CORS; cookie session; router mounting
  config.py                    # pydantic-settings; env-driven; NO secrets in code
  db/
    __init__.py
    engine.py                  # async engine + sessionmaker
    session.py                 # per-request session dep that SETs app.admin_id once (Layer 3)
    base.py                    # DeclarativeBase + TenantMixin (admin_id col) (§3.7.2b Layer 1)
    rls.py                     # helpers to emit RLS policy SQL / set app.admin_id
  models/                      # SQLAlchemy ORM models, one module per aggregate
    __init__.py
    instance.py                # accounts/owners + the Luciel instance (§3.7.1, §3.1.1)
    ...                        # added per arc
  schemas_api/                 # Pydantic request/response models — EXACT camelCase wire shapes
    __init__.py
    common.py                  # ApiError envelope, connection_status enum, ApiErrorCode
    auth.py luciel.py connections.py conversations.py billing.py analytics.py widget.py
  api/
    __init__.py
    deps.py                    # auth deps: resolve admin_id from JWT cookie (control) / embed key (data)
    errors.py                  # exception handlers -> exact STATUS_TO_CODE map (transport.ts)
    v1/
      __init__.py
      auth.py                  # /api/v1/auth/*  (§3.7.1a)
      admin/
        __init__.py
        luciel.py              # /api/v1/admin/luciel*  (5 pillars + lifecycle)
        knowledge.py           # /api/v1/admin/knowledge/*
        connections.py         # /api/v1/admin/connections/*
        usage.py               # /api/v1/admin/usage/*   (DOCTRINE ANCHOR app/api/v1/admin/usage.py)
        account.py             # /api/v1/admin/account/*
      billing.py               # /api/v1/billing/*
      dashboard.py             # /api/v1/dashboard/*  (conversations, leads, escalations)
      chat_widget.py           # /api/v1/chat-widget/*  (DATA PLANE; zero admin surface)
  policy/
    entitlements.py            # DOCTRINE ANCHOR — single plan; all features all accounts (§7 #12,#15)
  runtime/
    __init__.py
    orchestrator.py            # DOCTRINE ANCHOR — agentic loop, budget gate, escalation (§3.4.1)
    llm_router.py              # DOCTRINE ANCHOR — Anthropic primary / OpenAI fallback (§3.4.3)
    knowledge_retrieval.py     # DOCTRINE ANCHOR — hybrid vector+graph retrieval (§3.2)
    grounding.py               # DOCTRINE ANCHOR — grounding floor 0.50 (§3.4.13)
    input_safety.py            # DOCTRINE ANCHOR — prompt-injection + moderation (§3.4.14/15)
    handoff.py                 # DOCTRINE ANCHOR — human takeover (§3.4.12)
    escalation.py              # escalation judgment module (four fixed signals) (§3.4.5)
    channel_arbiter.py         # outbound channel selection (§3.4.2)
    sessions.py                # session lifecycle/boundary (§3.4.8), identity resolution (§3.4.9)
    memory.py                  # conversation memory + summarization (§3.4.6/7)
  channels/
    __init__.py
    base.py                    # ChannelAdapter Protocol (§3.1.1)
    email_adapter.py sms_adapter.py voice_adapter.py whatsapp_adapter.py meta_messaging_adapter.py
  connections/                 # DOCTRINE ANCHOR app/connections/ — Connections Layer (§3.8)
    __init__.py
    model.py registry.py health_worker.py credentials.py sandbox.py gating.py
  lifecycle/                   # DOCTRINE ANCHOR app/lifecycle/ — state machine + cascade (§3.6)
    __init__.py
    state_machine.py cascade.py export_bundle.py
  auth/
    __init__.py
    access.py                  # DOCTRINE ANCHOR app/auth/access.py — single-login + tenant scoping (§3.7)
    passwords.py               # argon2id
    tokens.py                  # JWT mint/verify; verification + reset link tokens
  billing/
    __init__.py
    metering.py                # DOCTRINE ANCHOR app/billing/metering.py — counter + Stripe overage (§3.4.1b)
    stripe_gateway.py          # Stripe client wrapper (test mode)
  notifications/               # DOCTRINE ANCHOR app/notifications/ — escalation delivery (§3.5)
    __init__.py
    delivery.py routing.py retry.py
  analytics/                   # DOCTRINE ANCHOR app/analytics/ — read-only aggregates (§3.9)
    __init__.py
    reports.py
  audit/
    __init__.py
    log.py                     # append-only, SHA-256 hash-chained per admin_id (§5.2/§5.3)
    events.py                  # the exact event_type enumeration (§5.2)
  migrations/                  # DOCTRINE ANCHOR app/migrations/ — Alembic (§5.9)
    env.py  versions/
tests/
  isolation/                   # DOCTRINE ANCHOR tests/isolation/ — non-overridable (§5.15)
  contract/                    # contract-conformance harness vs http-admin.ts / schemas
  unit/  integration/  migrations/
.github/workflows/ci.yml       # DOCTRINE ANCHOR — CI gate (§5.14/§5.15)
docker-compose.yml             # for the owner's machine (Postgres+pgvector, Redis)
pyproject.toml  alembic.ini  README.md  .env.example
```

## 3. Three-layer tenant isolation (§3.7.2b, Space Instr §3.1) — THE crown jewel
Every tenant table carries `admin_id uuid NOT NULL`. All three layers on every tenant table:
1. App filter: repositories always `WHERE admin_id = :admin_id` (via TenantMixin + scoped session).
2. Postgres RLS: `CREATE POLICY tenant_isolation ON <t> USING (admin_id = current_setting('app.admin_id')::uuid)`.
   - Enable + FORCE row level security (FORCE so the table owner is also subject to RLS).
   - For append-only audit log: add `WITH CHECK` and grant INSERT only (no UPDATE/DELETE).
3. Per-request `app.admin_id`: set ONCE per request from the authenticated subject (JWT/embed key),
   via `SET LOCAL app.admin_id = :id` inside the request's transaction. NEVER from query param/body.
- Tenant tables (apply all 3 as each is created): instances/accounts, leads, sessions, transcripts,
  session_summaries, knowledge_sources, knowledge_chunks, instance_connections, escalation_events,
  admin_audit_log.

## 4. Wire contract — EXACT (from packages/api-client) — field-for-field, status-for-status
- Session: httpOnly + Secure + SameSite cookie named `luciel_session`. credentials:'include'.
  CORS: allow dashboard origin(s) with credentials. Cookie carries JWT; body never returns raw JWT.
- Error body shape: `{ "message": str, "retryAfterSeconds"?: int }`. Code derived by FRONTEND from HTTP status.
  STATUS_TO_CODE (transport.ts): 401->unauthorized, 402->payment_required, 403->unauthorized,
  404->not_found, 409->conflict, 422->validation_error, 429->rate_limited, else server_error.
  => Therefore: gated-unverified write = 403; at_cap rejection (if any) = 402; slot taken = 409;
     bad input = 422; resend cooldown = 429 (+retryAfterSeconds).
- 204 No Content for void methods: logout, knowledge.deleteSource, connections.disconnect,
  leads.erase, leads.prune, conversations.flagAnswer, account.close.
- `GET /api/v1/admin/luciel` returns JSON `null` (200) when no Luciel — NOT 404.
- All field names camelCase exactly as in schemas/*. Enums verbatim (see §5 below).
- FLAGGED ASSUMPTION (verification_required / at_cap codes): transport.ts has NO status mapping to
  `verification_required` or `at_cap`. The documented unverified behavior is routing-based:
  `me()` returns 200 with account.state='unverified' + nextRoute='verify_wall'; gated writes return 403.
  `at_cap` is surfaced as DATA (`budget.atCap=true`), not a thrown error. We implement the documented
  behavior; carrying those two codes over the wire would require adding them to STATUS_TO_CODE in the
  frontend transport (a coordinated frontend-repo PR), noted as a recommended reconciliation, not invented.

### Exact endpoint map (http-admin.ts) — paths are frozen
auth: POST signup, POST login, POST logout, GET me, POST verify-email, POST resend-verification,
      POST forgot-password, POST reset-password   (all under /api/v1/auth/)
luciel: GET /api/v1/admin/luciel, POST (create), PUT .../channels {channels}, PUT .../tools {tools},
        PUT .../escalation (contact body), PUT .../personality (config body),
        POST .../voice-consent, POST .../pause, POST .../resume, POST .../delete, POST .../restore
knowledge: GET .../sources, GET .../sources/{id}/chunks, GET .../quota,
           DELETE .../sources/{id}, POST .../sources/{id}/resync
connections: GET /api/v1/admin/connections, POST (start) {connectionType,provider},
             POST .../{id}/reconnect, DELETE .../{id}
conversations(dashboard): GET /api/v1/dashboard/conversations, GET .../{sid}/messages,
             POST .../{sid}/take-over, POST .../{sid}/hand-back,
             GET .../{sid}/messages/{mid}/evidence, POST .../{sid}/messages/{mid}/flag,
             GET /api/v1/dashboard/escalations
leads(dashboard): GET /api/v1/dashboard/leads, DELETE .../{id}, POST .../prune {leadIds},
             POST .../{id}/archive
billing: GET /api/v1/billing, POST /api/v1/billing/checkout, DELETE /api/v1/billing/payment-method
analytics: GET /api/v1/admin/usage/overview, GET /api/v1/admin/usage/audit-log
account: POST /api/v1/admin/account/export, POST /api/v1/admin/account/close
widget(DATA PLANE): POST /api/v1/chat-widget/bootstrap {embedKey},
             POST /api/v1/chat-widget/messages {embedKey, sessionId?, text},
             GET  /api/v1/chat-widget/sessions/{sid}?embedKey=...

## 5. Exact enums / shapes the wire must use (camelCase keys)
- accountState: unverified|verified|paused|pending_deletion|closed
- session.nextRoute: first_run|dashboard|verify_wall
- lucielState: active|paused|luciel_grace_window|luciel_hard_deleted
- channelId: widget|email|sms|voice|whatsapp|instagram_messenger
- addonToolId: check_availability|book_appointment|send_email|send_sms|lookup_record|schedule_callback|push_to_crm|bring_your_own_webhook
- builtinCognition (non-interactive): capture_leads|escalate|handoff|summarize
- knowledgeSourceOrigin: upload|paste|csv|website_crawl|google_drive|notion|crm_kb
- knowledgeSyncStatus: synced|syncing|needs_reconnect
- ingestionStatus: pending|ready|error
- escalationSignal (the 4, fixed): explicit_human_request|strong_negative_sentiment|cannot_answer|high_value_lead
- escalationEventSignal: the 4 + budget_exhausted + llm_unavailable
- notificationChannel: email|sms
- personalityPreset: warm_concierge|professional_advisor|friendly_expert|trusted_authority|custom
- connectionType: calendar|crm|record_source|email_sender|sms_sender|outbound_webhook|channel_auth|knowledge_source
- connectionStatus: unconfigured|connected|error|expired|revoked|dormant
- conversationMode: ai|human_controlled
- leadState: active|archived  (NO 'pruned' — prune deletes)
- billingState: free_cap|payg_enabled ; dunningState: none|retrying|reduced
- budget.freeAllowance is literally 50; widget bootstrap.poweredByVantageMind literally true;
  widget aiAssistantLabel literally 'AI assistant'.
- apiErrorCode: unauthorized|verification_required|rate_limited|not_found|conflict|validation_error|at_cap|payment_required|server_error|network_error
- widgetRenderState: active|paused|at_cap

## 6. Locked numbers / invariants (Architecture §7/§9, Legal)
- Billing: free 50/mo; PAYG $39 per 100 above 50, rounded UP per 100-block, monthly arrears via Stripe metered.
- Conversation counter: once per session; fires on first model call OR human-controlled-before-model;
  idempotent on session id; synchronous write-through Postgres (source of truth) + Redis (hot cache).
  Reset on Stripe invoice.paid; no rollover. (Build-status honesty: write-through is the target; until it
  ships do not claim the §4.5 Redis-down fallback is operative.)
- Grounding floor: 0.50 uniform. Below floor with no resolving tool => cannot_answer escalation.
- Escalation: 4 signals fixed, never admin-configurable; only who/how routed.
- Audit log: append-only, INSERT-only grant, SHA-256 hash-chain per admin_id, genesis prev_hash NULL;
  no raw lead PII in payload (hashed resolved_lead_id only); migrations add-columns-only + 2nd reviewer.
- Secrets: Secrets Manager/SSM only; Postgres stores secret_ref pointer; never in logs/audit/export.
- Single login: one account_owner; argon2id; hard email-verify gate; verify link 24h TTL single-use,
  reissue invalidates, resend 3/15min; unverified reaped after 7 days; reset link 1h TTL single-use,
  completing reset revokes all sessions.
- Lifecycle: pause (non-destructive, instant); delete Luciel (fused quiesce -> 30d grace -> hard delete cascade,
  account shell survives); restore -> active (not paused); close account requires Luciel deleted first; export-first.
- Do NOT over-build: no MFA, no team seats, no model-selection field, no second Connections pillar,
  no custom-domain inbound email, no cross-customer learning.

## 7. Arc order (Space Instr §6 / Architecture §6) — dependency-ordered, PR per arc, CI green each
9 foundation/RLS -> 10 lifecycle+export -> 11 knowledge(pgvector)+11b live-sync -> 12 tools+sandbox
-> 13 email+SMS -> 14 runtime+escalation(+14b voice) -> 15 config UX surfaces -> 16 graph+hybrid
-> 17 Connections Layer -> 18 metering+Stripe+usage. Stop before Arc 19 (SOC2).

## 8. CI gate (§5.14/§5.15) — non-overridable
isolation suite + unit + integration + migration apply/rollback + ruff + mypy + SAST(bandit) +
dependency scan (pip-audit) + secret scan (detect-secrets/gitleaks) + doctrine-to-code sync check.
Isolation suite failure blocks all merges; no override.
