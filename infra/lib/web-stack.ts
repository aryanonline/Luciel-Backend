/**
 * Frontend web layer — SSR Next.js on ECS Fargate (mirrors ComputeStack, §5.5).
 *
 * apps/web (Luciel-Frontend) is a server-rendered Next.js app (force-dynamic +
 * per-request CSP middleware) — it must run as a long-lived Node server, NOT a
 * static export (see Luciel-Frontend/DEPLOY.md). So it deploys exactly like the
 * backend: an ECS Fargate service behind an ALB.
 *
 * ONE ALB PER ENVIRONMENT (round 7 WP-8b, owner decision D7). This stack used to run
 * its own internet-facing ALB (~$18/month standing, a second WAF association, a
 * second certificate). The web service now registers its target group on the
 * ComputeStack ALB behind a HOST rule — the environment's app hostnames
 * (env-config.ts `hostnames`: dev.vantagemind.ai, plus the D6 dual-host apex/www
 * while dev owns them) → this target group — and its A/AAAA aliases point at that
 * ALB. The listener rule, the target group and both security-group rules live HERE
 * (the shared listener and the ALB's security group are imported by id), so the
 * Compute stack never points back at this one.
 *
 * THE SWITCH IS ONE DEPLOY, WITHOUT A DARK WINDOW. The service the old ALB served is
 * replaced by a NEW service construct (`WebAppService`) rather than re-pointed: an
 * ECS service moved from one target group to another drains its old target while
 * the DNS still names the old ALB. CloudFormation instead (1) creates the target
 * group + host rule on the shared ALB, (2) creates the new service and waits until
 * its task is healthy behind that rule, (3) only then UPDATES the apex/www records
 * in place (same logical ids — an alias-target change is an atomic Route 53 UPSERT)
 * to the shared ALB, and (4) in its cleanup phase deletes the old service, the old
 * listeners, target group and ALB — after the records have moved, so nothing is
 * orphaned and no name ever points at an empty target group. bin/luciel-infra.ts
 * orders EdgeStack (which drops the old ALB's WebACL association) before this stack.
 *
 * CROSS-REPO IMAGE: the web image is built from the FRONTEND repo (Dockerfile at
 * apps/web/Dockerfile, build context = frontend repo root), which this backend CDK
 * app cannot see at synth. So — like the backend — the image is built in CI and
 * pushed to an ECR repo this stack OWNS (luciel-<env>-web). The service references
 * the image by tag (`-c webImageTag=<sha>`, default `latest`); the frontend CI
 * pushes the tag then rolls every service in the cluster. No Docker is needed at synth.
 *
 * Region-locked ca-central-1 via the residency guard, like every in-region stack.
 */
import { Stack, StackProps, Duration, CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53targets from 'aws-cdk-lib/aws-route53-targets';
import { Construct } from 'constructs';
import { EnvConfig, baseUrls, hostnames, recordIdFor } from './env-config';
import { ResidencyGuard } from './residency';
import { scaleOnLoad } from './service-scaling';
import { SERVICE_TASK_TAGGING } from './cost-tags';
import { LISTENER_RULE_PRIORITY, SharedIngress } from './compute-stack';
import { importPublicZone } from './dns-stack';

export interface WebStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly vpc: ec2.IVpc;
  /** The environment's ONE load balancer (ComputeStack.sharedIngress, D7). */
  readonly ingress: SharedIngress;
  /**
   * Write the app hostnames' A/AAAA aliases (to the shared ALB). Off by default so
   * the stack synthesizes standalone; bin/luciel-infra.ts turns it on.
   */
  readonly publicDns?: boolean;
}

