/**
 * Cost tags reach the Fargate tasks (owner decision D7).
 *
 * Fargate bills the task, and ECS copies no tag to a task unless the service asks.
 * Live dev read `propagateTags: NONE` with every task untagged, so the Environment
 * budget filter could never see Fargate even with the tag activated in Billing.
 * The scheduled sweep tasks are deliberately outside this: see lib/cost-tags.ts.
 */
import { Template } from 'aws-cdk-lib/assertions';
import { buildComputeAndWeb } from './support/stacks';
import { applyCostAllocationTags } from '../lib/cost-tags';
import { EnvName } from '../lib/env-config';

describe.each<EnvName>(['dev', 'preprod', 'prod'])('ECS tag propagation (%s)', (envName) => {
  const { compute, web, envConfig } = buildComputeAndWeb(envName, { publicDns: true });
  applyCostAllocationTags(compute, envConfig, 'Compute');
  applyCostAllocationTags(web, envConfig, 'Web');
  const services = [
    ...Object.values(Template.fromStack(compute).findResources('AWS::ECS::Service')),
    ...Object.values(Template.fromStack(web).findResources('AWS::ECS::Service')),
  ];

  test('both planes and the web tier copy the service tags onto their tasks', () => {
    expect(services).toHaveLength(3);
    for (const service of services) {
      expect(service.Properties.PropagateTags).toBe('SERVICE');
      expect(service.Properties.EnableECSManagedTags).toBe(true);
    }
  });

  test('the tag the budget filters on is on the service, so there is something to copy', () => {
    for (const service of services) {
      expect(service.Properties.Tags).toEqual(
        expect.arrayContaining([{ Key: 'Environment', Value: envName }]),
      );
    }
  });
});
