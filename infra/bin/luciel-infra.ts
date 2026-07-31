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
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { NetworkingStack } from '../lib/networking-stack';
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
import {
  getEnvConfig,
  applyContextOverrides,
  OFFERING_REGION,
  CLOUDFRONT_CERT_REGION,
  EnvName,
} from '../lib/env-config';

const app = new cdk.App();
const envName = (app.node.tryGetContext('env') as EnvName) ?? 'dev';
// Cheap-idle pause switch: `-c paused=true` forces every ECS service to count 0.
const paused = app.node.tryGetContext('paused') === 'true';
const envConfig = applyContextOverrides(getEnvConfig(envName), paused);
const account = process.env.CDK_DEFAULT_ACCOUNT;
const env: cdk.Environment = { account, region: OFFERING_REGION };
// CloudFront certs MUST be in us-east-1 (the single sanctioned region deviation).
const usEast1Env: cdk.Environment = { account, region: CLOUDFRONT_CERT_REGION };
// infra/ lives inside the backend repo, so the backend root (Docker build context)
// is two levels up from infra/bin/.
const backendPath = path.join(__dirname, '..', '..');

const prefix = `Luciel-${envName}`;

const net = new NetworkingStack(app, `${prefix}-Networking`, { env, envConfig });

const sec = new SecurityStack(app, `${prefix}-Security`, { env, envConfig });

const data = new DataStack(app, `${prefix}-Data`, {
  env,
  envConfig,
  vpc: net.vpc,
  cmk: sec.cmk,
});

// DNS + regional certs (created before compute so the api cert can attach to the
// backend ALB's HTTPS listener). Only the existing hosted zone is referenced.
const dns = new DnsStack(app, `${prefix}-Dns`, { env, envConfig });

const compute = new ComputeStack(app, `${prefix}-Compute`, {
  env,
  envConfig,
  vpc: net.vpc,
  cmkArn: sec.cmk.keyArn,
  knowledgeBucket: data.knowledgeBucket,
  exportBucket: data.exportBucket,
  dbSecretArn: data.db.secret!.secretArn,
  platformSecretArn: sec.platformSecret.secretArn,
  redisUrl: data.redisUrl,
  backendPath,
  apiCert: dns.apiCert,
  hostedZone: dns.hostedZone,
});

// Frontend web service (SSR Next.js on ECS) — mirrors the backend compute pattern.
new WebStack(app, `${prefix}-Web`, {
  env,
  envConfig,
  vpc: net.vpc,
  webCert: dns.webCert,
  hostedZone: dns.hostedZone,
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

new EdgeStack(app, `${prefix}-Edge`, {
  env,
  envConfig,
  alb: compute.alb,
});

new ObservabilityStack(app, `${prefix}-Observability`, {
  env,
  envConfig,
  alb: compute.alb,
});

new PipelineStack(app, `${prefix}-Pipeline`, {
  env,
  envConfig,
  githubRepo: 'aryanonline/Luciel-Backend',
  frontendRepo: 'aryanonline/Luciel-Frontend',
});

// App-wide so it also reaches the functions CDK generates inside constructs the
// stacks do not own — those are exactly the ones nobody would otherwise bound.
cdk.Aspects.of(app).add(new BoundedLambdaLogs(envConfig.logRetention));

app.synth();
