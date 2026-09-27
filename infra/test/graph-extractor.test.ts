/**
 * The LLM graph extractor is ON in every task that ingests knowledge (owner decision
 * D10, round 7 WP-4c #10).
 *
 * Round 6 built `graph_extract.LlmGraphExtractor` behind LUCIEL_GRAPH_EXTRACTOR=llm and
 * nothing ever set the variable, so the extractor was inert in every deployment: the
 * knowledge graph held only the explicit node/edge grammar lines, which no real
 * business document contains. The app now defaults to `llm` in live posture; this test
 * pins that the task definitions carry the explicit value too, on both app planes AND
 * the sweep task (knowledge_sync ingests synced pages from there), so the posture is
 * visible in the task definition rather than implied by a config default.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION, EnvName } from '../lib/env-config';

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

const INGESTING_CONTAINERS = ['data-planeContainer', 'control-planeContainer', 'SweepContainer'];

describe('LLM graph extractor posture (D10)', () => {
  test.each([['dev'], ['preprod'], ['prod']])(
    '%s: every ingesting task carries LUCIEL_GRAPH_EXTRACTOR=llm',
    (envName) => {
      const template = buildCompute(envName as EnvName);
      for (const containerName of INGESTING_CONTAINERS) {
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
          ContainerDefinitions: [
            Match.objectLike({
              Name: containerName,
              Environment: Match.arrayWith([{ Name: 'LUCIEL_GRAPH_EXTRACTOR', Value: 'llm' }]),
            }),
          ],
        });
      }
    },
  );
});
