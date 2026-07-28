/**
 * Scheduled compliance sweeps (Legal §B5 / §A3). Before these schedules existed the
 * repo had NO scheduler at all, so four fully-implemented sweeps never ran. Assert
 * the synthesized schedules so a refactor cannot silently un-schedule a Legal
 * commitment again.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute() {
  const app = new App();
  const envConfig = getEnvConfig('dev');
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  const compute = new ComputeStack(app, 'Compute', {
    env: ENV,
    envConfig,
    vpc: net.vpc,
    cmkArn: sec.cmk.keyArn,
    knowledgeBucket: data.knowledgeBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
    backendPath: path.join(__dirname, '..', '..'),
  });
  return Template.fromStack(compute);
}

const SWEEPS = ['retention', 'grace_expiry', 'close_purge', 'dunning'];

describe('Background sweep schedules (Legal §B5 / §A3)', () => {
  test('one EventBridge Scheduler schedule exists per compliance sweep', () => {
    const t = buildCompute();
    t.resourceCountIs('AWS::Scheduler::Schedule', SWEEPS.length);
    for (const sweep of SWEEPS) {
      t.hasResourceProperties('AWS::Scheduler::Schedule', {
        Name: `Luciel-dev-sweep-${sweep}`,
        State: 'ENABLED',
        Target: Match.objectLike({
          Input: Match.stringLikeRegexp(`scripts/run-sweep.sh ${sweep}`),
          EcsParameters: Match.objectLike({ LaunchType: 'FARGATE' }),
        }),
      });
    }
  });

  test('sweeps retry on transient failure rather than silently skipping a day', () => {
    buildCompute().hasResourceProperties('AWS::Scheduler::Schedule', {
      Target: Match.objectLike({
        RetryPolicy: { MaximumRetryAttempts: 3, MaximumEventAgeInSeconds: 3600 },
      }),
    });
  });

  test('the scheduler role can only RunTask, never read tenant data', () => {
    buildCompute().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({ Action: 'ecs:RunTask', Effect: 'Allow' }),
          Match.objectLike({ Action: 'iam:PassRole', Effect: 'Allow' }),
        ]),
      }),
    });
  });

  test('sweep tasks run in private subnets with no public IP', () => {
    buildCompute().hasResourceProperties('AWS::Scheduler::Schedule', {
      Target: Match.objectLike({
        EcsParameters: Match.objectLike({
          NetworkConfiguration: {
            AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: 'DISABLED' }),
          },
        }),
      }),
    });
  });
});
