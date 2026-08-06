# Infrastructure — CLAUDE.md

## Purpose
Defines CDK stacks and deployment posture for compute, data, networking, edge/widget, security, observability, costs, and releases.

## Key files
`bin/luciel-infra.ts` — CDK application entrypoint.
`lib/compute-stack.ts`, `data-stack.ts`, and `security-stack.ts` — runtime/data/security topology.
`lib/env-config.ts` and `lib/observability-stack.ts` — environment and operational signals.
`test/stripe-posture.test.ts` — deployment posture guard.

## Contracts this module owns
- Keep integration posture explicit and `/healthz`-visible; prod Stripe stays mock before owner-gated live cutover.
- Preserve cost ceilings: logs 30d dev/90d prod, RDS backups 7d, ECR/bucket expiry, and zero Redis snapshots.
- Deploy behind the ALB with backend proxy headers intact and per-tenant Secrets Manager `vm/` access.

## Testing pointers
- `infra/test/*.test.ts`, especially stripe posture, cost retention, tenant secrets, and sweep schedules.
- GitHub workflow verification plus backend health and integration tests.

## Gotchas from the 2026-08-06 truth delta
- Production release uses ephemeral prod-shaped preprod plus Verify and SeedTest hard gate, then always tears preprod down.
- Pause sets services to zero intentionally; it is a reversible cost control, not account deletion or a runtime bypass.

## Change checklist
- Read the root `CLAUDE.md` and trace callers and dependents before editing.
- Add or update a test that reproduces the real condition being changed.
- Keep tenant, live/mock, and externally visible status behavior explicit.
- If persistence, routes, or deployment posture changes, update the related migration, registry, or infrastructure test deliberately.
- Do not replace an error/unknown state with fabricated success.

## Review prompt
Before merge, ask whether this change could hide an unconfigured, failed,
or tenant-crossing condition behind a successful-looking result. If so,
make that state explicit and test it.
