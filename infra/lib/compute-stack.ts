/**
 * Compute layer - ECS Fargate + ALB + SQS (DEVOPS §5.5, Arch §3.1/§4.6).
 *
 * One container image, TWO logical planes per §0 (control-plane = admin API,
 * data-plane = channel/widget ingress) run as separate ECS services with their
 * own task roles (role-split, not physical separation). Background workers
 * (health-check, hard-delete cascade, summarization) run as separate tasks off
 * SQS so a heavy tenant job never blocks request handling.
 *
 * The task role gets least-privilege: read its platform secret, use the CMK,
 * read/write the knowledge bucket, connect to RDS. NOT admin.
 */
import * as cdk from 'aws-cdk-lib';
import { Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface ComputeStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly vpc: ec2.IVpc;
  /** CMK ARN (imported by ARN to avoid a cross-stack dependency cycle). */
  readonly cmkArn: string;
  readonly knowledgeBucket: s3.IBucket;
  /** DB master-secret ARN (imported by ARN - avoids cross-stack cycle via its KMS key). */
  readonly dbSecretArn: string;
  /** Platform secret ARN (imported by ARN to avoid a cross-stack cycle via its KMS key). */
  readonly platformSecretArn: string;
  /** Path to the backend repo (contains the Dockerfile). */
  readonly backendPath: string;
}

