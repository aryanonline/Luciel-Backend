/**
 * Per-environment configuration (DEVOPS §4 Environments & Promotion Model).
 *
 * One parameterized config drives dev / prod, plus an ephemeral preprod, from the
 * SAME constructs (§5.1: "one parameterized stack/construct per layer"). The model
 * is now two STANDING environments (dev + prod) and one EPHEMERAL, prod-shaped
 * `preprod` that is stood up on every prod release to run the §10 verification
 * harness as a HARD gate, then torn down (never left running, for cost).
 * Region is LOCKED to ca-central-1 for every environment of the current offering
 * (Locked Decision 21 / §4.2). Changing region per-offering is a deliberate future
 * action, never a silent default.
 */

import * as logs from 'aws-cdk-lib/aws-logs';

export type EnvName = 'dev' | 'preprod' | 'prod';

/** The ONE region for the current offering. No environment may deviate (LD 21, §4.2). */
export const OFFERING_REGION = 'ca-central-1';

/**
 * CloudFront ACM certs MUST live in us-east-1 (a hard AWS requirement) even though
 * the offering region is ca-central-1. This is the ONLY sanctioned deviation: a
 * CloudFront cert is a global-edge control-plane artifact, NOT customer data, so it
 * does not violate residency (LD 21). ALB / regional certs stay in ca-central-1.
 */
export const CLOUDFRONT_CERT_REGION = 'us-east-1';

/**
 * The shared name of the sweep ERROR metric: ComputeStack's log metric filter
 * EMITS it (it owns the sweep log group); ObservabilityStack's alarm READS it.
 * CloudWatch metrics are referenced by name, not construct edge, so this const
 * is the single source that keeps the two stacks pointing at the same metric
 * without a cross-stack prop.
 */
export const SWEEP_ERROR_METRIC = {
  metricNamespace: 'Luciel',
  metricName: 'SweepErrors',
} as const;

/**
 * The registrable domain every environment's hostnames live under. The founder owns
 * vantagemind.ai only — there is no .com (DEPLOY.md / DEVOPS §9). It is also the SES
 * domain identity (one per account, shared by every environment).
 */
export const APEX_DOMAIN = 'vantagemind.ai';

/**
 * The PUBLIC product names — the ones customers, OAuth consoles and embed snippets
 * know. Owner decision D6 (2026-09-14): exactly ONE environment serves them at a
 * time, the one whose `ownsPublicApex` is true. Today that is dev (as a dual-host
 * beside its own dev.* names); a production environment claims them by flipping both
 * flags in one commit (see `hostnames` and the cutover order in deploy.yml).
 */
export const PUBLIC_HOSTNAMES = {
  app: APEX_DOMAIN,
  www: `www.${APEX_DOMAIN}`,
  api: `api.${APEX_DOMAIN}`,
  embed: `embed.${APEX_DOMAIN}`,
} as const;

/**
 * The EXISTING Route 53 hosted zone for vantagemind.ai. Do NOT create a new zone —
 * it already exists; we look it up by these attributes (spec §1).
 */
export const HOSTED_ZONE = {
  id: 'Z0203683JUDTD67YTV93',
  name: APEX_DOMAIN,
} as const;

