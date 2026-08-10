/**
 * Tenant connection-secret IAM (Arch §3.8.3). The task role once held secretsmanager
 * grants on the platform + DB secrets ONLY, so every customer credential submit to
 * POST /api/v1/admin/connections/{id}/credentials returned 500 (AccessDenied on
 * CreateSecret). Pin the grant, and pin that it stays prefix-scoped.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as path from 'node:path';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION } from '../lib/env-config';
import type { EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute(envName: EnvName) {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  const compute = new ComputeStack(app, 'Compute', {
    env: ENV,
    envConfig,
    vpc: net.vpc,
    cmkArn: sec.cmk.keyArn,
    transcriptArchiveBucket: data.transcriptArchiveBucket,
    exportBucket: data.exportBucket,
    dbSecretArn: data.db.secret!.secretArn,
    platformSecretArn: sec.platformSecret.secretArn,
    redisUrl: data.redisUrl,
    backendPath: path.join(__dirname, '..', '..'),
  });
  return Template.fromStack(compute);
}

const TENANT_SECRET_ARN = `arn:aws:secretsmanager:${ENV.region}:${ENV.account}:secret:vm/*`;

function findTenantSecretStatement(template: Template) {
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .flatMap((policy) => policy.Properties.PolicyDocument.Statement)
    .find((statement) => statement.Sid === 'ManageTenantConnectionSecrets');
}

describe.each<EnvName>(['dev', 'prod'])('Tenant connection secrets IAM (%s)', (envName) => {
  test('the task role can write tenant connection secrets under the vm/ prefix', () => {
    const statement = findTenantSecretStatement(buildCompute(envName));
    expect(statement).toBeDefined();
    expect(statement.Effect).toBe('Allow');
    // Sorted: CDK policy minimization reorders actions, so compare as a set.
    expect([statement.Action].flat().sort()).toEqual(
      [
        'secretsmanager:CreateSecret',
        'secretsmanager:PutSecretValue',
        'secretsmanager:GetSecretValue',
        'secretsmanager:DescribeSecret',
        'secretsmanager:DeleteSecret',
        'secretsmanager:TagResource',
      ].sort(),
    );
    expect(statement.Resource).toBe(TENANT_SECRET_ARN);
  });

  test('the grant is prefix-scoped — no wildcard secretsmanager resource', () => {
    const policies = buildCompute(envName).findResources('AWS::IAM::Policy');
    for (const policy of Object.values(policies)) {
      for (const statement of policy.Properties.PolicyDocument.Statement) {
        const actions = [statement.Action].flat();
        if (!actions.some((a) => typeof a === 'string' && a.startsWith('secretsmanager:'))) continue;
        for (const resource of [statement.Resource].flat()) {
          expect(resource).not.toBe('*');
        }
      }
    }
  });
});
