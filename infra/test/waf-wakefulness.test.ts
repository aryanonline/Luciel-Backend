/**
 * WAF follows the environment's wakefulness in dev; production posture never does.
 *
 * A paused environment's ALB serves nothing but 503s — its firewall protects
 * nothing while billing hourly (measured ~$20+/month). The pause/unpause pipeline
 * deploys the Edge stack on every run, so gating the ACL on the paused flag means
 * it is torn down at pause and re-created the moment the environment wakes: an
 * UNPAUSED dev (live demos, real keys behind the ALB) is always protected, and an
 * idle one stops paying for theater. Environments with armed alarms (preprod/prod)
 * keep the WAF unconditionally — production posture is never cost-gated.
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EdgeStack } from '../lib/edge-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function edgeTemplate(envName: EnvName, paused: boolean, withWebAlb = false) {
  const app = new App();
  const envConfig = { ...getEnvConfig(envName), paused };
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
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
  });
  // Stand-in for WebStack's dashboard/marketing ALB (building the real
  // WebStack here would drag in certs + hosted zones the WAF doesn't care
  // about — the association only needs an ALB ARN).
  let webAlb: elbv2.IApplicationLoadBalancer | undefined;
  if (withWebAlb) {
    const helper = new Stack(app, 'WebHelper', { env: ENV });
    webAlb = new elbv2.ApplicationLoadBalancer(helper, 'WebAlb', {
      vpc: net.vpc,
      internetFacing: true,
    });
  }
  const edge = new EdgeStack(app, 'Edge', { env: ENV, envConfig, alb: compute.alb, webAlb });
  return Template.fromStack(edge);
}

describe('WAF wakefulness gating', () => {
  test('dev paused: no WebACL, no association — nothing to protect, nothing billed', () => {
    const t = edgeTemplate('dev', true);
    t.resourceCountIs('AWS::WAFv2::WebACL', 0);
    t.resourceCountIs('AWS::WAFv2::WebACLAssociation', 0);
  });

  test('dev unpaused: the WebACL exists and is bound to the ALB', () => {
    const t = edgeTemplate('dev', false);
    t.resourceCountIs('AWS::WAFv2::WebACL', 1);
    t.resourceCountIs('AWS::WAFv2::WebACLAssociation', 1);
  });

  for (const envName of ['preprod', 'prod'] as const) {
    test(`${envName}: WAF stays even when paused — production posture is never cost-gated`, () => {
      const t = edgeTemplate(envName, true);
      t.resourceCountIs('AWS::WAFv2::WebACL', 1);
      t.resourceCountIs('AWS::WAFv2::WebACLAssociation', 1);
    });
  }

  test('the dashboard/marketing ALB is bound to the SAME WebACL — one firewall, two doors', () => {
    // The entrypoint passes WebStack's ALB into EdgeStack; without it the SSR
    // dashboard (login) and the marketing site's anonymous contact form sat
    // outside the WAF entirely while the API ALB was protected.
    const t = edgeTemplate('prod', false, true);
    t.resourceCountIs('AWS::WAFv2::WebACL', 1);
    t.resourceCountIs('AWS::WAFv2::WebACLAssociation', 2);
  });

  test('dev paused with a web ALB: still nothing — wakefulness gates both doors', () => {
    const t = edgeTemplate('dev', true, true);
    t.resourceCountIs('AWS::WAFv2::WebACL', 0);
    t.resourceCountIs('AWS::WAFv2::WebACLAssociation', 0);
  });
});
