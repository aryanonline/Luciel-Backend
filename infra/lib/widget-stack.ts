/**
 * Widget CDN layer — S3 + CloudFront for embed.vantagemind.ai (§5.6 / spec §3).
 *
 * apps/widget (Luciel-Frontend) is a fully static Vite library bundle
 * (dist/luciel.js …) served at embed.vantagemind.ai/v1/luciel.js. So unlike the
 * SSR web app, it is S3 + CloudFront, not ECS.
 *
 * CROSS-REPO UPLOAD: the bundle is produced by the FRONTEND repo's CI; this CDK app
 * cannot see it at synth. So — like the web image — CDK owns ONLY the bucket +
 * distribution; the frontend CI `aws s3 sync`s dist/ to the bucket and invalidates
 * CloudFront (no cross-repo BucketDeployment).
 *
 * Two stacks live here because a CloudFront cert MUST be in us-east-1:
 *   - WidgetCertStack (us-east-1): the ACM cert for embed.vantagemind.ai. This is
 *     the ONE sanctioned region deviation (CLOUDFRONT_CERT_REGION) — a CloudFront
 *     cert is a global-edge artifact, not customer data, so residency holds (LD 21).
 *     It deliberately does NOT carry the ca-central-1 residency guard.
 *   - WidgetStack (ca-central-1): the S3 bucket (region-locked, public access
 *     blocked, TLS-only, OAC) + the CloudFront distribution. Carries the guard.
 *
 * DEFERRED (spec §3): the per-domain Origin/Referer allowlist + bot-challenge WAF
 * for the widget is OUT OF SCOPE here. Until those rules land, widget serving must
 * NOT be announced to customers — off-domain embedding is not yet rejected at the
 * edge. The existing regional WAF (EdgeStack) is unchanged.
 */
import { Stack, StackProps, RemovalPolicy, Duration, CfnOutput } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53targets from 'aws-cdk-lib/aws-route53-targets';
import { Construct } from 'constructs';
import { EnvConfig, DOMAINS, HOSTED_ZONE, OFFERING_REGION } from './env-config';
import { ResidencyGuard } from './residency';

export interface WidgetCertStackProps extends StackProps {
  readonly envConfig: EnvConfig;
}

/** us-east-1 ACM cert for the widget CloudFront distribution (required region). */
export class WidgetCertStack extends Stack {
  public readonly cert: acm.Certificate;

  constructor(scope: Construct, id: string, props: WidgetCertStackProps) {
    super(scope, id, props);

    // NOTE: deliberately us-east-1 — CloudFront cert requirement. No ca-central-1
    // residency guard here (a CloudFront cert is not customer data; see file header).
    const hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId: HOSTED_ZONE.id,
      zoneName: HOSTED_ZONE.name,
    });
    this.cert = new acm.Certificate(this, 'WidgetCert', {
      domainName: DOMAINS.widget,
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });
  }
}

export interface WidgetStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  /** us-east-1 cert for embed.vantagemind.ai (from WidgetCertStack). */
  readonly cert: acm.ICertificate;
}

export class WidgetStack extends Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: WidgetStackProps) {
    super(scope, id, props);
    const { envConfig, cert } = props;

    new ResidencyGuard(this, 'Residency');

    // Widget bundle bucket. Public access fully blocked (served only via CloudFront
    // OAC), TLS enforced, and a region-Deny so the bucket can't be read/copied from
    // outside the offering region (mirrors ResidencyGuard.hardenedBucket). SSE-S3
    // (not KMS) because the bundle is PUBLIC static JS, not customer data — this
    // keeps CloudFront OAC simple (no cross-stack KMS grant).
    this.bucket = new s3.Bucket(this, 'WidgetBucket', {
      // Deterministic name so the frontend OIDC deploy role can grant s3:sync
      // least-privilege by ARN (account-suffixed for global uniqueness).
      bucketName: `luciel-${envConfig.envName}-widget-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: envConfig.envName === 'prod',
      removalPolicy:
        envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      autoDeleteObjects: envConfig.envName !== 'prod',
      // No `expiration` rule: the current object IS the served bundle and must outlive
      // any timer. What is bounded is everything the CI leaves behind it — prod is
      // versioned, so each `s3 sync` of a rebuilt bundle pushes the old one to a
      // noncurrent version that is billed forever and can never be served again.
      lifecycleRules: [
        {
          id: 'widget-bundle-hygiene',
          enabled: true,
          noncurrentVersionExpiration: Duration.days(30),
          abortIncompleteMultipartUploadAfter: Duration.days(1),
        },
      ],
    });
    this.bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyOutsideOfferingRegion',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:*'],
        resources: [this.bucket.bucketArn, this.bucket.arnForObjects('*')],
        conditions: { StringNotEquals: { 'aws:RequestedRegion': OFFERING_REGION } },
      }),
    );

    // CloudFront with Origin Access Control (OAC) — the modern, signed S3 origin.
    // Long cache for the immutable, versioned bundle paths; the frontend CI issues
    // an invalidation on deploy.
    this.distribution = new cloudfront.Distribution(this, 'WidgetCdn', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        responseHeadersPolicy:
          cloudfront.ResponseHeadersPolicy.CORS_ALLOW_ALL_ORIGINS_WITH_PREFLIGHT,
      },
      domainNames: [DOMAINS.widget],
      certificate: cert,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultRootObject: 'index.html',
      comment: `Luciel ${envConfig.envName} widget CDN (embed.vantagemind.ai)`,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
    });

    // embed.vantagemind.ai -> CloudFront (A + AAAA ALIAS). Zone already exists.
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId: HOSTED_ZONE.id,
      zoneName: HOSTED_ZONE.name,
    });
    const cfTarget = route53.RecordTarget.fromAlias(
      new route53targets.CloudFrontTarget(this.distribution),
    );
    new route53.ARecord(this, 'WidgetA', {
      zone,
      recordName: DOMAINS.widget,
      target: cfTarget,
      ttl: Duration.minutes(5),
    });
    new route53.AaaaRecord(this, 'WidgetAaaa', {
      zone,
      recordName: DOMAINS.widget,
      target: cfTarget,
    });

    new CfnOutput(this, 'WidgetBucketName', { value: this.bucket.bucketName });
    new CfnOutput(this, 'WidgetDistributionId', {
      value: this.distribution.distributionId,
    });
    new CfnOutput(this, 'WidgetDomain', { value: this.distribution.distributionDomainName });
  }
}
