/**
 * Autoscaling follows the environment's own knob, not production posture
 * (round 7 WP-8b). Before this, `productionPosture` gated the whole policy, so dev —
 * which serves the public product — pinned one task per plane and a busy hour
 * queued. Now every service that serves requests (both backend planes and the web
 * tier) scales on CPU AND memory between its base count and the environment's
 * ceiling; dev's ceilings are deliberately small (2), prod keeps the ones it had,
 * and a paused environment carries no scalable target at all (a floor of 1 would
 * hold a task up under a service the pause set to zero).
 */
import { Template, Match } from 'aws-cdk-lib/assertions';
import { CPU_TARGET_PERCENT, MEMORY_TARGET_PERCENT } from '../lib/service-scaling';
import { EnvName, getEnvConfig } from '../lib/env-config';
import { buildComputeAndWeb } from './support/stacks';

function build(envName: EnvName, paused = false) {
  const { compute, web } = buildComputeAndWeb(envName, { paused });
  return { compute: Template.fromStack(compute), web: Template.fromStack(web) };
}

type Res = { Properties: Record<string, any> };

/** [min, max] of the scalable target whose ResourceId names the given service logical id. */
function bounds(t: Template, serviceLogicalIdPrefix: string): [number, number] {
  const targets = Object.values(t.findResources('AWS::ApplicationAutoScaling::ScalableTarget')) as Res[];
  const target = targets.find((r) =>
    JSON.stringify(r.Properties.ResourceId).includes(serviceLogicalIdPrefix),
  );
  if (!target) throw new Error(`no scalable target for ${serviceLogicalIdPrefix}`);
  return [target.Properties.MinCapacity, target.Properties.MaxCapacity];
}

function metricTypes(t: Template): string[] {
  return (Object.values(t.findResources('AWS::ApplicationAutoScaling::ScalingPolicy')) as Res[])
    .map(
      (p) =>
        p.Properties.TargetTrackingScalingPolicyConfiguration.PredefinedMetricSpecification
          .PredefinedMetricType,
    )
    .sort();
}

describe('autoscaling is an environment knob, not a production-posture side effect', () => {
  test('dev scales (it serves the public product) inside ceilings of two', () => {
    const cfg = getEnvConfig('dev');
    expect(cfg.productionPosture).toBe(false);
    expect(cfg.autoscaling).toEqual({ enabled: true, maxDataPlane: 2, maxControlPlane: 2, maxWeb: 2 });
    const { compute, web } = build('dev');
    expect(bounds(compute, 'DataPlane')).toEqual([1, 2]);
    expect(bounds(compute, 'ControlPlane')).toEqual([1, 2]);
    expect(bounds(web, 'WebApp')).toEqual([1, 2]);
  });

  test('prod keeps the ceilings production posture always had; the web tier gains its own', () => {
    const { compute, web } = build('prod');
    expect(bounds(compute, 'DataPlane')).toEqual([2, 6]);
    expect(bounds(compute, 'ControlPlane')).toEqual([1, 3]);
    expect(bounds(web, 'WebApp')).toEqual([2, 4]);
  });

  test.each<EnvName>(['dev', 'preprod', 'prod'])(
    '%s: every scaled service tracks CPU AND memory',
    (envName) => {
      const { compute, web } = build(envName);
      expect(metricTypes(compute)).toEqual([
        'ECSServiceAverageCPUUtilization',
        'ECSServiceAverageCPUUtilization',
        'ECSServiceAverageMemoryUtilization',
        'ECSServiceAverageMemoryUtilization',
      ]);
      expect(metricTypes(web)).toEqual([
        'ECSServiceAverageCPUUtilization',
        'ECSServiceAverageMemoryUtilization',
      ]);
      compute.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: MEMORY_TARGET_PERCENT,
          PredefinedMetricSpecification: { PredefinedMetricType: 'ECSServiceAverageMemoryUtilization' },
        }),
      });
      web.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: CPU_TARGET_PERCENT,
          PredefinedMetricSpecification: { PredefinedMetricType: 'ECSServiceAverageCPUUtilization' },
        }),
      });
    },
  );

  test('a paused environment carries no scalable target (nothing may hold a task up)', () => {
    const { compute, web } = build('dev', true);
    compute.resourceCountIs('AWS::ApplicationAutoScaling::ScalableTarget', 0);
    web.resourceCountIs('AWS::ApplicationAutoScaling::ScalableTarget', 0);
  });

  test('no environment sets a ceiling below its floor', () => {
    for (const envName of ['dev', 'preprod', 'prod'] as const) {
      const cfg = getEnvConfig(envName);
      const floor = cfg.multiAz ? 2 : 1;
      expect(cfg.autoscaling.maxDataPlane).toBeGreaterThanOrEqual(floor);
      expect(cfg.autoscaling.maxControlPlane).toBeGreaterThanOrEqual(1);
      expect(cfg.autoscaling.maxWeb).toBeGreaterThanOrEqual(floor);
    }
  });
});
