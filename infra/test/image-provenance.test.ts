/**
 * The image ECS runs must be the image the CVE gate scanned.
 *
 * The compute stack used to build its own DockerImageAsset at deploy time while the
 * pipeline separately built, pushed, and scanned `luciel-backend-<env>:<sha>` — two
 * different builds of the same context. The gate therefore never gated the deployed
 * bytes, and preprod (which had no build step at all) deployed with no scan of any
 * kind. Now `-c imageTag` points the task definitions at the scanned ECR artifact:
 * dev runs its own repo, and preprod runs the PROD repo so the §10 release rehearsal
 * exercises the exact artifact prod will run.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function computeTemplate(envName: EnvName, imageTag?: string) {
  const app = new App({ context: imageTag ? { imageTag } : {} });
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

/** Every container image string across every task definition in the template. */
function containerImages(template: Template): string[] {
  const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
  const images: string[] = [];
  for (const res of Object.values(taskDefs)) {
    const containers = (res as { Properties: { ContainerDefinitions: unknown[] } }).Properties
      .ContainerDefinitions as { Image: unknown }[];
    for (const c of containers) {
      // fromEcrRepository renders Image as an Fn::Join over the repo ARN/name and
      // tag; stringify so both literal and joined forms are searchable.
      images.push(JSON.stringify(c.Image));
    }
  }
  return images;
}

const REPO_BY_ENV = { dev: 'luciel-backend-dev', preprod: 'luciel-backend-prod', prod: 'luciel-backend-prod' } as const;

describe('deployed image provenance', () => {
  for (const envName of ['dev', 'preprod', 'prod'] as const) {
    test(`${envName}: with -c imageTag every task definition runs the gated ECR artifact`, () => {
      const images = containerImages(computeTemplate(envName, 'gitsha123'));
      expect(images.length).toBeGreaterThanOrEqual(4); // both planes + one-offs + sweep
      for (const image of images) {
        expect(image).toContain(REPO_BY_ENV[envName]);
        expect(image).toContain('gitsha123');
        // Never a CDK asset: an asset would be a fresh unscanned build.
        expect(image).not.toContain('cdk-hnb659fds-container-assets');
      }
    });
  }

  test('preprod runs the PROD repo image — the rehearsal must use the release artifact', () => {
    expect(getEnvConfig('preprod').imageRepoName).toBe('luciel-backend-prod');
    expect(getEnvConfig('prod').imageRepoName).toBe('luciel-backend-prod');
    expect(getEnvConfig('dev').imageRepoName).toBe('luciel-backend-dev');
  });

  test('without -c imageTag (tests/synth) a neutral placeholder is used, never an asset build', () => {
    const images = containerImages(computeTemplate('dev'));
    for (const image of images) {
      expect(image).toContain('public.ecr.aws/docker/library/python');
    }
  });
});