export interface EnvConfig {
  readonly envName: EnvName;
  /** Multi-AZ everywhere except dev (dev is single-AZ, small, ephemeral - §4 / §7). */
  readonly multiAz: boolean;
  /** Number of AZs the VPC spans. dev keeps cost down; preprod/prod mirror prod. */
  readonly maxAzs: number;
  /** NAT gateways: 1 in dev (cost), one-per-AZ in preprod/prod (no single point). */
  readonly natGateways: number;
  /** RDS instance class (layer 3 - NOT provisioned until founder dollar sign-off). */
  readonly rdsInstanceClass: string;
  /** Whether alarms/smoke-probe gating are armed (prod only fully armed). */
  readonly alarmsArmed: boolean;
  /**
   * 2026-09-05 audit F045: alarms are armed EVERYWHERE now (dev serves the public
   * product), so the cost-bearing telemetry — Container Insights, RDS Performance
   * Insights, keeping the WAF while paused — needs its own switch. True only where
   * production posture is never cost-gated (preprod/prod).
   */
  readonly productionPosture: boolean;
  /**
   * Retention for every log group this app owns. A CloudWatch log group created
   * without one keeps its logs FOREVER and bills for the privilege, which is a cost
   * that only ever grows and that nobody notices until the bill does. It lives here
   * rather than at each call site so the environments can differ on purpose: dev
   * churns and nobody reads a month-old dev log, preprod is torn down within the hour
   * of the release that created it, and prod keeps the 90 days doctrine §5.1 asks for.
   */
  readonly logRetention: logs.RetentionDays;
  /**
   * Days of automatic ElastiCache snapshots to keep. Zero means the cluster takes no
   * automatic backups at all — correct for a cache whose entire contents are LLM-cap
   * counters that rebuild themselves, and the difference between a bounded and an
   * unbounded snapshot bill.
   */
  readonly redisSnapshotRetentionDays: number;
  /**
   * ElastiCache node type. The cache holds LLM-cap counters and abuse ceilings —
   * kilobytes of TTL'd keys — so the smallest Graviton node fits every
   * environment today; it is a knob so prod can grow without a construct edit.
   */
  readonly redisNodeType: string;
  /**
   * Total Redis nodes in the replication group (one primary + replicas).
   * 1 in dev: Redis cannot scale to zero, and a dev cache that dies rebuilds
   * itself. 2 in preprod/prod: a replica in the SECOND AZ with automatic
   * failover — the Multi-AZ replication Architecture §4.4 promises production.
   * Before this knob every environment ran the single dev node (round 7 WP-8):
   * a lost AZ took the abuse ceilings and LLM caps down with it, and the app's
   * fallback is per-task in-memory counters that reset on every deploy (which
   * live /healthz reports as a degraded binding). Preprod mirrors prod so the
   * release rehearsal exercises the failover-capable shape.
   */
  readonly redisNodes: number;
  /**
   * Cheap-idle pause switch (§ pause/resume). When true, EVERY ECS service in the
   * environment (backend control/data plane + frontend web) is set to desiredCount
   * 0 so compute cost drops to zero while the data layer floor remains. Reversible
   * by flipping back to false and redeploying. Defaults false; flip per-deploy with
   * `-c paused=true` (applied in bin/luciel-infra.ts). See infra/CLAUDE.md.
   */
  readonly paused: boolean;
  /**
   * Which Stripe binding the app resolves (`LUCIEL_STRIPE_INTEGRATION_POSTURE`).
   *
   * 'mock' is the deterministic in-process FakeStripeGateway - no keys read, no network
   * possible. 'test' binds the real stripe-python client against the Stripe TEST keys
   * (sk_test_/pk_test_/whsec_) from the platform secret. There is deliberately no
   * 'live' value: binding a production Stripe key is owner-gated and NOT implemented by
   * this seam, so prod stays on 'mock' and /healthz reports the gateway as degraded -
   * which is the honest answer, rather than a posture that implies real billing works.
   *
   * Reporting usage in 'test' posture requires a Stripe Billing Meter with event_name
   * `luciel_payg_blocks` and a price backed by it; the usage-records endpoint the seam
   * used before is removed in API version 2025-03-31 'basil'.
   */
  readonly stripeIntegrationPosture: 'mock' | 'test';
  /**
   * Monthly AWS cost ceiling (USD) for the AWS Budgets alarm (BudgetStack). The one
   * control that catches everything the per-resource ceilings miss — including a
   * failed preprod teardown silently running a prod-shaped stack. Alerts fire at
   * 80% actual, 100% actual, and 100% forecast.
   */
  readonly monthlyBudgetUsd: number;
  /**
   * ECR repository whose SCANNED image this environment's ECS services run
   * (`-c imageTag` selects the tag). Dev runs its own repo; preprod deliberately
   * runs the PROD repo — the release rehearsal must exercise the exact gated
   * artifact prod will run, not a lookalike build of the same commit.
   */
  readonly imageRepoName: string;
  /**
   * Whether the VPC gets interface endpoints (Secrets Manager, ECR, ECR-Docker,
   * CloudWatch Logs, STS). They exist to keep that traffic off the NAT path, but
   * five endpoints x two AZs is ~$73/month of standing cost whether or not a byte
   * flows. FALSE in every environment since round 7 WP-8b: each environment
   * already pays for NAT (one in dev, one per AZ in preprod/prod) and the NAT
   * carries this exact traffic for metered pennies at our volume, so prod was
   * paying for the same reachability twice. The endpoints are not a security
   * control here — the tasks egress through NAT for the LLM/Twilio/Stripe calls
   * anyway. The future option, if Secrets Manager reachability ever needs to
   * survive a NAT outage, is the SECRETS MANAGER endpoint alone (~$15/month for
   * two AZs), not the set of five. The free S3 GATEWAY endpoint stays in every
   * environment (ECR layer pulls ride it).
   */
  readonly vpcInterfaceEndpoints: boolean;
  /**
   * Target-tracking autoscaling of the request-serving services (both backend
   * planes and the web tier; lib/service-scaling.ts — CPU AND memory). Round 7
   * WP-8b: the policy used to hide behind productionPosture, so dev — which serves
   * the public product — pinned one task per plane and a busy hour simply queued.
   * Dev now scales too, inside deliberately small ceilings (1..2) so a runaway
   * cannot become a bill; preprod/prod keep the ceilings they had. The FLOOR is
   * each service's base count (multi-AZ: 2 data-plane tasks, otherwise 1).
   */
  readonly autoscaling: {
    readonly enabled: boolean;
    readonly maxDataPlane: number;
    readonly maxControlPlane: number;
    readonly maxWeb: number;
  };
  /**
   * Whether this environment ALSO serves the public product names
   * (PUBLIC_HOSTNAMES: the apex, www, api, embed) — owner decision D6. EXACTLY ONE
   * environment may own them (test/hostnames.test.ts; `apexOwner()` refuses to synth
   * otherwise): two stacks cannot both hold the same Route 53 record or CloudFront
   * alias, and two environments answering on one name would split a customer's
   * session across two databases. Dev owns them today so nothing goes dark while no
   * production environment exists; prod claims them by flipping BOTH flags in one
   * commit (deploy.yml's prod job refuses to run while prod does not own them).
   * The flip is a HANDOVER, never 'deploy dev first': dev's cleanup would delete
   * the public records and CloudFront alias hours before prod could create them
   * (Phase 3 infra review) — see the runbook note in deploy.yml's refusal step.
   */
  readonly ownsPublicApex: boolean;
  /**
   * RDS snapshot this environment's database is RESTORED from, instead of being
   * created empty (DataStack builds `rds.DatabaseInstanceFromSnapshot` when set).
   * Unset = a fresh, empty database, as every environment had before. Dev names the
   * final snapshot taken when its database was deleted on 2026-11-14 to stop its AWS
   * cost, so standing dev back up brings its data back.
   *
   * ONE-WAY: once an environment has been restored from a snapshot, this field must
   * STAY set, to the same value, for as long as that database lives. DBSnapshotIdentifier
   * is not itself a replacement property (AWS::RDS::DBInstance lists it as "Some
   * interruptions", and CloudFormation lets it be removed after a restore); the damage
   * comes from what this stack does without the field:
   *  - Unset: DataStack switches to `rds.DatabaseInstance`, which adds DBName,
   *    MasterUsername, KmsKeyId and StorageEncrypted (each "Update requires:
   *    Replacement") under the same logical id, so CloudFormation replaces the restored
   *    database with a NEW, EMPTY one.
   *  - Another snapshot: CloudFormation restores a new instance from THAT snapshot.
   * Either way RETAIN orphans the current instance, data and all, outside the stack, and
   * the app is pointed at the new one (test/db-snapshot-restore.test.ts pins the four
   * added properties).
   *
   * The restored storage stays encrypted with the KMS key the snapshot was taken under
   * (dev's SecurityStack CMK); RDS cannot restore without it. Dev's CMK is DESTROY with
   * a 7-day pending window, so a destroyed dev Security stack leaves 7 days to cancel
   * the key's deletion (and re-enable it) before this snapshot becomes unrestorable.
   */
  readonly dbSnapshotIdentifier?: string;
}

