/**
 * Frontend infrastructure assertions (spec §1–§5 / DEVOPS §5.1).
 *
 * Locks the load-bearing properties of the new stacks so they cannot silently
 * regress in CI, INCLUDING the residency assertion (region = ca-central-1) the
 * spec requires for every new in-region stack — and the ONE sanctioned exception
 * (the CloudFront cert in us-east-1).
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as path from 'node:path';
import { DnsStack } from '../lib/dns-stack';
import { WidgetStack, WidgetCertStack } from '../lib/widget-stack';
import { buildCompute, buildComputeAndWeb } from './support/stacks';
import { PipelineStack } from '../lib/pipeline-stack';
import {
  getEnvConfig,
  OFFERING_REGION,
  CLOUDFRONT_CERT_REGION,
} from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };
const US_EAST_1 = { account: '111111111111', region: CLOUDFRONT_CERT_REGION };

function dnsTemplate() {
  const app = new App();
  return Template.fromStack(new DnsStack(app, 'Dns', { env: ENV, envConfig: getEnvConfig('dev') }));
}

function buildWeb(envName: 'dev' | 'preprod' | 'prod' = 'dev', paused = false) {
  // The web tier hangs on the ComputeStack ALB since round 7 WP-8b (D7).
  return Template.fromStack(buildComputeAndWeb(envName, { paused, publicDns: true }).web);
}

function buildWidget() {
  const app = new App();
  // Import a cert by ARN so the widget stack can be tested without the cross-region
  // cert-stack machinery (the real wiring is exercised by `cdk synth`).
  const stack = new Stack(app, 'Holder', { env: ENV });
  const cert = acm.Certificate.fromCertificateArn(
    stack,
    'Cert',
    `arn:aws:acm:${CLOUDFRONT_CERT_REGION}:111111111111:certificate/abc`,
  );
  const widget = new WidgetStack(app, 'Widget', {
    env: ENV,
    envConfig: getEnvConfig('dev'),
    cert,
  });
  return Template.fromStack(widget);
}

describe('Residency lock holds for new stacks (§4.2 / LD 21)', () => {
  test.each([
    ['Dns', () => dnsTemplate()],
    ['Web', () => buildWeb()],
    ['Widget', () => buildWidget()],
  ])('%s stack carries a region-lock CfnCondition == ca-central-1', (_name, build) => {
    const json = JSON.stringify((build() as Template).findConditions('*'));
    expect(json).toContain(OFFERING_REGION);
    expect(json).toContain('AWS::Region');
  });

  test('WidgetCertStack is the sanctioned us-east-1 exception (no ca-central-1 lock)', () => {
    const app = new App();
    const certStack = new WidgetCertStack(app, 'WidgetCert', {
      env: US_EAST_1,
      envConfig: getEnvConfig('dev'),
    });
    expect(certStack.region).toBe('us-east-1');
    const t = Template.fromStack(certStack);
    // The distribution's cert covers every embed host (wildcard: see widget-stack.ts),
    // and there is NO ca-central-1 condition.
    t.hasResourceProperties('AWS::CertificateManager::Certificate', {
      DomainName: '*.vantagemind.ai',
    });
    expect(JSON.stringify(t.findConditions('*'))).not.toContain(OFFERING_REGION);
  });
});

describe('DNS / certs (spec §1, reshaped round 7 WP-8b)', () => {
  test('the Dns stack publishes the hostnames and no longer owns a certificate', () => {
    const t = dnsTemplate();
    // The listener certificate lives beside the listener (ComputeStack) now.
    t.resourceCountIs('AWS::CertificateManager::Certificate', 0);
    t.hasOutput('ApiBaseUrl', {
      Value: 'https://api-dev.vantagemind.ai',
      Export: { Name: 'Luciel-dev-ApiBaseUrl' },
    });
    t.hasOutput('AppBaseUrl', { Value: 'https://dev.vantagemind.ai' });
    t.hasOutput('EmbedHost', { Value: 'embed-dev.vantagemind.ai' });
  });

  test('the one regional certificate is DNS-validated and covers the api + app names', () => {
    const t = Template.fromStack(buildCompute('dev', { publicDns: true }).compute);
    t.resourceCountIs('AWS::CertificateManager::Certificate', 1);
    t.hasResourceProperties('AWS::CertificateManager::Certificate', {
      DomainName: 'api-dev.vantagemind.ai',
      ValidationMethod: 'DNS',
      SubjectAlternativeNames: Match.arrayWith(['vantagemind.ai', 'www.vantagemind.ai']),
    });
  });
});

describe('Web service (spec §2 — mirrors backend ECS)', () => {
  test('creates the luciel-<env>-web ECR repo with scan-on-push', () => {
    buildWeb().hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'luciel-dev-web',
      ImageScanningConfiguration: { ScanOnPush: true },
    });
  });

  test('runs ONE Fargate service and no load balancer of its own (D7)', () => {
    const t = buildWeb();
    t.resourceCountIs('AWS::ECS::Service', 1);
    t.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 0);
    t.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 0);
    // Its target group joins the shared listener behind the app-host rule.
    t.resourceCountIs('AWS::ElasticLoadBalancingV2::ListenerRule', 1);
  });

  test('plain-HTTP app hosts still redirect to HTTPS (the retired ALB :80 behavior)', () => {
    Template.fromStack(buildCompute('dev', { publicDns: true }).compute).hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::ListenerRule',
      {
        Conditions: Match.arrayWith([
          Match.objectLike({
            Field: 'host-header',
            HostHeaderConfig: { Values: ['dev.vantagemind.ai', 'vantagemind.ai', 'www.vantagemind.ai'] },
          }),
        ]),
        Actions: [Match.objectLike({ Type: 'redirect', RedirectConfig: Match.objectLike({ Protocol: 'HTTPS' }) })],
      },
    );
  });

  test('health check targets the SSR root path', () => {
    buildWeb().hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      HealthCheckPath: '/',
    });
  });

  test('paused=true forces the web service to desiredCount 0', () => {
    buildWeb('dev', true).hasResourceProperties('AWS::ECS::Service', { DesiredCount: 0 });
  });
});

describe('Widget CDN (spec §3)', () => {
  test('bucket blocks all public access and denies non-offering-region access', () => {
    const t = buildWidget();
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
    t.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Condition: { StringNotEquals: { 'aws:RequestedRegion': OFFERING_REGION } },
          }),
        ]),
      }),
    });
  });

  test("CloudFront serves dev's embed hosts (embed-dev + the D6 dual-host) over TLS via OAC", () => {
    const t = buildWidget();
    t.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        Aliases: ['embed-dev.vantagemind.ai', 'embed.vantagemind.ai'],
        ViewerCertificate: Match.objectLike({ MinimumProtocolVersion: 'TLSv1.2_2021' }),
      }),
    });
    t.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
  });
});

describe('Frontend deploy role (spec §4)', () => {
  function pipeline() {
    const app = new App();
    return Template.fromStack(
      new PipelineStack(app, 'Pipeline', {
        env: ENV,
        envConfig: getEnvConfig('dev'),
        githubRepo: 'aryanonline/Luciel-Backend',
        frontendRepo: 'aryanonline/Luciel-Frontend',
      }),
    );
  }

  test('a least-privilege OIDC role trusting the FRONTEND repo is created', () => {
    pipeline().hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'luciel-dev-frontend-github-deploy',
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Condition: Match.objectLike({
              StringLike: Match.objectLike({
                'token.actions.githubusercontent.com:sub': Match.arrayWith([
                  'repo:aryanonline/Luciel-Frontend:ref:refs/heads/*',
                ]),
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test('only ONE GitHub OIDC provider is created (shared by both roles)', () => {
    pipeline().resourceCountIs('Custom::AWSCDKOpenIdConnectProvider', 1);
  });
});

describe('Backend ALB carries the HTTPS listener and the api aliases (spec §1)', () => {
  test('compute adds a 443 listener and keeps the existing HTTP:80 routing', () => {
    const t = Template.fromStack(buildCompute('dev', { publicDns: true }).compute);
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', { Port: 443 });
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', { Port: 80 });
  });

  test('api-dev.vantagemind.ai AND (dual-host) api.vantagemind.ai ALIAS records point at the ALB', () => {
    const t = Template.fromStack(buildCompute('dev', { publicDns: true }).compute);
    for (const name of ['api-dev.vantagemind.ai.', 'api.vantagemind.ai.']) {
      t.hasResourceProperties('AWS::Route53::RecordSet', { Name: name, Type: 'A' });
      t.hasResourceProperties('AWS::Route53::RecordSet', { Name: name, Type: 'AAAA' });
    }
  });
});

describe('Cost guard inputs (round 7 WP-8)', () => {
  test('the web cluster name is exported like the backend one so the guard reads both', () => {
    // .github/workflows/dev-cost-guard.yml sums desiredCount across BOTH clusters
    // before stopping a paused environment's database; the backend export is
    // Luciel-<env>-ClusterName (ComputeStack) and this is its web counterpart.
    buildWeb('dev').hasOutput('WebClusterName', {
      Export: { Name: 'Luciel-dev-WebClusterName' },
    });
    buildWeb('prod').hasOutput('WebClusterName', {
      Export: { Name: 'Luciel-prod-WebClusterName' },
    });
  });
});
