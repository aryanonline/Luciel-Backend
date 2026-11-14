/**
 * Restore from a kept snapshot (portfolio clean-up, 2026-11-14).
 *
 * The CDK app could only create a fresh, empty database. An environment that names
 * `dbSnapshotIdentifier` (env-config.ts) now gets `rds.DatabaseInstanceFromSnapshot`
 * with the same shape, the same construct id (so the instance, its master secret and
 * every consumer keep their logical ids) and a NEW generated master secret encrypted
 * with the CMK. Dev names its 2026-11-14 final snapshot; an environment without the
 * field synthesises exactly as before. The RETAIN / deletion-protection posture is
 * pinned in ops-posture.test.ts and holds on both paths.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EnvConfig, EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };
const DEV_SNAPSHOT = 'luciel-dev-final-2026-11-14';

function build(envConfig: EnvConfig) {
  const app = new App();
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  return { data, template: Template.fromStack(data) };
}

/** Dev exactly as it was before the field existed: a fresh database. */
function devWithoutSnapshot(): EnvConfig {
  return { ...getEnvConfig('dev'), dbSnapshotIdentifier: undefined };
}

function onlyInstance(template: Template): [string, any] {
  const instances = Object.entries(template.findResources('AWS::RDS::DBInstance'));
  expect(instances).toHaveLength(1);
  return instances[0];
}

function onlySecret(template: Template): [string, any] {
  const secrets = Object.entries(template.findResources('AWS::SecretsManager::Secret'));
  expect(secrets).toHaveLength(1);
  return secrets[0];
}

describe('only dev names a snapshot', () => {
  test('dev restores from its 2026-11-14 final snapshot; preprod and prod start fresh', () => {
    expect(getEnvConfig('dev').dbSnapshotIdentifier).toBe(DEV_SNAPSHOT);
    // Preprod is created from scratch and destroyed on every release; prod has no
    // snapshot to come back from.
    expect(getEnvConfig('preprod').dbSnapshotIdentifier).toBeUndefined();
    expect(getEnvConfig('prod').dbSnapshotIdentifier).toBeUndefined();
  });
});

describe('dev restores from the snapshot with a generated master secret', () => {
  test('the instance is restored from the snapshot, with everything else it had', () => {
    const [, instance] = onlyInstance(build(getEnvConfig('dev')).template);
    expect(instance.Properties).toMatchObject({
      DBSnapshotIdentifier: DEV_SNAPSHOT,
      Engine: 'postgres',
      EngineVersion: '16',
      DBInstanceClass: 'db.t4g.micro',
      MultiAZ: false,
      PubliclyAccessible: false,
      AllocatedStorage: '20',
      MaxAllocatedStorage: 50,
      BackupRetentionPeriod: 7,
      DeleteAutomatedBackups: true,
      DeletionProtection: true,
      EnableCloudwatchLogsExports: ['postgresql'],
    });
    expect(instance.DeletionPolicy).toBe('Retain');
    expect(instance.UpdateReplacePolicy).toBe('Retain');
  });

  test('storage encryption, its key, the master username and the database come from the snapshot', () => {
    const [, instance] = onlyInstance(build(getEnvConfig('dev')).template);
    // CloudFormation inherits these from an encrypted snapshot and refuses or ignores
    // them on a restore; setting them would not re-key or rename anything.
    for (const inherited of ['StorageEncrypted', 'KmsKeyId', 'MasterUsername', 'DBName']) {
      expect(instance.Properties).not.toHaveProperty(inherited);
    }
  });

  test('a NEW master secret is generated for luciel_master, encrypted with the CMK, and attached', () => {
    const { template } = build(getEnvConfig('dev'));
    const [secretId, secret] = onlySecret(template);
    expect(secret.Properties.GenerateSecretString).toMatchObject({
      SecretStringTemplate: '{"username":"luciel_master"}',
      GenerateStringKey: 'password',
    });
    // The CMK lives in the Security stack: a cross-stack reference, never the default key.
    expect(secret.Properties.KmsKeyId).toBeDefined();
    expect(JSON.stringify(secret.Properties.KmsKeyId)).toContain('Cmk');
    const [instanceId, instance] = onlyInstance(template);
    // RDS applies the generated password once the restore completes.
    expect(JSON.stringify(instance.Properties.MasterUserPassword)).toContain(secretId);
    // Attached, so the secret carries host/port/engine like the fresh instance's did.
    template.hasResourceProperties('AWS::SecretsManager::SecretTargetAttachment', {
      SecretId: { Ref: secretId },
      TargetId: { Ref: instanceId },
      TargetType: 'AWS::RDS::DBInstance',
    });
  });

  test('the construct id is kept: instance and secret have the logical ids of the fresh database', () => {
    const restored = build(getEnvConfig('dev')).template;
    const fresh = build(devWithoutSnapshot()).template;
    expect(onlyInstance(restored)[0]).toBe(onlyInstance(fresh)[0]);
    expect(onlySecret(restored)[0]).toBe(onlySecret(fresh)[0]);
  });

  test('Compute still receives the master secret ARN and the DB security group is unchanged', () => {
    // Built by hand: the Compute stack must join the app before anything synthesises.
    const app = new App();
    const dev = getEnvConfig('dev');
    const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: dev });
    const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig: dev });
    const data = new DataStack(app, 'Data', { env: ENV, envConfig: dev, vpc: net.vpc, cmk: sec.cmk });
    expect(data.db.secret).toBeDefined();
    const compute = new ComputeStack(app, 'Compute', {
      env: ENV,
      envConfig: getEnvConfig('dev'),
      vpc: net.vpc,
      cmkArn: sec.cmk.keyArn,
      transcriptArchiveBucket: data.transcriptArchiveBucket,
      exportBucket: data.exportBucket,
      voiceAudioBucket: data.voiceAudioBucket,
      knowledgeOriginalsBucket: data.knowledgeOriginalsBucket,
      dbSecretArn: data.db.secret!.secretArn,
      platformSecretArn: sec.platformSecret.secretArn,
      redisUrl: data.redisUrl,
    });
    const containers = Object.values(Template.fromStack(compute).findResources('AWS::ECS::TaskDefinition'))
      .flatMap((td: any) => td.Properties.ContainerDefinitions);
    const injected = containers.filter((c: any) =>
      (c.Secrets ?? []).some((s: any) => s.Name === 'LUCIEL_DB_SECRET'),
    );
    expect(injected.length).toBeGreaterThan(0);
    const template = Template.fromStack(data);
    const [, instance] = onlyInstance(template);
    const [sgId] = Object.keys(template.findResources('AWS::EC2::SecurityGroup', {
      Properties: { GroupDescription: Match.stringLikeRegexp('Luciel RDS') },
    }));
    expect(instance.Properties.VPCSecurityGroups).toEqual([{ 'Fn::GetAtt': [sgId, 'GroupId'] }]);
  });
});

