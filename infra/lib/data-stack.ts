/**
 * Data layer - RDS Postgres (pgvector) + S3 buckets (DEVOPS §5.3, Arch §4.2/§5.9).
 *
 * - RDS Postgres in PRIVATE_ISOLATED subnets (never public), KMS-encrypted,
 *   PITR/backup retention 7 days (Commitment 3), Multi-AZ in preprod/prod.
 * - pgvector enabled via migrations (app/migrations), not here.
 * - The app connects as the NON-superuser, NON-BYPASSRLS `luciel_app` role
 *   (bootstrapped post-deploy) so RLS tenant isolation actually binds (LD 18).
 * - S3 buckets use the residency-hardened factory (SSE-KMS, region-Deny, no public).
 *
 * COST: this is the first stack with meaningful recurring cost (RDS instance +
 * storage + backups). It is GATE 1 - do not deploy before founder dollar sign-off.
 */
import { Stack, StackProps, RemovalPolicy, Duration, CfnOutput } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as elasticache from 'aws-cdk-lib/aws-elasticache';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';
import { ResidencyGuard } from './residency';

export interface DataStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly vpc: ec2.IVpc;
  readonly cmk: kms.IKey;
}

export class DataStack extends Stack {
  public readonly db: rds.DatabaseInstance;
  public readonly knowledgeBucket: s3.Bucket;
  public readonly exportBucket: s3.Bucket;
  public readonly dbSecurityGroup: ec2.SecurityGroup;
  /**
   * Redis connection URL for the app's `redis_url` (config.py, env prefix LUCIEL_).
   * `rediss://` because in-transit TLS is enabled on the replication group.
   * Passed to ComputeStack and injected as LUCIEL_REDIS_URL (mirrors dbSecretArn).
   */
  public readonly redisUrl: string;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);
    const { envConfig, vpc, cmk } = props;

    const residency = new ResidencyGuard(this, 'Residency');

    // SG: Postgres reachable ONLY from inside the VPC (private app/data subnets).
    // We scope ingress to the VPC's private CIDR rather than referencing the app
    // SG from the compute stack, which would create a Data<->Compute dependency
    // cycle. Postgres is in PRIVATE_ISOLATED subnets and never public, so VPC-CIDR
    // ingress = app-tier-only in practice. (Caught by CDK tests.)
    this.dbSecurityGroup = new ec2.SecurityGroup(this, 'DbSg', {
      vpc,
      description: 'Luciel RDS - ingress from VPC private CIDR only (never public)',
      allowAllOutbound: false,
    });
    this.dbSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.tcp(5432),
      'app tier (VPC-internal) to postgres',
    );

    this.db = new rds.DatabaseInstance(this, 'Postgres', {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16,
      }),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED }, // never public
      securityGroups: [this.dbSecurityGroup],
      instanceType: new ec2.InstanceType(envConfig.rdsInstanceClass.replace('db.', '')),
      multiAz: envConfig.multiAz,
      allocatedStorage: 20,
      maxAllocatedStorage: envConfig.envName === 'prod' ? 200 : 50,
      storageEncrypted: true,
      storageEncryptionKey: cmk,
      backupRetention: Duration.days(7), // Commitment 3 (PITR window)
      deleteAutomatedBackups: envConfig.envName !== 'prod',
      deletionProtection: envConfig.envName === 'prod',
      removalPolicy:
        envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      databaseName: 'luciel',
      // Master creds in Secrets Manager, KMS-encrypted. App uses luciel_app, NOT master.
      credentials: rds.Credentials.fromGeneratedSecret('luciel_master', {
        encryptionKey: cmk,
      }),
      enablePerformanceInsights: envConfig.alarmsArmed,
      cloudwatchLogsExports: ['postgresql'],
    });

    // Knowledge / artifact bucket with the full residency envelope.
    //
    // STORAGE-COST BOUNDING (Architecture §4.4 / Decision #38, P0-4): the customer-data
    // bucket carries a lifecycle envelope so its footprint PLATEAUS rather than growing
    // unbounded — transition cold at 90d, hard-delete at the 1-year retention boundary,
    // and bound noncurrent versions so prior versions never accumulate.
    //
    // TRANSCRIPT-ARCHIVE-BUCKET DECISION (spec step 2): the doctrine's separate
    // `vm-transcript-archive` bucket is NOT provisioned here. Transcripts currently live
    // only in Postgres (no hot→cold S3 offload ships yet), so a dedicated archive bucket
    // would be wired to nothing. Per the "make the minimal honest choice; don't invent an
    // unused bucket" guidance we apply the lifecycle envelope to the existing customer-data
    // (knowledge/artifact) bucket now; the dedicated transcript-archive bucket binds — with
    // this same envelope — when the hot→cold offload lands. The app-side age sweep
    // (app/lifecycle/retention.py) is the Postgres-side reconciliation of the same policy.
    this.knowledgeBucket = residency.hardenedBucket('Knowledge', {
      key: cmk,
      versioned: envConfig.envName === 'prod',
      removalPolicy:
        envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      lifecycleRules: [
        {
          id: 'customer-data-retention-envelope',
          enabled: true,
          // Transition current versions to cheaper storage as they age (§4.4).
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: Duration.days(90),
            },
            {
              storageClass: s3.StorageClass.GLACIER,
              transitionAfter: Duration.days(180),
            },
          ],
          // 1-year hard-delete backstop: an object cannot outlive the retention policy.
          expiration: Duration.days(365),
          // Bound prior (noncurrent) versions so the versioned bucket doesn't accumulate.
          noncurrentVersionExpiration: Duration.days(90),
          // Reap incomplete multipart uploads (cost hygiene, no retention meaning).
          abortIncompleteMultipartUploadAfter: Duration.days(7),
        },
      ],
    });

    // Account-export bundles (§3.6.6). app/lifecycle/export_store.py wrote to this
    // bucket and nothing created it (P1-14): in live mode the put_object failed
    // AccessDenied, and the docstring's claim that "the bucket's own lifecycle rule
    // bounds the object's life" was false — a ZIP holding a full tenant data dump had
    // no expiry at all once the 7-day presigned link went stale.
    //
    // Separate from the knowledge bucket rather than a prefix on it: these two have
    // genuinely different retention (7 days vs 1 year) and different blast radius. An
    // export bundle is every table a tenant owns in one object.
    this.exportBucket = residency.hardenedBucket('Export', {
      key: cmk,
      // Named because the app addresses it by name, and per-env because one hardcoded
      // name cannot exist twice — that is how a dev export lands in the prod bucket.
      bucketName: `luciel-${envConfig.envName}-export-bundles-${this.account}`,
      removalPolicy:
        envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      lifecycleRules: [
        {
          id: 'export-bundle-expiry',
          enabled: true,
          // The presigned link lives export_link_ttl_days (7). The object must not
          // outlive the link by any meaningful margin; the extra day is slack for a
          // download started just before expiry, not a retention period.
          expiration: Duration.days(8),
          abortIncompleteMultipartUploadAfter: Duration.days(1),
        },
      ],
    });

    // --- Redis (ElastiCache) — Commitment 9: Redis-backed LLM cap (§9 C9 / §4.6) -- #
    // Placed in the SAME PRIVATE_ISOLATED data tier as RDS (never public, no internet
    // route). SG ingress mirrors the RDS pattern: scope to the VPC private CIDR rather
    // than referencing the compute-stack appSg, which would create a Data<->Compute
    // dependency cycle. Redis is in PRIVATE_ISOLATED subnets and never public, so
    // VPC-CIDR ingress on 6379 = app-tier-only in practice. (Caught by CDK tests.)
    const redisSg = new ec2.SecurityGroup(this, 'RedisSg', {
      vpc,
      description: 'Luciel Redis - ingress 6379 from VPC private CIDR only (never public)',
      allowAllOutbound: false,
    });
    redisSg.addIngressRule(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.tcp(6379),
      'app tier (VPC-internal) to redis',
    );

    // Subnet group over the isolated data subnets so the cluster has no public route.
    const redisSubnets = new elasticache.CfnSubnetGroup(this, 'RedisSubnets', {
      description: 'Luciel Redis - isolated data subnets (never public)',
      subnetIds: vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }).subnetIds,
      cacheSubnetGroupName: `luciel-${envConfig.envName}-redis`,
    });

    // Single small node for dev; preprod/prod scale the node type via env if needed.
    // At-rest encryption uses the CMK; in-transit TLS is ON (so the app uses rediss://).
    const redis = new elasticache.CfnReplicationGroup(this, 'Redis', {
      replicationGroupDescription: `Luciel ${envConfig.envName} LLM-cap Redis`,
      engine: 'redis',
      cacheNodeType: 'cache.t4g.micro',
      numCacheClusters: 1, // single node for dev (Redis cannot scale to 0)
      automaticFailoverEnabled: false,
      multiAzEnabled: false,
      atRestEncryptionEnabled: true,
      kmsKeyId: cmk.keyId,
      transitEncryptionEnabled: true,
      cacheSubnetGroupName: redisSubnets.ref,
      securityGroupIds: [redisSg.securityGroupId],
      port: 6379,
    });
    redis.addDependency(redisSubnets);

    // rediss:// because in-transit TLS is enabled (matches config.py redis_url shape).
    this.redisUrl = `rediss://${redis.attrPrimaryEndPointAddress}:6379/0`;
    new CfnOutput(this, 'RedisEndpoint', {
      value: redis.attrPrimaryEndPointAddress,
      exportName: `Luciel-${envConfig.envName}-RedisEndpoint`,
    });
    new CfnOutput(this, 'RedisUrl', { value: this.redisUrl });
  }
}
