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
 * Founder-confirmed public domains (DEPLOY.md / DEVOPS §9). The founder owns
 * vantagemind.ai only — there is no .com.
 */
export const DOMAINS = {
  apex: 'vantagemind.ai',
  www: 'www.vantagemind.ai',
  api: 'api.vantagemind.ai',
  widget: 'embed.vantagemind.ai',
} as const;

/**
 * The EXISTING Route 53 hosted zone for vantagemind.ai. Do NOT create a new zone —
 * it already exists; we look it up by these attributes (spec §1).
 */
export const HOSTED_ZONE = {
  id: 'Z0203683JUDTD67YTV93',
  name: 'vantagemind.ai',
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
   * five endpoints x two AZs is ~$73/month of standing cost — the single largest
   * line item in an idle dev environment, which already has a NAT gateway that
   * serves the same traffic for pennies at dev volume. So dev goes without and
   * rides NAT; preprod/prod keep the endpoints for throughput and blast-radius
   * isolation. The free S3 GATEWAY endpoint stays in every environment.
   */
  readonly vpcInterfaceEndpoints: boolean;
}

const CONFIGS: Record<EnvName, EnvConfig> = {
  dev: {
    envName: 'dev',
    multiAz: false,
    maxAzs: 2, // 2 AZs for valid subnet layout, but single-AZ data plane (§4)
    natGateways: 1,
    rdsInstanceClass: 'db.t4g.micro',
    alarmsArmed: false,
    logRetention: logs.RetentionDays.ONE_MONTH,
    redisSnapshotRetentionDays: 0,
    paused: false,
    stripeIntegrationPosture: 'test',
    monthlyBudgetUsd: 150,
    imageRepoName: 'luciel-backend-dev',
    vpcInterfaceEndpoints: false,
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
    // The environment itself does not outlive the release that created it, so a week
    // is already longer than any preprod log can be worth reading.
    logRetention: logs.RetentionDays.ONE_WEEK,
    redisSnapshotRetentionDays: 0,
    paused: false,
    // Prod-shaped in sizing, but the Stripe rehearsal runs against TEST keys - preprod
    // must never move money, and 'live' is not a value this seam accepts anyway.
    stripeIntegrationPosture: 'test',
    // Prod-shaped burn: this ceiling exists precisely for the failure mode where a
    // teardown fails and the ephemeral stack quietly runs for a month.
    monthlyBudgetUsd: 400,
    // The release rehearsal runs the exact gated artifact prod will run.
    imageRepoName: 'luciel-backend-prod',
    vpcInterfaceEndpoints: true,
  },
  prod: {
    envName: 'prod',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.r6g.large',
    alarmsArmed: true,
    logRetention: logs.RetentionDays.THREE_MONTHS, // doctrine §5.1: 90d app logs
    redisSnapshotRetentionDays: 0,
    paused: false,
    // NOT 'test': prod must not bill against a Stripe test account, and the live
    // cutover is owner-gated and unimplemented. 'mock' keeps prod honest - /healthz
    // lists the gateway as degraded rather than implying billing reaches Stripe.
    stripeIntegrationPosture: 'mock',
    monthlyBudgetUsd: 400,
    imageRepoName: 'luciel-backend-prod',
    vpcInterfaceEndpoints: true,
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
