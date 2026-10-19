/**
 * 2026-09-05 audit WP7-B: operations posture.
 *  - alarms are armed in every environment and refuse to synth without an inbox (F045);
 *  - the app's EMF metrics have alarms and a failed sweep TASK pages by itself (F032/F034);
 *  - every environment scales on load inside its own ceilings (F009, round 7 WP-8b);
 *  - dev data stores are as hard to destroy as prod's; only preprod is disposable (F003);
 *  - the data plane polls mailboxes in-process and the schedule is an hourly backstop;
 *  - the app planes log JSON.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function build(envName: EnvName, alertEmail: string | undefined = 'ops@example.com') {
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
    clusterArn: compute.cluster.clusterArn,
    sweepTaskFamily: compute.sweepTaskFamily,
  });
  return {
    data: Template.fromStack(data),
    compute: Template.fromStack(compute),
    obs: Template.fromStack(obs),
  };
}

describe('alarms are armed everywhere and always reach someone (F045)', () => {
  test.each<EnvName>(['dev', 'preprod', 'prod'])('%s is armed', (envName) => {
    expect(getEnvConfig(envName).alarmsArmed).toBe(true);
    const { obs } = build(envName);
    obs.resourceCountIs('AWS::SNS::Subscription', 1);
    expect(Object.keys(obs.findResources('AWS::CloudWatch::Alarm')).length).toBeGreaterThanOrEqual(6);
  });

  test('an armed environment without an inbox is a synth error, not a silent topic', () => {
    expect(() => build('dev', '')).toThrow(/no operator inbox/);
  });
});

describe("alarms on the app's own metrics and on sweep task failures (F032/F034)", () => {
  test('the EMF metrics have alarms in the Luciel namespace on the [Environment] set', () => {
    const { obs } = build('dev');
    for (const metricName of [
      'LLMFallbackUsed',
      'SweepTenantFailure',
      'BudgetGateHit',
      'KnowledgeRetrievalDegraded',
      'EscalationBacklog',
    ]) {
      obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
        Namespace: 'Luciel',
        MetricName: metricName,
        Dimensions: [{ Name: 'Environment', Value: 'dev' }],
        Statistic: 'Sum',
      });
    }
  });

  test('one escalation page left queued by the deferred sweep pages an operator (round 7 WP-4b item 8)', () => {
    // The sweep emits EscalationBacklog once per hourly run (app/jobs/sweeps.py →
    // app/notifications/backlog.py), so the period is the sweep cadence and Sum is
    // the count; a run that never happens is the SweepTask rules' job, not this one.
    const { obs } = build('dev');
    obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: 'Luciel',
      MetricName: 'EscalationBacklog',
      Dimensions: [{ Name: 'Environment', Value: 'dev' }],
      Statistic: 'Sum',
      Threshold: 1,
      Period: 3600,
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
      AlarmDescription: Match.stringLikeRegexp('escalation_backlog'),
    });
  });

  test('a sweep task that exits non-zero or never starts is an EventBridge event to the topic', () => {
    const { obs } = build('dev');
    obs.resourceCountIs('AWS::Events::Rule', 2);
    obs.hasResourceProperties('AWS::Events::Rule', {
      EventPattern: Match.objectLike({
        source: ['aws.ecs'],
        'detail-type': ['ECS Task State Change'],
        detail: Match.objectLike({
          lastStatus: ['STOPPED'],
          containers: { exitCode: [{ 'anything-but': 0 }] },
        }),
      }),
      Targets: [Match.objectLike({ Arn: { Ref: Match.stringLikeRegexp('^Alarms') } })],
    });
    obs.hasResourceProperties('AWS::Events::Rule', {
      EventPattern: Match.objectLike({
        detail: Match.objectLike({ stopCode: ['TaskFailedToStart'] }),
      }),
    });
  });
});

describe('capacity follows load (F009, widened round 7 WP-8b)', () => {
  test('every environment scales both planes on CPU (memory is pinned in autoscaling.test.ts)', () => {
    for (const envName of ['dev', 'prod'] as const) {
      const compute = build(envName).compute;
      compute.resourceCountIs('AWS::ApplicationAutoScaling::ScalableTarget', 2);
      compute.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
        PolicyType: 'TargetTrackingScaling',
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: 60,
          PredefinedMetricSpecification: { PredefinedMetricType: 'ECSServiceAverageCPUUtilization' },
        }),
      });
    }
  });
});

describe('data stores are durable everywhere but the disposable preprod (F003)', () => {
  test('dev RDS has deletion protection and Retain; preprod stays destroyable', () => {
    build('dev').data.hasResource('AWS::RDS::DBInstance', {
      Properties: Match.objectLike({ DeletionProtection: true }),
      DeletionPolicy: 'Retain',
    });
    build('preprod').data.hasResource('AWS::RDS::DBInstance', {
      Properties: Match.objectLike({ DeletionProtection: false }),
      DeletionPolicy: 'Delete',
    });
  });

  test('every dev bucket is Retain; every preprod bucket is Delete', () => {
    const devBuckets = Object.values(build('dev').data.findResources('AWS::S3::Bucket'));
    expect(devBuckets.length).toBeGreaterThanOrEqual(3);
    for (const b of devBuckets) expect((b as any).DeletionPolicy).toBe('Retain');
    for (const b of Object.values(build('preprod').data.findResources('AWS::S3::Bucket')))
      expect((b as any).DeletionPolicy).toBe('Delete');
  });

  test('cost-bearing telemetry stays a production-posture choice', () => {
    expect(getEnvConfig('dev').productionPosture).toBe(false);
    expect(getEnvConfig('prod').productionPosture).toBe(true);
    build('dev').compute.hasResourceProperties('AWS::ECS::Cluster', {
      ClusterSettings: Match.arrayWith([{ Name: 'containerInsights', Value: 'disabled' }]),
    });
  });
});

describe('the data plane polls mailboxes in-process; the schedule is a backstop', () => {
  test('only the data-plane container carries the in-process interval; every app plane logs JSON', () => {
    const { compute } = build('dev');
    const defs = Object.entries(compute.findResources('AWS::ECS::TaskDefinition'));
    const envOf = (id: string) => {
      const [, td] = defs.find(([k]) => k.startsWith(id))!;
      const container = (td as any).Properties.ContainerDefinitions[0];
      return Object.fromEntries(container.Environment.map((e: any) => [e.Name, e.Value]));
    };
    expect(envOf('dataplaneTask').LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS).toBe('300');
    expect(envOf('dataplaneTask').LUCIEL_LOG_FORMAT).toBe('json');
    expect(envOf('controlplaneTask').LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS).toBeUndefined();
    expect(envOf('controlplaneTask').LUCIEL_LOG_FORMAT).toBe('json');
    expect(envOf('SweepTask').LUCIEL_LOG_FORMAT).toBe('json');
  });

  test('the mailbox_poll schedule is hourly, not every five minutes', () => {
    build('dev').compute.hasResourceProperties('AWS::Scheduler::Schedule', {
      Name: 'Luciel-dev-sweep-mailbox_poll',
      ScheduleExpression: 'cron(7 * * * ? *)',
    });
  });
});