describe('the field is one-way once dev has been restored', () => {
  // DBSnapshotIdentifier alone may be removed after a restore (AWS::RDS::DBInstance:
  // "Update requires: Some interruptions"). Unsetting the field is destructive because
  // of what the fresh path adds under the SAME logical id: four properties whose
  // AWS::RDS::DBInstance entries each say "Update requires: Replacement", so
  // CloudFormation would replace the restored database with an empty one, and RETAIN
  // would orphan the restored instance outside the stack.
  const REPLACEMENT_PROPERTIES = ['DBName', 'KmsKeyId', 'MasterUsername', 'StorageEncrypted'];

  test('unsetting it adds exactly the four replacement properties to the same instance', () => {
    const [restoredId, restored] = onlyInstance(build(getEnvConfig('dev')).template);
    const [freshId, fresh] = onlyInstance(build(devWithoutSnapshot()).template);
    expect(freshId).toBe(restoredId);
    const added = Object.keys(fresh.Properties).filter((key) => !(key in restored.Properties));
    const removed = Object.keys(restored.Properties).filter((key) => !(key in fresh.Properties));
    expect(added.sort()).toEqual(REPLACEMENT_PROPERTIES);
    expect(removed).toEqual(['DBSnapshotIdentifier']);
    // Nothing else differs, so these four are the whole replacement trigger.
    for (const key of Object.keys(restored.Properties).filter((k) => k in fresh.Properties)) {
      expect([key, fresh.Properties[key]]).toEqual([key, restored.Properties[key]]);
    }
    expect(restored.UpdateReplacePolicy).toBe('Retain');
  });
});

describe('an environment without a snapshot synthesises as before', () => {
  test.each<[string, () => EnvConfig]>([
    ['prod', () => getEnvConfig('prod')],
    ['preprod', () => getEnvConfig('preprod')],
    ['dev without the field', devWithoutSnapshot],
  ])('%s: a fresh, CMK-encrypted luciel database with a generated master secret', (_name, config) => {
    const [, instance] = onlyInstance(build(config()).template);
    expect(instance.Properties).not.toHaveProperty('DBSnapshotIdentifier');
    expect(instance.Properties).toMatchObject({
      StorageEncrypted: true,
      DBName: 'luciel',
      MasterUsername: 'luciel_master',
      Engine: 'postgres',
      EngineVersion: '16',
    });
    expect(instance.Properties.KmsKeyId).toBeDefined();
  });

  test.each<EnvName>(['preprod', 'prod'])('%s keeps its removal posture (Delete in preprod only)', (envName) => {
    const [, instance] = onlyInstance(build(getEnvConfig(envName)).template);
    expect(instance.DeletionPolicy).toBe(envName === 'preprod' ? 'Delete' : 'Retain');
    expect(instance.Properties.DeletionProtection).toBe(envName !== 'preprod');
  });
});
