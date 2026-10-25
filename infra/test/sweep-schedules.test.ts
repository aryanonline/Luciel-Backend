/**
 * Scheduled compliance sweeps (Legal §B5 / §A3). Before these schedules existed the
 * repo had NO scheduler at all, so four fully-implemented sweeps never ran. Assert
 * the synthesized schedules so a refactor cannot silently un-schedule a Legal
 * commitment again.
 *
 * The same defect recurred twice after that: ``usage_reconcile`` and ``unverified_reap``
 * each shipped an entrypoint and a discovery branch with no schedule behind them (P0-8).
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
    transcriptArchiveBucket: data.transcriptArchiveBucket,
    exportBucket: data.exportBucket, voiceAudioBucket: data.voiceAudioBucket,
    knowledgeOriginalsBucket: data.knowledgeOriginalsBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
  });
  return Template.fromStack(compute);
}

// Must stay equal to SWEEP_KINDS in app/jobs/sweeps.py — an entrypoint with no schedule
// is a compliance promise that never runs (P0-8). tests/unit/test_sweep_scheduling.py
// asserts the two halves agree from the Python side.
const SWEEPS = [
  'retention',
  'grace_expiry',
  'close_purge',
  'dunning',
  'usage_reconcile',
  'unverified_reap',
  'deferred',
  'health',
  'audit_retention',
  'session_finalize',
  'transcript_archive',
  'mailbox_poll',
  'knowledge_sync',
  'billing_notices',
  'model_currency',
  'daily_brief',
];

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

  test('RunTask is granted on the SweepTask FAMILY, so a rolled-back (re-registered) revision still runs', () => {
    // Phase 3 infra-fix review: scoped to the one deployed revision, every sweep after
    // a pipeline rollback was denied RunTask and stopped silently.
    const template = buildCompute();
    const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
      (p) => (p as any).Properties.PolicyDocument.Statement as any[],
    );
    const runTask = statements.find((s) => s.Action === 'ecs:RunTask');
    expect(runTask).toBeDefined();
    const resource = JSON.stringify(runTask.Resource);
    // The family is `<stack id>SweepTask<hash>` (the test stack is 'Compute'); the grant
    // must end in `:*`, every revision of it.
    expect(resource).toMatch(/task-definition\/[A-Za-z]*ComputeSweepTask[0-9A-F]+:\*/);
    expect(runTask.Condition).toEqual({ ArnEquals: { 'ecs:cluster': expect.anything() } });
  });

  test('no two sweeps share a firing minute (round 7 WP-8: daily_brief vs knowledge_sync)', () => {
    // daily_brief shipped at :50 hourly, which is knowledge_sync's :50 every-6h
    // slot: four times a day two sweep tasks launched at the same instant and
    // contended for the same DB. Expand every schedule into the (hour, minute)
    // slots it fires in over a day and refuse any slot claimed twice. The parser
    // accepts exactly the cron shapes this stack uses so a new shape has to be
    // added here deliberately rather than slipping past the check.
    const schedules = buildCompute().findResources('AWS::Scheduler::Schedule');
    const hoursOf = (field: string): number[] => {
      if (field === '*') return Array.from({ length: 24 }, (_, h) => h);
      const step = /^\*\/(\d+)$/.exec(field);
      if (step) {
        const n = Number(step[1]);
        return Array.from({ length: Math.ceil(24 / n) }, (_, i) => i * n);
      }
      if (/^\d+$/.test(field)) return [Number(field)];
      throw new Error(`unsupported cron hour field: ${field}`);
    };
    const claimed = new Map<string, string>();
    for (const [, res] of Object.entries(schedules)) {
      const props = res.Properties as { Name: string; ScheduleExpression: string };
      const m = /^cron\((\d+) (\S+) \S+ \S+ \S+ \S+\)$/.exec(props.ScheduleExpression);
      if (!m) throw new Error(`unsupported cron expression: ${props.ScheduleExpression}`);
      const minute = Number(m[1]);
      for (const hour of hoursOf(m[2])) {
        const slot = `${hour}:${minute}`;
        const other = claimed.get(slot);
        expect(other === undefined ? '' : `${props.Name} and ${other} both fire at ${slot} UTC`).toBe('');
        claimed.set(slot, props.Name);
      }
    }
    expect(claimed.size).toBeGreaterThan(SWEEPS.length);
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
