/**
 * Per-environment configuration (DEVOPS §4 Environments & Promotion Model).
 *
 * One parameterized config drives dev / staging / prod from the SAME constructs
 * (§5.1: "one parameterized stack/construct per layer instantiated three times").
 * Region is LOCKED to ca-central-1 for every environment of the current offering
 * (Locked Decision 21 / §4.2). Changing region per-offering is a deliberate future
 * action, never a silent default.
 */

export type EnvName = 'dev' | 'staging' | 'prod';

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
  /** Number of AZs the VPC spans. dev keeps cost down; staging/prod mirror prod. */
  readonly maxAzs: number;
  /** NAT gateways: 1 in dev (cost), one-per-AZ in staging/prod (no single point). */
  readonly natGateways: number;
  /** RDS instance class (layer 3 - NOT provisioned until founder dollar sign-off). */
  readonly rdsInstanceClass: string;
  /** Whether alarms/smoke-probe gating are armed (prod only fully armed). */
  readonly alarmsArmed: boolean;
  /**
   * Cheap-idle pause switch (§ pause/resume). When true, EVERY ECS service in the
   * environment (backend control/data plane + frontend web) is set to desiredCount
   * 0 so compute cost drops to zero while the data layer floor remains. Reversible
   * by flipping back to false and redeploying. Defaults false; flip per-deploy with
   * `-c paused=true` (applied in bin/luciel-infra.ts). See infra/PAUSE.md.
   */
  readonly paused: boolean;
}

const CONFIGS: Record<EnvName, EnvConfig> = {
  dev: {
    envName: 'dev',
    multiAz: false,
    maxAzs: 2, // 2 AZs for valid subnet layout, but single-AZ data plane (§4)
    natGateways: 1,
    rdsInstanceClass: 'db.t4g.micro',
    alarmsArmed: false,
    paused: false,
  },
  staging: {
    envName: 'staging',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.t4g.medium',
    alarmsArmed: true,
    paused: false,
  },
  prod: {
    envName: 'prod',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.r6g.large',
    alarmsArmed: true,
    paused: false,
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
 * environment to desiredCount 0. Kept here so dev/staging/prod share one code path.
 */
export function applyContextOverrides(cfg: EnvConfig, paused: boolean): EnvConfig {
  return paused ? { ...cfg, paused: true } : cfg;
}
