/**
 * Cost-allocation tags (round 7 WP-8, owner decision D7 2026-09-14).
 *
 * Every stack carries two tags that CloudFormation propagates to each resource
 * that supports tagging:
 *
 *   Environment = dev | preprod | prod   — which environment's bill this is
 *   Component   = Networking | Data | Compute | Web | ...  — which stack
 *
 * They exist for Cost Explorer grouping and, above all, for BudgetStack: each
 * environment's AWS Budgets ceiling filters on `user:Environment$<env>` so a
 * dev budget watches dev spend rather than the whole account (before this the
 * three per-environment budgets all summed the SAME account total, and only the
 * lowest ceiling ever meant anything).
 *
 * OWNER STEP, once per account: AWS Budgets and Cost Explorer only see a tag
 * after it is ACTIVATED as a cost allocation tag (Billing console → Cost
 * allocation tags → user-defined → activate `Environment` and `Component`), and
 * activation takes up to 24h to reach cost data. Until then a tag-filtered budget
 * reads $0 — so the activation is part of shipping this, not an optional tidy-up.
 */
import { Stack, Tags } from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import { EnvConfig } from './env-config';

export const COST_TAG_ENVIRONMENT = 'Environment';
export const COST_TAG_COMPONENT = 'Component';

/** The AWS Budgets `TagKeyValue` cost-filter term that selects one environment. */
export function environmentCostFilter(envConfig: EnvConfig): string {
  // User-defined cost allocation tags are namespaced `user:` in Budgets/Cost
  // Explorer; the `$` separates key from value in a TagKeyValue filter term.
  return `user:${COST_TAG_ENVIRONMENT}$${envConfig.envName}`;
}

/**
 * ECS does not copy a service's tags to its tasks unless asked, and Fargate bills the
 * TASK, so without this a tag-filtered budget never sees Fargate however the tags are
 * activated. Spread into every long-running FargateService. Applies to tasks started
 * after the setting lands, so running tasks gain the tags at their next roll.
 *
 * The scheduled sweep tasks are left untagged on purpose: they run for minutes, and
 * tagging at RunTask needs extra permissions on the EventBridge Scheduler role. Their
 * Fargate spend stays outside the tag filter.
 */
export const SERVICE_TASK_TAGGING = {
  propagateTags: ecs.PropagatedTagSource.SERVICE,
  enableECSManagedTags: true,
} as const;

/** Stack-level tags; CloudFormation propagates them to every taggable resource. */
export function applyCostAllocationTags(stack: Stack, envConfig: EnvConfig, component: string): void {
  Tags.of(stack).add(COST_TAG_ENVIRONMENT, envConfig.envName);
  Tags.of(stack).add(COST_TAG_COMPONENT, component);
}
