# Luciel Backend — CLAUDE.md

## Purpose

Luciel is a single-account AI assistant for a business. This service supplies the
control plane, embeddable chat data plane, channel webhooks, knowledge-grounded
runtime, external connections, billing, lifecycle work, and AWS deployment
configuration that let one owner operate one Luciel across its customer channels.

## THE AUTHORITATIVE DOCS NOTE

**Business truth does not live in this repository.** The authoritative documents
are the Perplexity project files `VANTAGEMIND_VISION`, `VANTAGEMIND_ARCHITECTURE`,
`VANTAGEMIND_CUSTOMER_JOURNEY`, and `VANTAGEMIND_LEGAL`, updated 2026-08-06.
Repository documentation is deliberately minimal: this file and the module-local
`CLAUDE.md` files are operational maps, not a competing product specification.

When code, a task, and those project files conflict, resolve the conflict with the
owner before making a product-behavior change. Do not recreate historical briefs,
hand-offs, credentials notes, or narrative architecture documents in the repo.
Never put credentials, customer data, or live operational secrets in this repo.

## Branch discipline

- Work only on `feat/docs-conformance-build-20260713-r2` for this change stream.
- Treat `main` as a stale revert; never use it as the implementation base.
- Start by checking `git status --short --branch` and the branch tip.
- Keep a change narrow, tested, and reviewable; do not mix product rewrites with
  opportunistic refactors.
- Preserve the route registry, tenant-isolation suite, migration sequence, and
  deployment gates whenever a surface changes.
- A live behavior fix is incomplete until a test models the condition that caused it.

## Architecture map

| Area | Purpose |
| --- | --- |
| `app/main.py` | FastAPI factory, plane-aware CORS, `/healthz`, and router wiring. |
| `app/api/` | v1 HTTP routes, dependencies, errors, and control/data-plane boundaries. |
| `app/auth/` | Account access, password/token flows, CAPTCHA, and unverified reaping. |
| `app/analytics/` | Owner-facing usage and analytics calculations. |
| `app/audit/` | Append-only audit events and internal-access logging. |
| `app/billing/` | PAYG metering, Stripe gateway/webhooks, dunning, and billing notices. |
| `app/channels/` | Widget/email/SMS/voice/Meta ingress and honest outbound delivery. |
| `app/connections/` | BYO provider lifecycle, OAuth, health, registry, and tenant secrets. |
| `app/db/` | SQLAlchemy engine/session setup and tenant RLS context. |
| `app/jobs/` | Scheduled lifecycle, retention, reconciliation, and deferred-health sweeps. |
| `app/knowledge/` | File ingestion, sync, scope, chunks, embeddings, graph, and retrieval inputs. |
| `app/lifecycle/` | Delete/restore/close, exports, grace expiry, retention, and cascades. |
| `app/migrations/` | Alembic schema history; audit lock follows migration as an operations step. |
| `app/models/` | SQLAlchemy persistence models for account, runtime, billing, and integrations. |
| `app/notifications/` | Email/SMS notifications and deferred escalation delivery. |
| `app/policy/` | Product entitlement and capability decisions. |
| `app/runtime/` | Grounded orchestration, sessions, safety, handoff, LLMs, voice, and budgets. |
| `app/schemas_api/` | API request/response schemas and wire-shape aliases. |
| `app/tools/` | Authorization, catalog, broker, tool result contract, and calendar backend. |
| `infra/` | CDK stacks, environment posture, security, observability, deploy, and costs. |
| `scripts/` | Database role/bootstrap, migration, verification, sweep, and deploy helpers. |
| `tests/` | Unit, integration, contract, migration, and non-overridable isolation coverage. |

Read the local `CLAUDE.md` before changing a module. Follow execution paths across
modules rather than duplicating business rules in a new location.

## Non-negotiable product invariants

### Account and commercial model

- One account has one Luciel and one login; there are no seats.
- There is no model picker and no other pricing tier.
- Pricing is $0 for the first 50 conversations, then $39 per 100-conversation
  block, pay-as-you-go.
