/**
 * The GitHub deploy roles can do what deploy.yml now asks of them (round 7 WP-8b).
 *
 *  - Migrate-before-roll: scripts/migrate-before-roll.sh registers a revision of the
 *    current MigrationTask definition with the new image, runs it and deregisters it,
 *    BEFORE the Compute stack rolls the services. The deploy role needs the ECS
 *    task-definition actions for that (PassRole on the Compute roles it already had).
 *  - One OIDC provider per account: dev's PipelineStack owns it, every other
 *    environment imports it. Each PipelineStack used to create its own, so the prod
 *    one (which creates luciel-prod-github-deploy, the role every prod job assumes)
 *    could never deploy — the account already had a provider for that URL.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { PipelineStack, GITHUB_OIDC_HOST, OIDC_PROVIDER_OWNER } from '../lib/pipeline-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function pipeline(envName: EnvName) {
  const app = new App();
  return Template.fromStack(
    new PipelineStack(app, 'Pipeline', {
      env: ENV,
      envConfig: getEnvConfig(envName),
      githubRepo: 'aryanonline/Luciel-Backend',
      frontendRepo: 'aryanonline/Luciel-Frontend',
    }),
  );
}

describe('the deploy role can migrate with the new image before the roll', () => {
  test.each<EnvName>(['dev', 'prod'])('%s: describe/register/deregister/tag task definitions, region-bound', (envName) => {
    pipeline(envName).hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'MigrateBeforeRoll',
            Effect: 'Allow',
            Action: [
              'ecs:DescribeTaskDefinition',
              'ecs:RegisterTaskDefinition',
              'ecs:DeregisterTaskDefinition',
              'ecs:TagResource',
            ],
            Condition: { StringEquals: { 'aws:RequestedRegion': OFFERING_REGION } },
          }),
          // What a registered revision may run AS stays bounded to the Compute roles.
          Match.objectLike({
            Sid: 'PassTaskRoles',
            Action: 'iam:PassRole',
            Resource: `arn:aws:iam::111111111111:role/Luciel-${envName}-Compute-*`,
          }),
        ]),
      }),
    });
  });
});

describe('one GitHub OIDC provider per account', () => {
  test('dev owns (creates) it', () => {
    expect(OIDC_PROVIDER_OWNER).toBe('dev');
    pipeline('dev').resourceCountIs('Custom::AWSCDKOpenIdConnectProvider', 1);
  });

  test.each<EnvName>(['preprod', 'prod'])('%s imports it and its roles trust the account provider', (envName) => {
    const t = pipeline(envName);
    t.resourceCountIs('Custom::AWSCDKOpenIdConnectProvider', 0);
    for (const roleName of [`luciel-${envName}-github-deploy`, `luciel-${envName}-frontend-github-deploy`]) {
      t.hasResourceProperties('AWS::IAM::Role', {
        RoleName: roleName,
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'sts:AssumeRoleWithWebIdentity',
              Principal: {
                Federated: {
                  'Fn::Join': [
                    '',
                    ['arn:', { Ref: 'AWS::Partition' }, `:iam::111111111111:oidc-provider/${GITHUB_OIDC_HOST}`],
                  ],
                },
              },
            }),
          ]),
        }),
      });
    }
  });
});
