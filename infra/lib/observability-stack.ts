/**
 * Observability layer (DEVOPS §5.7, Arch §5.1).
 *
 * CloudWatch alarms on the load-bearing signals + log-group retention matching
 * doctrine (90d app logs; audit/trace 1yr held in the data tier, not here).
 * Alarms are fully armed in preprod/prod; dev gets the groups but relaxed alarms.
 */
import { Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';
import { EnvConfig, SWEEP_ERROR_METRIC } from './env-config';

export interface ObservabilityStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly alb: elbv2.IApplicationLoadBalancer;
  /**
   * Operator inbox subscribed to the Alarms topic. Optional because it is a
   * deploy-time context value — but note that WITHOUT it the topic has zero
   * subscribers and an alarm reaches nobody (the pre-2026-08-17 state).
   * SNS emails a confirmation link on first deploy; the subscription is
   * inert until the operator clicks it.
   */
  readonly alertEmail?: string;
  /** The ECS cluster the sweeps run in, for the task-failure rule (F034). */
  readonly clusterArn?: string;
  /** The sweep task definition family the rule matches on (F034). */
  readonly sweepTaskFamily?: string;
}

export class ObservabilityStack extends Stack {
  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    const { envConfig, alb, alertEmail, clusterArn, sweepTaskFamily } = props;
    // 2026-09-05 audit F045: an armed environment with no subscriber is an alarm
    // that reaches nobody — refused at synth rather than discovered in an outage.
    if (envConfig.alarmsArmed && !alertEmail) {
      throw new Error(
        `${envConfig.envName}: alarms are armed but no operator inbox was given — pass ` +
          '-c alarmEmail=<inbox> (or -c budgetAlertEmail=<inbox>) to the deploy.',
      );
    }

    // NOTE: the former `/luciel/<env>/app` log group was removed (cost hygiene):
    // no producer ever wrote to it — every ECS container declares its own
    // `/luciel/<env>/<plane>` group in ComputeStack, so this one sat empty as a
    // dead resource. Audit/trace stores live in RDS with their own 1-year
    // retention enforced by the app, not CloudWatch.

    const alarmTopic = new sns.Topic(this, 'Alarms', {
      displayName: `Luciel ${envConfig.envName} alarms`,
    });
    // The topic had ZERO subscribers from its creation until 2026-08-17 — every
    // alarm below fired into the void. The operator email arrives as deploy
    // context (`-c alarmEmail`, falling back to the budget address) so no
    // address is ever committed to the repo.
    if (alertEmail) {
      alarmTopic.addSubscription(new subs.EmailSubscription(alertEmail));
    }

    if (envConfig.alarmsArmed) {
      // 5xx from the ALB target group - user-facing errors.
      new cw.Alarm(this, 'Alb5xxAlarm', {
        metric: alb.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, {
          period: Duration.minutes(5),
        }),
        threshold: 10,
        evaluationPeriods: 2,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction({
        bind: () => ({ alarmActionArn: alarmTopic.topicArn }),
      });

      // p95 latency SLO breach (§ SLO: < 5s text first response).
      new cw.Alarm(this, 'LatencyAlarm', {
        metric: alb.metrics.targetResponseTime({
          period: Duration.minutes(5),
          statistic: 'p95',
        }),
        threshold: 5,
        evaluationPeriods: 3,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction({
        bind: () => ({ alarmActionArn: alarmTopic.topicArn }),
      });

      // ERROR-level lines in the sweep log group (ComputeStack's SweepErrors
      // metric filter). A sweep is a compliance promise running on a schedule
      // nobody watches: an operator-facing logger.error there — a retired LLM
      // model pin (model_currency), a §A5 short-lead reduction — must page a
      // human, not decorate a log stream. Referenced by metric NAME (shared
      // const in env-config.ts), so no cross-stack construct edge is needed.
      // 2026-09-05 audit F032: alarms on the app's own EMF metrics (namespace
      // Luciel, dimension set [Environment] — app/observability/metrics.py).
      const emfAlarm = (
        id: string,
        metricName: string,
        threshold: number,
        periodMinutes: number,
        alarmDescription: string,
      ) =>
        new cw.Alarm(this, id, {
          alarmDescription,
          metric: new cw.Metric({
            namespace: 'Luciel',
            metricName,
            dimensionsMap: { Environment: envConfig.envName },
            statistic: 'Sum',
            period: Duration.minutes(periodMinutes),
          }),
          threshold,
          evaluationPeriods: 1,
          treatMissingData: cw.TreatMissingData.NOT_BREACHING,
        }).addAlarmAction({ bind: () => ({ alarmActionArn: alarmTopic.topicArn }) });
      emfAlarm(
        'LlmFallbackAlarm',
        'LLMFallbackUsed',
        5,
        15,
        'The fallback LLM answered 5+ turns in 15 minutes: the primary provider is failing.',
      );
      emfAlarm(
        'SweepTenantFailureAlarm',
        'SweepTenantFailure',
        1,
        5,
        "A sweep's per-tenant work raised; that tenant was skipped this run.",
      );
      emfAlarm(
        'BudgetGateAbuseAlarm',
        'BudgetGateHit',
        100,
        60,
        'A conversation cap was hit 100+ times in an hour: a hammered widget or a real tenant at cap.',
      );

      // 2026-09-05 audit F034: a sweep task that exits non-zero or never starts
      // is an EventBridge task-state event straight to the operator topic —
      // independent of whether the sweep managed to log anything.
      if (clusterArn && sweepTaskFamily) {
        new events.Rule(this, 'SweepTaskExitedNonZero', {
          description: 'A scheduled sweep task stopped with a non-zero exit code',
          eventPattern: {
            source: ['aws.ecs'],
            detailType: ['ECS Task State Change'],
            detail: {
              clusterArn: [clusterArn],
              lastStatus: ['STOPPED'],
              group: [`family:${sweepTaskFamily}`],
              containers: { exitCode: [{ 'anything-but': 0 }] },
            },
          },
          targets: [new targets.SnsTopic(alarmTopic)],
        });
        new events.Rule(this, 'SweepTaskFailedToStart', {
          description: 'A scheduled sweep task could not start (image pull, secrets, capacity)',
          eventPattern: {
            source: ['aws.ecs'],
            detailType: ['ECS Task State Change'],
            detail: {
              clusterArn: [clusterArn],
              lastStatus: ['STOPPED'],
              group: [`family:${sweepTaskFamily}`],
              stopCode: ['TaskFailedToStart'],
            },
          },
          targets: [new targets.SnsTopic(alarmTopic)],
        });
      }

      new cw.Alarm(this, 'SweepErrorAlarm', {
        metric: new cw.Metric({
          namespace: SWEEP_ERROR_METRIC.metricNamespace,
          metricName: SWEEP_ERROR_METRIC.metricName,
          statistic: 'Sum',
          period: Duration.minutes(5),
        }),
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction({
        bind: () => ({ alarmActionArn: alarmTopic.topicArn }),
      });
    }
  }
}
