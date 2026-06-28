/**
 * CI/CD pipeline foundation (DEVOPS §5.8).
 *
 * Provides the GitHub OIDC deploy role the pipeline assumes (no static keys - the
 * fix for INFRA-SEC-01) + the ECR repository the backend image is pushed to. The
 * actual build/test/deploy steps run in GitHub Actions (Docker-capable runners),
 * which is why the container image is built in the pipeline, not in the sandbox.
 *
 * Promotion model (§4): PR -> CI gate -> build+scan image -> deploy dev -> deploy
 * staging -> MANUAL approval gate -> prod. The manual gate is a GitHub Environment
 * protection rule (configured on the repo), enforced outside CloudFormation.
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface PipelineStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  /** GitHub org/repo allowed to assume the deploy role, e.g. "aryanonline/Luciel-Backend". */
  readonly githubRepo: string;
  /**
   * FRONTEND GitHub org/repo (e.g. "aryanonline/Luciel-Frontend") that gets its OWN
   * least-privilege OIDC deploy role: push the web image to ECR, sync the widget
   * bucket, invalidate CloudFront, and roll the web ECS service. Reuses the single
   * account-wide OIDC provider created here (a 2nd provider for the same URL would
   * conflict). When omitted, no frontend role is created.
   */
  readonly frontendRepo?: string;
}

