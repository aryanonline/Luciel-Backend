/**
 * Fargate on Graviton (round 7 WP-8, owner decision D7 2026-09-14).
 *
 * A task definition that says ARM64 over an amd64-only image is a service that
 * never starts, so the move is only safe as a PAIR: every backend task
 * definition declares ARM64, AND the pipeline pushes a multi-arch image (buildx,
 * linux/amd64 + linux/arm64 under one tag) whose gate refuses an index missing
 * the arm64 child. Both halves are pinned here. The web image is built by the
 * Luciel-Frontend repo, so the web task stays x86 until THAT CI builds
 * multi-arch — pinned too, so nobody flips it early.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack, GRAVITON_RUNTIME } from '../lib/compute-stack';
import { DnsStack } from '../lib/dns-stack';
import { WebStack } from '../lib/web-stack';
import { getEnvConfig, OFFERING_REGION, EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };
const REPO_ROOT = path.resolve(__dirname, '..', '..');

type TaskDefProps = {
  Family?: string;
  RuntimePlatform?: { CpuArchitecture?: string; OperatingSystemFamily?: string };
};

function backendTaskDefs(envName: EnvName) {
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
  return Template.fromStack(compute).findResources('AWS::ECS::TaskDefinition');
}

function webTaskDefs() {
  const app = new App();
  const envConfig = getEnvConfig('dev');
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const dns = new DnsStack(app, 'Dns', { env: ENV, envConfig });
  const web = new WebStack(app, 'Web', {
    env: ENV,
    envConfig,
    vpc: net.vpc,
    webCert: dns.webCert,
    hostedZone: dns.hostedZone,
  });
  return Template.fromStack(web).findResources('AWS::ECS::TaskDefinition');
}

describe('Backend Fargate tasks run on Graviton (D7)', () => {
  test.each([['dev'], ['prod']])(
    '%s: every backend task definition (both planes, migrate, verify, seedtest, sweep) declares ARM64/LINUX',
    (envName) => {
      const defs = backendTaskDefs(envName as EnvName);
      const ids = Object.keys(defs);
      expect(ids).toHaveLength(6);
      for (const id of ids) {
        const props = defs[id].Properties as TaskDefProps;
        expect({ id, platform: props.RuntimePlatform }).toEqual({
          id,
          platform: { CpuArchitecture: 'ARM64', OperatingSystemFamily: 'LINUX' },
        });
      }
    },
  );

  test('the shared runtime const is ARM64 on Linux', () => {
    expect(GRAVITON_RUNTIME.cpuArchitecture).toBe(ecs.CpuArchitecture.ARM64);
    expect(GRAVITON_RUNTIME.operatingSystemFamily).toBe(ecs.OperatingSystemFamily.LINUX);
  });

  test('the web task does NOT declare ARM64 yet (its image is built amd64-only by the frontend repo)', () => {
    const defs = webTaskDefs();
    expect(Object.keys(defs)).toHaveLength(1);
    for (const def of Object.values(defs)) {
      const platform = (def.Properties as TaskDefProps).RuntimePlatform;
      expect(platform?.CpuArchitecture ?? 'X86_64').toBe('X86_64');
    }
  });
});

describe('The pipeline pushes a multi-arch image before any task says ARM64', () => {
  // Normalise line endings: a Windows checkout (core.autocrlf) hands the workflow
  // text back with CRLF, and the buildx continuation regex below keys on `\` + LF.
  const read = (rel: string) =>
    fs.readFileSync(path.join(REPO_ROOT, rel), 'utf8').replace(/\r\n/g, '\n');

  test.each([['.github/workflows/deploy.yml'], ['.github/workflows/image-rebuild.yml']])(
    '%s builds linux/amd64 + linux/arm64 with buildx and gates through the multi-arch gate',
    (rel) => {
      const text = read(rel);
      // No single-arch build path may remain: a bare `docker build` on the amd64
      // runner pushes an image the ARM64 task definitions cannot run.
      expect(text).not.toMatch(/^\s*docker build\s/m);
      expect(text).not.toMatch(/^\s*docker push\s/m);
      const buildx = text.match(/docker buildx build[^\n]*\n(?:[^\n]*\\\n)*[^\n]*/g) ?? [];
      expect(buildx.length).toBeGreaterThan(0);
      for (const invocation of buildx) {
        expect(invocation).toContain('--platform linux/amd64,linux/arm64');
        expect(invocation).toContain('--push');
      }
      expect(text).toContain('docker/setup-qemu-action');
      expect(text).toContain('docker/setup-buildx-action');
      // The gate resolves and scans every platform child; a tag-level findings
      // lookup would read the index, which is not an image ECR can scan.
      expect(text).toContain('scripts/gate_image_scan.sh');
      expect(text).not.toContain('describe-image-scan-findings');
    },
  );

  test('the gate requires the arm64 child by default', () => {
    const gate = read('scripts/gate_image_scan.sh');
    expect(gate).toContain('REQUIRED_PLATFORMS="${REQUIRED_PLATFORMS:-linux/amd64 linux/arm64}"');
    expect(gate).toContain('scripts/check_image_scan.py');
  });
});
