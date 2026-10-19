/**
 * Edge layer - regional WAF on the ALBs (DEVOPS §5.6, Arch §3.1.5).
 *
 * What this WebACL actually enforces — exactly three rules, pinned by
 * infra/test (round 6 WP-I, audit F066: the previous header claimed an
 * Origin/Referer allowlist and a bot challenge that were never built):
 *  - AWSCommon: the AWS managed common rule set (baseline);
 *  - KnownBadInputs: the AWS managed known-bad-inputs set;
 *  - RateLimitPerIp: a per-IP request ceiling (denial-of-wallet backstop, §3.1.5).
 *
 * What it does NOT do: there is no CloudFront distribution, no bot-control /
 * challenge action, and no per-tenant or per-domain rule here. The owner's
 * website allowlist is data (instances.allowed_origins) and is enforced by the
 * widget data plane (app/api/v1/chat_widget._enforce_origin), which compares the
 * page origin of every bootstrap / send / history call; per-source and
 * per-tenant ceilings live in app/channels/rate_limit.py. One WebACL, ONE ALB
 * association: since round 7 WP-8b (owner decision D7) the environment has a single
 * ALB serving the API AND the dashboard/marketing hosts (host-based routing), so the
 * one association covers login and the anonymous contact form as well as the API.
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface EdgeStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  /** The environment's one ALB (ComputeStack): API + dashboard/marketing hosts (D7). */
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
    if (!envConfig.productionPosture && envConfig.paused) {
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

    // Bind the WAF to the environment's one ALB. The web surface (login, the
    // anonymous contact form) rides the same ALB since D7, so it stays firewalled —
    // the separate web-ALB association this stack used to carry went with that ALB.
    new wafv2.CfnWebACLAssociation(this, 'WebAclAssoc', {
      resourceArn: alb.loadBalancerArn,
      webAclArn: this.webAcl.attrArn,
    });
  }
}
