#!/usr/bin/env node
/**
 * Luciel infrastructure CDK app (DEVOPS §5.1 — single IaC source).
 *
 * Builds a full environment as layered stacks (§5 order):
 *   networking -> security -> data -> compute -> edge -> observability
 * Region LOCKED to ca-central-1 for every environment (§4.2 / LD 21).
 * DEV-FIRST: synth/deploy dev; prod (and the ephemeral prod-shaped preprod)
 * reuse the same constructs.
 *
 * GATE 1 (§2.8): the data/compute/edge stacks carry real recurring cost — do not
 * deploy before the founder approves the dev cost figure.
 */
import * as cdk from 'aws-cdk-lib';
import { NetworkingStack } from '../lib/networking-stack';
import { BudgetStack } from '../lib/budget-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EdgeStack } from '../lib/edge-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { DnsStack } from '../lib/dns-stack';
import { WebStack } from '../lib/web-stack';
import { WidgetCertStack, WidgetStack } from '../lib/widget-stack';
import { BoundedLambdaLogs } from '../lib/log-retention-aspect';
import { applyCostAllocationTags } from '../lib/cost-tags';
import {
  getEnvConfig,
  applyContextOverrides,
  apexOwner,
  OFFERING_REGION,
  CLOUDFRONT_CERT_REGION,
  EnvName,
} from '../lib/env-config';

const app = new cdk.App();
const envName = (app.node.tryGetContext('env') as EnvName) ?? 'dev';
// Cheap-idle pause switch: `-c paused=true` forces every ECS service to count 0.
const paused = app.node.tryGetContext('paused') === 'true';
const envConfig = applyContextOverrides(getEnvConfig(envName), paused);
// Owner decision D6: exactly one environment serves the public names. A config that
// gives them to two environments (or none, or to the ephemeral preprod) is refused
// here, before any stack is built.
apexOwner();
const account = process.env.CDK_DEFAULT_ACCOUNT;
const env: cdk.Environment = { account, region: OFFERING_REGION };
// CloudFront certs MUST be in us-east-1 (the single sanctioned region deviation).
const usEast1Env: cdk.Environment = { account, region: CLOUDFRONT_CERT_REGION };

const prefix = `Luciel-${envName}`;

const net = new NetworkingStack(app, `${prefix}-Networking`, { env, envConfig });

const sec = new SecurityStack(app, `${prefix}-Security`, { env, envConfig });

const data = new DataStack(app, `${prefix}-Data`, {
  env,
  envConfig,
  vpc: net.vpc,
  cmk: sec.cmk,
});

const compute = new ComputeStack(app, `${prefix}-Compute`, {
  env,
  envConfig,
  vpc: net.vpc,
  cmkArn: sec.cmk.keyArn,
  transcriptArchiveBucket: data.transcriptArchiveBucket,
  exportBucket: data.exportBucket, voiceAudioBucket: data.voiceAudioBucket,
  knowledgeOriginalsBucket: data.knowledgeOriginalsBucket,
  dbSecretArn: data.db.secret!.secretArn,
  platformSecretArn: sec.platformSecret.secretArn,
  redisUrl: data.redisUrl,
  // The environment's hostnames (env-config.ts `hostnames`, D6): one certificate for
  // every app + api name on the one ALB's HTTPS listener, and the api names' aliases.
  publicDns: true,
});

// Frontend web service (SSR Next.js on ECS) on the environment's ONE ALB (round 7
// WP-8b, owner decision D7): its target group joins Compute's listener behind the
// app-host rule, and the app names' aliases point at that ALB.
const web = new WebStack(app, `${prefix}-Web`, {
  env,
  envConfig,
  vpc: net.vpc,
  ingress: compute.sharedIngress,
  publicDns: true,
});

// Widget CDN: the cert must be in us-east-1 (CloudFront), the bucket+distribution
// in ca-central-1. crossRegionReferences lets the ca-central-1 stack consume the
// us-east-1 cert.
const widgetCert = new WidgetCertStack(app, `${prefix}-WidgetCert`, {
  env: usEast1Env,
  envConfig,
  crossRegionReferences: true,
});
new WidgetStack(app, `${prefix}-Widget`, {
  env,
  envConfig,
  cert: widgetCert.cert,
  crossRegionReferences: true,
});

const edge = new EdgeStack(app, `${prefix}-Edge`, {
  env,
  envConfig,
  // The one ALB: the API and the dashboard/marketing hosts (D7) share the WebACL.
  alb: compute.alb,
});
// The deploy that retires the web ALB must drop its WebACL association BEFORE the
// ALB is deleted: Edge (which used to depend on Web for that ALB) now deploys first.
web.addStackDependency(edge, 'Edge drops the retired web ALB association before Web deletes it');

// The environment's hostnames as stack exports (+ the zone handle). It deploys AFTER
// Compute and Web on purpose: it used to own the regional certificates their
// listeners held, and the deploy that moves those listeners onto ComputeStack's own
// certificate is the same one that deletes the old certs here — which CloudFormation
// can only do once nothing uses them.
const dns = new DnsStack(app, `${prefix}-Dns`, { env, envConfig });
dns.addStackDependency(compute, 'retired listener certificates are deleted after the listener moves');
dns.addStackDependency(web, 'retired web certificate is deleted after the web ALB is gone');

new ObservabilityStack(app, `${prefix}-Observability`, {
  env,
  envConfig,
  alb: compute.alb,
  // Operator inbox for the Alarms topic. A dedicated `-c alarmEmail` wins;
  // otherwise the budget address doubles as the operator inbox (one knob).
  // Same no-address-in-the-repo rule as BudgetStack.
  alertEmail: (app.node.tryGetContext('alarmEmail') ??
    app.node.tryGetContext('budgetAlertEmail')) as string | undefined,
  clusterArn: compute.cluster.clusterArn,
  sweepTaskFamily: compute.sweepTaskFamily,
});

new PipelineStack(app, `${prefix}-Pipeline`, {
  env,
  envConfig,
  githubRepo: 'aryanonline/Luciel-Backend',
  frontendRepo: 'aryanonline/Luciel-Frontend',
});

// Monthly cost ceiling (AWS Budgets) — the backstop that catches everything the
// per-resource ceilings miss, incl. a failed preprod teardown. Alert email comes
// from deploy-time context so no address is committed to the repo.
new BudgetStack(app, `${prefix}-Budget`, {
  env,
  envConfig,
  alertEmail: app.node.tryGetContext('budgetAlertEmail') as string | undefined,
});

// Cost-allocation tags on EVERY stack (round 7 WP-8, D7): Environment=<env> and
// Component=<stack short name>, propagated by CloudFormation to each taggable
// resource. BudgetStack filters each environment's ceiling on the Environment
// tag, so a stack missing the tag is spend the budget cannot see — hence the
// loop over the app's stacks rather than a per-stack call somebody forgets.
for (const child of app.node.children) {
  if (cdk.Stack.isStack(child)) {
    applyCostAllocationTags(child, envConfig, child.stackName.replace(`${prefix}-`, ''));
  }
}

// App-wide so it also reaches the functions CDK generates inside constructs the
// stacks do not own — those are exactly the ones nobody would otherwise bound.
cdk.Aspects.of(app).add(new BoundedLambdaLogs(envConfig.logRetention));

app.synth();
