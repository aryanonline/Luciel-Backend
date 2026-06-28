/**
 * Observability layer (DEVOPS §5.7, Arch §5.1).
 *
 * CloudWatch alarms on the load-bearing signals + log-group retention matching
 * doctrine (90d app logs; audit/trace 1yr held in the data tier, not here).
 * Alarms are fully armed in staging/prod; dev gets the groups but relaxed alarms.
 */
import { Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface ObservabilityStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly alb: elbv2.IApplicationLoadBalancer;
}

export class ObservabilityStack extends Stack {
  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    const { envConfig, alb } = props;

    // App log group - 90 day retention (§5.1). Audit/trace stores live in RDS with
    // their own 1-year retention enforced by the app, not CloudWatch.
    new logs.LogGroup(this, 'AppLogs', {
      logGroupName: `/luciel/${envConfig.envName}/app`,
      retention: logs.RetentionDays.THREE_MONTHS,
    });

    const alarmTopic = new sns.Topic(this, 'Alarms', {
      displayName: `Luciel ${envConfig.envName} alarms`,
    });

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
    }
  }
}