const CONFIGS: Record<EnvName, EnvConfig> = {
  dev: {
    envName: 'dev',
    multiAz: false,
    maxAzs: 2, // 2 AZs for valid subnet layout, but single-AZ data plane (§4)
    natGateways: 1,
    rdsInstanceClass: 'db.t4g.micro',
    // Armed (F045): dev serves the public product; an alarm that reaches nobody is
    // refused at synth (ObservabilityStack requires the subscriber).
    alarmsArmed: true,
    productionPosture: false,
    logRetention: logs.RetentionDays.ONE_MONTH,
    redisSnapshotRetentionDays: 0,
    redisNodeType: 'cache.t4g.micro',
    redisNodes: 1, // single node: dev is single-AZ by design (§4)
    paused: false,
    stripeIntegrationPosture: 'test',
    // Dev's measured run-rate (Cost Explorer 2026-09-01..13) is ≈ $195/mo
    // running incl. tax and ≈ $110/mo paused: RDS t4g.micro, one NAT, 2×ALB,
    // Redis, Fargate for two planes plus the sweeps. The previous 150 sat BELOW
    // the running rate, so the 80% alert fired on day ~19 of every month and the
    // ceiling meant "dev is on", not "something changed" — the same
    // alert-blindness the prod entry below describes. 250 restores meaning: 80%
    // (~$200) is the top of a normal running month, 100% means a real change
    // (an un-paused weekend, a runaway NAT bill, a leaked resource).
    monthlyBudgetUsd: 250,
    imageRepoName: 'luciel-backend-dev',
    vpcInterfaceEndpoints: false,
    // Small ceilings on purpose: dev may double under load, never more.
    autoscaling: { enabled: true, maxDataPlane: 2, maxControlPlane: 2, maxWeb: 2 },
    // D6: dev serves the public names as a dual-host until a production environment
    // claims them (flip this and prod's together).
    ownsPublicApex: true,
    // Restore dev's data on its next stand-up. Never unset or change it once dev has
    // been restored (see the field doc: unsetting it replaces the database with an empty
    // one).
    dbSnapshotIdentifier: 'luciel-dev-final-2026-11-14',
  },
  // EPHEMERAL pre-production. NOT a standing environment: it is created at release
  // time by the prod pipeline, runs the §10 verification harness as a HARD gate
  // (any failure blocks the prod deploy), and is ALWAYS torn down afterwards
  // (cdk destroy; ephemeral RDS deleted with NO final snapshot). It mirrors `prod`
  // sizing EXACTLY (multiAz, azCount, NAT-per-AZ, RDS class, prod-like desired
  // counts) so the rehearsal is faithful to production. Must never be left running.
  preprod: {
    envName: 'preprod',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.r6g.large',
    alarmsArmed: true,
    productionPosture: true,
    // The environment itself does not outlive the release that created it, so a week
    // is already longer than any preprod log can be worth reading.
    logRetention: logs.RetentionDays.ONE_WEEK,
    redisSnapshotRetentionDays: 0,
    // Prod-shaped: primary + one replica across the two AZs, automatic failover.
    redisNodeType: 'cache.t4g.micro',
    redisNodes: 2,
    paused: false,
    // Prod-shaped in sizing, but the Stripe rehearsal runs against TEST keys - preprod
    // must never move money, and 'live' is not a value this seam accepts anyway.
    stripeIntegrationPosture: 'test',
    // Prod-shaped burn: this ceiling exists precisely for the failure mode where a
    // teardown fails and the ephemeral stack quietly runs for a month.
    monthlyBudgetUsd: 400,
    // The release rehearsal runs the exact gated artifact prod will run.
    imageRepoName: 'luciel-backend-prod',
    // Mirrors prod (see the field doc): NAT carries the AWS-API traffic.
    vpcInterfaceEndpoints: false,
    // Prod-shaped, so the rehearsal runs the policy prod runs.
    autoscaling: { enabled: true, maxDataPlane: 6, maxControlPlane: 3, maxWeb: 4 },
    // Never: an ephemeral environment holding the public names would take them dark
    // at every teardown. It rehearses on preprod.* / api-preprod.* / embed-preprod.*.
    ownsPublicApex: false,
  },
  prod: {
    envName: 'prod',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.r6g.large',
    alarmsArmed: true,
    productionPosture: true,
    logRetention: logs.RetentionDays.THREE_MONTHS, // doctrine §5.1: 90d app logs
    redisSnapshotRetentionDays: 0,
    // Architecture §4.4: Multi-AZ replication in prod — primary + one replica in
    // the second AZ with automatic failover (~$12/mo for the second t4g.micro).
    redisNodeType: 'cache.t4g.micro',
    redisNodes: 2,
    paused: false,
    // NOT 'test': prod must not bill against a Stripe test account, and the live
    // cutover is owner-gated and unimplemented. 'mock' keeps prod honest - /healthz
    // lists the gateway as degraded rather than implying billing reaches Stripe.
    stripeIntegrationPosture: 'mock',
    // Prod's standing run-rate is ~$750–900/mo before traffic (RDS r6g.large
    // Multi-AZ ≈ $380, Fargate ≈ $180, NAT ≈ $70, ALB ≈ $18, Redis + misc; the
    // interface endpoints (≈ $73) and the second ALB (≈ $18) left in round 7 WP-8b). The previous 400 sat at roughly HALF that,
    // so the alarm would have fired permanently from day one and trained
    // alert-blindness. 1000 restores meaning: 80% (~$800) marks the top of
    // normal, 100% means something real changed.
    monthlyBudgetUsd: 1000,
    imageRepoName: 'luciel-backend-prod',
    // Round 7 WP-8b: NAT (one per AZ) already carries this traffic; paying ~$73/mo
    // for five interface endpoints on top bought the same reachability twice.
    vpcInterfaceEndpoints: false,
    // The ceilings production posture always had (F009); web gains its own.
    autoscaling: { enabled: true, maxDataPlane: 6, maxControlPlane: 3, maxWeb: 4 },
    // Prod's names ARE the public names, but dev holds them until the cutover: flip
    // this to true and dev's to false in the same commit when prod claims them.
    ownsPublicApex: false,
  },
};

