/**
 * Every resource in this app that grows without being told to stop (Milestone 3 B).
 *
 * These are not correctness properties — a stack with none of them deploys, serves
 * traffic, and passes every other test in this directory. They are the properties
 * whose absence is invisible: a log group with no retention looks identical to one
 * with retention until the bill arrives, and nothing about an untagged ECR layer or a
 * noncurrent S3 version appears in a console's default view. So they are asserted
 * here, resource by resource, because a review will not catch their removal.
 *
 * The rule the whole file encodes: an AWS default that is "keep forever" is never
 * acceptable by omission. Keeping something forever is allowed, but it has to be
 * written down.
 */
import { App, Aspects, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import { BoundedLambdaLogs } from '../lib/log-retention-aspect';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { DnsStack } from '../lib/dns-stack';
import { WebStack } from '../lib/web-stack';
import { WidgetStack } from '../lib/widget-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import {
  EnvName,
  getEnvConfig,
  OFFERING_REGION,
  CLOUDFRONT_CERT_REGION,
} from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute(envName: EnvName = 'dev') {
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
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
  });
  return Template.fromStack(compute);
}

function buildData(envName: EnvName = 'dev') {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  return Template.fromStack(
    new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk }),
  );
}

function buildWeb(envName: EnvName = 'dev') {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const dns = new DnsStack(app, 'Dns', { env: ENV, envConfig });
  return Template.fromStack(
    new WebStack(app, 'Web', {
      env: ENV,
      envConfig,
      vpc: net.vpc,
      webCert: dns.webCert,
      hostedZone: dns.hostedZone,
    }),
  );
}

function buildWidget(envName: EnvName = 'prod') {
  const app = new App();
  const holder = new Stack(app, 'Holder', { env: ENV });
  const cert = acm.Certificate.fromCertificateArn(
    holder,
    'Cert',
    `arn:aws:acm:${CLOUDFRONT_CERT_REGION}:111111111111:certificate/abc`,
  );
  return Template.fromStack(
    new WidgetStack(app, 'Widget', { env: ENV, envConfig: getEnvConfig(envName), cert }),
  );
}

/** Every log group in a template, as [logicalId, props] pairs. */
function logGroups(t: Template): [string, Record<string, unknown>][] {
  return Object.entries(t.findResources('AWS::Logs::LogGroup')).map(([id, r]) => [
    id,
    (r as { Properties?: Record<string, unknown> }).Properties ?? {},
  ]);
}

describe('No log group retains forever', () => {
  // Named individually rather than looped so a stack that stops emitting log groups
  // entirely fails here instead of passing an empty forEach.
  test.each([
    ['Compute', () => buildCompute(), 6],
    ['Web', () => buildWeb(), 1],
  ])('%s declares retention on all %i of its log groups', (_name, build, expected) => {
    const groups = logGroups(build() as Template);
    expect(groups).toHaveLength(expected as number);
    for (const [id, props] of groups) {
      expect(`${id}: ${props.RetentionInDays}`).toBe(`${id}: 30`); // dev = ONE_MONTH
    }
  });

  test('the sweep, migrate, verify and seedtest tasks each log to a bounded group', () => {
    const t = buildCompute();
    for (const plane of ['data-plane', 'control-plane', 'migrate', 'verify', 'seedtest', 'sweep']) {
      t.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: `/luciel/dev/${plane}`,
        RetentionInDays: 30,
      });
    }
  });

  test('no ECS container falls back to an implicitly created log group', () => {
    // awsLogs without an explicit group is the failure mode this whole section exists
    // for: ECS creates the group on first write, and a group ECS creates never expires.
    const defs = buildCompute().findResources('AWS::ECS::TaskDefinition');
    expect(Object.keys(defs).length).toBeGreaterThan(0);
    for (const [id, res] of Object.entries(defs)) {
      const containers = (res as { Properties: { ContainerDefinitions: unknown[] } }).Properties
        .ContainerDefinitions as { LogConfiguration?: { Options?: Record<string, unknown> } }[];
      for (const c of containers) {
        expect(`${id}: ${c.LogConfiguration?.Options?.['awslogs-group'] !== undefined}`).toBe(
          `${id}: true`,
        );
      }
    }
  });

  test('the functions CDK generates inside constructs we do not own are bounded too', () => {
    // The S3 auto-delete provider is the representative case: nothing in widget-stack.ts
    // mentions a Lambda, yet `autoDeleteObjects` produces one whose service-created log
    // group never expires. Without the aspect this test finds a function with no
    // LoggingConfig — which is precisely the state that is invisible in the template.
    const app = new App();
    const holder = new Stack(app, 'Holder', { env: ENV });
    const cert = acm.Certificate.fromCertificateArn(
      holder,
      'Cert',
      `arn:aws:acm:${CLOUDFRONT_CERT_REGION}:111111111111:certificate/abc`,
    );
    const widget = new WidgetStack(app, 'Widget', {
      env: ENV,
      envConfig: getEnvConfig('dev'),
      cert,
    });
    Aspects.of(app).add(new BoundedLambdaLogs(logs.RetentionDays.ONE_MONTH));

    const t = Template.fromStack(widget);
    const fns = Object.entries(t.findResources('AWS::Lambda::Function'));
    expect(fns.length).toBeGreaterThan(0);
    for (const [id, res] of fns) {
      const cfg = (res as { Properties: { LoggingConfig?: { LogGroup?: unknown } } }).Properties
        .LoggingConfig;
      expect(`${id}: ${cfg?.LogGroup !== undefined}`).toBe(`${id}: true`);
    }
    for (const [, props] of logGroups(t)) {
      expect(props.RetentionInDays).toBe(30);
    }
  });

  test('prod keeps app logs for the 90 days doctrine §5.1 asks for', () => {
    const app = new App();
    const envConfig = getEnvConfig('prod');
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
      dbSecretArn: data.db.secret!.secretArn,
      platformSecretArn: sec.platformSecret.secretArn,
      redisUrl: data.redisUrl,
    });
    // The observability stack declares NO log group of its own (the former
    // `/luciel/<env>/app` group had no producer and was removed — cost hygiene);
    // every real log group is declared by the container that writes to it.
    const obs = Template.fromStack(
      new ObservabilityStack(app, 'Obs', { env: ENV, envConfig, alb: compute.alb }),
    );
    obs.resourceCountIs('AWS::Logs::LogGroup', 0);
    for (const [, props] of logGroups(Template.fromStack(compute))) {
      expect(props.RetentionInDays).toBe(90);
    }
  });
});

