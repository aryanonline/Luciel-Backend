/**
 * Widget CDN layer — S3 + CloudFront for the environment's embed hosts (§5.6 / spec §3).
 *
 * apps/widget (Luciel-Frontend) is a fully static Vite library bundle
 * (dist/luciel.js …) served at <embed host>/v1/luciel.js — embed-dev.vantagemind.ai
 * for dev (owner decision D6, round 7 WP-8b) plus embed.vantagemind.ai while dev owns
 * the public names; the customer-facing embed snippet keeps naming the public host
 * until a production environment claims it. Unlike the SSR web app, it is S3 +
 * CloudFront, not ECS.
 *
 * CROSS-REPO UPLOAD: the bundle is produced by the FRONTEND repo's CI; this CDK app
 * cannot see it at synth. So — like the web image — CDK owns ONLY the bucket +
 * distribution; the frontend CI `aws s3 sync`s dist/ to the bucket and invalidates
 * CloudFront (no cross-repo BucketDeployment).
 *
 * Two stacks live here because a CloudFront cert MUST be in us-east-1:
 *   - WidgetCertStack (us-east-1): the ACM cert for the embed hosts. This is
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
import { APEX_DOMAIN, EnvConfig, OFFERING_REGION, PUBLIC_HOSTNAMES, hostnames, recordIdFor } from './env-config';
import { importPublicZone } from './dns-stack';
import { ResidencyGuard } from './residency';

export interface WidgetCertStackProps extends StackProps {
  readonly envConfig: EnvConfig;
}

/**
 * The CloudFront distribution's ONE certificate (CloudFront takes a single cert, so it
 * must cover every embed host the environment serves). A WILDCARD for the apex domain,
 * deliberately, and only here: this cert lives in another region's stack, so a cert
 * whose names changed would be REPLACED by a stack that deploys before the
 * distribution lets go of the old one — and CloudFormation cannot delete a cert a
 * distribution still holds. `*.vantagemind.ai` covers embed-dev / embed-preprod /
 * embed under every D6 ownership, so the cutover never touches it. (The regional ALB
 * certificate names its hosts explicitly: it lives beside its listener, where a
 * replacement is safe — compute-stack.ts.) ACM keys are not exportable, so the wider
 * name buys no extractable material.
 */
export const EMBED_CERT_DOMAIN = `*.${APEX_DOMAIN}`;

/** us-east-1 ACM cert for the widget CloudFront distribution (required region). */
export class WidgetCertStack extends Stack {
  public readonly cert: acm.Certificate;

  constructor(scope: Construct, id: string, props: WidgetCertStackProps) {
    super(scope, id, props);

    // NOTE: deliberately us-east-1 — CloudFront cert requirement. No ca-central-1
    // residency guard here (a CloudFront cert is not customer data; see file header).
    const hostedZone = importPublicZone(this);
    this.cert = new acm.Certificate(this, 'EmbedCert', {
      domainName: EMBED_CERT_DOMAIN,
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    // RETIRING (round 7 WP-8b): dev's distribution held this embed.vantagemind.ai-only
    // cert until the deploy that moves it to EmbedCert. This stack deploys BEFORE the
    // distribution switches, so deleting it in that same deploy would hit a cert still
    // in use. It stays, unreferenced, for that one deploy; remove it (and this block)
    // in the next change. Unchanged properties, so this deploy does not touch it.
    if (props.envConfig.envName === 'dev') {
      new acm.Certificate(this, 'WidgetCert', {
        domainName: PUBLIC_HOSTNAMES.embed,
        validation: acm.CertificateValidation.fromDns(hostedZone),
      });
    }
  }
}

export interface WidgetStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  /** us-east-1 cert covering every embed host (from WidgetCertStack). */
  readonly cert: acm.ICertificate;
}

export class WidgetStack extends Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: WidgetStackProps) {
    super(scope, id, props);
    const { envConfig, cert } = props;
    const names = hostnames(envConfig.envName);

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
      // Every embed host the environment serves (D6: embed-dev + the dual-host
      // embed.vantagemind.ai while dev owns the public names).
      domainNames: [...names.embedNames],
      certificate: cert,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultRootObject: 'index.html',
      // The frontend CI finds the distribution by the "Luciel <env> widget" prefix
      // (Luciel-Frontend deploy.yml) — keep it.
      comment: `Luciel ${envConfig.envName} widget CDN (${names.embed})`,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
    });

    // Every embed host -> CloudFront (A + AAAA ALIAS). Zone already exists.
    // recordIdFor keeps embed.vantagemind.ai on its original WidgetA/WidgetAaaa ids,
    // so the deployed records are updated in place, never re-created.
    const zone = importPublicZone(this);
    const cfTarget = route53.RecordTarget.fromAlias(
      new route53targets.CloudFrontTarget(this.distribution),
    );
    for (const name of names.embedNames) {
      const rid = recordIdFor(name);
      // No ttl: Route 53 ignores it on an alias record (the alias carries the
      // target's own TTL), and CDK warned at every synth.
      new route53.ARecord(this, `${rid}A`, { zone, recordName: name, target: cfTarget });
      new route53.AaaaRecord(this, `${rid}Aaaa`, {
        zone,
        recordName: name,
        target: cfTarget,
      });
    }

    new CfnOutput(this, 'WidgetBucketName', { value: this.bucket.bucketName });
    new CfnOutput(this, 'WidgetDistributionId', {
      value: this.distribution.distributionId,
    });
    new CfnOutput(this, 'WidgetDomain', { value: this.distribution.distributionDomainName });
    new CfnOutput(this, 'EmbedHost', { value: names.embed });
  }
}