export class WebStack extends Stack {
  public readonly service: ecs.FargateService;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  public readonly ecrRepo: ecr.Repository;
  public readonly cluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);
    const { envConfig, vpc, ingress, publicDns } = props;
    const names = hostnames(envConfig.envName);

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
      // TODO(round 7 WP-8, D7 Graviton): `runtimePlatform: GRAVITON_RUNTIME`
      // (compute-stack.ts) — ONLY after Luciel-Frontend's CI builds the web image
      // multi-arch (docker buildx --platform linux/amd64,linux/arm64 --push in
      // apps/web's workflow). This image is built by THAT repo, so flipping here
      // first would point an ARM64 task at an amd64-only image and the service
      // would never start. test/graviton.test.ts asserts this task stays x86
      // until the frontend precondition lands; the owner flips it afterwards.
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
        // also set the runtime API base so server components resolve correctly —
        // the environment's OWN api origin (D6: api-dev.* for dev), derived from the
        // same helper as the backend's API_BASE_URL.
        NEXT_PUBLIC_API_BASE_URL: baseUrls(envConfig.envName).api,
        NEXT_PUBLIC_API_ADAPTER: 'http',
      },
      portMappings: [{ containerPort: 3000 }],
    });

    // Cheap-idle pause: desiredCount 0 across the env when paused (§ pause/resume).
    const baseCount = envConfig.multiAz ? 2 : 1;
    // A NEW construct id on purpose (the old ALB's service was `WebService`): see the
    // header — the replacement comes up healthy behind the shared ALB BEFORE the
    // records move, and the old one is deleted only afterwards.
    this.service = new ecs.FargateService(this, 'WebAppService', {
      cluster: this.cluster,
      taskDefinition: taskDef,
      desiredCount: envConfig.paused ? 0 : baseCount,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      ...SERVICE_TASK_TAGGING,
      // Auto-rollback on a failed deploy (DEVOPS §5.5) — same protection as the
      // backend services (PR #17). Without it a bad/missing image churns for hours.
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
    });
    // Same load-following policy as the backend planes (round 7 WP-8b): CPU and
    // memory, floor = the base count, ceiling = the environment's maxWeb.
    scaleOnLoad(envConfig, this.service, 'Web', baseCount, envConfig.autoscaling.maxWeb);

    // The shared listener and the ALB's security group, imported in THIS stack's
    // scope: the target-group registration then writes the ALB→task egress rule and
    // the task's ingress rule here (allowAllOutbound:false is what lets the imported
    // group take the egress rule), and ComputeStack never references this stack.
    const albSg = ec2.SecurityGroup.fromSecurityGroupId(this, 'SharedAlbSg', ingress.securityGroupId, {
      allowAllOutbound: false,
    });
    const sharedListener = elbv2.ApplicationListener.fromApplicationListenerAttributes(
      this,
      'SharedListener',
      { listenerArn: ingress.listenerArn, securityGroup: albSg },
    );

    // The SSR container on 3000, health check on `/` (spec §2) — unchanged from the
    // retired ALB's target group. A 30 s deregistration delay (the default is 300 s):
    // an SSR request is short, and every web deploy waits out this drain.
    this.targetGroup = new elbv2.ApplicationTargetGroup(this, 'WebAppTargets', {
      vpc,
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [this.service],
      deregistrationDelay: Duration.seconds(30),
      healthCheck: {
        path: '/',
        healthyHttpCodes: '200-399',
        interval: Duration.seconds(30),
      },
    });
    // App hostnames → the web tier, ahead of every api rule (priority band appHost).
    const hostRule = new elbv2.ApplicationListenerRule(this, 'WebAppHostRule', {
      listener: sharedListener,
      priority: LISTENER_RULE_PRIORITY.appHost,
      conditions: [elbv2.ListenerCondition.hostHeaders([...names.appNames])],
      targetGroups: [this.targetGroup],
    });

    if (publicDns) {
      // Every app name -> the SHARED ALB (A + AAAA ALIAS). recordIdFor keeps the
      // apex and www on their original ApexA/ApexAaaa/WwwA ids so the deployed
      // records are UPDATED in place (an atomic alias-target change), never deleted
      // and re-created. Each record waits for the new service (and so its healthy
      // target behind the host rule) before it moves.
      const zone = importPublicZone(this);
      const albTarget = route53.RecordTarget.fromAlias(
        new route53targets.LoadBalancerTarget(ingress.alb),
      );
      for (const name of names.appNames) {
        const rid = recordIdFor(name);
        for (const rec of [
          new route53.ARecord(this, `${rid}A`, { zone, recordName: name, target: albTarget }),
          new route53.AaaaRecord(this, `${rid}Aaaa`, { zone, recordName: name, target: albTarget }),
        ]) {
          rec.node.addDependency(this.service, hostRule);
        }
      }
    }

    new CfnOutput(this, 'WebEcrRepoUri', { value: this.ecrRepo.repositoryUri });
    new CfnOutput(this, 'WebServiceName', { value: this.service.serviceName });
    // Exported (round 7 WP-8) so the cost guard workflow can read BOTH clusters'
    // desired counts before it re-stops a paused environment's database — the
    // backend ClusterName export was the only one, and a web service left
    // running was invisible to the guard. Same export naming as ComputeStack.
    new CfnOutput(this, 'WebClusterName', {
      value: this.cluster.clusterName,
      exportName: `Luciel-${envConfig.envName}-WebClusterName`,
    });
  }
}
