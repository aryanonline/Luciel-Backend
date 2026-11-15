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
- Sixteen sweep schedules (incl. the daily `model_currency` watchdog; `mailbox_poll`
  is an HOURLY backstop since 2026-09-05 — the data plane polls in-process every
  5 min via `LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS`); ERROR-level sweep log lines
  feed the `SweepErrors` metric filter (ComputeStack); alarms are armed in EVERY
  environment and ObservabilityStack refuses to synth without `-c alarmEmail`/
  `-c budgetAlertEmail`; the app's EMF metrics (namespace `Luciel`) and ECS
  task-state rules for failed sweep tasks alarm too; both publish to the Alarms topic,
  whose policy must name BOTH principals, because any topic policy replaces SNS's
  implicit default (`test/alarm-topic-policy.test.ts`; from 2026-09-06 no alarm action
  could publish); `productionPosture`
  (preprod/prod) gates cost-bearing telemetry and the paused-WAF exception;
  autoscaling has its own `autoscaling` knob (CPU + memory target tracking on
  both planes and the web tier; dev 1..2, prod as before — round 7 WP-8b);
  dev data stores are RETAIN + deletion-protected (only preprod is disposable);
  NO environment has VPC interface endpoints (env-gated, ~$73/mo — NAT already
  carries that traffic; round 7 WP-8b).
- Hostnames are per environment (owner decision D6, round 7 WP-8b): every name comes
  from `hostnames(envName)` in env-config.ts — dev `dev.` / `api-dev.` /
  `embed-dev.vantagemind.ai`, preprod the same with `preprod`, prod the public
  names. EXACTLY ONE environment has `ownsPublicApex` (dev, which serves the apex,
  www, api and embed names as a dual-host when it runs; dev has been paused since
  2026-11-14, so none of these names answers now); `apexOwner()` refuses to synth
  otherwise and deploy.yml refuses a prod deploy while prod does not own them. Never hard-code a
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
- HTTPS only: wherever the HTTPS listener exists, :80's default action is a permanent
  redirect and it has no forward rules, on any host. Only a synth with no domain keeps
  :80 forwarding. `scripts/smoke_probe.sh` proves the redirect, and the 404s on
  `/openapi.json` and `/docs`, after each deploy.
- ECS services copy their cost tags onto their tasks (`SERVICE_TASK_TAGGING` in
  `lib/cost-tags.ts`), so a tag-filtered budget can see Fargate once the owner activates
  the tags. Scheduled sweep tasks stay untagged on purpose.
- Dev AND prod deploys (round 7 migrations review / WP-8b): apply the Pipeline stack's
  grants first, record the serving task definitions, MIGRATE WITH THE NEW IMAGE BEFORE
  THE ROLL (`scripts/migrate-before-roll.sh`: a registered revision of the current
  MigrationTask with the image swapped, deregistered afterwards), deploy, re-run the
  migration as a no-op safety, then `scripts/smoke_probe.sh <env>` (health, both planes,
  HTTPS-only edge) + the §10 verify
  task (+ on dev the strict D10 SeedTest gate when live) — rolling the SERVICES back
  if the deploy or any post-roll step fails. The rollback is `scripts/rollback_services.py`
  (Phase 3 infra review): the Compute cleanup DEREGISTERS the previous revisions and
  ECS refuses to point a service at an INACTIVE one, so the script re-registers an
  exact copy first, and points the `Luciel-<env>-sweep-*` schedules (recorded beside
  the services) back too. A live deploy then proves each SES topic kept a CONFIRMED
  https subscriber (`scripts/check_sns_subscriptions.sh`; SNS replaces a subscription
  whose endpoint changes, so the subscriptions name the api origin that does not move
  with the dual-host step). Migrations stay expand-first (old code runs on the new
  schema for the length of the roll). The prod job refuses to run while prod does not
  own the public names (D6) — and the cutover is a HANDOVER of the records and the
  CloudFront alias, never 'deploy dev first' (which deletes them hours before prod
  can create them).
