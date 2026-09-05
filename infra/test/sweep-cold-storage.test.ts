/**
 * The sweep plane must be told where cold storage actually lives.
 *
 * Every operation that touches an archived transcript runs in the SWEEP task, not in
 * the app planes: `transcript_archive` writes the 90-day offload (Commitment #38),
 * `retention` deletes the archived object at the 1-year boundary, and the hard-delete
 * cascade drops a closed tenant's entire prefix. The app planes had both bucket names
 * injected; the sweep task had neither.
 *
 * That omission could not fail loudly. `transcript_archive_bucket` carries a
 * placeholder default, so a sweep with no injected value still produces a perfectly
 * well-formed bucket NAME — `vm-transcript-archive`, which exists in no account we own.
 * In a live deployment the sweep would have addressed that phantom bucket on every run:
 * the archive put fails (so, correctly, no hot rows are deleted — but nothing is ever
 * archived either) and the retention delete targets a bucket that is not ours. The
 * customer-visible result is a retention promise that never executes while transcripts
 * accumulate in hot Postgres indefinitely.
 *
 * Asserting the values are CloudFormation references rather than strings is the part
 * that matters: it is what distinguishes "the stack wired the real per-environment
 * bucket" from "the container fell back to the default and looked configured".
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };
const COLD_STORAGE_VARS = [
  'LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET',
  'LUCIEL_EXPORT_BUCKET',
  // Original uploaded knowledge files (F016): written by the app planes, purged
  // by the sweep plane — both must know the bucket.
  'LUCIEL_KNOWLEDGE_ORIGINALS_BUCKET',
] as const;

function computeTemplate(envName: EnvName) {
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

describe('the sweep plane knows where cold storage is', () => {
  for (const envName of ['dev', 'preprod', 'prod'] as const) {
    test(`${envName}: the sweep container carries both bucket names`, () => {
      const template = computeTemplate(envName);
      for (const name of COLD_STORAGE_VARS) {
        const value = envValue(template, 'SweepContainer', name);
        expect(value).toBeDefined();
        // A Ref/Fn::* object, never a literal: a hardcoded string here would mean the
        // sweep is addressing something other than THIS environment's bucket.
        expect(typeof value).toBe('object');
      }
    });

    test(`${envName}: the sweep resolves the SAME buckets as the app planes`, () => {
      // A sweep pointed at a different bucket than the app writes to would archive into
      // storage the dashboard read-through never looks in — transcripts would appear to
      // vanish at 90 days even though the objects exist.
      const template = computeTemplate(envName);
      for (const name of COLD_STORAGE_VARS) {
        expect(envValue(template, 'SweepContainer', name)).toEqual(
          envValue(template, 'data-planeContainer', name),
        );
      }
    });
  }
});
