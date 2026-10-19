/**
 * SES events pipeline + inbound codification (ComputeStack).
 *
 * Outbound platform email previously had NO feedback loop: nothing carried a
 * configuration set, so a hard bounce or spam complaint reached nobody and the
 * app kept mailing dead/complaining addresses. These tests pin the loop:
 * config set -> BOUNCE/COMPLAINT event destination -> SNS topic (publishable by
 * SES from THIS account only) -> HTTPS subscription to the app's events webhook
 * — and that both app planes are actually told the config-set name + topic ARN.
 *
 * They also pin the codified SES inbound path (round 5B item 17): the `-v2`
 * receipt rule set/topic that replace the console-created `luciel-<env>-inbound`
 * state (parallel names; activation is a documented ops cutover step — SES
 * allows one active rule set per account, and CFN never activates one).
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION, APEX_DOMAIN, baseUrls } from '../lib/env-config';
import type { EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute(envName: EnvName) {
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
    exportBucket: data.exportBucket, voiceAudioBucket: data.voiceAudioBucket,
    knowledgeOriginalsBucket: data.knowledgeOriginalsBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
  });
  return Template.fromStack(compute);
}

/** The environment entry `name` on container `container`, or undefined. */
function envValue(template: Template, container: string, name: string): unknown {
  const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
  for (const res of Object.values(taskDefs)) {
    const containers = (res as { Properties: { ContainerDefinitions: unknown[] } }).Properties
      .ContainerDefinitions as { Name: string; Environment?: { Name: string; Value: unknown }[] }[];
    for (const c of containers) {
      if (c.Name !== container) continue;
      const entry = (c.Environment ?? []).find((e) => e.Name === name);
      if (entry) return entry.Value;
    }
  }
  return undefined;
}

describe.each<EnvName>(['dev', 'prod'])('SES events pipeline (%s)', (envName) => {
  const template = buildCompute(envName);

  test('the configuration set exists and carries the env name', () => {
    template.hasResourceProperties('AWS::SES::ConfigurationSet', {
      Name: `luciel-${envName}-platform`,
    });
  });

  test('the event destination publishes BOUNCE + COMPLAINT to the events topic', () => {
    template.hasResourceProperties('AWS::SES::ConfigurationSetEventDestination', {
      ConfigurationSetName: `luciel-${envName}-platform`,
      EventDestination: Match.objectLike({
        Enabled: true,
        // Exactly the two feedback-loop types — an OPEN/CLICK tracking pixel is a
        // different (privacy-relevant) decision this pipeline does not make.
        MatchingEventTypes: ['BOUNCE', 'COMPLAINT'],
        SnsDestination: {
          // A Ref to the topic (Ref on AWS::SNS::Topic yields the ARN), never a
          // hardcoded string pointing at some other account's topic. NB the CFN
          // key really is TopicARN (all-caps ARN) on this one resource.
          TopicARN: Match.objectLike({ Ref: Match.anyValue() }),
        },
      }),
    });
  });

  test('every SES-publishable topic policy conditions on aws:SourceAccount', () => {
    // Both topics (events + inbound) grant ses.amazonaws.com Publish; each grant
    // must be pinned to this account or anyone's SES could inject forged events.
    const policies = Object.values(template.findResources('AWS::SNS::TopicPolicy'));
    const sesStatements = policies
      .flatMap((p) => p.Properties.PolicyDocument.Statement as Record<string, any>[])
      .filter((s) => s.Principal?.Service === 'ses.amazonaws.com');
    expect(sesStatements).toHaveLength(2);
    for (const statement of sesStatements) {
      expect(statement.Effect).toBe('Allow');
      expect([statement.Action].flat()).toEqual(['sns:Publish']);
      expect(statement.Condition).toEqual({
        StringEquals: { 'aws:SourceAccount': ENV.account },
      });
    }
  });

  test('the events topic has an HTTPS subscription to the events webhook', () => {
    template.hasResourceProperties('AWS::SNS::Subscription', {
      Protocol: 'https',
      Endpoint: `${baseUrls(envName).api}/api/v1/webhooks/ses/events`,
    });
  });

  test('both app planes carry SES_CONFIGURATION_SET and SES_EVENTS_TOPIC_ARN', () => {
    for (const container of ['data-planeContainer', 'control-planeContainer']) {
      expect(envValue(template, container, 'SES_CONFIGURATION_SET')).toBe(
        `luciel-${envName}-platform`,
      );
      // A Ref object, never a literal: a hardcoded ARN would mean the plane
      // verifies webhook envelopes against something other than THIS env's topic.
      expect(typeof envValue(template, container, 'SES_EVENTS_TOPIC_ARN')).toBe('object');
    }
  });

  test('the sweep plane sends with the SAME configuration set as the app planes', () => {
    // billing_notices/dunning send platform email from the sweep task; a sweep
    // send without the config set would be mail with no bounce/complaint loop.
    expect(envValue(template, 'SweepContainer', 'SES_CONFIGURATION_SET')).toBe(
      `luciel-${envName}-platform`,
    );
    expect(envValue(template, 'SweepContainer', 'SES_EVENTS_TOPIC_ARN')).toEqual(
      envValue(template, 'data-planeContainer', 'SES_EVENTS_TOPIC_ARN'),
    );
  });
});

