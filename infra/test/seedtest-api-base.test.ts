/**
 * The SeedTest harness must be told which API it is proving (round 7 WP-8).
 *
 * scripts/seed_and_livetest.py drives the API named by LUCIEL_API_BASE and
 * reads the environment's own database through the injected secret. The task
 * definition never set the variable, so the harness fell back to its default —
 * the public hostname — and the preprod §10 gate would have exercised whatever
 * that hostname pointed at while reading preprod's rows. The variable is now
 * injected per environment — since round 7 WP-8b the environment's OWN api host
 * (env-config.ts `hostnames`: api-dev / api-preprod / api) — and this test pins
 * that the seedtest task carries it, that it differs per environment, and that it
 * is the same origin the stack's own SNS webhooks are registered against.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION, baseUrls, EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute(envName: EnvName) {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  return Template.fromStack(
    new ComputeStack(app, 'Compute', {
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
    }),
  );
}

describe('SeedTest task knows which API it drives', () => {
  test.each([['dev'], ['preprod'], ['prod']])(
    "%s: the seedtest container carries LUCIEL_API_BASE = the environment's OWN api origin",
    (envName) => {
      buildCompute(envName as EnvName).hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: [
          Match.objectLike({
            Name: 'SeedTestContainer',
            // arrayWith is order-sensitive: LUCIEL_ENV is declared first.
            Environment: Match.arrayWith([
              { Name: 'LUCIEL_ENV', Value: envName },
              { Name: 'LUCIEL_API_BASE', Value: baseUrls(envName as EnvName).api },
            ]),
          }),
        ],
      });
    },
  );
});

describe('the harness origin is per environment (D6)', () => {
  test('dev, preprod and prod drive three different APIs', () => {
    expect(baseUrls('dev').api).toBe('https://api-dev.vantagemind.ai');
    expect(baseUrls('preprod').api).toBe('https://api-preprod.vantagemind.ai');
    expect(baseUrls('prod').api).toBe('https://api.vantagemind.ai');
  });

  test.each<EnvName>(['dev', 'prod'])(
    '%s: the SES webhook subscriptions name the same origin the harness drives',
    (envName) => {
      buildCompute(envName).hasResourceProperties('AWS::SNS::Subscription', {
        Endpoint: `${baseUrls(envName).api}/api/v1/webhooks/ses/events`,
      });
    },
  );
});
