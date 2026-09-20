/**
 * The SeedTest harness must be told which API it is proving (round 7 WP-8).
 *
 * scripts/seed_and_livetest.py drives the API named by LUCIEL_API_BASE and
 * reads the environment's own database through the injected secret. The task
 * definition never set the variable, so the harness fell back to its default —
 * the public hostname — and the preprod §10 gate would have exercised whatever
 * that hostname pointed at while reading preprod's rows. The variable is now
 * injected per environment, derived from DOMAINS.api until hostnames become
 * per-environment; this test pins that the seedtest task carries it and that
 * it is the same origin the stack's own SNS webhooks are registered against.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION, DOMAINS, EnvName } from '../lib/env-config';

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
    '%s: the seedtest container carries LUCIEL_API_BASE = https://<DOMAINS.api>',
    (envName) => {
      buildCompute(envName as EnvName).hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: [
          Match.objectLike({
            Name: 'SeedTestContainer',
            // arrayWith is order-sensitive: LUCIEL_ENV is declared first.
            Environment: Match.arrayWith([
              { Name: 'LUCIEL_ENV', Value: envName },
              { Name: 'LUCIEL_API_BASE', Value: `https://${DOMAINS.api}` },
            ]),
          }),
        ],
      });
    },
  );
});
