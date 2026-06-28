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

    // Platform secret placeholder. Real values are injected post-deploy via the
    // secure path (NOT committed). Generated empty so nothing sensitive is in IaC.
    this.platformSecret = new secrets.Secret(this, 'PlatformSecret', {
      secretName: `luciel/${envConfig.envName}/platform`,
      description: 'Platform integration credentials (LLM/Twilio/SES). Populated out-of-band.',
      encryptionKey: this.cmk,
    });
  }
}
