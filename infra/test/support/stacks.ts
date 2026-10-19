/**
 * Shared test wiring for the stacks that depend on one another the way
 * bin/luciel-infra.ts wires them (round 7 WP-8b): the web tier now hangs on the
 * ComputeStack ALB, so a WebStack cannot be built without a ComputeStack beside it.
 */
import { App } from 'aws-cdk-lib';
import { NetworkingStack } from '../../lib/networking-stack';
import { SecurityStack } from '../../lib/security-stack';
import { DataStack } from '../../lib/data-stack';
import { ComputeStack } from '../../lib/compute-stack';
import { WebStack } from '../../lib/web-stack';
import { applyContextOverrides, EnvName, getEnvConfig, OFFERING_REGION } from '../../lib/env-config';

export const ENV = { account: '111111111111', region: OFFERING_REGION };

export interface BuildOptions {
  readonly paused?: boolean;
  /** Turn on the hostnames' certificate, HTTPS listener and records (as bin does). */
  readonly publicDns?: boolean;
}

export function buildCompute(envName: EnvName, opts: BuildOptions = {}) {
  const app = new App();
  const envConfig = applyContextOverrides(getEnvConfig(envName), opts.paused ?? false);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  const compute = new ComputeStack(app, 'Compute', {
    env: ENV,
    envConfig,
    vpc: net.vpc,
    cmkArn: sec.cmk.keyArn,
    transcriptArchiveBucket: data.transcriptArchiveBucket,
    exportBucket: data.exportBucket,
    voiceAudioBucket: data.voiceAudioBucket,
    knowledgeOriginalsBucket: data.knowledgeOriginalsBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
    publicDns: opts.publicDns,
  });
  return { app, envConfig, net, sec, data, compute };
}

export function buildComputeAndWeb(envName: EnvName, opts: BuildOptions = {}) {
  const built = buildCompute(envName, opts);
  const web = new WebStack(built.app, 'Web', {
    env: ENV,
    envConfig: built.envConfig,
    vpc: built.net.vpc,
    ingress: built.compute.sharedIngress,
    publicDns: opts.publicDns,
  });
  return { ...built, web };
}
