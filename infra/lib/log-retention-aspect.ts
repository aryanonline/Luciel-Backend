/**
 * Bound the log groups nobody in this repo declares (Milestone 3 B).
 *
 * Every log group the stacks create by hand carries a retention. The ones left over
 * belong to functions CDK synthesizes on our behalf — the S3 auto-delete provider, the
 * VPC default-SG restrictor, the OIDC-provider handler, the LogRetention setter — and
 * their groups are created by the Lambda service at first invocation, with retention
 * "Never expire". They are tiny (a few lines per deploy) but they are also permanent,
 * unattributed, and impossible to notice: they do not appear in any template, so no
 * amount of reading the stacks reveals them.
 *
 * An aspect is the only lever, because these functions are internal to constructs we
 * do not own. It cannot set retention directly — the group does not exist at synth —
 * so instead it creates a group we DO own and points the function's LoggingConfig at
 * it, which is the supported way to redirect a Lambda's logs. Functions that already
 * declare a LoggingConfig are left alone.
 *
 * Note the resource-type check rather than an `instanceof lambda.CfnFunction`: CDK's
 * core `CustomResourceProvider` emits a raw ``CfnResource`` of type
 * AWS::Lambda::Function specifically to avoid depending on aws-lambda, so three of the
 * four functions this exists for would slip past a class check.
 */
import { CfnResource, IAspect, RemovalPolicy } from 'aws-cdk-lib';
import * as logs from 'aws-cdk-lib/aws-logs';
import { IConstruct } from 'constructs';

export class BoundedLambdaLogs implements IAspect {
  constructor(private readonly retention: logs.RetentionDays) {}

  visit(node: IConstruct): void {
    if (!CfnResource.isCfnResource(node) || node.cfnResourceType !== 'AWS::Lambda::Function') {
      return;
    }
    if (node.node.tryFindChild('BoundedLogs') !== undefined) return;
    const group = new logs.LogGroup(node, 'BoundedLogs', {
      retention: this.retention,
      // These functions run only during a stack operation; their logs have no value
      // once the stack that produced them is gone.
      removalPolicy: RemovalPolicy.DESTROY,
    });
    node.addPropertyOverride('LoggingConfig.LogGroup', group.logGroupName);
  }
}
