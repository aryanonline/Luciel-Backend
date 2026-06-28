/**
 * Security layer - KMS CMK + Secrets Manager namespace (DEVOPS §5.4, Arch §3.8.3).
 *
 * One customer-managed KMS key per environment encrypts RDS, S3, and Secrets
 * Manager. Secrets live ONLY in Secrets Manager (never in Postgres) - Postgres
 * stores opaque pointers `vm/{admin_id}/{instance_id}/{type}` (LD 20). This stack
 * provisions the key + the secret namespace the app's SecretsManagerStore binds to.
 */
import { Stack, StackProps, RemovalPolicy, Duration } from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface SecurityStackProps extends StackProps {
  readonly envConfig: EnvConfig;
}

export class SecurityStack extends Stack {
  public readonly cmk: kms.Key;
  /** Platform-level secrets (LLM keys, Twilio, SES creds) - distinct from per-tenant connection secrets. */
  public readonly platformSecret: secrets.Secret;

  constructor(scope: Construct, id: string, props: SecurityStackProps) {
    super(scope, id, props);
    const { envConfig } = props;

    // Customer-managed key. Rotation on; retain in prod so encrypted data stays readable.
    this.cmk = new kms.Key(this, 'Cmk', {
      description: `Luciel ${envConfig.envName} CMK (RDS, S3, Secrets) - §5.4`,
      enableKeyRotation: true,
      removalPolicy: envConfig.envName === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      pendingWindow: Duration.days(envConfig.envName === 'prod' ? 30 : 7),
    });
    this.cmk.addAlias(`alias/luciel-${envConfig.envName}`);

    // Allow this account's IAM principals to use the key for decrypt ONLY via
    // Secrets Manager (the kms:ViaService guard). This lets the ECS execution role
    // resolve KMS-encrypted secrets at task startup without a cross-stack grant
    // (which would create a Security<->Compute dependency cycle). Without this the
    // task fails: 'unable to retrieve secret ... AccessDeniedException: Access to
    // KMS is not allowed'. Scoped to SecretsManager so it is not a blanket grant.
    this.cmk.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowDecryptViaSecretsManager',
        principals: [new iam.AccountRootPrincipal()],
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'kms:ViaService': `secretsmanager.${this.region}.amazonaws.com`,
          },
        },
      }),
    );

    // Platform secret placeholder. Real values are injected post-deploy via the
    // secure path (NOT committed). Generated empty so nothing sensitive is in IaC.
    this.platformSecret = new secrets.Secret(this, 'PlatformSecret', {
      secretName: `luciel/${envConfig.envName}/platform`,
      description: 'Platform integration credentials (LLM/Twilio/SES). Populated out-of-band.',
      encryptionKey: this.cmk,
    });
  }
}
