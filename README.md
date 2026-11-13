# Luciel — Backend

Luciel is an AI assistant that a business runs on its own knowledge. This service is
everything behind it: the API for the owner dashboard, the public chat API used by the
website widget, the webhooks for other customer channels, knowledge ingestion, grounded
answering with honest escalation, billing, scheduled lifecycle jobs, and the AWS
infrastructure that runs it.

One account has one Luciel and one login. The business connects its own accounts
(mailbox, Twilio number, Meta pages, knowledge sources), and Luciel answers across those
channels from that knowledge.

## How a customer message is handled

1. **Ingress.** A message arrives from the widget or a channel webhook. Twilio, SES and
   Meta requests are signature-checked before any tenant lookup or AI work.
2. **Tenant context.** The request is bound to one tenant, and PostgreSQL row-level
   security keeps every query inside it.
3. **Runtime.** The orchestrator resolves the session, applies safety checks, retrieves
   evidence from the tenant's knowledge (vector and graph retrieval) and drafts an answer.
4. **Grounding gate.** A grounding score is stored with every answer. Low grounding *or*
   low confidence (floor 0.50) leads to a refusal or a hand-off to the team, never a guess.
5. **Delivery.** The reply goes out through the channel's own provider. Delivery fails
   closed: no faked sends, provider IDs or hand-offs.
6. **Afterwards.** Sessions end deterministically and are summarized, usage is metered
   for billing, and audit events are appended.

## Tech stack

- **API:** Python 3.12, FastAPI, Uvicorn, Pydantic
- **Data:** PostgreSQL with pgvector, SQLAlchemy 2 (async, asyncpg), Alembic migrations, Redis
- **AI:** Anthropic Claude as the primary model with an OpenAI fallback; OpenAI
  `text-embedding-3-small` embeddings; vector plus knowledge-graph retrieval
- **Knowledge sources:** file uploads (PDF, Word), Google Drive, Notion, website crawl,
  HubSpot and Salesforce
- **Channels:** website widget, email from the business's own Outlook mailbox, SMS and
  voice on the business's own Twilio number, WhatsApp and Messenger (Meta), Instagram
- **Tools:** calendar booking (including Calendly), CRM and record lookups through a
  typed tool broker
- **Billing:** Stripe Billing Meters, pay-as-you-go
- **Infrastructure:** AWS (ECS Fargate, RDS, ElastiCache, S3, CloudFront, SES), defined
  with the AWS CDK in TypeScript under [`infra/`](infra)

## Repository layout

| Path | What lives there |
| --- | --- |
| `app/main.py` | FastAPI app factory, plane-aware CORS, `/healthz`, router wiring |
| `app/api/` | Versioned HTTP routes and the control-plane / data-plane boundary |
| `app/auth/` | Accounts, passwords and tokens, CAPTCHA |
| `app/runtime/` | The AI loop: sessions, safety, retrieval, grounding, hand-off, model providers, voice |
| `app/knowledge/` | Ingestion, sync and scope, chunking, embeddings, graph, retrieval inputs |
| `app/channels/` | Widget, email, SMS, voice and Meta ingress, plus outbound delivery |
| `app/connections/` | Connection lifecycle for the customer's own providers: OAuth, health, secrets |
| `app/tools/` | Tool authorization, catalog, broker and result contract |
| `app/billing/` | Metering, Stripe gateway and webhooks, dunning, billing notices |
| `app/lifecycle/` | Pause, delete with a 30-day restore window, close, exports, retention |
| `app/jobs/` | Scheduled sweeps: lifecycle, retention, reconciliation, deferred health checks |
| `app/analytics/`, `app/audit/`, `app/notifications/`, `app/policy/`, `app/employee/` | Owner analytics, append-only audit log, notifications, entitlements, status and morning brief |
| `app/models/`, `app/schemas_api/`, `app/db/`, `app/migrations/` | Persistence models, API schemas, database session and RLS context, Alembic history |
| `infra/` | CDK stacks: networking, data, compute, web, widget CDN, DNS, edge, security, observability, pipeline, budgets |
| `scripts/` | Database bootstrap, migration, verification, sweep and deploy helpers |
| `tests/` | Unit, integration, contract and tenant-isolation suites |

Each module has its own `CLAUDE.md` with its key files, the rules it owns, its tests
and known pitfalls. Read it before changing that module.

## Local development

Requirements: Python 3.12, Docker (for PostgreSQL with pgvector, and Redis).

```bash
docker compose up -d                # PostgreSQL 17 + pgvector, and Redis
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                # then fill in the values
```

`.env.example` lists every setting. Keep `LUCIEL_INTEGRATION_MODE=mock` locally. Live
mode talks to real providers and must be chosen on purpose.

Database setup follows the same three steps as production (`scripts/migrate.sh`):

1. `scripts/bootstrap_roles.sql`, as a superuser: creates the application role and the audit-log owner
2. `alembic upgrade head`, as the application role: schema, row-level security and grants
3. `scripts/lock_audit_log.sql`, as a superuser: makes the audit log append-only

Then start the API:

```bash
uvicorn app.main:app --reload --port 8000
```

## Tests

```bash
export LUCIEL_INTEGRATION_MODE=mock
export LUCIEL_PG_HOST=127.0.0.1 LUCIEL_PG_PORT=5432   # point at a PostgreSQL with pgvector
python -m pytest
```

The suite has about 1,300 tests. Markers separate the tenant-isolation suite, which can't
be overridden in CI, and the frontend wire-contract tests. CI (`.github/workflows/ci.yml`)
also runs ruff, strict mypy, bandit, pip-audit and detect-secrets, applies and rolls back
the migrations, and checks the audit-log lock. The deploy workflow scans the container
image for CVEs against a dated allowlist whose waivers expire.

## Deployment

Deployments run only by manual dispatch of `.github/workflows/deploy.yml`, choosing
`environment` (`dev` or `prod`) and `integration_mode` explicitly. A guard job refuses
any branch other than the release branch, `main`, and nothing deploys on push. Production
releases first stand up a temporary production-shaped environment, run verification
and seed tests against it as a hard gate, and remove it afterwards. Migrations run as a
one-off task inside the VPC before services roll.

Secrets never go in this repository. Per-tenant provider credentials live in AWS
Secrets Manager, and deploy-time secrets come from the pipeline.

## Working in this codebase

Start with [`CLAUDE.md`](CLAUDE.md). It lists the rules that must not break (tenant
isolation, the grounding gate, honest delivery, billing metering and webhook replay
protection), the testing doctrine and the deploy procedure. Product and business
documents are kept outside this repository.