export function getEnvConfig(envName: EnvName): EnvConfig {
  const cfg = CONFIGS[envName];
  if (!cfg) {
    throw new Error(`Unknown environment: ${envName}`);
  }
  return cfg;
}

/**
 * Apply per-deploy context overrides to a base config. Currently the single
 * cheap-idle pause switch (`-c paused=true`) which forces every ECS service in the
 * environment to desiredCount 0. Kept here so dev/preprod/prod share one code path.
 */
export function applyContextOverrides(cfg: EnvConfig, paused: boolean): EnvConfig {
  return paused ? { ...cfg, paused: true } : cfg;
}

/**
 * The ONE environment that serves the public names (D6). Throws unless exactly one
 * config owns them and it is not the ephemeral preprod — bin/luciel-infra.ts calls it
 * so a config that breaks the rule cannot even synthesize.
 */
export function apexOwner(): EnvName {
  const owners = (Object.keys(CONFIGS) as EnvName[]).filter((e) => CONFIGS[e].ownsPublicApex);
  if (owners.length !== 1) {
    throw new Error(
      `exactly one environment may own the public apex names; found ${owners.length} ` +
        `(${owners.join(', ') || 'none'}) — flip the old owner and the new one in the same commit`,
    );
  }
  if (owners[0] === 'preprod') {
    throw new Error('preprod is torn down after every release; it may never own the public apex');
  }
  return owners[0];
}

