/**
 * Interface endpoints are env-gated (env-config.ts vpcInterfaceEndpoints).
 *
 * Five interface endpoints x two AZs is ~$73/month of standing cost whether or not a
 * single request flows. Every environment already pays for NAT, which carries the
 * same AWS-API traffic for metered pennies at our volume — so since round 7 WP-8b
 * NO environment pays for both (prod used to). The S3 GATEWAY endpoint is free and
 * stays everywhere — ECR image layers download from S3, so losing it would push
 * pull traffic through NAT data processing in every environment.
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
  test.each<EnvName>(['dev', 'preprod', 'prod'])(
    '%s: no interface endpoints — NAT carries the traffic; free S3 gateway stays',
    (envName) => {
      expect(getEnvConfig(envName).vpcInterfaceEndpoints).toBe(false);
      const counts = endpointsByType(networkTemplate(envName));
      expect(counts.interface).toBe(0);
      expect(counts.gateway).toBe(1);
    },
  );

  test('the flag still brings the five endpoints back (the construct is not dead code)', () => {
    const app = new App();
    const net = new NetworkingStack(app, 'Net', {
      env: ENV,
      envConfig: { ...getEnvConfig('prod'), vpcInterfaceEndpoints: true },
    });
    const counts = endpointsByType(Template.fromStack(net));
    expect(counts.interface).toBe(5);
    expect(counts.gateway).toBe(1);
  });
});
