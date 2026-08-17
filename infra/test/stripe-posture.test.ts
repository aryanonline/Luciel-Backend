/**
 * Which Stripe binding each environment resolves (`LUCIEL_STRIPE_INTEGRATION_POSTURE`).
 *
 * This is a money property expressed entirely in configuration, which is what makes it
 * worth pinning: a container that silently falls back to the posture default resolves
 * FakeStripeGateway, and a fake gateway does not fail — it accepts every report and
 * advances the billing watermark, so usage is marked billed that Stripe never received.
 * Nothing in a deployed environment would look wrong until an invoice was missing.
 *
 * The other half is the prod value. 'live' is not a posture this seam accepts (binding a
 * production Stripe key is owner-gated and unimplemented), so prod runs 'mock' and
 * /healthz reports the gateway as degraded. A future edit flipping prod to 'test' would
 * point production billing at a Stripe TEST account, which fails here rather than in
 * front of a customer.
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function computeTemplate(envName: EnvName) {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  const compute = new ComputeStack(app, 'Compute', {
    env: ENV,
    envConfig,
    vpc: net.vpc,
    cmkArn: sec.cmk.keyArn,
    transcriptArchiveBucket: data.transcriptArchiveBucket,
    exportBucket: data.exportBucket, voiceAudioBucket: data.voiceAudioBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
  });
  return Template.fromStack(compute);
}

/** The posture value on every container that carries one, keyed by container name. */
function posturesByContainer(template: Template): Record<string, string | undefined> {
  const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
  const out: Record<string, string | undefined> = {};
  for (const res of Object.values(taskDefs)) {
    const containers = (res as { Properties: { ContainerDefinitions: unknown[] } }).Properties
      .ContainerDefinitions as {
      Name: string;
      Environment?: { Name: string; Value: string }[];
    }[];
    for (const c of containers) {
      const entry = (c.Environment ?? []).find(
        (e) => e.Name === 'LUCIEL_STRIPE_INTEGRATION_POSTURE',
      );
      out[c.Name] = entry?.Value;
    }
  }
  return out;
}

describe('Stripe integration posture is explicit on every container that bills', () => {
  test('dev and preprod run the real gateway against Stripe TEST keys', () => {
    for (const envName of ['dev', 'preprod'] as const) {
      const postures = posturesByContainer(computeTemplate(envName));
      // Both app planes: the data plane reports usage on the conversation path, the
      // control plane serves the billing webhook and card routes.
      expect(postures['data-planeContainer']).toBe('test');
      expect(postures['control-planeContainer']).toBe('test');
    }
  });

  test('prod stays on mock — the live cutover is owner-gated and unimplemented', () => {
    const postures = posturesByContainer(computeTemplate('prod'));
    expect(postures['data-planeContainer']).toBe('mock');
    expect(postures['control-planeContainer']).toBe('mock');
  });

  test('the sweep task matches the app planes', () => {
    // usage_reconcile posts meter events and advances the watermark. A sweep resolving
    // a different gateway than the app would mark blocks reported that Stripe never got.
    for (const envName of ['dev', 'preprod', 'prod'] as const) {
      const postures = posturesByContainer(computeTemplate(envName));
      expect(postures['SweepContainer']).toBe(getEnvConfig(envName).stripeIntegrationPosture);
    }
  });
});
