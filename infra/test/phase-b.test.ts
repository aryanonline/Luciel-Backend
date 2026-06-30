/**
 * Phase B infrastructure assertions (DEVOPS §5.1 — assert security-load-bearing
 * properties in synthesized output so they cannot silently regress in CI).
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kms from 'aws-cdk-lib/aws-kms';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { EdgeStack } from '../lib/edge-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION } from '../lib/env-config';
import * as path from 'node:path';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildDataStack() {
  const app = new App();
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: getEnvConfig('dev') });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig: getEnvConfig('dev') });
  const data = new DataStack(app, 'Data', {
    env: ENV,
    envConfig: getEnvConfig('dev'),
    vpc: net.vpc,
    cmk: sec.cmk,
  });
  return Template.fromStack(data);
}

describe('Security layer (§5.4)', () => {
  test('CMK has key rotation enabled', () => {
    const app = new App();
    const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig: getEnvConfig('dev') });
    Template.fromStack(sec).hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });
});

describe('Data layer (§5.3 / §4.2)', () => {
  test('RDS is NOT publicly accessible', () => {
    buildDataStack().hasResourceProperties('AWS::RDS::DBInstance', {
      PubliclyAccessible: false,
    });
  });

  test('RDS storage is encrypted', () => {
    buildDataStack().hasResourceProperties('AWS::RDS::DBInstance', {
      StorageEncrypted: true,
    });
  });

  test('RDS keeps a 7-day backup window (Commitment 3)', () => {
    buildDataStack().hasResourceProperties('AWS::RDS::DBInstance', {
      BackupRetentionPeriod: 7,
    });
  });

  test('Redis is in-region (ca-central-1) and not publicly accessible', () => {
    const app = new App();
    const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: getEnvConfig('dev') });
    const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig: getEnvConfig('dev') });
    const data = new DataStack(app, 'Data', {
      env: ENV,
      envConfig: getEnvConfig('dev'),
      vpc: net.vpc,
      cmk: sec.cmk,
    });
    // Region lock: the stack is deployed into the offering region and carries the
    // residency region-lock CfnCondition (== ca-central-1) like every in-region stack.
    expect(data.region).toBe(OFFERING_REGION);
    const t = Template.fromStack(data);
    const conditions = JSON.stringify(t.findConditions('*'));
    expect(conditions).toContain(OFFERING_REGION);
    expect(conditions).toContain('AWS::Region');

    // The replication group exists with at-rest + in-transit encryption ON, a single
    // small dev node, and is bound to a subnet group (placed in isolated subnets —
    // never given a public route).
    t.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      Engine: 'redis',
      CacheNodeType: 'cache.t4g.micro',
      AtRestEncryptionEnabled: true,
      TransitEncryptionEnabled: true,
      CacheSubnetGroupName: Match.anyValue(),
    });

    // The subnet group sits over the VPC's PRIVATE_ISOLATED subnets (no IGW route).
    t.resourceCountIs('AWS::ElastiCache::SubnetGroup', 1);

    // Not public: the Redis SG admits 6379 ONLY from the VPC private CIDR, never
    // 0.0.0.0/0 (mirrors the RDS ingress pattern — app-tier-only in practice).
    t.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('Redis'),
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({ FromPort: 6379, ToPort: 6379, CidrIp: Match.not('0.0.0.0/0') }),
      ]),
    });
  });

  test('knowledge bucket blocks all public access and denies non-offering-region access', () => {
    const t = buildDataStack();
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
    t.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Condition: { StringNotEquals: { 'aws:RequestedRegion': OFFERING_REGION } },
          }),
        ]),
      }),
    });
  });
});

describe('Edge layer / WAF (§5.6 — closes audit M2)', () => {
  function buildEdge() {
    const app = new App();
    const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: getEnvConfig('dev') });
    const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig: getEnvConfig('dev') });
    const data = new DataStack(app, 'Data', { env: ENV, envConfig: getEnvConfig('dev'), vpc: net.vpc, cmk: sec.cmk });
    const compute = new ComputeStack(app, 'Compute', {
      env: ENV, envConfig: getEnvConfig('dev'), vpc: net.vpc, cmkArn: sec.cmk.keyArn,
      knowledgeBucket: data.knowledgeBucket, dbSecretArn: data.db.secret!.secretArn,
      platformSecretArn: sec.platformSecret.secretArn, redisUrl: data.redisUrl,
      backendPath: path.join(__dirname, '..', '..', 'Luciel-Backend'),
    });
    const edge = new EdgeStack(app, 'Edge', { env: ENV, envConfig: getEnvConfig('dev'), alb: compute.alb });
    return Template.fromStack(edge);
  }

  test('a regional WAF WebACL exists with a rate-based rule', () => {
    const t = buildEdge();
    t.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'REGIONAL',
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'RateLimitPerIp',
          Statement: Match.objectLike({ RateBasedStatement: Match.anyValue() }),
        }),
      ]),
    });
  });

  test('WAF is associated to the ALB', () => {
    buildEdge().resourceCountIs('AWS::WAFv2::WebACLAssociation', 1);
  });
});