export class PipelineStack extends Stack {
  public readonly ecrRepo: ecr.Repository;
  public readonly deployRole: iam.Role;
  public readonly frontendDeployRole?: iam.Role;

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);
    const { envConfig, githubRepo, frontendRepo } = props;

    // ECR repo for the backend image, with scan-on-push (CVE gate, §5.8).
    this.ecrRepo = new ecr.Repository(this, 'BackendRepo', {
      repositoryName: `luciel-backend-${envConfig.envName}`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      lifecycleRules: [{ maxImageCount: 20 }],
    });

    // GitHub OIDC provider (one per account; created here for the deploy role to trust).
    const oidcProvider = new iam.OpenIdConnectProvider(this, 'GitHubOidc', {
      url: 'https://token.actions.githubusercontent.com',
      clientIds: ['sts.amazonaws.com'],
    });

    // The role GitHub Actions assumes via OIDC - scoped to this repo + branch.
    // This REPLACES the exposed static admin key for CI deploys (INFRA-SEC-01 fix).
    this.deployRole = new iam.Role(this, 'GitHubDeployRole', {
      roleName: `luciel-${envConfig.envName}-github-deploy`,
      assumedBy: new iam.WebIdentityPrincipal(
        oidcProvider.openIdConnectProviderArn,
        {
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          },
          StringLike: {
            // Accept both branch-ref tokens AND environment-scoped tokens. Jobs that
            // declare `environment:` get a sub of `repo:<repo>:environment:<env>`,
            // not `ref:refs/heads/*` — so both forms must be allowed or OIDC assume
            // fails with 'Not authorized to perform sts:AssumeRoleWithWebIdentity'.
            'token.actions.githubusercontent.com:sub': [
              `repo:${githubRepo}:ref:refs/heads/*`,
              `repo:${githubRepo}:environment:*`,
            ],
          },
        },
      ),
      description: 'GitHub Actions OIDC deploy role (assumed by CI; no static keys).',
    });

    // Push to ECR + read scan findings (CVE gate) + read CDK bootstrap roles.
    this.ecrRepo.grantPullPush(this.deployRole);
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcrScanFindings',
        actions: ['ecr:DescribeImageScanFindings', 'ecr:DescribeImages'],
        resources: [this.ecrRepo.repositoryArn],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AssumeCdkRoles',
        actions: ['sts:AssumeRole'],
        resources: [`arn:aws:iam::${this.account}:role/cdk-*`],
      }),
    );

    // Permissions for run-migration-task.sh: read stack exports, run the one-off
    // migration ECS task, look up the VPC/subnets, and pass the task roles.
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CfnExports',
        actions: ['cloudformation:ListExports', 'cloudformation:DescribeStacks'],
        resources: ['*'],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RunMigrationTask',
        // run-migration-task.sh discovers the cluster/taskdef via CFN exports, then
        // looks up the service (ListServices + DescribeServices) to copy its network
        // config onto the one-off migration RunTask, waits, and reads the exit code.
        actions: [
          'ecs:RunTask',
          'ecs:DescribeTasks',
          'ecs:ListTasks',
          'ecs:ListServices',
          'ecs:DescribeServices',
        ],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:RequestedRegion': this.region } },
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DescribeNet',
        actions: ['ec2:DescribeSubnets', 'ec2:DescribeVpcs', 'ec2:DescribeSecurityGroups'],
        resources: ['*'],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PassTaskRoles',
        actions: ['iam:PassRole'],
        resources: [`arn:aws:iam::${this.account}:role/Luciel-${envConfig.envName}-Compute-*`],
      }),
    );

    // ---- Frontend (Luciel-Frontend) deploy role -----------------------------
    // Separate, least-privilege role for the frontend repo's CI. It builds the web
    // image + widget bundle (in the frontend repo) and delivers them to resources
    // THIS app owns: the luciel-<env>-web ECR repo, the widget S3 bucket, the widget
    // CloudFront distribution, and the web ECS service. Resource patterns mirror the
    // backend role (built from account/region + deterministic names) so there is no
    // cross-stack construct dependency. prod is NEVER auto-deployed — the manual
    // approval gate is a GitHub Environment rule, enforced outside CloudFormation.
    if (frontendRepo) {
      const region = this.region;
      const account = this.account;
      const webRepoArn = `arn:aws:ecr:${region}:${account}:repository/luciel-${envConfig.envName}-web`;
      const widgetBucketArn = `arn:aws:s3:::luciel-${envConfig.envName}-widget-${account}`;

      this.frontendDeployRole = new iam.Role(this, 'FrontendGitHubDeployRole', {
        roleName: `luciel-${envConfig.envName}-frontend-github-deploy`,
        assumedBy: new iam.WebIdentityPrincipal(oidcProvider.openIdConnectProviderArn, {
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          },
          StringLike: {
            'token.actions.githubusercontent.com:sub': [
              `repo:${frontendRepo}:ref:refs/heads/*`,
              `repo:${frontendRepo}:environment:*`,
            ],
          },
        }),
        description: 'GitHub Actions OIDC deploy role for Luciel-Frontend (no static keys).',
      });

      // Push the web image to its ECR repo (+ auth token, which is account-wide).
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'EcrAuth',
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        }),
      );
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'EcrPushWeb',
          actions: [
            'ecr:BatchCheckLayerAvailability',
            'ecr:CompleteLayerUpload',
            'ecr:InitiateLayerUpload',
            'ecr:PutImage',
            'ecr:UploadLayerPart',
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:DescribeImages',
            'ecr:DescribeImageScanFindings',
          ],
          resources: [webRepoArn],
        }),
      );
      // Sync the widget bundle to S3.
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'SyncWidgetBucket',
          actions: ['s3:PutObject', 's3:DeleteObject', 's3:GetObject', 's3:ListBucket'],
          resources: [widgetBucketArn, `${widgetBucketArn}/*`],
        }),
      );
      // Invalidate CloudFront (id unknown at synth; CreateInvalidation is low-risk).
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'InvalidateCdn',
          actions: ['cloudfront:CreateInvalidation', 'cloudfront:GetInvalidation', 'cloudfront:ListDistributions'],
          resources: ['*'],
        }),
      );
      // Roll the web ECS service (force-new-deployment on :latest). ECS service ARNs
      // are name-generated, so scope by region condition like the backend role.
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'RollWebService',
          actions: ['ecs:UpdateService', 'ecs:DescribeServices', 'ecs:DescribeTasks', 'ecs:ListTasks', 'ecs:ListServices'],
          resources: ['*'],
          conditions: { StringEquals: { 'aws:RequestedRegion': region } },
        }),
      );
      // PassRole for the web task + execution roles when registering task defs.
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'PassWebTaskRoles',
          actions: ['iam:PassRole'],
          resources: [`arn:aws:iam::${account}:role/Luciel-${envConfig.envName}-Web-*`],
        }),
      );
      // Parity with the backend role: allow assuming CDK bootstrap roles + reading
      // stack exports, in case the frontend CI runs `cdk deploy` for the web/widget
      // stacks rather than a force-new-deployment rollout.
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'AssumeCdkRoles',
          actions: ['sts:AssumeRole'],
          resources: [`arn:aws:iam::${account}:role/cdk-*`],
        }),
      );
      this.frontendDeployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'CfnExports',
          actions: ['cloudformation:ListExports', 'cloudformation:DescribeStacks'],
          resources: ['*'],
        }),
      );
    }
  }
}
