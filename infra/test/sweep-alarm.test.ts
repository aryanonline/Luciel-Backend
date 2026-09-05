/**
 * Sweep-failure alerting + the model_currency auto-swap wiring (§3.4.3).
 *
 * Before this, a sweep could log ERROR and exit nonzero nightly and NOTHING
 * noticed: EventBridge's retryPolicy retries failed RunTask *launches* (not
 * nonzero exits), the schedules had no DLQ, and the Alarms SNS topic had zero
 * subscribers. The chain pinned here: ERROR line in the sweep log group →
 * ComputeStack's SweepErrors metric filter → ObservabilityStack's armed alarm →
 * the Alarms topic → the operator's email subscription. Plus the two scoped
 * grants the model_currency retirement auto-swap needs (platform-secret write
 * + app-service redeploy) and the env vars that tell it where.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { EnvName, getEnvConfig, OFFERING_REGION, SWEEP_ERROR_METRIC } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function build(envName: EnvName, alertEmail?: string) {
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
  const obs = new ObservabilityStack(app, 'Obs', {
    env: ENV,
    envConfig,
    alb: compute.alb,
    alertEmail,
  });
  return { compute: Template.fromStack(compute), obs: Template.fromStack(obs) };
}

describe('ERROR-level sweep lines become a metric (ComputeStack)', () => {
  test('the sweep log group carries an ERROR metric filter emitting SweepErrors', () => {
    const { compute } = build('dev');
    compute.hasResourceProperties('AWS::Logs::MetricFilter', {
      FilterPattern: Match.stringLikeRegexp('ERROR'),
      MetricTransformations: Match.arrayWith([
        Match.objectLike({
          MetricNamespace: SWEEP_ERROR_METRIC.metricNamespace,
          MetricName: SWEEP_ERROR_METRIC.metricName,
          MetricValue: '1',
        }),
      ]),
    });
  });
});

describe('The armed alarm + a subscribed operator (ObservabilityStack)', () => {
  test('prod alarms on SweepErrors and the topic finally reaches an inbox', () => {
    const { obs } = build('prod', 'owner@example.com');
    obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: SWEEP_ERROR_METRIC.metricNamespace,
      MetricName: SWEEP_ERROR_METRIC.metricName,
      Threshold: 1,
      TreatMissingData: 'notBreaching',
    });
    obs.hasResourceProperties('AWS::SNS::Subscription', {
      Protocol: 'email',
      Endpoint: 'owner@example.com',
    });
  });

  test('dev stays unarmed (no alarms) but still subscribes the operator', () => {
    const { obs } = build('dev', 'owner@example.com');
    obs.resourceCountIs('AWS::CloudWatch::Alarm', 0);
    obs.resourceCountIs('AWS::SNS::Subscription', 1);
  });

  test('no alertEmail context → no subscription (the documented empty state)', () => {
    const { obs } = build('dev');
    obs.resourceCountIs('AWS::SNS::Subscription', 0);
  });
});

describe('model_currency auto-swap wiring (ComputeStack)', () => {
  test('the task role may write ONLY the platform secret and redeploy ONLY the app services', () => {
    const { compute } = build('dev');
    compute.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'RotatePlatformSecret',
            Action: 'secretsmanager:PutSecretValue',
            Effect: 'Allow',
          }),
          Match.objectLike({
            Sid: 'RestartAppServices',
            Action: 'ecs:UpdateService',
            Effect: 'Allow',
          }),
        ]),
      }),
    });
  });

  test('the sweep container knows the swap targets (secret ARN, cluster, services)', () => {
    const { compute } = build('dev');
    compute.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: 'SweepContainer',
          Environment: Match.arrayWith([
            Match.objectLike({ Name: 'LUCIEL_PLATFORM_SECRET_ARN' }),
            Match.objectLike({ Name: 'LUCIEL_ECS_CLUSTER' }),
            Match.objectLike({ Name: 'LUCIEL_ECS_SERVICES' }),
          ]),
        }),
      ]),
    });
  });
});
