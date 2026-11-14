/**
 * Data layer - RDS Postgres (pgvector) + S3 buckets (DEVOPS §5.3, Arch §4.2/§5.9).
 *
 * - RDS Postgres in PRIVATE_ISOLATED subnets (never public), KMS-encrypted,
 *   PITR/backup retention 7 days (Commitment 3), Multi-AZ in preprod/prod.
 * - Created empty, or RESTORED from `envConfig.dbSnapshotIdentifier` when an
 *   environment names a snapshot (dev: its 2026-11-14 final snapshot). One-way: see
 *   the field doc in env-config.ts.
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
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';
import { ResidencyGuard } from './residency';

export interface DataStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly vpc: ec2.IVpc;
  readonly cmk: kms.IKey;
}

export class DataStack extends Stack {
  /**
   * The environment's Postgres: created empty, or restored from
   * `envConfig.dbSnapshotIdentifier`. Either way `db.secret` is the generated master
   * secret, attached to the instance (host, port, username, password) — what Compute
   * injects as LUCIEL_DB_SECRET.
   */
  public readonly db: rds.DatabaseInstance | rds.DatabaseInstanceFromSnapshot;
  public readonly transcriptArchiveBucket: s3.Bucket;
  public readonly exportBucket: s3.Bucket;
  public readonly voiceAudioBucket: s3.Bucket;
  /** Original uploaded knowledge files (§5.10 export promise, audit F016). */
  public readonly knowledgeOriginalsBucket: s3.Bucket;
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

    // What the database is, whether it is created empty or restored from a snapshot.
    const instanceProps: rds.DatabaseInstanceSourceProps = {
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
      backupRetention: Duration.days(7), // Commitment 3 (PITR window)
      deleteAutomatedBackups: envConfig.envName !== 'prod',
      // 2026-09-05 audit F003 (owner decision 6): dev serves the public product, so
      // its data stores are as hard to destroy as prod's — cost-neutral hardening.
      // Only the EPHEMERAL preprod stays destroyable (its teardown is the point).
      deletionProtection: envConfig.envName !== 'preprod',
      removalPolicy:
        envConfig.envName !== 'preprod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      enablePerformanceInsights: envConfig.productionPosture,
      cloudwatchLogsExports: ['postgresql'],
      // RDS creates /aws/rds/instance/<id>/postgresql itself, and a group RDS creates
      // never expires. This is the one log group in the app that is NOT ours to declare
      // directly, so CDK sets its retention through a custom resource instead.
      cloudwatchLogsRetention: envConfig.logRetention,
    };

    // Same construct id on both paths, so the instance and its secret keep their
    // logical ids and every consumer (the secret ARN Compute injects, the endpoint,
    // the DbSg wiring) is unchanged.
    if (envConfig.dbSnapshotIdentifier) {
      // RESTORE (env-config.ts dbSnapshotIdentifier). ONE-WAY: once an environment
      // has been restored, the field must stay set. Removing DBSnapshotIdentifier (or
      // changing it) makes CloudFormation REPLACE this database with a new, EMPTY one.
      //
      // Inherited from the snapshot, so not set here: the storage encryption and its
      // KMS key (the key the snapshot was taken under must still exist), the master
      // username (luciel_master, which the secret below must match) and the
      // `luciel` database itself (RDS does not take DBName on a PostgreSQL restore).
      // The master password is NEW: a generated secret, encrypted with the CMK like
      // the one it replaces, which RDS applies once the restore completes. The app's
      // luciel_app password derives from the master password (scripts/migrate.sh), so
      // the first migration after a restore re-sets it, as on a first deploy.
      this.db = new rds.DatabaseInstanceFromSnapshot(this, 'Postgres', {
        ...instanceProps,
        snapshotIdentifier: envConfig.dbSnapshotIdentifier,
        // Master creds in Secrets Manager, KMS-encrypted. App uses luciel_app, NOT master.
        credentials: rds.SnapshotCredentials.fromGeneratedSecret('luciel_master', {
          encryptionKey: cmk,
        }),
      });
    } else {
      this.db = new rds.DatabaseInstance(this, 'Postgres', {
        ...instanceProps,
        storageEncrypted: true,
        storageEncryptionKey: cmk,
        databaseName: 'luciel',
        // Master creds in Secrets Manager, KMS-encrypted. App uses luciel_app, NOT master.
        credentials: rds.Credentials.fromGeneratedSecret('luciel_master', {
          encryptionKey: cmk,
        }),
      });
    }

    // Transcript cold-storage bucket (Architecture §3.4.10 / §4.4 / Commitment #38).
    //
    // This REPLACES the former 'Knowledge' bucket, which no code path ever wrote to —
    // knowledge ingest is Postgres-only (pgvector chunks), so that bucket was a dead
    // resource wearing the customer-data lifecycle envelope as a placeholder. The
    // hot→cold transcript offload (app/lifecycle/transcript_archive.py, the
    // `transcript_archive` sweep) is now real, and this is its home.
    //
    // Anchoring (deliberate, D2): objects land here already ~90 days old (the sweep
    // moves transcripts at the 90-day hot boundary) and are written STANDARD_IA at
    // upload — no transition rules needed. The AUTHORITATIVE deleter at the 1-year
    // transcript-age boundary is the app retention sweep (app/lifecycle/retention.py),
    // which removes the S3 object and the session rows together and emits the
    // data_retention_hard_delete audit event. The bucket's own 365-days-from-upload
    // expiration is the §4.4 BACKSTOP for an object the app somehow failed to delete:
    // late-safe (worst case ~90 days past the boundary), never early — an
    // upload-anchored 275d rule would delete data EARLY whenever archival ran late.
    this.transcriptArchiveBucket = residency.hardenedBucket('TranscriptArchive', {
      key: cmk,
      // Named because the app addresses it by name (LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET),
      // per-env + per-account for global uniqueness (same rationale as the export bucket).
      bucketName: `luciel-${envConfig.envName}-transcript-archive-${this.account}`,
      versioned: true,
      removalPolicy:
        envConfig.envName !== 'preprod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY, // F003
      lifecycleRules: [
        {
          id: 'transcript-archive-backstop',
          enabled: true,
          // 1-year-from-upload hard-delete backstop (see anchoring note above).
          expiration: Duration.days(365),
          // Bound prior (noncurrent) versions so the versioned bucket doesn't accumulate:
          // a noncurrent version only exists after a re-archive overwrite or an app
          // delete, and neither is a state we retain (the app delete IS the deletion).
          // ONE day, not 30 (2026-09-05 audit F022): the app's delete removes every
          // version explicitly, so this tail only ever holds bytes a failed app
          // delete left behind — and those must not outlive the promise by a month.
          noncurrentVersionExpiration: Duration.days(1),
          // Reap incomplete multipart uploads (cost hygiene, no retention meaning).
          abortIncompleteMultipartUploadAfter: Duration.days(7),
        },
        {
          // The backstop's expiration on a versioned bucket writes a DELETE MARKER
          // rather than deleting; without this rule the marker (and the listing
          // entry behind it) lived on indefinitely (F022). S3 forbids combining
          // expired-object-delete-marker with a Days/Date expiration in one rule.
          id: 'transcript-archive-delete-marker-cleanup',
          enabled: true,
          expiredObjectDeleteMarker: true,
        },
      ],
    });

    // The customer's original uploaded knowledge files (§5.10 export promise; 2026-09-05
    // audit F016). No expiration: an original lives exactly as long as its source —
    // the source purge deletes its object and the tenant cascade sweeps the prefix.
    // Unversioned: the app's delete IS the deletion, and there is no re-upload path.
    this.knowledgeOriginalsBucket = residency.hardenedBucket('KnowledgeOriginals', {
      key: cmk,
      bucketName: `luciel-${envConfig.envName}-knowledge-originals-${this.account}`,
      removalPolicy:
        envConfig.envName !== 'preprod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY, // F003
      lifecycleRules: [
        {
          id: 'knowledge-originals-hygiene',
          enabled: true,
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
        envConfig.envName !== 'preprod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY, // F003
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

    // Voice reply audio (§3.1.2 <Play> hosting). One object per spoken turn:
    // ElevenLabs-synthesized mp3 that Twilio fetches once through a ≤15-minute
    // presigned link (app/runtime/voice_audio.py). The 1-day expiry is both the
    // storage plateau AND the deleted-tenant bound — these objects are deliberately
    // NOT in the delete cascade, because the lifecycle rule already erases a
    // departed tenant's audio within ~24-48h (S3 expiry rounds to midnight UTC),
    // vs the ~1-year exposure that forced transcripts into the cascade.
    this.voiceAudioBucket = residency.hardenedBucket('VoiceAudio', {
      key: cmk,
      // Named because the app addresses it by name (LUCIEL_VOICE_AUDIO_BUCKET),
      // per-env + per-account for global uniqueness (same rationale as exports).
      bucketName: `luciel-${envConfig.envName}-voice-audio-${this.account}`,
      removalPolicy:
        envConfig.envName !== 'preprod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY, // F003
      lifecycleRules: [
        {
          id: 'voice-audio-expiry',
          enabled: true,
          expiration: Duration.days(1),
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

    // Node type and count come from env-config (round 7 WP-8): a single node in
    // dev (Redis cannot scale to 0), primary + replica across the two AZs with
    // automatic failover in preprod/prod — the Multi-AZ replication Architecture
    // §4.4 promises. Before that knob every environment carried this stack's
    // dev shape. At-rest encryption uses the CMK; in-transit TLS is ON (so the
    // app uses rediss://).
    const redisMultiAz = envConfig.redisNodes > 1;
    const redis = new elasticache.CfnReplicationGroup(this, 'Redis', {
      replicationGroupDescription: `Luciel ${envConfig.envName} LLM-cap Redis`,
      engine: 'redis',
      cacheNodeType: envConfig.redisNodeType,
      numCacheClusters: envConfig.redisNodes,
      // Both require a second node; ElastiCache places the replica in another
      // AZ of the subnet group (isolated subnets in every AZ the VPC spans).
      automaticFailoverEnabled: redisMultiAz,
      multiAzEnabled: redisMultiAz,
      atRestEncryptionEnabled: true,
      kmsKeyId: cmk.keyId,
      transitEncryptionEnabled: true,
      cacheSubnetGroupName: redisSubnets.ref,
      securityGroupIds: [redisSg.securityGroupId],
      port: 6379,
      // Declared rather than defaulted. Everything in here is an LLM-cap counter with
      // a TTL — a restored snapshot would be a stale count, so a backup of it has no
      // recovery value and a retained one is pure storage cost.
      snapshotRetentionLimit: envConfig.redisSnapshotRetentionDays,
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
