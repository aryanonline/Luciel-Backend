/**
 * Round 7 WP-6: LLM telemetry alarms.
 *  - item 4: a dual-provider outage (no model served a turn) pages an operator. The
 *    app emits LLMUnavailable from the orchestrator's llm_unavailable path
 *    (app/runtime/orchestrator.py); LlmFallbackAlarm never sees that case because the
 *    fallback never answered.
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
  const obs = new ObservabilityStack(app, 'Obs', {
    env: ENV,
    envConfig,
    alb: compute.alb,
    alertEmail: 'ops@example.com',
    clusterArn: compute.cluster.clusterArn,
    sweepTaskFamily: compute.sweepTaskFamily,
  });
  return Template.fromStack(obs);
}

describe('a dual-provider LLM outage pages an operator (round 7 WP-6 item 4)', () => {
  test.each<EnvName>(['dev', 'preprod', 'prod'])(
    '%s alarms on one LLMUnavailable datum in five minutes, to the alarm topic',
    (envName) => {
      const obs = buildObs(envName);
      obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
        Namespace: 'Luciel',
        MetricName: 'LLMUnavailable',
        Dimensions: [{ Name: 'Environment', Value: envName }],
        Statistic: 'Sum',
        Threshold: 1,
        Period: 300,
        EvaluationPeriods: 1,
        ComparisonOperator: 'GreaterThanOrEqualToThreshold',
        TreatMissingData: 'notBreaching',
        AlarmDescription: Match.stringLikeRegexp('both LLM providers failed'),
        AlarmActions: [{ Ref: Match.stringLikeRegexp('^Alarms') }],
      });
    },
  );

  test('it sits beside the fallback alarm, which a dual outage never trips', () => {
    const obs = buildObs('dev');
    obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'LLMFallbackUsed',
      Threshold: 5,
      Period: 900,
    });
  });

  // The metrics carry no cause. The router logs one line per failed provider call
  // (llm_failover before the next leg is tried, llm_fallback_failed when the fallback
  // fails for good), and each page names the lines that say why.
  test('each LLM page names the log lines that carry the cause', () => {
    const obs = buildObs('dev');
    obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'LLMFallbackUsed',
      AlarmDescription: Match.stringLikeRegexp('llm_failover log lines with leg=primary'),
    });
    obs.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'LLMUnavailable',
      AlarmDescription: Match.stringLikeRegexp('llm_failover \\(leg=primary\\) and llm_fallback_failed'),
    });
  });
});