/** Every hostname an environment answers on, by tier — the D6 naming in one place. */
export interface EnvHostnames {
  /** Primary dashboard/marketing host (APP_BASE_URL, the cookie's registrable domain). */
  readonly app: string;
  /** Primary API host (API_BASE_URL, NEXT_PUBLIC_API_BASE_URL, SeedTest, the smoke probe). */
  readonly api: string;
  /** Primary widget CDN host. */
  readonly embed: string;
  /** Every OTHER name the environment answers on (the dual-host), flattened. */
  readonly aliases: readonly string[];
  /** All names per tier, primary first: certificates, records and ALB host rules iterate these. */
  readonly appNames: readonly string[];
  readonly apiNames: readonly string[];
  readonly embedNames: readonly string[];
}

/**
 * The label a non-production environment's names carry: dev.vantagemind.ai,
 * api-dev.vantagemind.ai, embed-dev.vantagemind.ai (owner decision D6). `null` = the
 * environment's own names are the public ones (prod).
 */
const ENV_HOST_LABEL: Record<EnvName, string | null> = {
  dev: 'dev',
  preprod: 'preprod',
  prod: null,
};

/**
 * hostnames(envName) → { app, api, embed, aliases } (owner decision D6, 2026-09-14).
 *
 *   dev     → dev.vantagemind.ai / api-dev.vantagemind.ai / embed-dev.vantagemind.ai,
 *             PLUS — while it owns the public apex — vantagemind.ai, www, api, embed
 *             as aliases (the dual-host that keeps the public names lit until a
 *             production environment claims them);
 *   preprod → preprod.vantagemind.ai / api-preprod.vantagemind.ai / embed-preprod...
 *             (its own names: the ephemeral rehearsal never touches the public ones);
 *   prod    → vantagemind.ai (+ www) / api.vantagemind.ai / embed.vantagemind.ai.
 *
 * Replaces the old flat DOMAINS const, which every stack read unqualified — so dev,
 * preprod and prod all claimed api.vantagemind.ai, and the preprod rehearsal could
 * not even create its records while dev held them.
 */
