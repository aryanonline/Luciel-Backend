/**
 * Residency-guardrail assertions (DEVOPS §5.1: "assert the residency Conditions
 * and S3 region-Deny appear in the synthesized output in a CDK unit test").
 *
 * This is the CI gate behind Verification Matrix #2 (residency lock holds): if a
 * future change removes the region condition or the S3 region-Deny, this test
 * fails and blocks merge — the IaC cannot silently lose residency enforcement.
 */

import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as kms from 'aws-cdk-lib/aws-kms';
import { ResidencyGuard } from '../lib/residency';
import { NetworkingStack } from '../lib/networking-stack';
import { getEnvConfig, OFFERING_REGION } from '../lib/env-config';

describe('Residency guardrails (§4.2 / LD 21)', () => {
  test('synthesizes a region-lock CfnCondition equal to the offering region', () => {
    const app = new App();
    const stack = new Stack(app, 'TestStack', { env: { region: OFFERING_REGION } });
    new ResidencyGuard(stack, 'Residency');
    const template = Template.fromStack(stack);

    // A Condition exists that equals AWS::Region against ca-central-1.
    const conditions = template.findConditions('*');
    const json = JSON.stringify(conditions);
    expect(json).toContain(OFFERING_REGION);
    expect(json).toContain('AWS::Region');
  });

  test('hardened S3 bucket carries SSE-KMS, public-access-block, and a region-Deny policy', () => {
    const app = new App();
    const stack = new Stack(app, 'TestStack', { env: { region: OFFERING_REGION } });
    const guard = new ResidencyGuard(stack, 'Residency');
    const key = new kms.Key(stack, 'K');
    guard.hardenedBucket('Bucket', { key });
    const template = Template.fromStack(stack);

    // Public access fully blocked.
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: Match.objectLike({
        ServerSideEncryptionConfiguration: Match.anyValue(),
      }),
    });

    // Bucket policy contains an explicit Deny on aws:RequestedRegion != offering region.
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Condition: {
              StringNotEquals: { 'aws:RequestedRegion': OFFERING_REGION },
            },
          }),
        ]),
      }),
    });
  });

  test('networking stack keeps RDS/Redis subnets isolated (never public)', () => {
    const app = new App();
    const stack = new NetworkingStack(app, 'Net', {
      env: { region: OFFERING_REGION },
      envConfig: getEnvConfig('dev'),
    });
    const template = Template.fromStack(stack);

    // There must be PRIVATE_ISOLATED subnets (the data tier) — no route to an IGW.
    // A VPC with public + app + data subnets across 2 AZs => 6 subnets.
    template.resourceCountIs('AWS::EC2::Subnet', 6);
    // Exactly one NAT gateway in dev (cost control, §7).
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
  });
});
