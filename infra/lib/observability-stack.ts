/**
 * Observability layer (DEVOPS §5.7, Arch §5.1).
 *
 * CloudWatch alarms on the load-bearing signals + log-group retention matching
 * doctrine (90d app logs; audit/trace 1yr held in the data tier, not here).
 * Alarms are fully armed in preprod/prod; dev gets the groups but relaxed alarms.
 */
import { Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
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
}

export class ObservabilityStack extends Stack {
  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    const { envConfig, alb, alertEmail } = props;

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