export class ComputeStack extends Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly cluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);
    const { envConfig, vpc, cmkArn, knowledgeBucket, dbSecretArn, platformSecretArn, backendPath } =
      props;
    const dbSecret = secrets.Secret.fromSecretCompleteArn(this, 'ImportedDbSecret', dbSecretArn);
    // Import the CMK + platform secret by ARN: external (unowned) references, so
    // grants do not create a reverse construct edge back to SecurityStack (cycle).
    const cmk = kms.Key.fromKeyArn(this, 'ImportedCmk', cmkArn);
    const platformSecret = secrets.Secret.fromSecretCompleteArn(
      this,
      'ImportedPlatformSecret',
      platformSecretArn,
    );

    this.cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsights: envConfig.alarmsArmed });

    // App tier SG. Egress open (needs outbound for LLM/Twilio/etc. via NAT).
    // Ingress to Postgres is granted by the DB SG (VPC-CIDR rule in DataStack),
    // so we do NOT mutate the DB SG here - that would create a Data<->Compute cycle.
    const appSg = new ec2.SecurityGroup(this, 'AppSg', {
      vpc,
      description: 'Luciel app tier (ECS tasks)',
      allowAllOutbound: true,
    });

    // Background-worker queues (§3.8.5/§3.6.5/§3.4.7).
    const workerQueue = new sqs.Queue(this, 'WorkerQueue', {
      encryptionMasterKey: cmk,
      visibilityTimeout: Duration.seconds(120),
      deadLetterQueue: {
        maxReceiveCount: 5,
        queue: new sqs.Queue(this, 'WorkerDlq', { encryptionMasterKey: cmk }),
      },
    });

    // Container image. Built from the backend Dockerfile ONLY when -c buildImage=true
    // (requires a Docker daemon - i.e. the CI/CD runner, per §5.8: images are built
    // in the pipeline, not by hand). For synth/validation without Docker we use a
    // neutral placeholder so the data/networking/security stacks can be synthesized
    // and cost-reviewed independently of an image build.
    const buildImage = this.node.tryGetContext('buildImage') === 'true';
    const containerImage = buildImage
      ? ecs.ContainerImage.fromDockerImageAsset(
          new ecr_assets.DockerImageAsset(this, 'BackendImage', {
            directory: backendPath,
            platform: ecr_assets.Platform.LINUX_AMD64,
            // CRITICAL: exclude the infra dir + its cdk.out from the asset source.
            // The backend root CONTAINS infra/, so without this the asset staging
            // recurses into infra/cdk.out (its own output) -> ENAMETOOLONG. Also
            // trims the build context. Mirrors .dockerignore.
            exclude: [
              'infra',
              '.git',
              '.github',
              '.venv',
              'node_modules',
              'tests',
              '**/__pycache__',
              '.pytest_cache',
              '.ruff_cache',
              '.mypy_cache',
            ],
          }),
        )
      : ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.12-slim');

    // Least-privilege task role.
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Luciel ECS task role - least privilege (secrets, cmk, bucket, queue)',
    });
    // Grants are applied on the ROLE side only (inline policies referencing ARNs).
    // Using the resource-side grant helpers (secret.grantRead / cmk.grant*) would
    // mutate the Security/Data stacks and create a cross-stack dependency CYCLE
    // (Security<->Compute). Role-side policies keep the edges one-directional:
    // Compute depends on Security/Data, never the reverse. (Caught by CDK tests.)
    knowledgeBucket.grantReadWrite(taskRole); // same-stack (knowledge bucket arn is cross-stack but read/write only touches role + bucket policy in DataStack -> still one-way Compute->Data)
    workerQueue.grantConsumeMessages(taskRole);
    workerQueue.grantSendMessages(taskRole);

    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadSecrets',
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
        resources: [platformSecret.secretArn, dbSecret.secretArn],
      }),
    );
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'UseCmk',
        actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
        resources: [cmk.keyArn],
      }),
    );

    const makeTaskDef = (name: string) => {
      const td = new ecs.FargateTaskDefinition(this, `${name}Task`, {
        cpu: envConfig.envName === 'prod' ? 1024 : 256,
        memoryLimitMiB: envConfig.envName === 'prod' ? 2048 : 512,
        taskRole,
      });
      // The EXECUTION role resolves `secrets:` at task startup. It needs both
      // secret-read AND kms:Decrypt on the CMK (the secrets are CMK-encrypted).
      // CDK can't auto-add the KMS grant because the key is imported by ARN, so we
      // add it explicitly here. Without this: 'AccessDeniedException: Access to KMS
      // is not allowed' and tasks never start.
      td.addToExecutionRolePolicy(
        new iam.PolicyStatement({
          actions: ['kms:Decrypt', 'kms:DescribeKey'],
          resources: [cmk.keyArn],
        }),
      );
      td.addToExecutionRolePolicy(
        new iam.PolicyStatement({
          actions: ['secretsmanager:GetSecretValue'],
          resources: [dbSecret.secretArn, platformSecret.secretArn],
        }),
      );
      td.addContainer(`${name}Container`, {
        image: containerImage,
        logging: ecs.LogDrivers.awsLogs({ streamPrefix: `luciel-${name}` }),
        environment: {
          LUCIEL_ENV: envConfig.envName,
          LUCIEL_PLANE: name,
          // Integrations remain MOCK until a gated cutover flips them (GATE 2).
          LUCIEL_INTEGRATION_MODE: 'mock',
        },
        secrets: {
          LUCIEL_DB_SECRET: ecs.Secret.fromSecretsManager(dbSecret),
          LUCIEL_PLATFORM_SECRET: ecs.Secret.fromSecretsManager(platformSecret),
        },
        // Execution role must decrypt the secrets with the CMK; granted role-side
        // via the imported key (no reverse edge).
        // (cmk is an imported IKey; grantDecrypt only touches the role policy.)
        portMappings: [{ containerPort: 8000 }],
      });
      return td;
    };

    // Data plane behind the ALB (public ingress: widget, channels).
    const dataService = new ecs.FargateService(this, 'DataPlane', {
      cluster: this.cluster,
      taskDefinition: makeTaskDef('data-plane'),
      desiredCount: envConfig.multiAz ? 2 : 1,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    // Control plane (admin API) - same image, separate service/role-split.
    new ecs.FargateService(this, 'ControlPlane', {
      cluster: this.cluster,
      taskDefinition: makeTaskDef('control-plane'),
      desiredCount: 1,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    // One-off MIGRATION task definition (run by the pipeline before app deploy).
    // Same image; entrypoint overridden to scripts/migrate.sh. Gets master creds +
    // app password from Secrets Manager. Run inside the VPC (RDS is private).
    const migrationTask = new ecs.FargateTaskDefinition(this, 'MigrationTask', {
      cpu: 256,
      memoryLimitMiB: 512,
      taskRole,
    });
    migrationTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: [cmk.keyArn],
      }),
    );
    migrationTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [dbSecret.secretArn],
      }),
    );
    migrationTask.addContainer('MigrationContainer', {
      image: containerImage,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'luciel-migrate' }),
      command: ['bash', 'scripts/migrate.sh'],
      secrets: {
        LUCIEL_DB_SECRET: ecs.Secret.fromSecretsManager(dbSecret),
      },
      environment: { LUCIEL_ENV: envConfig.envName },
    });
    new cdk.CfnOutput(this, 'MigrationTaskArn', {
      value: migrationTask.taskDefinitionArn,
      exportName: `Luciel-${envConfig.envName}-MigrationTaskArn`,
    });
    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      exportName: `Luciel-${envConfig.envName}-ClusterName`,
    });

    this.alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
    });
    const listener = this.alb.addListener('Http', { port: 80, open: true });
    listener.addTargets('DataPlaneTargets', {
      port: 8000,
      targets: [dataService],
      healthCheck: { path: '/healthz', interval: Duration.seconds(30) },
    });
  }
}
