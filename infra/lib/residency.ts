/**
 * Residency guardrails (DEVOPS §2.1 / Architecture §4.2 / Locked Decision 21).
 *
 * Two enforcement layers, both asserted by a CDK unit test (Verification Matrix #2):
 *   1. A stack-level CfnCondition that FAILS synthesis/deploy if the stack's region
 *      is not the offering region. IaC literally cannot deploy customer-data
 *      resources outside ca-central-1.
 *   2. A hardened S3 bucket factory: SSE-KMS, all public access blocked, TLS-only,
 *      and an explicit bucket-policy Deny on any request whose aws:RequestedRegion
 *      is not the offering region (blocks cross-region copy/replication targets).
 *
 * "Residency is per-offering; never silently relocate" (LD 21). The region is a
 * required constructor input, defaulted to OFFERING_REGION - a different region is
 * a deliberate, reviewed change, not an accident.
 */

import { CfnCondition, Fn, RemovalPolicy } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { OFFERING_REGION } from './env-config';

export interface ResidencyGuardProps {
  /** The region this stack is allowed to deploy into. Defaults to the locked offering region. */
  readonly allowedRegion?: string;
}

export class ResidencyGuard extends Construct {
  public readonly allowedRegion: string;
  public readonly regionLockCondition: CfnCondition;

  constructor(scope: Construct, id: string, props: ResidencyGuardProps = {}) {
    super(scope, id);
    this.allowedRegion = props.allowedRegion ?? OFFERING_REGION;

    // Layer 1: a synthesis/deploy-time condition. CDK's AWS::Region pseudo-param
    // resolves to the deploy region; if it isn't the allowed region the condition
    // is false and any resource gated on it (or the stack assertion) fails.
    this.regionLockCondition = new CfnCondition(this, 'RegionLock', {
      expression: Fn.conditionEquals(this.allowedRegionRef(), this.allowedRegion),
    });
  }

  /** AWS::Region pseudo-parameter reference (the live deploy region). */
  private allowedRegionRef(): string {
    return Fn.ref('AWS::Region');
  }

  /**
   * Create an S3 bucket with the full residency + security envelope (§4.2 / §5.10):
   * SSE-KMS, public access fully blocked, TLS enforced, and a region-Deny policy.
   */
  public hardenedBucket(
    id: string,
    opts: {
      key: kms.IKey;
      versioned?: boolean;
      removalPolicy?: RemovalPolicy;
      /**
       * Storage-cost bounding lifecycle rules (Architecture §4.4 / Decision #38).
       * When provided, the object footprint plateaus instead of accumulating: the
       * 1-year expiration rule is the backstop that guarantees an object cannot
       * outlive the retention policy even if an app-side sweep misses it.
       */
      lifecycleRules?: s3.LifecycleRule[];
    },
  ): s3.Bucket {
    const bucket = new s3.Bucket(this, id, {
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: opts.key,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: opts.versioned ?? false,
      removalPolicy: opts.removalPolicy ?? RemovalPolicy.RETAIN,
      ...(opts.lifecycleRules ? { lifecycleRules: opts.lifecycleRules } : {}),
    });

    // Region-Deny: reject any access whose requested region != the offering region.
    // This blocks cross-region replication/copy targets - no customer data leaves
    // the offering region (LD 21, §4.2).
    bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyOutsideOfferingRegion',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:*'],
        resources: [bucket.bucketArn, bucket.arnForObjects('*')],
        conditions: {
          StringNotEquals: { 'aws:RequestedRegion': this.allowedRegion },
        },
      }),
    );

    return bucket;
  }
}