describe.each<EnvName>(['dev', 'prod'])('SES inbound codification (%s)', (envName) => {
  const template = buildCompute(envName);

  test('the -v2 receipt rule set exists (parallel to the console-created one)', () => {
    // `-v2` is deployment safety, not versioning vanity: `luciel-dev-inbound`
    // already exists console-created, and CFN fails on a taken physical name.
    // Activation of the v2 set is an explicit ops step (see the OPS CUTOVER NOTE
    // in compute-stack.ts), so deploying this template never touches live mail.
    template.hasResourceProperties('AWS::SES::ReceiptRuleSet', {
      RuleSetName: `luciel-${envName}-inbound-v2`,
    });
  });

  test('the receipt rule matches .reply addresses and delivers content inline to SNS', () => {
    template.hasResourceProperties('AWS::SES::ReceiptRule', {
      RuleSetName: `luciel-${envName}-inbound-v2`,
      Rule: Match.objectLike({
        Enabled: true,
        Recipients: [`.reply.${APEX_DOMAIN}`],
        Actions: [
          {
            // SNS action = full raw message inline in the notification ("content
            // inline", the console rule's behavior), UTF-8 encoded.
            SNSAction: {
              TopicArn: Match.objectLike({ Ref: Match.anyValue() }),
              Encoding: 'UTF-8',
            },
          },
        ],
      }),
    });
  });

  test('both app planes pin the inbound topics: the managed -v2 topic and the console one by name', () => {
    // Round 7 WP-7: SES_INBOUND_TOPIC_ARN was never injected, so the inbound
    // route's topic pin (audit F048) was skipped and any AWS account's signed
    // topic could feed a tenant inbound mail. The route now fails CLOSED in live
    // mode without it, so both planes must carry it.
    const inboundTopicIds = Object.keys(
      template.findResources('AWS::SNS::Topic', {
        Properties: { TopicName: `luciel-${envName}-ses-inbound-v2` },
      }),
    );
    expect(inboundTopicIds).toHaveLength(1);
    for (const container of ['data-planeContainer', 'control-planeContainer']) {
      const value = envValue(template, container, 'SES_INBOUND_TOPIC_ARN');
      expect(value).toBeDefined();
      const rendered = JSON.stringify(value);
      // The managed topic by Ref (never a literal ARN of some other env's topic)...
      expect(rendered).toContain(`{"Ref":"${inboundTopicIds[0]}"}`);
      // ...and the console-created topic by NAME, in THIS account and region (the
      // closing quote keeps `-ses-inbound-v2` from satisfying it).
      expect(rendered).toContain(`:sns:${ENV.region}:${ENV.account}:luciel-${envName}-ses-inbound"`);
    }
  });

  test('the -v2 inbound topic exists with its HTTPS subscription to the inbound webhook', () => {
    template.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: `luciel-${envName}-ses-inbound-v2`,
    });
    template.hasResourceProperties('AWS::SNS::Subscription', {
      Protocol: 'https',
      Endpoint: `${baseUrls(envName).api}/api/v1/webhooks/ses/inbound`,
    });
  });
});