describe('RDS keeps a bounded backup and log footprint', () => {
  test('backups are retained 7 days (Commitment 3 PITR window), not indefinitely', () => {
    buildData().hasResourceProperties('AWS::RDS::DBInstance', { BackupRetentionPeriod: 7 });
  });

  test('dev drops its automated backups on delete; prod keeps them', () => {
    buildData('dev').hasResourceProperties('AWS::RDS::DBInstance', {
      DeleteAutomatedBackups: true,
    });
    buildData('prod').hasResourceProperties('AWS::RDS::DBInstance', {
      DeleteAutomatedBackups: false,
      DeletionProtection: true,
    });
  });

  test('the exported postgresql log group gets a retention set for it', () => {
    // RDS, not CDK, creates /aws/rds/instance/<id>/postgresql, so CDK reaches it with
    // a LogRetention custom resource rather than an AWS::Logs::LogGroup.
    buildData().hasResourceProperties('Custom::LogRetention', { RetentionInDays: 30 });
  });
});

describe('ElastiCache takes no unbounded pile of snapshots', () => {
  test('snapshot retention is declared, not defaulted', () => {
    buildData().hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      SnapshotRetentionLimit: 0,
    });
  });
});

describe('Every S3 bucket plateaus', () => {
  test('the export bucket expires bundles just after the presigned link dies', () => {
    buildData().hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Id: 'export-bundle-expiry',
            ExpirationInDays: 8,
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 },
          }),
        ]),
      },
    });
  });

  test('the transcript archive bounds current AND noncurrent objects', () => {
    // Commitment #38: cold storage plateaus. The app retention sweep is the
    // authoritative 1-year deleter; this 365-days-from-upload rule is the §4.4
    // backstop (late-safe by design — objects land already ~90 days old).
    buildData().hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Id: 'transcript-archive-backstop',
            ExpirationInDays: 365,
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
          }),
        ]),
      },
    });
  });

  test('the widget bucket reaps superseded bundles but never the live one', () => {
    const t = buildWidget('prod');
    t.hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Id: 'widget-bundle-hygiene',
            Status: 'Enabled',
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 },
          }),
        ]),
      },
    });
    // An expiration on the CURRENT version would delete the bundle that is being
    // served. The rule must bound the history and nothing else.
    const buckets = Object.values(t.findResources('AWS::S3::Bucket'));
    for (const b of buckets) {
      const rules = (
        b as {
          Properties: { LifecycleConfiguration?: { Rules: Record<string, unknown>[] } };
        }
      ).Properties.LifecycleConfiguration?.Rules;
      for (const rule of rules ?? []) {
        expect(rule.ExpirationInDays).toBeUndefined();
      }
    }
  });
});

describe('ECR does not accumulate images nobody can name', () => {
  function pipelineTemplate() {
    const app = new App();
    return Template.fromStack(
      new PipelineStack(app, 'Pipeline', {
        env: ENV,
        envConfig: getEnvConfig('dev'),
        githubRepo: 'aryanonline/Luciel-Backend',
      }),
    );
  }

  test.each([
    ['backend', () => pipelineTemplate()],
    ['web', () => buildWeb()],
  ])('the %s repo expires untagged images and caps the tagged history', (_name, build) => {
    const repos = Object.values((build() as Template).findResources('AWS::ECR::Repository'));
    expect(repos).toHaveLength(1);
    const policy = JSON.parse(
      (repos[0] as { Properties: { LifecyclePolicy: { LifecyclePolicyText: string } } }).Properties
        .LifecyclePolicy.LifecyclePolicyText,
    ) as { rules: { selection: { tagStatus: string; countNumber: number } }[] };
    const untagged = policy.rules.find((r) => r.selection.tagStatus === 'untagged');
    expect(untagged?.selection.countNumber).toBe(10);
    expect(policy.rules.find((r) => r.selection.tagStatus === 'any')?.selection.countNumber).toBe(
      20,
    );
  });
});
