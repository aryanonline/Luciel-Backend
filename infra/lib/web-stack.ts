/**
 * Frontend web layer — SSR Next.js on ECS Fargate (mirrors ComputeStack, §5.5).
 *
 * apps/web (Luciel-Frontend) is a server-rendered Next.js app (force-dynamic +
 * per-request CSP middleware) — it must run as a long-lived Node server, NOT a
 * static export (see Luciel-Frontend/DEPLOY.md). So it deploys exactly like the
 * backend: an ECS Fargate service behind an ALB. apex vantagemind.ai + www ->
 * this ALB (no CloudFront in front of SSR; simplest correct entry per spec §1).
 *
 * CROSS-REPO IMAGE: the web image is built from the FRONTEND repo (Dockerfile at
 * apps/web/Dockerfile, build context = frontend repo root), which this backend CDK
 * app cannot see at synth. So — like the backend — the image is built in CI and
 * pushed to an ECR repo this stack OWNS (luciel-<env>-web). The service references
 * the image by tag (`-c webImageTag=<sha>`, default `latest`); the frontend CI
 * pushes the tag then rolls the service. No Docker is needed at synth.
 *
 * Region-locked ca-central-1 via the residency guard, like every in-region stack.
 */
import { Stack, StackProps, Duration, CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53targets from 'aws-cdk-lib/aws-route53-targets';
import { Construct } from 'constructs';
import { EnvConfig, DOMAINS } from './env-config';
import { ResidencyGuard } from './residency';

export interface WebStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly vpc: ec2.IVpc;
  /** Regional (ca-central-1) cert for vantagemind.ai (+www) — the web ALB listener. */
  readonly webCert: acm.ICertificate;
  /** Existing hosted zone (looked up in DnsStack) for the apex/www ALIAS records. */
  readonly hostedZone: route53.IHostedZone;
}

export class WebStack extends Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly service: ecs.FargateService;
  public readonly ecrRepo: ecr.Repository;
  public readonly cluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);
    const { envConfig, vpc, webCert, hostedZone } = props;

    new ResidencyGuard(this, 'Residency');

    // ECR repo the frontend CI pushes the web image into. MUTABLE so the CI can
    // re-push `:latest` for force-new-deployment rollouts (the cross-repo rollout
    // path; the backend repo uses IMMUTABLE+sha because it owns its own cdk deploy).
    // Each push is also tagged with the commit sha for traceability/rollback.
    this.ecrRepo = new ecr.Repository(this, 'WebRepo', {
      repositoryName: `luciel-${envConfig.envName}-web`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      lifecycleRules: [
        // MUTABLE tags mean every re-push of `:latest` orphans the image it replaced.
        // Those untagged layers are invisible in the console's default view and are
        // billed forever, so they get a tighter ceiling than the tagged history — and
        // this rule must precede the catch-all, which CDK requires to be last.
        { description: 'expire untagged images', tagStatus: ecr.TagStatus.UNTAGGED, maxImageCount: 10 },
        { description: 'keep the last 20 tagged images', maxImageCount: 20 },
      ],
    });

    // The image tag the service runs. Default `latest`; pinned per-deploy via
    // `-c webImageTag=<sha>`. The repo need not contain the image at synth time.
    const imageTag = (this.node.tryGetContext('webImageTag') as string) ?? 'latest';

    this.cluster = new ecs.Cluster(this, 'WebCluster', {
      vpc,
      containerInsights: envConfig.productionPosture,
    });

    const appSg = new ec2.SecurityGroup(this, 'WebSg', {
      vpc,
      description: 'Luciel web tier (Next.js SSR ECS tasks)',
      allowAllOutbound: true,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'WebTask', {
      cpu: envConfig.envName === 'prod' ? 1024 : 256,
      memoryLimitMiB: envConfig.envName === 'prod' ? 2048 : 512,
    });
    taskDef.addContainer('WebContainer', {
      image: ecs.ContainerImage.fromEcrRepository(this.ecrRepo, imageTag),
      // Explicit group so the retention is declared; an implicitly created one never
      // expires (same reasoning as ComputeStack).
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'luciel-web',
        logGroup: new logs.LogGroup(this, 'WebLogs', {
          logGroupName: `/luciel/${envConfig.envName}/web`,
          retention: envConfig.logRetention,
          removalPolicy:
            envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
        }),
      }),
      environment: {
        NODE_ENV: 'production',
        PORT: '3000',
        // Public, non-secret. The image bakes NEXT_PUBLIC_* at build time, but we
        // also set the runtime API base so server components resolve correctly.
        NEXT_PUBLIC_API_BASE_URL: `https://${DOMAINS.api}`,
        NEXT_PUBLIC_API_ADAPTER: 'http',
      },
      portMappings: [{ containerPort: 3000 }],
    });

    // Cheap-idle pause: desiredCount 0 across the env when paused (§ pause/resume).
    const baseCount = envConfig.multiAz ? 2 : 1;
    this.service = new ecs.FargateService(this, 'WebService', {
      cluster: this.cluster,
      taskDefinition: taskDef,
      desiredCount: envConfig.paused ? 0 : baseCount,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      // Auto-rollback on a failed deploy (DEVOPS §5.5) — same protection as the
      // backend services (PR #17). Without it a bad/missing image churns for hours.
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
    });

    this.alb = new elbv2.ApplicationLoadBalancer(this, 'WebAlb', {
      vpc,
      internetFacing: true,
    });

    // HTTP:80 -> redirect to HTTPS (SSR app should always be TLS).
    this.alb.addListener('Http', {
      port: 80,
      open: true,
      defaultAction: elbv2.ListenerAction.redirect({
        protocol: 'HTTPS',
        port: '443',
        permanent: true,
      }),
    });

    // HTTPS:443 -> the SSR container on 3000, health check on `/` (spec §2).
    const https = this.alb.addListener('Https', {
      port: 443,
      open: true,
      certificates: [webCert],
    });
    https.addTargets('WebTargets', {
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [this.service],
      healthCheck: {
        path: '/',
        healthyHttpCodes: '200-399',
        interval: Duration.seconds(30),
      },
    });

    // apex + www -> web ALB (A + AAAA ALIAS). Zone already exists (spec §1).
    const albTarget = route53.RecordTarget.fromAlias(
      new route53targets.LoadBalancerTarget(this.alb),
    );
    new route53.ARecord(this, 'ApexA', {
      zone: hostedZone,
      recordName: DOMAINS.apex,
      target: albTarget,
    });
    new route53.AaaaRecord(this, 'ApexAaaa', {
      zone: hostedZone,
      recordName: DOMAINS.apex,
      target: albTarget,
    });
    new route53.ARecord(this, 'WwwA', {
      zone: hostedZone,
      recordName: DOMAINS.www,
      target: albTarget,
    });

    new CfnOutput(this, 'WebEcrRepoUri', { value: this.ecrRepo.repositoryUri });
    new CfnOutput(this, 'WebServiceName', { value: this.service.serviceName });
    new CfnOutput(this, 'WebClusterName', { value: this.cluster.clusterName });
    new CfnOutput(this, 'WebAlbDns', { value: this.alb.loadBalancerDnsName });
  }
}
