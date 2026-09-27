/**
 * Compute layer - ECS Fargate + ALB (DEVOPS §5.5, Arch §3.1/§4.6).
 *
 * One container image, TWO logical planes per §0 (control-plane = admin API,
 * data-plane = channel/widget ingress) run as separate ECS services with their
 * own task roles (role-split, not physical separation). Background work
 * (health-check, retention, cascade, summarization/finalization, transcript
 * archive) runs as EventBridge-scheduled one-shot ECS sweep tasks — never in
 * the request-handling planes, so a heavy tenant job never blocks requests.
 *
 * The task role gets least-privilege: read its platform secret, use the CMK,
 * read/write the transcript-archive + export buckets, connect to RDS. NOT admin.
 */
import * as cdk from 'aws-cdk-lib';
import { Stack, StackProps, Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ses from 'aws-cdk-lib/aws-ses';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53targets from 'aws-cdk-lib/aws-route53-targets';
import { Construct } from 'constructs';
import { EnvConfig, DOMAINS, SWEEP_ERROR_METRIC } from './env-config';

export interface ComputeStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly vpc: ec2.IVpc;
  /** CMK ARN (imported by ARN to avoid a cross-stack dependency cycle). */
  readonly cmkArn: string;
  readonly transcriptArchiveBucket: s3.IBucket;
  readonly exportBucket: s3.IBucket;
  /** Voice reply audio (§3.1.2 <Play> hosting) — presigned by the app planes. */
  readonly voiceAudioBucket: s3.IBucket;
  /** Original uploaded knowledge files (§5.10 export promise, audit F016). */
  readonly knowledgeOriginalsBucket: s3.IBucket;
  /** DB master-secret ARN (imported by ARN - avoids cross-stack cycle via its KMS key). */
  readonly dbSecretArn: string;
  /** Platform secret ARN (imported by ARN to avoid a cross-stack cycle via its KMS key). */
  readonly platformSecretArn: string;
  /**
   * Redis connection URL (from DataStack) injected as LUCIEL_REDIS_URL on both planes.
   * The app reads it as `redis_url` (config.py); the live LLM cap uses it (§9 C9).
   */
  readonly redisUrl: string;
  /**
   * Regional (ca-central-1) ACM cert for api.vantagemind.ai (from DnsStack). When
   * provided, an HTTPS:443 listener is added to the ALB and an api.vantagemind.ai
   * ALIAS record points at it. Optional so the stack still synthesizes standalone
   * (e.g. unit tests) before DNS is wired.
   */
  readonly apiCert?: acm.ICertificate;
  /** Existing hosted zone for the api ALIAS record (from DnsStack). */
  readonly hostedZone?: route53.IHostedZone;
}

/**
 * Paths the CONTROL plane answers (2026-09-05 audit F067). An ALB rule takes at
 * most five patterns, hence the grouping; every group becomes one rule on every
 * listener. Kept in sync with app/main.py CONTROL_PLANE_PREFIXES by the CDK test
 * (plane-routing.test.ts) and the Python test (tests/unit/test_plane_routing.py).
 */
export const CONTROL_PLANE_PATH_PATTERNS: ReadonlyArray<ReadonlyArray<string>> = [
  ['/api/v1/admin/*', '/api/v1/auth/*', '/api/v1/dashboard/*', '/api/v1/connections/*', '/api/v1/exports/*'],
  ['/api/v1/billing', '/api/v1/billing/*', '/api/v1/contact', '/api/v1/contact/*', '/api/v1/escalation-contact/*'],
];

/**
 * Every backend task runs on Graviton (round 7 WP-8, owner decision D7
 * 2026-09-14): ARM64 Fargate bills ~20% less per vCPU-hour than x86 for the
 * same work. Declaring ARM64 here is only safe because deploy.yml pushes a
 * MULTI-ARCH image (buildx, linux/amd64 + linux/arm64 under one tag) and
 * scripts/gate_image_scan.sh refuses a tag whose index lacks the arm64 child —
 * a task definition that says ARM64 over an amd64-only image is a service that
 * never starts (CannotPullContainerError). test/graviton.test.ts pins both
 * halves. The web task (web-stack.ts) stays x86 until the FRONTEND repo builds
 * its image multi-arch; see the TODO there.
 */
export const GRAVITON_RUNTIME: ecs.RuntimePlatform = {
  cpuArchitecture: ecs.CpuArchitecture.ARM64,
  operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
};