- Keep integration posture explicit and `/healthz`-visible; prod Stripe stays mock before owner-gated live cutover.
- Preserve cost ceilings: logs 30d dev/90d prod, RDS backups 7d, ECR/bucket expiry, zero Redis snapshots, the transcript-archive bucket's 365d backstop, and the per-env AWS Budgets monthly ceiling (BudgetStack).
- Deploy behind the ALB with backend proxy headers intact and per-tenant Secrets Manager `vm/` access.
- The sweep task carries the same cold-storage bucket names as the app planes. Every
  archive/retention/cascade operation on S3 runs there, and the app's bucket settings
  have placeholder defaults, so an un-injected sweep addresses a phantom bucket instead
  of failing (`test/sweep-cold-storage.test.ts`).
- An environment that names `dbSnapshotIdentifier` (env-config.ts) RESTORES its database
  with `rds.DatabaseInstanceFromSnapshot` under the same construct id, with a new
  generated master secret; dev names `luciel-dev-final-2026-11-14`. One-way: never unset
  or change the field while that database lives. Unset, DataStack builds
  `rds.DatabaseInstance`, which adds DBName, MasterUsername, KmsKeyId and StorageEncrypted
  (each "Update requires: Replacement") under the same logical id, so CloudFormation
  replaces the restored database with an EMPTY one; another snapshot restores a new
  instance from that snapshot. Either way RETAIN orphans the current instance.
  DBSnapshotIdentifier itself is not a replacement property. The snapshot is encrypted
  with dev's CMK (in the kept `Luciel-dev-Security` stack), which must still exist to
  restore (`test/db-snapshot-restore.test.ts`).

## Testing pointers
- `infra/test/*.test.ts`, especially stripe posture, cost retention, tenant secrets, and sweep schedules.
- GitHub workflow verification plus backend health and integration tests.

## Gotchas from the 2026-08-06 truth delta
- Production release uses ephemeral prod-shaped preprod plus Verify and SeedTest hard gate, then always tears preprod down. `preprod-standup` applies `Luciel-prod-Pipeline` first: the strict gate reads its logs with that role, and `prod-deploy` cannot run until the gate passes.
- Pause sets services to zero intentionally; it is a reversible cost control, not account deletion or a runtime bypass.
- The 2026-11-14 pause of dev went further than that switch: most dev stacks were deleted, and the
  Security and Pipeline stacks, the database snapshot and an S3 archive of the data buckets were kept.
  What was kept and the restore order are in `docs/operations/teardown-and-restore.md`.

## npm overrides (`package.json`)
- `glob` 14 everywhere and `minimatch` 11 for test-exclude keep jest's toolchain free of
  minimatch below 11.0.1 (GHSA-g3hv-75wg-m249; the maintainer ships no 9.x/10.x fix,
  isaacs/minimatch#333). Override glob, never minimatch alone: glob 10 then matches no
  wildcard pattern and raises no error, and glob's published entry point carries its own
  copy of minimatch inside, which no override reaches. That copy is minimatch 10 in glob
  13 and still unfixed in glob 14.0.0 to 14.0.2, so the floor is glob 14.0.3 (past glob's
  own GHSA-3g3m-2fjr-6pw4 too, fixed in 14.0.2).
- aws-cdk-lib bundles its own minimatch (`inBundle` in the lockfile). Overrides cannot
  reach a bundled package and GitHub's dependency graph counts it, so the alert stays open
  until an aws-cdk-lib release carries aws/aws-cdk#39595 (minimatch 11; merged to main
  2026-11-13, not in 2.277.0).
- `test/dependency-overrides.test.ts` pins this, because plain `npx jest` never loads
  glob, minimatch or test-exclude: the lockfile holds no glob below 14.0.3 and no
  minimatch below 11.0.1 outside aws-cdk-lib's bundle, and the glob and test-exclude
  copies jest loads still find files and carry the fix.
- Drop the overrides once upstream ranges make them no-ops (jestjs/jest#16530 for glob 14;
  istanbuljs/test-exclude#79 plus a babel-plugin-istanbul release on it): without them,
  that test must still pass, so `npm ls glob minimatch` shows only glob 14.0.3+ and,
  outside aws-cdk-lib, minimatch 11.0.1+.

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
