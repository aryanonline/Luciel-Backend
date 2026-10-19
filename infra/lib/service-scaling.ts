/**
 * Target-tracking autoscaling for an ECS service (round 7 WP-8b).
 *
 * One policy shape for every request-serving service (both backend planes and the
 * web tier) so the three cannot drift: CPU AND memory, each a target-tracking
 * policy on the same scalable target. Application Auto Scaling scales OUT when
 * either policy asks and scales IN only when every policy agrees — so a
 * memory-bound plane (the data plane holds per-conversation state and the
 * in-process mailbox poll) is no longer invisible to a CPU-only policy.
 *
 * Off when the environment is paused: a scalable target's floor would hold a task
 * up under a service the pause set to zero, and there is nothing to scale anyway.
 */
import { Duration } from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import { EnvConfig } from './env-config';

/** CPU target (%): headroom for a burst while the next task starts (~1 min on Fargate). */
export const CPU_TARGET_PERCENT = 60;
/** Memory target (%): memory does not burst-recover the way CPU does, so a wider margin. */
export const MEMORY_TARGET_PERCENT = 75;

export function scaleOnLoad(
  envConfig: EnvConfig,
  service: ecs.FargateService,
  idPrefix: string,
  minCapacity: number,
  maxCapacity: number,
): boolean {
  if (!envConfig.autoscaling.enabled || envConfig.paused) return false;
  if (maxCapacity < minCapacity) {
    throw new Error(
      `${envConfig.envName}/${idPrefix}: autoscaling ceiling ${maxCapacity} is below the floor ${minCapacity}`,
    );
  }
  const scaling = service.autoScaleTaskCount({ minCapacity, maxCapacity });
  scaling.scaleOnCpuUtilization(`${idPrefix}Cpu`, {
    targetUtilizationPercent: CPU_TARGET_PERCENT,
    scaleInCooldown: Duration.minutes(5),
    scaleOutCooldown: Duration.minutes(2),
  });
  scaling.scaleOnMemoryUtilization(`${idPrefix}Memory`, {
    targetUtilizationPercent: MEMORY_TARGET_PERCENT,
    scaleInCooldown: Duration.minutes(5),
    scaleOutCooldown: Duration.minutes(2),
  });
  return true;
}
