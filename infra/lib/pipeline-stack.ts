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
}

export class PipelineStack extends Stack {
  public readonly ecrRepo: ecr.Repository;
  public readonly deployRole: iam.Role;

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);
    const { envConfig, githubRepo } = props;

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

    // Push to ECR + read CDK bootstrap roles to run cdk deploy.
    this.ecrRepo.grantPullPush(this.deployRole);
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AssumeCdkRoles',
        actions: ['sts:AssumeRole'],
        resources: [`arn:aws:iam::${this.account}:role/cdk-*`],
      }),
    );
  }
}