export class ComputeStack extends Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly cluster: ecs.Cluster;
  /** The sweep task definition family — the ObservabilityStack's task-failure rule
   *  matches on it (2026-09-05 audit F034). */
  public readonly sweepTaskFamily: string;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);
    const { envConfig, vpc, cmkArn, transcriptArchiveBucket, exportBucket, voiceAudioBucket, knowledgeOriginalsBucket, dbSecretArn, platformSecretArn, redisUrl, apiCert, hostedZone } =
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

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
      containerInsights: envConfig.productionPosture,
    });

    // App tier SG. Egress open (needs outbound for LLM/Twilio/etc. via NAT).
    // Ingress to Postgres is granted by the DB SG (VPC-CIDR rule in DataStack),
    // so we do NOT mutate the DB SG here - that would create a Data<->Compute cycle.
    const appSg = new ec2.SecurityGroup(this, 'AppSg', {
      vpc,
      description: 'Luciel app tier (ECS tasks)',
      allowAllOutbound: true,
    });

    // NOTE: the former WorkerQueue/WorkerDlq SQS pair was removed (cost hygiene):
    // no app code ever consumed or produced on it — background work runs as
    // EventBridge-scheduled ECS sweep tasks (sweepSchedules below), not off a queue.
    // A queue returns only if a consumer ships with it.

    // Container image: the SCANNED artifact from the pipeline's ECR repo, selected by
    // `-c imageTag=<git sha>`. This used to be a CDK DockerImageAsset built fresh at
    // deploy time — which meant the image the CVE gate scanned and the image ECS ran
    // were two *different builds* of the same context, and the gate never gated the
    // deployed bytes (and preprod, which had no build step at all, deployed with no
    // scan whatsoever). Consuming the gated tag makes the pipeline's scan → gate →
    // deploy sequence true of the artifact that actually runs (§5.8/§5.14). For
    // synth/validation without a pipeline (CDK tests, cost review) we fall back to a
    // neutral placeholder so no ECR lookup or Docker daemon is required.
    const imageTag = this.node.tryGetContext('imageTag') as string | undefined;
    // GATE 2 cutover flag. Defaults to 'mock' (EchoModel, no external LLM). A
    // deliberate `-c integrationMode=live` flips real Anthropic/OpenAI on — set per
    // environment, so dev can go live for verification while prod stays mock
    // until their own gated cutover. The flip is an explicit deploy parameter, never
    // a silent default (DEVOPS §6 / §2.8).
    const integrationMode = this.node.tryGetContext('integrationMode') === 'live' ? 'live' : 'mock';
    const containerImage = imageTag
      ? ecs.ContainerImage.fromEcrRepository(
          // By NAME, not a cross-stack export: the repo predates this stack (created
          // by the per-env PipelineStack) and preprod deliberately runs the PROD
          // repo's image — the release rehearses the exact gated artifact prod will
          // run, not a lookalike build (imageRepoName in env-config.ts).
          ecr.Repository.fromRepositoryName(this, 'GatedBackendImage', envConfig.imageRepoName),
          imageTag,
        )
      : ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.12-slim');

    // Least-privilege task role.
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Luciel ECS task role - least privilege (secrets, cmk, buckets)',
    });
    // Grants are applied on the ROLE side only (inline policies referencing ARNs).
    // Using the resource-side grant helpers (secret.grantRead / cmk.grant*) would
    // mutate the Security/Data stacks and create a cross-stack dependency CYCLE
    // (Security<->Compute). Role-side policies keep the edges one-directional:
    // Compute depends on Security/Data, never the reverse. (Caught by CDK tests.)
    // Everything an APP PLANE needs, granted role-side (see the note above) to
    // whichever role is handed in: the control plane's `taskRole` and the sweeps'
    // `sweepRole` hold this same set, so RLS and every seam behave identically for
    // a request and a sweep. The data plane (below) holds a narrower one.
    const grantAppPlaneAccess = (role: iam.Role) => {
      // Transcript cold storage (Commitment #38): the archive sweep writes/deletes
      // JSONL objects and the dashboard/export read them back. grantReadWrite includes
      // s3:DeleteObject*, which the retention sweep + cascade prefix-delete need.
      transcriptArchiveBucket.grantReadWrite(role);
      // Account-export ZIPs (§3.6.6). Without this the live put_object is AccessDenied
      // and the export the tenant was promised never appears (P1-14).
      exportBucket.grantReadWrite(role);
      // Voice reply audio (§3.1.2 <Play>): the data plane uploads each turn's mp3 and
      // presigns the GET Twilio fetches. The presigned URL is authorized AS this role,
      // so this grant plus UseCmk (the objects are CMK-encrypted) are what make the
      // fetch succeed — Twilio itself needs nothing.
      voiceAudioBucket.grantReadWrite(role);
      // Original uploaded knowledge files (F016): the app planes write them at upload
      // and read them into the export; the sweep plane deletes them at source purge /
      // tenant cascade. grantReadWrite covers all three.
      knowledgeOriginalsBucket.grantReadWrite(role);
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'ReadSecrets',
          actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
          resources: [platformSecret.secretArn, dbSecret.secretArn],
        }),
      );
      // Per-tenant connection credentials (Arch §3.8.3). app/connections/secrets.py
      // SecretsManagerStore creates/reads/rotates/deletes one secret per connection at
      // `vm/{admin_id}/{instance_id}/{connection_type}`. The ReadSecrets grant above
      // covers only the platform + DB secrets, so without this every credential submit
      // fails AccessDenied -> 500. Scoped to the `vm/` tenant prefix, never `*`, so the
      // task can reach connection secrets and nothing else in the account (§3.8.3 rule 5).
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'ManageTenantConnectionSecrets',
          actions: [
            'secretsmanager:CreateSecret',
            'secretsmanager:PutSecretValue',
            'secretsmanager:GetSecretValue',
            'secretsmanager:DescribeSecret',
            'secretsmanager:DeleteSecret',
            // CreateSecret with tags is authorized against TagResource; harmless today
            // (no Tags are passed) and required the moment tenant tagging is added.
            'secretsmanager:TagResource',
          ],
          // Secrets Manager appends a 6-char suffix to the name in the ARN, so the
          // prefix match is `secret:vm/*`.
          resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:vm/*`],
        }),
      );
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'UseCmk',
          actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
          resources: [cmk.keyArn],
        }),
      );
      // Transactional email (Arch §3.1.2 / §3.6.7): verification, password reset, export
      // links and dunning notices all go out through SESv2 (app/notifications/
      // email_sender.py). Without this grant a live-mode send returns AccessDenied and
      // no customer can complete signup, since unverified accounts are hard-gated.
      // Scoped to the offering region + this account's identities for the founder domain
      // — never a wildcard identity, so the task can only send AS VantageMind.
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'SendPlatformEmail',
          actions: ['ses:SendEmail', 'ses:SendRawEmail'],
          resources: [
            // Domain identity (covers every address under it), subdomain identities
            // (e.g. inbound.vantagemind.ai), and individually-verified addresses.
            `arn:aws:ses:${this.region}:${this.account}:identity/${DOMAINS.apex}`,
            `arn:aws:ses:${this.region}:${this.account}:identity/*.${DOMAINS.apex}`,
            `arn:aws:ses:${this.region}:${this.account}:identity/*@${DOMAINS.apex}`,
            `arn:aws:ses:${this.region}:${this.account}:configuration-set/*`,
          ],
        }),
      );
    };
    grantAppPlaneAccess(taskRole);

    // 2026-09-05 audit F067: the PUBLIC-INGRESS data plane (widget, channel webhooks)
    // holds a narrower role than the control plane and the sweeps. It reads a tenant's
    // credentials to send AS the tenant and rotates a refreshed OAuth token in place;
    // it never creates or deletes a tenant secret, never touches account exports or
    // uploaded knowledge originals, and can neither restart services nor rotate the
    // platform secret. A request-smuggling or deserialization hole on the plane that
    // faces the internet must not hand out the control plane's blast radius.
    const dataPlaneRole = new iam.Role(this, 'DataPlaneTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Luciel data-plane task role - narrower than the control plane (F067)',
    });
    voiceAudioBucket.grantReadWrite(dataPlaneRole);
    transcriptArchiveBucket.grantRead(dataPlaneRole);
    dataPlaneRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadSecrets',
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
        resources: [platformSecret.secretArn, dbSecret.secretArn],
      }),
    );
    dataPlaneRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'UseTenantConnectionSecrets',
        // Read to send as the tenant; PutSecretValue only for the in-place refresh of
        // an expiring OAuth token (app/connections/refresh.py) — no Create, no Delete.
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:DescribeSecret',
          'secretsmanager:PutSecretValue',
        ],
        resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:vm/*`],
      }),
    );
    dataPlaneRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'UseCmk',
        actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
        resources: [cmk.keyArn],
      }),
    );
    dataPlaneRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SendPlatformEmail',
        actions: ['ses:SendEmail', 'ses:SendRawEmail'],
        resources: [
          `arn:aws:ses:${this.region}:${this.account}:identity/${DOMAINS.apex}`,
          `arn:aws:ses:${this.region}:${this.account}:identity/*.${DOMAINS.apex}`,
          `arn:aws:ses:${this.region}:${this.account}:identity/*@${DOMAINS.apex}`,
          `arn:aws:ses:${this.region}:${this.account}:configuration-set/*`,
        ],
      }),
    );

    // 2026-09-05 audit F115: the sweeps run as their OWN role. It carries the app
    // planes' grants PLUS the two auto-swap grants no request-serving plane may
    // hold: rotating the platform secret and force-redeploying the app services
    // belong to the model_currency watchdog alone, so a compromised control plane
    // cannot rewrite the platform secret or restart the fleet.
    const sweepRole = new iam.Role(this, 'SweepTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Luciel sweep task role - app-plane grants plus the auto-swap grants (F115)',
    });
    grantAppPlaneAccess(sweepRole);

    // The public API origin, composed exactly the way WebStack composes
    // NEXT_PUBLIC_API_BASE_URL: `https://${DOMAINS.api}`. Both SNS webhook
    // subscriptions below land on routes under this origin (the ALB HTTPS:443
    // listener -> data plane, wired further down in this stack).
    const publicApiBaseUrl = `https://${DOMAINS.api}`;

    // --- SES events pipeline (bounce/complaint) ------------------------------------
    // Outbound platform email (verification, reset, export links, dunning/billing
    // notices) is sent through SES with NO feedback loop: a hard bounce or a spam
    // complaint today lands in the void, so the app keeps mailing a dead or
    // complaining address — the exact behavior that erodes SES sender reputation.
    // This wires the loop: sends stamped with the configuration set below publish
    // BOUNCE/COMPLAINT events to an SNS topic, which POSTs them to the app's
    // events webhook. Lives HERE because this stack owns the SES send IAM
    // (SendPlatformEmail above — note its configuration-set/* resource already
    // authorizes sending with this set).
    const sesConfigurationSetName = `luciel-${envConfig.envName}-platform`;
    const sesConfigSet = new ses.CfnConfigurationSet(this, 'SesConfigurationSet', {
      name: sesConfigurationSetName,
    });

    const sesEventsTopic = new sns.Topic(this, 'SesEventsTopic', {
      topicName: `luciel-${envConfig.envName}-ses-events`,
      displayName: `Luciel ${envConfig.envName} SES bounce/complaint events`,
    });
    // Only SES — and only SES acting for THIS account — may publish. Without the
    // SourceAccount condition any AWS customer could point their own SES event
    // destination at this topic and inject forged bounce events into the webhook.
    const sesEventsTopicPolicy = new sns.TopicPolicy(this, 'SesEventsTopicPolicy', {
      topics: [sesEventsTopic],
    });
    sesEventsTopicPolicy.document.addStatements(
      new iam.PolicyStatement({
        sid: 'AllowSesPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('ses.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [sesEventsTopic.topicArn],
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
    );

    const sesEventsDestination = new ses.CfnConfigurationSetEventDestination(
      this,
      'SesEventsDestination',
      {
        configurationSetName: sesConfigurationSetName,
        eventDestination: {
          name: `luciel-${envConfig.envName}-bounce-complaint`,
          enabled: true,
          matchingEventTypes: ['BOUNCE', 'COMPLAINT'],
          snsDestination: { topicArn: sesEventsTopic.topicArn },
        },
      },
    );
    // configurationSetName is a literal (no Ref), so CFN sees no implicit edge to
    // the config set; and SES validates it can publish at destination-creation
    // time, so the topic policy must exist first. Both orderings made explicit.
    sesEventsDestination.addDependency(sesConfigSet);
    sesEventsDestination.node.addDependency(sesEventsTopicPolicy);

    // CFN creates this subscription in PendingConfirmation: SNS POSTs a
    // SubscriptionConfirmation to the endpoint and the app route auto-confirms it
    // in LIVE integration mode only. A mock environment deliberately leaves the
    // subscription unconfirmed (there is no live consumer to confirm it) — that
    // is correct standing state, not a deploy failure.
    new sns.Subscription(this, 'SesEventsHttpsSubscription', {
      topic: sesEventsTopic,
      protocol: sns.SubscriptionProtocol.HTTPS,
      endpoint: `${publicApiBaseUrl}/api/v1/webhooks/ses/events`,
    });

    // --- SES inbound for platform reply addresses (codified console state) ---------
    // Round 5B item 17. Root CLAUDE.md documents the inbound path as OUT-OF-BAND
    // console ops (stood up 2026-08-17 after the vm_subdomain reply addresses
    // shipped unreceivable): receipt rule set `luciel-<env>-inbound` matching
    // `.reply.vantagemind.ai` -> SNS topic `luciel-<env>-ses-inbound` (content
    // inline) -> HTTPS subscription to /api/v1/webhooks/ses/inbound. This block
    // codifies those pieces so a NEW environment gets them from IaC.
    //
    // ============================ OPS CUTOVER NOTE =================================
    // DEPLOYMENT SAFETY — why every physical name below carries a `-v2` suffix:
    // the console-created rule set (`luciel-dev-inbound`) and topic
    // (`luciel-dev-ses-inbound`) ALREADY EXIST in the dev account. CloudFormation
    // cannot create a resource whose physical name is taken, and this repo has no
    // CDK import/adoption precedent (DnsStack's fromHostedZoneAttributes is a
    // by-reference lookup, not resource adoption), so reusing the exact names
    // would fail the very first deploy. The safest option — chosen and documented
    // here — is parallel `-v2` resources plus a deliberate operator cutover:
    //
    //   1. Deploy this stack: creates rule set `luciel-<env>-inbound-v2` (INERT —
    //      CFN never activates a receipt rule set) + topic
    //      `luciel-<env>-ses-inbound-v2` + its pending HTTPS subscription.
    //   2. Confirm the v2 topic's HTTPS subscription is Confirmed (live envs
    //      auto-confirm via the webhook route; see the events note above).
    //   3. Activate the v2 rule set: `aws ses set-active-receipt-rule-set
    //      --rule-set-name luciel-<env>-inbound-v2`. SES allows ONE active rule
    //      set per account+region, so this atomically deactivates the console
    //      rule set — inbound mail flow is uninterrupted.
    //   4. After verifying inbound replies still reach the webhook, delete the
    //      console-created rule set + topic by hand (they are unmanaged).
    //
    // Until step 3 runs, deploys are a no-op for live mail flow: the console rule
    // set stays active and the v2 resources sit idle. That is the intended safe
    // default — activation is an explicit ops decision, never a deploy side effect.
    // ===============================================================================
    //
    // ROUTE53 (documented, deliberately NOT codified): inbound also requires ONE
    // zone-global wildcard MX in the vantagemind.ai zone:
    //
    //   *.reply.vantagemind.ai  MX  10 inbound-smtp.ca-central-1.amazonaws.com
    //
    // The hosted zone IS referenced in-code (DnsStack), but the record is not
    // created here for two reasons: (1) it ALREADY EXISTS (console-created
    // 2026-08-17, root CLAUDE.md) and CloudFormation fails when creating an
    // existing record set — unlike the resources above, an MX record's NAME is
    // its contract, so it cannot be suffixed around the collision; (2) the record
    // is ZONE-GLOBAL while this stack is per-environment — dev and prod stacks
    // would both claim the same record, and tearing either down would delete it
    // out from under the other. Adoption would need `cdk import` or the
    // deleteExisting custom resource, both riskier than one documented record.
    const sesInboundTopic = new sns.Topic(this, 'SesInboundTopic', {
      topicName: `luciel-${envConfig.envName}-ses-inbound-v2`,
      displayName: `Luciel ${envConfig.envName} SES inbound reply mail (v2)`,
    });
    // Same publish lockdown as the events topic: SES only, this account only.
    const sesInboundTopicPolicy = new sns.TopicPolicy(this, 'SesInboundTopicPolicy', {
      topics: [sesInboundTopic],
    });
    sesInboundTopicPolicy.document.addStatements(
      new iam.PolicyStatement({
        sid: 'AllowSesPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('ses.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [sesInboundTopic.topicArn],
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
    );

    const sesInboundRuleSetName = `luciel-${envConfig.envName}-inbound-v2`;
    const sesInboundRuleSet = new ses.CfnReceiptRuleSet(this, 'SesInboundRuleSet', {
      ruleSetName: sesInboundRuleSetName,
    });
    const sesInboundRule = new ses.CfnReceiptRule(this, 'SesInboundRule', {
      ruleSetName: sesInboundRuleSetName,
      rule: {
        name: `luciel-${envConfig.envName}-inbound-reply`,
        enabled: true,
        // Leading dot = every address under the reply subdomain (the per-session
        // vm_* reply addresses), exactly what the console rule matched.
        recipients: [`.reply.${DOMAINS.apex}`],
        // SNS action = the full raw message delivered INLINE in the notification
        // ("content inline", matching the console rule), UTF-8 encoded. Only the
        // console-documented settings are codified — no scanEnabled/tlsPolicy
        // beyond the SES defaults the console rule ran with.
        actions: [
          {
            snsAction: {
              topicArn: sesInboundTopic.topicArn,
              encoding: 'UTF-8',
            },
          },
        ],
      },
    });
    // Same explicit ordering as the events destination: the literal ruleSetName
    // carries no Ref, and SES validates topic access when the rule is created.
    sesInboundRule.addDependency(sesInboundRuleSet);
    sesInboundRule.node.addDependency(sesInboundTopicPolicy);

    // Pending-confirmation semantics identical to the events subscription above:
    // the inbound route auto-confirms in live mode only; unconfirmed in mock
    // environments is the correct standing state.
    new sns.Subscription(this, 'SesInboundHttpsSubscription', {
      topic: sesInboundTopic,
      protocol: sns.SubscriptionProtocol.HTTPS,
      endpoint: `${publicApiBaseUrl}/api/v1/webhooks/ses/inbound`,
    });

    // Every awsLogs driver below names an EXPLICIT log group. Left to itself the driver
    // creates one on first write with retention "Never expire", which is the one AWS
    // default in this stack that converts a fixed cost into a growing one — and it is
    // invisible, because nothing in the template mentions the group at all.
    const logGroupFor = (name: string) =>
      new logs.LogGroup(this, `${name}Logs`, {
        logGroupName: `/luciel/${envConfig.envName}/${name}`,
        retention: envConfig.logRetention,
        removalPolicy:
          envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      });

    const makeTaskDef = (name: string, role: iam.IRole = taskRole) => {
      const td = new ecs.FargateTaskDefinition(this, `${name}Task`, {
        cpu: envConfig.envName === 'prod' ? 1024 : 256,
        memoryLimitMiB: envConfig.envName === 'prod' ? 2048 : 512,
        taskRole: role,
        runtimePlatform: GRAVITON_RUNTIME,
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
        // Entrypoint builds the luciel_app (non-superuser) DB URL from the secret
        // so RLS binds, then execs uvicorn.
        command: ['bash', 'scripts/app-entrypoint.sh'],
        logging: ecs.LogDrivers.awsLogs({
          streamPrefix: `luciel-${name}`,
          logGroup: logGroupFor(name),
        }),
        environment: {
          LUCIEL_ENV: envConfig.envName,
          LUCIEL_PLANE: name,
          // JSON lines with trace_id/admin_id (F035); CloudWatch extracts the EMF
          // metric lines from the same stream (F032).
          LUCIEL_LOG_FORMAT: 'json',
          // The data plane polls BYO mailboxes in-process every 5 minutes (cost:
          // 288 Fargate launches a day became a background task); the hourly
          // mailbox_poll schedule below is the backstop, same advisory lock.
          ...(name === 'data-plane' ? { LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS: '300' } : {}),
          // Integrations remain MOCK until a gated cutover flips them (GATE 2).
          LUCIEL_INTEGRATION_MODE: integrationMode,
          // Knowledge-graph extraction infers nodes/edges from prose through the fast
          // model at ingest (owner decision D10, round 7 WP-4c). The app binds it in
          // live posture only, so this is inert under a mock deploy; set explicitly
          // so the posture is visible in the task definition rather than implied.
          LUCIEL_GRAPH_EXTRACTOR: 'llm',
          // The Stripe seam has its OWN posture, one level down from the LLM cutover,
          // so Stripe can move to real TEST-mode calls without flipping the LLM flag.
          // Set on BOTH planes: the data plane reports usage on the conversation path
          // and the control plane serves the billing webhook and card routes.
          LUCIEL_STRIPE_INTEGRATION_POSTURE: envConfig.stripeIntegrationPosture,
          // Redis-backed LLM cap (§9 C9) and abuse counters (§3.1.5). Same URL on both
          // planes (makeTaskDef is called for data-plane + control-plane). App reads
          // it as redis_url.
          LUCIEL_REDIS_URL: redisUrl,
          // uvicorn --forwarded-allow-ips: trust X-Forwarded-For only from the VPC
          // (the ALB), so request.client.host is the address the ALB appended and a
          // client-written leftmost entry can never impersonate another visitor's IP
          // (per-IP abuse ceilings, §3.1.5).
          LUCIEL_TRUSTED_PROXY_CIDRS: vpc.vpcCidrBlock,
          // Per-env, so a dev export can never be written to the prod bucket.
          LUCIEL_EXPORT_BUCKET: exportBucket.bucketName,
          // Transcript cold storage (Commitment #38) — same per-env naming rule.
          LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET: transcriptArchiveBucket.bucketName,
          // Original uploaded knowledge files (F016). PRESENCE binds the S3 store.
          LUCIEL_KNOWLEDGE_ORIGINALS_BUCKET: knowledgeOriginalsBucket.bucketName,
          // Voice reply audio (§3.1.2 <Play> hosting). The PRESENCE of this var is
          // the binding gate (voice_audio.get_voice_audio_store) — deliberately not
          // injected into the sweep container, which never touches call audio.
          LUCIEL_VOICE_AUDIO_BUCKET: voiceAudioBucket.bucketName,
          // SES events pipeline: sends stamped with this configuration set get
          // bounce/complaint feedback published to the events topic; the webhook
          // route uses the topic ARN to pin which topic's envelopes it accepts.
          SES_CONFIGURATION_SET: sesConfigurationSetName,
          SES_EVENTS_TOPIC_ARN: sesEventsTopic.topicArn,
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

    // Cheap-idle pause (§ pause/resume): desiredCount 0 across the env when paused.
    const dataCount = envConfig.paused ? 0 : envConfig.multiAz ? 2 : 1;
    const controlCount = envConfig.paused ? 0 : 1;

    // Data plane behind the ALB (public ingress: widget, channels).
    const dataService = new ecs.FargateService(this, 'DataPlane', {
      cluster: this.cluster,
      taskDefinition: makeTaskDef('data-plane', dataPlaneRole),
      desiredCount: dataCount,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      // Auto-rollback on a failed deploy (DEVOPS §5.5). Without this, a deploy whose
      // tasks never reach healthy churns for up to 3h instead of failing fast.
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
    });

    // Control plane (admin API) - same image, separate service/role-split.
    const controlService = new ecs.FargateService(this, 'ControlPlane', {
      cluster: this.cluster,
      taskDefinition: makeTaskDef('control-plane'),
      desiredCount: controlCount,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
    });

    // 2026-09-05 audit F009: production posture scales on load instead of pinning a
    // task count. Target-tracking on CPU; the floor is the multi-AZ count, the
    // ceiling bounds cost. Dev keeps one task (it is single-AZ by design), and a
    // paused environment has nothing to scale.
    if (envConfig.productionPosture && !envConfig.paused) {
      dataService
        .autoScaleTaskCount({ minCapacity: dataCount, maxCapacity: 6 })
        .scaleOnCpuUtilization('DataPlaneCpu', {
          targetUtilizationPercent: 60,
          scaleInCooldown: Duration.minutes(5),
          scaleOutCooldown: Duration.minutes(2),
        });
      controlService
        .autoScaleTaskCount({ minCapacity: controlCount, maxCapacity: 3 })
        .scaleOnCpuUtilization('ControlPlaneCpu', {
          targetUtilizationPercent: 60,
          scaleInCooldown: Duration.minutes(5),
          scaleOutCooldown: Duration.minutes(2),
        });
    }

    // One-off MIGRATION task definition (run by the pipeline before app deploy).
    // Same image; entrypoint overridden to scripts/migrate.sh. Gets master creds +
    // app password from Secrets Manager. Run inside the VPC (RDS is private).
    const migrationTask = new ecs.FargateTaskDefinition(this, 'MigrationTask', {
      cpu: 256,
      memoryLimitMiB: 512,
      taskRole,
      runtimePlatform: GRAVITON_RUNTIME,
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
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'luciel-migrate',
        logGroup: logGroupFor('migrate'),
      }),
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

    // VERIFY task (§10 live probe): same image, runs scripts/verify.sh as luciel_app
    // against the live RDS to prove audit append-only, RLS, and no-secret-material on
    // real infra. Read-only/non-destructive (its mutating attempts are expected to be
    // REJECTED). Run via scripts/run-verify-task.sh, mirroring the migration runner.
    const verifyTask = new ecs.FargateTaskDefinition(this, 'VerifyTask', {
      cpu: 256,
      memoryLimitMiB: 512,
      taskRole,
      runtimePlatform: GRAVITON_RUNTIME,
    });
    verifyTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: [cmk.keyArn],
      }),
    );
    verifyTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [dbSecret.secretArn],
      }),
    );
    verifyTask.addContainer('VerifyContainer', {
      image: containerImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'luciel-verify',
        logGroup: logGroupFor('verify'),
      }),
      command: ['bash', 'scripts/verify.sh'],
      secrets: {
        LUCIEL_DB_SECRET: ecs.Secret.fromSecretsManager(dbSecret),
      },
      environment: { LUCIEL_ENV: envConfig.envName },
    });
    new cdk.CfnOutput(this, 'VerifyTaskArn', {
      value: verifyTask.taskDefinitionArn,
      exportName: `Luciel-${envConfig.envName}-VerifyTaskArn`,
    });

    // SEED + LIVE-BEHAVIOR TEST task (§10 live probe): same image, runs
    // scripts/seed_and_livetest.py against the live API+RDS to convert §10 rows
    // (live LLM happy path, free-cap zero-LLM/denial-of-wallet, per-admin 50/min
    // cap, fail-safe escalation, primary→fallback failover, channel parity +
    // AI-identity disclosure) from CI-proven to live-proven. Synthetic data only;
    // idempotent; needs the PLATFORM secret too (live LLM keys) plus the DB secret.
    // Run via scripts/run-seedtest-task.sh, mirroring the verify runner.
    const seedTestTask = new ecs.FargateTaskDefinition(this, 'SeedTestTask', {
      cpu: 256,
      memoryLimitMiB: 512,
      taskRole,
      runtimePlatform: GRAVITON_RUNTIME,
    });
    seedTestTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: [cmk.keyArn],
      }),
    );
    seedTestTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [dbSecret.secretArn, platformSecret.secretArn],
      }),
    );
    seedTestTask.addContainer('SeedTestContainer', {
      image: containerImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'luciel-seedtest',
        logGroup: logGroupFor('seedtest'),
      }),
      command: ['python3', 'scripts/seed_and_livetest.py'],
      secrets: {
        LUCIEL_DB_SECRET: ecs.Secret.fromSecretsManager(dbSecret),
        LUCIEL_PLATFORM_SECRET: ecs.Secret.fromSecretsManager(platformSecret),
      },
      environment: {
        LUCIEL_ENV: envConfig.envName,
        // Same gated cutover flag as the app: 'live' exercises real Anthropic/OpenAI.
        LUCIEL_INTEGRATION_MODE: integrationMode,
        // Wire the real Redis so the cap tests (T2/T3) exercise the Redis-backed
        // store exactly as the app does, not the in-memory degraded fallback.
        LUCIEL_REDIS_URL: redisUrl,
        // The API the harness drives (scripts/seed_and_livetest.py reads
        // LUCIEL_API_BASE and otherwise defaults to the public hostname). Injected
        // per environment (round 7 WP-8): without it the PREPROD rehearsal drove
        // whatever the public hostname pointed at while reading preprod's own
        // database — a harness proving one environment's behavior against
        // another's rows. Derived from DOMAINS.api for now; the per-environment
        // hostname move (owner console work) changes this ONE value and nothing
        // in the harness.
        LUCIEL_API_BASE: publicApiBaseUrl,
      },
    });
    new cdk.CfnOutput(this, 'SeedTestTaskArn', {
      value: seedTestTask.taskDefinitionArn,
      exportName: `Luciel-${envConfig.envName}-SeedTestTaskArn`,
    });

    // --- Scheduled background sweeps (Legal §B5 / §A3) -----------------------------
    // Before this, the repo had NO scheduler of any kind, so four compliance sweeps
    // whose logic was fully implemented never ran: retention hard-delete, day-30
    // grace-window purge, close-account purge, and the day-7 dunning advance. Each is
    // one invocation of `python -m app.jobs <sweep>` against the SAME image and the
    // same non-superuser DB role the app uses, so RLS applies to the sweeps exactly as
    // it does to a request (the sweeps do per-tenant work in per-tenant transactions).
    const sweepTask = new ecs.FargateTaskDefinition(this, 'SweepTask', {
      cpu: 256,
      memoryLimitMiB: 512,
      taskRole: sweepRole,
      runtimePlatform: GRAVITON_RUNTIME,
    });
    this.sweepTaskFamily = sweepTask.family;
    // The model_currency watchdog's RETIREMENT AUTO-SWAP (owner decision, §3.4.3):
    // when a pinned chat model disappears at the provider, the sweep writes the
    // same-family successor into the platform secret and force-redeploys the app
    // services so fresh tasks read it. Both grants sit on the SWEEP role only
    // (F115): PutSecretValue is limited to the ONE platform secret the planes can
    // already read, and UpdateService to the app services — nothing else in the
    // account, and nothing a request-serving plane can reach.
    sweepRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RotatePlatformSecret',
        actions: ['secretsmanager:PutSecretValue'],
        resources: [platformSecret.secretArn],
      }),
    );
    // Scoped by the CLUSTER's service namespace rather than the two service ARNs:
    // referencing dataService/controlService construct attributes here creates a
    // CloudFormation cycle (service → role default policy → service). The cluster
    // is created before the role, so its name is cycle-free, and nothing but the
    // two app planes lives in it — same effective blast radius, deployable graph.
    sweepRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RestartAppServices',
        actions: ['ecs:UpdateService'],
        resources: [
          `arn:${this.partition}:ecs:${this.region}:${this.account}:service/${this.cluster.clusterName}/*`,
        ],
      }),
    );
    sweepTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: [cmk.keyArn],
      }),
    );
    sweepTask.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [dbSecret.secretArn, platformSecret.secretArn],
      }),
    );
    const sweepContainerName = 'SweepContainer';
    const sweepLogGroup = logGroupFor('sweep');
    sweepTask.addContainer(sweepContainerName, {
      image: containerImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'luciel-sweep',
        logGroup: sweepLogGroup,
      }),
      // Placeholder command; every schedule overrides it with its own sweep name so
      // one task definition backs all four schedules.
      command: ['bash', '-lc', 'scripts/run-sweep.sh retention'],
      secrets: {
        LUCIEL_DB_SECRET: ecs.Secret.fromSecretsManager(dbSecret),
        LUCIEL_PLATFORM_SECRET: ecs.Secret.fromSecretsManager(platformSecret),
      },
      environment: {
        LUCIEL_ENV: envConfig.envName,
        LUCIEL_PLANE: 'sweep',
        LUCIEL_LOG_FORMAT: 'json',
        LUCIEL_INTEGRATION_MODE: integrationMode,
        // The knowledge_sync sweep ingests synced pages (parse → chunk → embed →
        // graph), so it needs the same extractor posture as the app planes (D10).
        LUCIEL_GRAPH_EXTRACTOR: 'llm',
        // MUST match the app planes. The usage_reconcile sweep posts meter events and
        // advances the billing watermark; if it resolved a different gateway than the
        // app, it would mark blocks reported that Stripe never received.
        LUCIEL_STRIPE_INTEGRATION_POSTURE: envConfig.stripeIntegrationPosture,
        LUCIEL_REDIS_URL: redisUrl,
        // Both buckets, for the same reason the posture above is here: the sweeps are
        // the ONLY things that touch cold storage. transcript_archive writes the 90-day
        // offload, retention deletes the archived object at the 1-year boundary, and
        // the hard-delete cascade drops a closed tenant's whole prefix. Omitting these
        // did not fail loudly — `transcript_archive_bucket` carries a placeholder
        // default, so the sweep silently addressed a bucket named `vm-transcript-archive`
        // that exists in no account we own. Commitment #38 cannot run from a plane that
        // was never told where the bucket is.
        LUCIEL_EXPORT_BUCKET: exportBucket.bucketName,
        LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET: transcriptArchiveBucket.bucketName,
        // The retention pass purges expired owner-deleted sources (their originals
        // with them) and the cascade sweeps the tenant prefix — from this plane.
        LUCIEL_KNOWLEDGE_ORIGINALS_BUCKET: knowledgeOriginalsBucket.bucketName,
        // SES events pipeline — MUST match the app planes for the same reason the
        // Stripe posture above must: the billing_notices and dunning sweeps send
        // platform email through the same sender seam, and a sweep-sent notice
        // without the configuration set would be a send with no bounce/complaint
        // feedback. (The seedtest task carries neither: it exercises email only
        // through the live API, never by sending directly.)
        SES_CONFIGURATION_SET: sesConfigurationSetName,
        SES_EVENTS_TOPIC_ARN: sesEventsTopic.topicArn,
        // model_currency auto-swap targets (§3.4.3): WHERE to write the successor
        // model ID and WHICH services to redeploy so it takes effect. Absent in
        // local/CI runs, where the watchdog degrades to alert-only by design.
        LUCIEL_PLATFORM_SECRET_ARN: platformSecretArn,
        LUCIEL_ECS_CLUSTER: this.cluster.clusterName,
        LUCIEL_ECS_SERVICES: `${dataService.serviceName},${controlService.serviceName}`,
      },
    });

    // ERROR-level sweep lines become a metric (SweepErrors) the ObservabilityStack
    // alarms on in armed environments. This is what makes an operator-facing
    // `logger.error` in ANY sweep — the model_currency watchdog, the §A5 short-lead
    // billing notice — an alert instead of a line nobody reads. Lives HERE because
    // this stack owns the sweep log group; the alarm lives with the alarm topic.
    // CloudWatch term matching is token-delimited, so tracebacks mentioning
    // SpeechError/ProviderError do not match the standalone ERROR level token.
    new logs.MetricFilter(this, 'SweepErrorMetric', {
      logGroup: sweepLogGroup,
      metricNamespace: SWEEP_ERROR_METRIC.metricNamespace,
      metricName: SWEEP_ERROR_METRIC.metricName,
      filterPattern: logs.FilterPattern.literal('ERROR'),
      metricValue: '1',
    });

    // Role EventBridge Scheduler assumes to RunTask. Separate from the task role: the
    // scheduler needs ecs:RunTask + iam:PassRole for the task/execution roles and
    // nothing else. NEW IAM — note for the deploy step.
    const schedulerRole = new iam.Role(this, 'SweepSchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
      description: 'EventBridge Scheduler -> ECS RunTask for the Luciel compliance sweeps',
    });
    schedulerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['ecs:RunTask'],
        resources: [sweepTask.taskDefinitionArn],
        conditions: { ArnEquals: { 'ecs:cluster': this.cluster.clusterArn } },
      }),
    );
    schedulerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['iam:PassRole'],
        resources: [sweepRole.roleArn, sweepTask.executionRole!.roleArn],
        conditions: { StringLike: { 'iam:PassedToService': 'ecs-tasks.amazonaws.com' } },
      }),
    );

    const sweepSubnets = vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS });
    // Staggered off-peak UTC so two sweeps never contend for the same DB, and so the
    // grace/close purges run AFTER the retention prune has trimmed the same tables.
    const sweepSchedules: { name: string; cron: string; description: string }[] = [
      {
        name: 'retention',
        cron: 'cron(15 3 * * ? *)',
        description: 'Hard-delete transcripts/summaries past the 1-year retention (Legal §B5)',
      },
      {
        name: 'grace_expiry',
        cron: 'cron(45 3 * * ? *)',
        description: 'Fire the delete cascade at day 30 of the grace window (Legal §B5)',
      },
      {
        name: 'close_purge',
        cron: 'cron(15 4 * * ? *)',
        description: 'Purge closed-account shells past their 30-day window (Legal §B5)',
      },
      {
        name: 'dunning',
        cron: 'cron(45 4 * * ? *)',
        description: 'Advance day-7 dunning retrying -> reduced (Legal §A3)',
      },
      {
        name: 'usage_reconcile',
        cron: 'cron(15 5 * * ? *)',
        description: 'Post billable blocks Stripe was never told about (§3.4.1b)',
      },
      {
        name: 'unverified_reap',
        cron: 'cron(45 5 * * ? *)',
        description: 'Hard-purge accounts left unverified past 7 days (§3.7.1a, Legal §A1)',
      },
      {
        // Not a nightly: a queued callback is a promise to a customer with a time on it,
        // and the rule-D TTL is measured in hours. Hourly is the coarsest cadence that
        // still delivers near when the lead asked (§3.8.7 rule D).
        name: 'deferred',
        cron: 'cron(10 * * * ? *)',
        description: 'Retry due deferred actions; abandon past-TTL ones with notice (§3.8.7 D)',
      },
      {
        // Every 6h rather than nightly: connection status is what the universal gate
        // reads before it runs a tool, so a day-old verdict gates a day of decisions.
        name: 'health',
        cron: 'cron(40 */6 * * ? *)',
        description: 'Re-check non-revoked connections and transition them (§3.8.5)',
      },
      {
        // Weekly rather than nightly: the boundary is a year, so a few days of lag
        // costs nothing, and unlike the other sweeps this one visits every tenant
        // rather than a due-list.
        name: 'audit_retention',
        cron: 'cron(30 6 ? * SUN *)',
        description: 'Delete audit rows past the published 1-year retention (Legal §B5)',
      },
      {
        // Hourly, not nightly: the shortest inactivity window is 30 minutes (widget),
        // and cross-session memory (§3.4.6) only exists once the summary is written —
        // a nightly close would leave a same-day returning lead unremembered.
        // :25 staggers off the deferred (:10) and health (:40) hourly slots.
        name: 'session_finalize',
        cron: 'cron(25 * * * ? *)',
        description: 'Close sessions past their inactivity window; summarize (§3.4.7/§3.4.8)',
      },
      {
        // Nightly, BEFORE the 03:15 retention prune: a session crossing both the
        // 90-day archive boundary and the 1-year deletion boundary in one night is
        // archived first and then aged out through the same policy path.
        name: 'transcript_archive',
        cron: 'cron(30 2 * * ? *)',
        description: 'Move transcripts past 90 days to S3 cold storage (Commitment #38)',
      },
      {
        // Every 5 minutes — this is the email channel's inbound TRANSPORT for BYO-
        // mailbox tenants (§3.1.6a delta poll), not a compliance sweep, so its cadence
        // is customer-facing reply latency. Cost is deliberate and bounded: ~288
        // 256-CPU Fargate runs/day ≈ $3-4/month, each a no-op index scan on a platform
        // with no connected mailboxes (candidates limited to CONNECTED outlook rows,
        // migration 0038). Pauses with the environment like every other schedule.
        name: 'mailbox_poll',
        // Hourly BACKSTOP since 2026-09-05: the data plane polls in-process every
        // five minutes (LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS above). The same
        // per-tenant advisory lock makes the two paths mutually exclusive.
        cron: 'cron(7 * * * ? *)',
        description: 'Pull new mail from connected BYO Outlook mailboxes (§3.1.6a)',
      },
      {
        // Every 6h, staggered off health's :40 slot: the periodic half of §3.2.3
        // "kept in sync" — before this schedule existed, live-sync sources were
        // synced exactly as often as the owner clicked Re-sync, and a crawled
        // website was never re-crawled. Per-origin cadence lives in the worker
        // (delta providers 6h, website crawl 24h); candidates are only tenants
        // with a CONNECTED knowledge connection (migration 0042).
        name: 'knowledge_sync',
        cron: 'cron(50 */6 * * ? *)',
        description: 'Re-sync due live-sync knowledge sources; re-crawl websites (§3.2.3)',
      },
      {
        // Daily, morning UTC: the EMAIL half of the account-notice commitments —
        // Legal §A5 owes an allowance reduction notice "BOTH by email AND as an
        // in-dashboard notification" (the dashboard half renders on login; before
        // this schedule nothing ever sent the email), plus §A10/§B6 operator-
        // published notices and the Vision §7 80% pay-as-you-go heads-up. Once-only
        // is the notice_deliveries ledger, so daily at-least-once delivery is safe.
        name: 'billing_notices',
        cron: 'cron(20 6 * * ? *)',
        description: 'Email due billing/legal notices once per tenant (§A5/§A10 + 80% nudge)',
      },
      {
        // Daily at 06:35 UTC — deliberately NOT :40 (health fires 06:40) or :50
        // (knowledge_sync). Verifies every pinned LLM model ID against its
        // provider's live model list; a retired CHAT pin auto-swaps to its
        // same-family successor (platform-secret write + service redeploy +
        // operator email), the embedding pin and anything unremediable alerts
        // via ERROR log (→ SweepErrors alarm) + nonzero exit (§3.4.3).
        name: 'model_currency',
        cron: 'cron(35 6 * * ? *)',
        description: 'Verify pinned LLM model IDs; auto-swap retired chat pins (§3.4.3)',
      },
      {
        // Hourly, because owners sit in different zones: a tenant is due once its
        // LOCAL clock passes 07:00 and no ledger row exists for the day (round 6 WP-F).
        // :55 staggers off the deferred (:10), session_finalize (:25) and health
        // (:40) hourly slots AND off knowledge_sync's :50 — the previous :50 slot
        // collided with it four times a day (00/06/12/18 UTC), two sweep tasks
        // contending for the same DB at once (round 7 WP-8). sweep-schedules.test.ts
        // refuses any two schedules that share a firing minute.
        name: 'daily_brief',
        cron: 'cron(55 * * * ? *)',
        description: "Email each owner's morning brief once per local day (round 6 WP-F)",
      },
    ];
    // Every name in app/jobs/sweeps.py SWEEP_KINDS must appear above: an entrypoint with
    // no schedule is a compliance promise that never runs, and nothing else in the stack
    // notices. tests/unit/test_sweep_scheduling.py asserts the two lists agree.
    for (const sweep of sweepSchedules) {
      new scheduler.CfnSchedule(this, `Sweep-${sweep.name}`, {
        name: `Luciel-${envConfig.envName}-sweep-${sweep.name}`,
        description: sweep.description,
        flexibleTimeWindow: { mode: 'OFF' },
        scheduleExpression: sweep.cron,
        scheduleExpressionTimezone: 'UTC',
        // Paused envs stop the sweeps with everything else (cheap-idle switch).
        state: envConfig.paused ? 'DISABLED' : 'ENABLED',
        target: {
          arn: this.cluster.clusterArn,
          roleArn: schedulerRole.roleArn,
          ecsParameters: {
            taskDefinitionArn: sweepTask.taskDefinitionArn,
            launchType: 'FARGATE',
            taskCount: 1,
            networkConfiguration: {
              awsvpcConfiguration: {
                subnets: sweepSubnets.subnetIds,
                securityGroups: [appSg.securityGroupId],
                assignPublicIp: 'DISABLED',
              },
            },
          },
          // The sweep name is the ONLY thing that differs per schedule.
          input: JSON.stringify({
            containerOverrides: [
              {
                name: sweepContainerName,
                command: ['bash', '-lc', `scripts/run-sweep.sh ${sweep.name}`],
              },
            ],
          }),
          // Sweeps are idempotent, so a retry is safe; without a retry policy a
          // transient DB blip would silently skip a compliance sweep for a day.
          retryPolicy: { maximumRetryAttempts: 3, maximumEventAgeInSeconds: 3600 },
        },
      });
    }

    this.alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
    });
    const listener = this.alb.addListener('Http', { port: 80, open: true });
    // ONE target group for the data plane, shared by both the HTTP and HTTPS
    // listeners. Registering a single-task dev service into two separate target
    // groups made the rolling deployment unable to satisfy minHealthy across both
    // groups, causing an endless register/drain loop (the service never reached
    // steady state and the CFN update hung). One TG + multiple listeners forwarding
    // to it is the correct ALB pattern.
    const dataPlaneTg = listener.addTargets('DataPlaneTargets', {
      port: 8000,
      targets: [dataService],
      healthCheck: { path: '/healthz', interval: Duration.seconds(30) },
    });

    // §0 two-plane split, made real at the edge (2026-09-05 audit F067). Before
    // this, BOTH listeners forwarded every path to the data plane and the
    // control-plane service received no traffic at all — which only mattered once
    // the data-plane role was narrowed: admin flows that create tenant secrets,
    // write export bundles or store knowledge originals must run on the plane whose
    // role can. Admin authority (auth, admin, dashboard, billing, connections,
    // exports, contact, escalation-contact) is answered by the CONTROL plane;
    // everything else — the widget, channel + provider webhooks, /healthz — stays
    // on the data plane, which remains the DEFAULT action so an unlisted path can
    // never be dropped. app/main.py declares the same split (CONTROL_PLANE_PREFIXES)
    // and its unit test refuses a router that is on neither list. One target group
    // per service (the single-service-in-two-TGs deploy hang above does not apply).
    const controlPlaneTg = new elbv2.ApplicationTargetGroup(this, 'ControlPlaneTargets', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [controlService],
      healthCheck: { path: '/healthz', interval: Duration.seconds(30) },
    });
    const routeControlPlane = (l: elbv2.ApplicationListener, tag: string) => {
      CONTROL_PLANE_PATH_PATTERNS.forEach((patterns, i) => {
        l.addAction(`${tag}ControlPlane${i}`, {
          priority: 10 + i,
          conditions: [elbv2.ListenerCondition.pathPatterns([...patterns])],
          action: elbv2.ListenerAction.forward([controlPlaneTg]),
        });
      });
    };
    routeControlPlane(listener, 'Http');

    // HTTPS:443 with the api.vantagemind.ai cert (spec §1: add the HTTPS listener to
    // the EXISTING ALB; keep the existing HTTP routing above). Only wired when DNS
    // has provided the regional cert. Forwards to the SAME data-plane target group
    // (no second TG — see the comment above).
    if (apiCert) {
      const https = this.alb.addListener('Https', {
        port: 443,
        open: true,
        certificates: [apiCert],
        defaultTargetGroups: [dataPlaneTg],
      });
      routeControlPlane(https, 'Https');
    }

    // api.vantagemind.ai -> this ALB (A + AAAA ALIAS). Zone already exists (spec §1).
    if (hostedZone) {
      const albTarget = route53.RecordTarget.fromAlias(
        new route53targets.LoadBalancerTarget(this.alb),
      );
      new route53.ARecord(this, 'ApiA', {
        zone: hostedZone,
        recordName: DOMAINS.api,
        target: albTarget,
      });
      new route53.AaaaRecord(this, 'ApiAaaa', {
        zone: hostedZone,
        recordName: DOMAINS.api,
        target: albTarget,
      });
    }
  }
}
