/**
 * Interface endpoints are env-gated (env-config.ts vpcInterfaceEndpoints).
 *
 * Five interface endpoints x two AZs is ~$73/month of standing cost whether or not a
 * single request flows — the largest line item in an idle dev environment, which
 * already has a NAT gateway serving the same traffic for metered pennies at dev
 * volume. Dev therefore goes without; preprod/prod keep them for throughput and
 * blast-radius isolation. The S3 GATEWAY endpoint is free and stays everywhere —
 * ECR image layers download from S3, so losing it would push pull traffic through
 * NAT data processing in every environment.
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { EnvName, getEnvConfig, OFFERING_REGION } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function networkTemplate(envName: EnvName) {
  const app = new App();
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig: getEnvConfig(envName) });
  return Template.fromStack(net);
}

function endpointsByType(template: Template): { interface: number; gateway: number } {
  const endpoints = template.findResources('AWS::EC2::VPCEndpoint');
  let iface = 0;
  let gateway = 0;
  for (const res of Object.values(endpoints)) {
    const type = (res as { Properties: { VpcEndpointType?: string } }).Properties.VpcEndpointType;
    if (type === 'Interface') iface += 1;
    else gateway += 1; // Gateway is the CFN default when the property is absent
  }
  return { interface: iface, gateway };
}

describe('VPC endpoint cost gating', () => {
  test('dev: no interface endpoints — NAT carries the traffic; free S3 gateway stays', () => {
    const counts = endpointsByType(networkTemplate('dev'));
    expect(counts.interface).toBe(0);
    expect(counts.gateway).toBe(1);
  });

  for (const envName of ['preprod', 'prod'] as const) {
    test(`${envName}: all five interface endpoints + the S3 gateway`, () => {
      const counts = endpointsByType(networkTemplate(envName));
      expect(counts.interface).toBe(5);
      expect(counts.gateway).toBe(1);
    });
  }
});
