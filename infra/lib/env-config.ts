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
}

const CONFIGS: Record<EnvName, EnvConfig> = {
  dev: {
    envName: 'dev',
    multiAz: false,
    maxAzs: 2, // 2 AZs for valid subnet layout, but single-AZ data plane (§4)
    natGateways: 1,
    rdsInstanceClass: 'db.t4g.micro',
    alarmsArmed: false,
  },
  staging: {
    envName: 'staging',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.t4g.medium',
    alarmsArmed: true,
  },
  prod: {
    envName: 'prod',
    multiAz: true,
    maxAzs: 2,
    natGateways: 2,
    rdsInstanceClass: 'db.r6g.large',
    alarmsArmed: true,
  },
};

export function getEnvConfig(envName: EnvName): EnvConfig {
  const cfg = CONFIGS[envName];
  if (!cfg) {
    throw new Error(`Unknown environment: ${envName}`);
  }
  return cfg;
}
