# Infrastructure — CLAUDE.md

## Purpose
Defines CDK stacks and deployment posture for compute, data, networking, edge/widget, security, observability, costs, and releases.

## Key files
`bin/luciel-infra.ts` — CDK application entrypoint.
`lib/compute-stack.ts`, `data-stack.ts`, and `security-stack.ts` — runtime/data/security topology.
`lib/env-config.ts` and `lib/observability-stack.ts` — environment and operational signals.
`test/stripe-posture.test.ts` — deployment posture guard.

## Contracts this module owns
- ECS runs the SCANNED artifact: `-c imageTag=<sha>` points Compute at the pipeline's
  gated ECR image (`imageRepoName` in env-config; preprod runs the PROD repo — the
  rehearsal uses the release artifact). Never reintroduce a deploy-time asset build.
- The CVE gate is `scripts/check_image_scan.py` + `.security/cve-allowlist.json`
  (governed, expiring exceptions; fails CLOSED on scan unavailability). Pause runs
  skip the build and reuse the deployed tag — taking an env down never needs a build.
- Fifteen sweep schedules (incl. the daily `model_currency` watchdog; `mailbox_poll`
  is an HOURLY backstop since 2026-09-05 — the data plane polls in-process every
  5 min via `LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS`); ERROR-level sweep log lines
  feed the `SweepErrors` metric filter (ComputeStack); alarms are armed in EVERY
  environment and ObservabilityStack refuses to synth without `-c alarmEmail`/
  `-c budgetAlertEmail`; the app's EMF metrics (namespace `Luciel`) and ECS
  task-state rules for failed sweep tasks alarm too; `productionPosture`
  (preprod/prod) gates cost-bearing telemetry, the paused-WAF exception and
  autoscaling; dev data stores are RETAIN + deletion-protected (only preprod is
  disposable); NO environment has VPC interface endpoints (env-gated, ~$73/mo —
  NAT already carries that traffic; round 7 WP-8b).
- Two-plane ALB routing (2026-09-05): `CONTROL_PLANE_PATH_PATTERNS` forward admin
  authority to the control-plane service; the data plane is the default action.
- Dev deploys run `scripts/smoke_probe.sh` + the §10 verify task after migrating and
  roll the app services back to the recorded task definitions on failure.
- Keep integration posture explicit and `/healthz`-visible; prod Stripe stays mock before owner-gated live cutover.
- Preserve cost ceilings: logs 30d dev/90d prod, RDS backups 7d, ECR/bucket expiry, zero Redis snapshots, the transcript-archive bucket's 365d backstop, and the per-env AWS Budgets monthly ceiling (BudgetStack).
- Deploy behind the ALB with backend proxy headers intact and per-tenant Secrets Manager `vm/` access.
- The sweep task carries the same cold-storage bucket names as the app planes. Every
  archive/retention/cascade operation on S3 runs there, and the app's bucket settings
  have placeholder defaults, so an un-injected sweep addresses a phantom bucket instead
  of failing (`test/sweep-cold-storage.test.ts`).

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
