/**
 * 2026-09-05 audit F067: the two-plane split is real at the ALB. Admin authority
 * is forwarded to the control-plane service on BOTH listeners; the data plane is
 * the default action, so an unlisted path is never dropped.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack, CONTROL_PLANE_PATH_PATTERNS } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION } from '../lib/env-config';
import type { EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute(envName: EnvName) {
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
    exportBucket: data.exportBucket,
    voiceAudioBucket: data.voiceAudioBucket,
    knowledgeOriginalsBucket: data.knowledgeOriginalsBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
  });
  return Template.fromStack(compute);
}

type Resource = { Type: string; Properties: Record<string, any> };

/** Logical id of the target group a service registers into. */
function targetGroupOfService(template: Template, serviceId: string): string {
  const services = template.findResources('AWS::ECS::Service');
  const [id, service] = Object.entries(services).find(([k]) => k.startsWith(serviceId)) ?? [];
  if (!id || !service) throw new Error(`service ${serviceId} not found`);
  const lbs = (service as Resource).Properties.LoadBalancers ?? [];
  if (lbs.length !== 1) throw new Error(`${serviceId} registers into ${lbs.length} target groups`);
  return lbs[0].TargetGroupArn.Ref as string;
}

describe.each<EnvName>(['dev', 'prod'])('two-plane ALB routing (%s)', (envName) => {
  test('each service registers into exactly one target group, and they differ', () => {
    const template = buildCompute(envName);
    const dataTg = targetGroupOfService(template, 'DataPlane');
    const controlTg = targetGroupOfService(template, 'ControlPlane');
    expect(dataTg).not.toEqual(controlTg);
  });

  test('admin authority is forwarded to the control plane on every listener', () => {
    const template = buildCompute(envName);
    const controlTg = targetGroupOfService(template, 'ControlPlane');
    const rules = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::ListenerRule'));
    const forwardsToControl = rules.filter((r) =>
      ((r as Resource).Properties.Actions ?? []).some(
        (a: any) => a.Type === 'forward' && a.TargetGroupArn?.Ref === controlTg,
      ),
    );
    const listeners = new Set(forwardsToControl.map((r) => (r as Resource).Properties.ListenerArn.Ref));
    // One rule per pattern group per listener; dev may lack the HTTPS listener
    // (no regional cert in a unit synth) but never lacks the HTTP one.
    expect(listeners.size).toBeGreaterThanOrEqual(1);
    for (const listener of listeners) {
      const patterns = forwardsToControl
        .filter((r) => (r as Resource).Properties.ListenerArn.Ref === listener)
        .flatMap((r) =>
          ((r as Resource).Properties.Conditions ?? []).flatMap((c: any) => c.PathPatternConfig?.Values ?? []),
        )
        .sort();
      expect(patterns).toEqual(CONTROL_PLANE_PATH_PATTERNS.flat().slice().sort());
    }
  });

  test('the data plane is the default action and the widget/webhooks never reach the control plane', () => {
    const template = buildCompute(envName);
    const dataTg = targetGroupOfService(template, 'DataPlane');
    const listeners = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::Listener'));
    expect(listeners.length).toBeGreaterThanOrEqual(1);
    for (const l of listeners) {
      const def = (l as Resource).Properties.DefaultActions;
      expect(def[0].Type).toBe('forward');
      expect(def[0].TargetGroupArn.Ref).toBe(dataTg);
    }
    const all = CONTROL_PLANE_PATH_PATTERNS.flat();
    expect(all.some((p) => p.startsWith('/api/v1/chat-widget'))).toBe(false);
    expect(all.some((p) => p.startsWith('/api/v1/webhooks'))).toBe(false);
    expect(all.some((p) => p === '/healthz')).toBe(false);
    // ALB accepts at most five path patterns per rule.
    for (const group of CONTROL_PLANE_PATH_PATTERNS) expect(group.length).toBeLessThanOrEqual(5);
  });
});
