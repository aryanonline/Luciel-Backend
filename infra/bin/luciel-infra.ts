#!/usr/bin/env node
/**
 * Luciel infrastructure CDK app (DEVOPS §5.1 — single IaC source).
 *
 * Builds a full environment as layered stacks (§5 order):
 *   networking -> security -> data -> compute -> edge -> observability
 * Region LOCKED to ca-central-1 for every environment (§4.2 / LD 21).
 * DEV-FIRST: synth/deploy dev; staging/prod reuse the same constructs.
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
import { getEnvConfig, OFFERING_REGION, EnvName } from '../lib/env-config';

const app = new cdk.App();
const envName = (app.node.tryGetContext('env') as EnvName) ?? 'dev';
const envConfig = getEnvConfig(envName);
const account = process.env.CDK_DEFAULT_ACCOUNT;
const env: cdk.Environment = { account, region: OFFERING_REGION };
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

const compute = new ComputeStack(app, `${prefix}-Compute`, {
  env,
  envConfig,
  vpc: net.vpc,
  cmkArn: sec.cmk.keyArn,
  knowledgeBucket: data.knowledgeBucket,
  dbSecretArn: data.db.secret!.secretArn,
  platformSecretArn: sec.platformSecret.secretArn,
  backendPath,
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
});

app.synth();
