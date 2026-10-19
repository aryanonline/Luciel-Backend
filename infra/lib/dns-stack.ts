/**
 * DNS facts layer (DEVOPS §5.6 / §9, Arch §3.1.5; reshaped round 7 WP-8b, D6/D7).
 *
 * Holds a handle to the EXISTING hosted zone (we never create one — spec §1 / LD: the
 * zone Z0203683JUDTD67YTV93 already exists) and PUBLISHES the environment's hostnames
 * (env-config.ts `hostnames`) as stack exports, so tooling outside this app — the
 * frontend CI's build-time API base, an operator — reads the environment's names from
 * the environment instead of hard-coding the public ones.
 *
 * CERTIFICATES MOVED OUT. This stack used to own the regional ACM certs (api, and
 * apex+www) that ComputeStack's and WebStack's ALB listeners consumed. With one ALB
 * per environment (D7) the one listener needs one certificate covering BOTH name
 * sets, and it now lives in ComputeStack beside the listener: a certificate whose
 * names change (D6's cutover drops the public names from dev) is then REPLACED inside
 * the stack that uses it — CloudFormation creates the new one, moves the listener,
 * and only then deletes the old. Owned here, a replacement deleted the old cert while
 * the consuming stack's listener still held it.
 *
 * The two certs that lived here (ApiCert, WebCert) are retired by the same deploy
 * that moves the listener: bin/luciel-infra.ts makes this stack deploy AFTER Compute
 * and Web, so by the time CloudFormation deletes them nothing references them.
 *
 * The CloudFront cert for the widget is NOT here: CloudFront requires its cert in
 * us-east-1, so it lives in WidgetCertStack (the one sanctioned region deviation — see
 * CLOUDFRONT_CERT_REGION). This stack is regional ca-central-1 and carries the
 * residency guard like every other in-region stack.
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';
import { EnvConfig, HOSTED_ZONE, baseUrls, hostnames } from './env-config';
import { ResidencyGuard } from './residency';

/**
 * The existing vantagemind.ai zone, imported by attributes (no lookup, no resource,
 * no cross-stack edge): every stack that writes a record or validates a certificate
 * imports it in its own scope.
 */
export function importPublicZone(scope: Construct, id = 'Zone'): route53.IHostedZone {
  return route53.HostedZone.fromHostedZoneAttributes(scope, id, {
    hostedZoneId: HOSTED_ZONE.id,
    zoneName: HOSTED_ZONE.name,
  });
}

export interface DnsStackProps extends StackProps {
  readonly envConfig: EnvConfig;
}

export class DnsStack extends Stack {
  public readonly hostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);
    const env = props.envConfig.envName;

    // Region lock (ca-central-1), like every in-region stack.
    new ResidencyGuard(this, 'Residency');

    // Look up the EXISTING zone — never create one (spec §1).
    this.hostedZone = importPublicZone(this);

    const names = hostnames(env);
    const urls = baseUrls(env);
    new CfnOutput(this, 'AppBaseUrl', {
      value: urls.app,
      exportName: `Luciel-${env}-AppBaseUrl`,
      description: 'Primary dashboard/marketing origin (APP_BASE_URL)',
    });
    new CfnOutput(this, 'ApiBaseUrl', {
      value: urls.api,
      exportName: `Luciel-${env}-ApiBaseUrl`,
      description: 'Primary API origin (API_BASE_URL / NEXT_PUBLIC_API_BASE_URL / widget API base)',
    });
    new CfnOutput(this, 'EmbedHost', {
      value: names.embed,
      exportName: `Luciel-${env}-EmbedHost`,
      description: 'Primary widget CDN host',
    });
    new CfnOutput(this, 'AliasHosts', {
      // CloudFormation refuses an empty output value.
      value: names.aliases.length ? names.aliases.join(',') : '-',
      description: 'Other names this environment answers on (the D6 dual-host); "-" = none',
    });
  }
}
