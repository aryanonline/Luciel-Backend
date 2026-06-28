/**
 * DNS + regional certificates layer (DEVOPS §5.6 / §9, Arch §3.1.5).
 *
 * Holds the REGIONAL (ca-central-1) ACM certs that attach to the ALB HTTPS
 * listeners, plus a handle to the EXISTING hosted zone (we never create a new one
 * — spec §1 / LD: the zone Z0203683JUDTD67YTV93 already exists).
 *
 *   - apiCert : api.vantagemind.ai  -> backend ALB 443 listener (ComputeStack)
 *   - webCert : vantagemind.ai (+www) -> web ALB 443 listener (WebStack)
 *
 * Both are DNS-validated against the existing zone, so synth/deploy creates the
 * CNAME validation records automatically — no manual cert dance.
 *
 * The CloudFront cert for embed.vantagemind.ai is NOT here: CloudFront requires its
 * cert in us-east-1, so it lives in WidgetCertStack (the one sanctioned region
 * deviation — see CLOUDFRONT_CERT_REGION). This stack is regional ca-central-1 and
 * carries the residency guard like every other in-region stack.
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';
import { EnvConfig, DOMAINS, HOSTED_ZONE } from './env-config';
import { ResidencyGuard } from './residency';

export interface DnsStackProps extends StackProps {
  readonly envConfig: EnvConfig;
}

export class DnsStack extends Stack {
  public readonly hostedZone: route53.IHostedZone;
  public readonly apiCert: acm.Certificate;
  public readonly webCert: acm.Certificate;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    // Region lock (ca-central-1) — these certs are regional, attached to ALBs.
    new ResidencyGuard(this, 'Residency');

    // Look up the EXISTING zone — never create one (spec §1).
    this.hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId: HOSTED_ZONE.id,
      zoneName: HOSTED_ZONE.name,
    });

    // Regional cert for the backend API ALB HTTPS listener.
    this.apiCert = new acm.Certificate(this, 'ApiCert', {
      domainName: DOMAINS.api,
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });

    // Regional cert for the web (apex marketing + dashboard) ALB HTTPS listener.
    this.webCert = new acm.Certificate(this, 'WebCert', {
      domainName: DOMAINS.apex,
      subjectAlternativeNames: [DOMAINS.www],
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });

    new CfnOutput(this, 'ApiCertArn', {
      value: this.apiCert.certificateArn,
      exportName: `Luciel-${props.envConfig.envName}-ApiCertArn`,
    });
    new CfnOutput(this, 'WebCertArn', {
      value: this.webCert.certificateArn,
      exportName: `Luciel-${props.envConfig.envName}-WebCertArn`,
    });
  }
}
