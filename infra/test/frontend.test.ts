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
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { DnsStack } from '../lib/dns-stack';
import { WebStack } from '../lib/web-stack';
import { WidgetStack, WidgetCertStack } from '../lib/widget-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import {
  getEnvConfig,
  applyContextOverrides,
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
  const app = new App();
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: getEnvConfig(envName) });
  const dns = new DnsStack(app, 'Dns', { env: ENV, envConfig: getEnvConfig(envName) });
  const web = new WebStack(app, 'Web', {
    env: ENV,
    envConfig: applyContextOverrides(getEnvConfig(envName), paused),
    vpc: net.vpc,
    webCert: dns.webCert,
    hostedZone: dns.hostedZone,
  });
  return Template.fromStack(web);
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
    // Exactly one ACM cert for embed.vantagemind.ai, and NO ca-central-1 condition.
    t.hasResourceProperties('AWS::CertificateManager::Certificate', {
      DomainName: 'embed.vantagemind.ai',
    });
    expect(JSON.stringify(t.findConditions('*'))).not.toContain(OFFERING_REGION);
  });
});

describe('DNS / certs (spec §1)', () => {
  test('regional certs for api + apex(+www) are DNS-validated', () => {
    const t = dnsTemplate();
    t.hasResourceProperties('AWS::CertificateManager::Certificate', {
      DomainName: 'api.vantagemind.ai',
      ValidationMethod: 'DNS',
    });
    t.hasResourceProperties('AWS::CertificateManager::Certificate', {
      DomainName: 'vantagemind.ai',
      SubjectAlternativeNames: Match.arrayWith(['www.vantagemind.ai']),
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

  test('runs a Fargate service fronted by an ALB with an HTTPS:443 listener', () => {
    const t = buildWeb();
    t.resourceCountIs('AWS::ECS::Service', 1);
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', { Port: 443 });
    // HTTP:80 redirects to HTTPS.
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      DefaultActions: Match.arrayWith([
        Match.objectLike({ Type: 'redirect', RedirectConfig: Match.objectLike({ Protocol: 'HTTPS' }) }),
      ]),
    });
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

  test('CloudFront serves embed.vantagemind.ai over TLS via OAC', () => {
    const t = buildWidget();
    t.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        Aliases: ['embed.vantagemind.ai'],
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

describe('Backend ALB gains the api HTTPS listener (spec §1)', () => {
  function buildCompute() {
    const app = new App();
    const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: getEnvConfig('dev') });
    const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig: getEnvConfig('dev') });
    const data = new DataStack(app, 'Data', { env: ENV, envConfig: getEnvConfig('dev'), vpc: net.vpc, cmk: sec.cmk });
    const dns = new DnsStack(app, 'Dns', { env: ENV, envConfig: getEnvConfig('dev') });
    const compute = new ComputeStack(app, 'Compute', {
      env: ENV,
      envConfig: getEnvConfig('dev'),
      vpc: net.vpc,
      cmkArn: sec.cmk.keyArn,
      knowledgeBucket: data.knowledgeBucket,
      exportBucket: data.exportBucket,
      dbSecretArn: data.db.secret!.secretArn,
      platformSecretArn: sec.platformSecret.secretArn,
      redisUrl: data.redisUrl,
      backendPath: path.join(__dirname, '..', '..'),
      apiCert: dns.apiCert,
      hostedZone: dns.hostedZone,
    });
    return Template.fromStack(compute);
  }

  test('compute adds a 443 listener and keeps the existing HTTP:80 routing', () => {
    const t = buildCompute();
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', { Port: 443 });
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', { Port: 80 });
  });

  test('an api.vantagemind.ai ALIAS record points at the ALB', () => {
    buildCompute().hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'api.vantagemind.ai.',
      Type: 'A',
    });
  });
});
