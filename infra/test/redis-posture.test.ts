/**
 * Redis topology per environment (round 7 WP-8; Architecture §4.4).
 *
 * The data stack hard-coded the DEV shape — one cache.t4g.micro, no replica, no
 * failover — for every environment, while §4.4 promises Multi-AZ replication in
 * production. The shape now comes from env-config: dev keeps its single node,
 * preprod/prod run primary + replica across the two AZs with automatic failover.
 * Preprod mirrors prod so the release rehearsal exercises the failover-capable
 * topology, not a lookalike.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { getEnvConfig, OFFERING_REGION, EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildData(envName: EnvName) {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  return Template.fromStack(
    new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk }),
  );
}

describe('Redis topology follows the environment (§4.4)', () => {
  test('dev: one cache.t4g.micro, no replica, no failover (single-AZ by design)', () => {
    const cfg = getEnvConfig('dev');
    expect(cfg.redisNodes).toBe(1);
    buildData('dev').hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      CacheNodeType: 'cache.t4g.micro',
      NumCacheClusters: 1,
      AutomaticFailoverEnabled: false,
      MultiAZEnabled: false,
    });
  });

  test.each([['preprod'], ['prod']])(
    '%s: primary + replica across AZs with automatic failover',
    (envName) => {
      const cfg = getEnvConfig(envName as EnvName);
      expect(cfg.redisNodes).toBe(2);
      expect(cfg.maxAzs).toBeGreaterThanOrEqual(2);
      const t = buildData(envName as EnvName);
      t.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
        CacheNodeType: cfg.redisNodeType,
        NumCacheClusters: 2,
        AutomaticFailoverEnabled: true,
        MultiAZEnabled: true,
      });
      // The replica can only land in a second AZ if the subnet group spans one.
      const groups = t.findResources('AWS::ElastiCache::SubnetGroup');
      for (const group of Object.values(groups)) {
        const subnets = (group.Properties as { SubnetIds: unknown[] }).SubnetIds;
        expect(subnets.length).toBeGreaterThanOrEqual(2);
      }
    },
  );

  test('snapshot retention stays zero in every environment (cost ceiling, unchanged)', () => {
    for (const envName of ['dev', 'preprod', 'prod'] as EnvName[]) {
      buildData(envName).hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
        SnapshotRetentionLimit: 0,
      });
    }
  });
});
