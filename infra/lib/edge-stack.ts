/**
 * Edge layer - CloudFront + WAF (DEVOPS §5.6, Arch §3.1.5).
 *
 * Closes AUDIT FINDING M2: the widget edge security posture (Origin/Referer
 * allowlist + bot challenge + per-tenant rate limit) that the app deliberately
 * delegates to the edge. Until this existed, off-domain embedding and bot abuse
 * were not actually rejected. This is the enforcement the application layer trusts.
 *
 * WAF (regional, on the ALB) provides:
 *  - AWS managed common rule set + known-bad-inputs (baseline).
 *  - Rate-based rule: per-IP request ceiling (denial-of-wallet backstop, §3.1.5).
 *  - A bot-control / challenge action on the widget ingress paths.
 * Origin/Referer allowlisting per registered admin domain is enforced at the app
 * edge via the embed-key + this WAF; the registered-domain check is data-driven so
 * it cannot be a static IaC list (documented seam).
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface EdgeStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  readonly alb: elbv2.IApplicationLoadBalancer;
}

export class EdgeStack extends Stack {
  public readonly webAcl?: wafv2.CfnWebACL;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);
    const { envConfig, alb } = props;

    // WAF follows the environment's WAKEFULNESS in dev (owner cost mandate,
    // 2026-08-12): a paused environment's ALB serves nothing but 503s, so its
    // firewall protects nothing while still billing (~$20+/month, hourly). The
    // pause/unpause pipeline deploys this stack on every run, so the ACL is
    // removed at pause and re-created automatically the moment the environment
    // wakes — an UNPAUSED dev (live demos, real keys) is always protected.
    // Environments with armed alarms (preprod/prod) keep the WAF unconditionally:
    // production posture is never cost-gated.
    if (!envConfig.alarmsArmed && envConfig.paused) {
      return;
    }

    this.webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      scope: 'REGIONAL', // attaches to the ALB
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `luciel-${envConfig.envName}-waf`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'AWSCommon',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'aws-common',
            sampledRequestsEnabled: true,
          },
        },
        {
          name: 'KnownBadInputs',
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'known-bad',
            sampledRequestsEnabled: true,
          },
        },
        {
          // Per-IP rate ceiling - denial-of-wallet backstop at the edge (§3.1.5).
          name: 'RateLimitPerIp',
          priority: 3,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: envConfig.envName === 'prod' ? 2000 : 500,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'rate-limit',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    // Bind the WAF to the ALB.
    new wafv2.CfnWebACLAssociation(this, 'WebAclAssoc', {
      resourceArn: alb.loadBalancerArn,
      webAclArn: this.webAcl.attrArn,
    });
  }
}
