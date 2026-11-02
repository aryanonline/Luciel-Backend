/**
 * Who may publish to the Alarms topic, read from the synthesized policy.
 *
 * Why this exists: a topic policy REPLACES SNS's implicit default, so the EventBridge
 * target's policy (events.amazonaws.com only) silently removed CloudWatch's right to
 * publish. From 2026-09-06 every alarm action in dev ended "Failed: CloudWatch Alarms
 * is not authorized to perform: SNS:Publish" while each alarm still read as armed with
 * an action and a confirmed email subscriber. No synth, deploy or probe noticed; only
 * describe-alarm-history did. Losing either statement below brings that back, so both
 * principals are pinned for every environment that has the topic.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildObs(envName: EnvName): Template {
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
  return Template.fromStack(
    new ObservabilityStack(app, 'Obs', {
      env: ENV,
      envConfig,
      alb: compute.alb,
      alertEmail: 'ops@example.com',
      clusterArn: compute.cluster.clusterArn,
      sweepTaskFamily: compute.sweepTaskFamily,
    }),
  );
}

type Statement = {
  Sid?: string;
  Effect: string;
  Principal: { Service?: string };
  Action: string | string[];
  Resource: unknown;
  Condition?: Record<string, Record<string, unknown>>;
};

function topicStatements(template: Template): Statement[] {
  const policies = Object.values(template.findResources('AWS::SNS::TopicPolicy'));
  // One policy document per topic: a second one would replace the first at deploy.
  expect(policies).toHaveLength(1);
  return policies[0].Properties.PolicyDocument.Statement as Statement[];
}

/** Fn::Join parts flattened to a string, with Refs and pseudo parameters named. */
function flatten(value: unknown): string {
  if (typeof value === 'string') return value;
  const join = (value as { 'Fn::Join'?: [string, unknown[]] })['Fn::Join'];
  if (join) return join[1].map(flatten).join(join[0]);
  const ref = (value as { Ref?: string }).Ref;
  if (ref) return `{${ref}}`;
  return JSON.stringify(value);
}

describe.each<EnvName>(['dev', 'preprod', 'prod'])('the Alarms topic policy (%s)', (envName) => {
  const statements = topicStatements(buildObs(envName));
  const forService = (service: string) =>
    statements.filter((s) => s.Principal.Service === service);

  test('CloudWatch Alarms may publish: this account, this region, alarms only', () => {
    const [statement, ...others] = forService('cloudwatch.amazonaws.com');
    expect(others).toHaveLength(0);
    expect(statement.Effect).toBe('Allow');
    expect(statement.Action).toBe('sns:Publish');
    expect(statement.Condition?.StringEquals?.['aws:SourceAccount']).toBe(ENV.account);
    expect(flatten(statement.Condition?.ArnLike?.['aws:SourceArn'])).toBe(
      `arn:{AWS::Partition}:cloudwatch:${ENV.region}:${ENV.account}:alarm:*`,
    );
  });

  test('EventBridge may still publish: the sweep task-state rules target this topic', () => {
    const [statement] = forService('events.amazonaws.com');
    expect(statement.Effect).toBe('Allow');
    expect(statement.Action).toBe('sns:Publish');
  });

  test('no other principal may publish', () => {
    expect(statements.map((s) => s.Principal.Service).sort()).toEqual([
      'cloudwatch.amazonaws.com',
      'events.amazonaws.com',
    ]);
  });
});