export function hostnames(envName: EnvName): EnvHostnames {
  return hostnamesFor(envName, getEnvConfig(envName).ownsPublicApex);
}

/**
 * `hostnames` for an explicit ownership — the shape an environment takes on either
 * side of the D6 cutover (dev without the public names, prod with them), so the
 * cutover can be tested before the flags flip.
 */
export function hostnamesFor(envName: EnvName, ownsPublicApex: boolean): EnvHostnames {
  const label = ENV_HOST_LABEL[envName];
  if (label === null) {
    // The environment's own names are the public ones.
    return {
      app: PUBLIC_HOSTNAMES.app,
      api: PUBLIC_HOSTNAMES.api,
      embed: PUBLIC_HOSTNAMES.embed,
      aliases: [PUBLIC_HOSTNAMES.www],
      appNames: [PUBLIC_HOSTNAMES.app, PUBLIC_HOSTNAMES.www],
      apiNames: [PUBLIC_HOSTNAMES.api],
      embedNames: [PUBLIC_HOSTNAMES.embed],
    };
  }
  const own = {
    app: `${label}.${APEX_DOMAIN}`,
    api: `api-${label}.${APEX_DOMAIN}`,
    embed: `embed-${label}.${APEX_DOMAIN}`,
  };
  const dual = ownsPublicApex;
  const appAliases = dual ? [PUBLIC_HOSTNAMES.app, PUBLIC_HOSTNAMES.www] : [];
  const apiAliases = dual ? [PUBLIC_HOSTNAMES.api] : [];
  const embedAliases = dual ? [PUBLIC_HOSTNAMES.embed] : [];
  return {
    ...own,
    aliases: [...appAliases, ...apiAliases, ...embedAliases],
    appNames: [own.app, ...appAliases],
    apiNames: [own.api, ...apiAliases],
    embedNames: [own.embed, ...embedAliases],
  };
}

/**
 * True when an environment's own names are the public ones but it does not own them
 * (prod before the D6 cutover). Its stacks would collide with the owner's records and
 * CloudFront alias, and its smoke probe would be answered by the OWNER's API — a
 * green probe proving the wrong environment. deploy.yml refuses such a deploy.
 */
export function servesUnownedPublicNames(envName: EnvName): boolean {
  return ENV_HOST_LABEL[envName] === null && !getEnvConfig(envName).ownsPublicApex;
}

/** `https://` origins for the primary app and API hosts (the per-environment base URLs). */
export function baseUrls(envName: EnvName): { readonly app: string; readonly api: string } {
  const h = hostnames(envName);
  return { app: `https://${h.app}`, api: `https://${h.api}` };
}

/**
 * Stable construct id for a hostname's Route 53 records. The four public names keep
 * the ids their records have always had (ApexA, WwwA, ApiA, WidgetA …) so the
 * records already deployed are UPDATED in place — a new id would make CloudFormation
 * create a second record set for the same name (which fails: it already exists)
 * before deleting the old one. Every other name derives its id from its label:
 * api-dev.vantagemind.ai → ApiDev.
 */
export function recordIdFor(hostname: string): string {
  if (hostname === PUBLIC_HOSTNAMES.app) return 'Apex';
  if (hostname === PUBLIC_HOSTNAMES.embed) return 'Widget';
  const suffix = `.${APEX_DOMAIN}`;
  if (!hostname.endsWith(suffix)) {
    throw new Error(`${hostname} is not under ${APEX_DOMAIN}`);
  }
  const label = hostname.slice(0, -suffix.length);
  return label
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join('');
}