- A conversation is a billed session. It counts once per billing period despite
  multiple turns and must mean the same thing in Overview, Billing, and Analytics.
  On every channel the session key is the transport identifier (participant key,
  §3.4.8); sessions END deterministically (inactivity sweep / close beacon) and are
  summarized at close (§3.4.7). Transcripts move to S3 cold storage at 90 days and
  hard-delete at 1 year (Commitment #38).
- The free-50 window is anchored to the Stripe billing period; before a card exists,
  use the calendar-month fallback.
- Meter only deltas: persist the watermark, use idempotent identifiers, flush
  residual blocks on card detach, and reset after `invoice.paid`.
- Verify Stripe webhook signatures with the five-minute tolerance and replay-gate
  processed event IDs. The legacy Stripe usage-records API is banned.

### Trust, grounding, and delivery

- State that Luciel is an AI on every customer channel; preserve the disclosure in
  greetings and never represent a human as answering.
- Persist real grounding scores. The floor is 0.50; low grounding **or** low
  confidence must refuse/escalate. Never change that to an AND gate.
- Use explicit evidence states: `has_sources`, `no_sources_retrieved`, or
  `attribution_unavailable`; legacy answers are “Not scored (before scoring existed)”.
- Tool outcomes use `ok`, `empty`, `ambiguous`, `failed`, or `missing_input` and
  must be read back faithfully.
- Live delivery fails closed. Never fabricate a sent message, a provider ID, or a
  successful handoff. Null senders are mock-only.
- An unconfigured provider must be honestly disabled, not presented as connected.
- Missing contacts or grounding failure may say the request was passed to the team;
  never claim someone is being connected in real time.

### Tenant, connection, and channel boundaries

- Tenant RLS isolation is mandatory. The application role is non-superuser and
  non-BYPASSRLS; do not bypass the tenant context for convenience.
- The product is BYO: customers provide their accounts and assets. Store per-tenant
  external secrets in Secrets Manager under the `vm/` prefix.
- SMS and voice use the customer’s own Twilio account and designated number. Luciel
  neither pools/resells numbers nor performs the customer’s A2P registration.
- Email is BYO-mailbox (§3.1.6a; Outlook first): the customer connects their own work
  mailbox under the existing `email_sender` type; replies go out AS that mailbox with
  no silent SES fallback, inbound arrives via the 5-minute `mailbox_poll` delta sweep
  (the FIRST poll drains history unprocessed — the baseline rule), and the platform
  SES / own-domain modes remain the non-mailbox alternative.
- One Meta connection serves WhatsApp and Messenger; Instagram is a separate
  Business Login connection. The wire/config enums carry SEPARATE `whatsapp`,
  `messenger`, and `instagram` channel ids (§3.1.2); the legacy combined
  `instagram_messenger` value survives only on pre-split session rows (read-path
  tolerance, never configurable). Consent alone is not a live channel: each
  destination must bind before it can truthfully be Connected.
- Lifecycle is Connect → healthy / Action needed / Reconnect → Disconnect → Switch.
  Swap keeps the old credential serving until the replacement verifies.
- Disabling a channel never disconnects its provider or clears its number.
- Pausing the Luciel pauses every channel through the shared runtime gate.
- Widget human-takeover replies arrive on the next poll; do not promise push delivery.
- Validate Twilio/SES/Meta ingress before tenant lookup or runtime work. Uvicorn must
  retain `--proxy-headers` because the ALB-terminated HTTPS URL is signature material.

### Lifecycle, security, and operations

- Account closure is two-step: delete Luciel (with export offer) and then close the
  account. Deletion has a 30-day grace and restore returns it to active; pause differs.
- Export links expire after seven days. Session content and summaries over one year
  are deleted. Lead auto-pruning is per-tenant opt-in (`leadRetentionDays`; NULL is
  off). Allowance-reduction notices are real in BOTH halves (§A5): the dashboard
  notice renders on login and the daily `billing_notices` sweep emails each notice
  once per tenant (`notice_deliveries` ledger).
- The audit log is append-only with one-year retention. The audit ownership lock is a
  migration postcondition: run the superuser lock step after `alembic upgrade head`.
- SSRF protection, session-epoch invalidation on close, constant-time comparisons,
  malformed-live-secret failure, and advisory-locked sweeps are security contracts.
- `/healthz` must report the resolved `integrationMode` and degraded bindings loudly.
  A non-empty live-mode degraded list is a delivery/binding problem, not a success.

## Engineering doctrine

1. Trace the request from route/webhook through connection, runtime, persistence, and
   delivery before changing it.
2. Model the live failure in a focused test before or with the fix; mock behavior is
   not proof of live correctness.
3. Keep mock and live explicit. Integration mode must never silently select a fake
   sender in a live path.
4. Prefer explicit state transitions and persisted proof (watermarks, replay IDs,
   grounding scores, destination bindings) to inferred or in-memory state.
5. Read before writing for scheduling changes; report ambiguity or conflicts honestly.
6. Do not weaken RLS, signature checks, replay gates, audit locks, proxy headers, or
   the shared pause gate to satisfy a narrow test.
7. Add a migration for persistence changes, test upgrade/rollback as appropriate, and
   preserve audit-lock operations after the migration.
8. Do not use Stripe usage records. Stripe Billing Meters only; production stays mock
   until the owner-gated live cutover.

## Local test environment

Use the supplied PostgreSQL and virtualenv for this branch’s full suite:

```bash
export PATH="/tmp/vcal/bin:/usr/lib/postgresql/18/bin:$PATH"
export LUCIEL_SUPERUSER_TCP=1
export LUCIEL_PG_HOST=127.0.0.1
export LUCIEL_PG_PORT=55432
export LUCIEL_SUPERUSER=postgres
export PGPASSWORD=postgres
export LUCIEL_INTEGRATION_MODE=mock
pip install -e .
python -m pytest -v
```

For a fresh local environment, create/activate a Python 3.12 virtual environment,
then install `-e .` plus the required test dependencies if the supplied environment
does not already contain them. The expected baseline is about **1327 passed, 0 failed**.
Run focused tests while iterating, then run the full command above before commit.

## Deploy

- Dispatch the named workflow from the intended commit, for example:
  `gh workflow run deploy -f environment=dev -f integration_mode=mock`.
- `environment` is `dev` or `prod`; choose `integration_mode` explicitly (`mock` or
  `live`). Do not treat a deploy command as approval for a real integration.
- Dev is the normal target for verification. Production is only for explicit owner
  approval and uses the protected `prod` environment.
- A production release first creates ephemeral prod-shaped preprod, runs Verify and
  SeedTest as a hard gate, deploys production only after those pass, then tears preprod
  down even after failure.
- Keep deploy-time secrets out of git and do not rotate or activate production
  credentials without the owner’s explicit direction.
- Email deliverability is OUT-OF-BAND ops (no CDK): the SES identity
  `vantagemind.ai` has DKIM enabled AND a custom MAIL FROM `mail.vantagemind.ai`
  (Route53: MX → feedback-smtp.ca-central-1.amazonses.com, TXT SPF include);
  `_dmarc` is `p=quarantine; adkim=s; aspf=r`. Set 2026-08-17 after live sends
  quarantined: strict-SPF DMARC with no custom MAIL FROM fails SPF alignment on
  every SES send. Do not re-strict `aspf` without keeping the MAIL FROM domain.

## Before handing off

- `git status --short` contains only intentional files.
- Every behavior change has an appropriately scoped test and the full suite is green.
- New/changed endpoints update `tests/unit/test_route_registry.py` deliberately.
- Migration changes include the post-migration audit-lock consideration.
- Live channel changes preserve signature verification, proxy headers, pause, and
  honest delivery semantics.
- Report the exact test command/result and commit SHA; do not assert a provider send
  occurred unless the provider response was actually observed.
