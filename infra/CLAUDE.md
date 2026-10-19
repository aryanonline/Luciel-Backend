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
  (preprod/prod) gates cost-bearing telemetry and the paused-WAF exception;
  autoscaling has its own `autoscaling` knob (CPU + memory target tracking on
  both planes and the web tier; dev 1..2, prod as before — round 7 WP-8b);
  dev data stores are RETAIN + deletion-protected (only preprod is disposable);
  NO environment has VPC interface endpoints (env-gated, ~$73/mo — NAT already
  carries that traffic; round 7 WP-8b).
- Hostnames are per environment (owner decision D6, round 7 WP-8b): every name comes
  from `hostnames(envName)` in env-config.ts — dev `dev.` / `api-dev.` /
  `embed-dev.vantagemind.ai`, preprod the same with `preprod`, prod the public
  names. EXACTLY ONE environment has `ownsPublicApex` (dev today, serving the apex,
  www, api and embed names as a dual-host); `apexOwner()` refuses to synth otherwise
  and deploy.yml refuses a prod deploy while prod does not own them. Never hard-code a
  hostname in a stack, a workflow or a script — `npx ts-node bin/hostnames.ts <env>`
  prints them. The public records keep their original logical ids (`recordIdFor`).
- ONE ALB per environment (D7): the web tier's target group hangs on ComputeStack's
  listener behind the app-host rule (WebStack owns that rule and the SG rules); the
  api hosts carry the control-plane path rules (re-grouped to the ALB's five
  condition values per rule) and a data-plane catch-all; the data plane stays the
  default action. The listener's ONE certificate lives in ComputeStack beside it (a
  name change replaces it in-stack); the CloudFront cert is the `*.vantagemind.ai`
  wildcard (WidgetCertStack, us-east-1) so no ownership flip touches it.
- Two-plane ALB routing (2026-09-05): `CONTROL_PLANE_PATH_PATTERNS` forward admin
  authority to the control-plane service; the data plane is the default action.
- Dev AND prod deploys (round 7 migrations review / WP-8b): apply the Pipeline stack's
  grants first, record the serving task definitions, MIGRATE WITH THE NEW IMAGE BEFORE
  THE ROLL (`scripts/migrate-before-roll.sh`: a registered revision of the current
  MigrationTask with the image swapped, deregistered afterwards), deploy, re-run the
  migration as a no-op safety, then `scripts/smoke_probe.sh <env>` + the §10 verify
  task (+ on dev the strict D10 SeedTest gate when live) — rolling the SERVICES back
  if any post-roll step fails. Migrations stay expand-first (old code runs on the new
  schema for the length of the roll). The prod job refuses to run while prod does not
  own the public names (D6).
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
