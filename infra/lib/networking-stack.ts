/**
 * Networking layer (DEVOPS §5.2).
 *
 * VPC across two AZs with public subnets (ALB only) and private subnets (ECS tasks,
 * RDS, ElastiCache). RDS and Redis are never publicly reachable. Carries the
 * ResidencyGuard so every stack in the app inherits the ca-central-1 region lock
 * (§4.2 / LD 21).
 *
 * NOTE: this stack does provision a NAT gateway (a paid resource). Per the Phase B
 * plan it is the cheapest billable layer; the founder dollar sign-off hard-stop is
 * BEFORE the data layer (RDS), not here. dev uses a single NAT to keep cost down.
 */

import { Stack, StackProps } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';
import { ResidencyGuard } from './residency';

export interface NetworkingStackProps extends StackProps {
  readonly envConfig: EnvConfig;
}

export class NetworkingStack extends Stack {
  public readonly vpc: ec2.Vpc;
  public readonly residency: ResidencyGuard;

  constructor(scope: Construct, id: string, props: NetworkingStackProps) {
    super(scope, id, props);
    const { envConfig } = props;

    // Region lock applies to this and every dependent stack.
    this.residency = new ResidencyGuard(this, 'Residency');

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: envConfig.maxAzs,
      natGateways: envConfig.natGateways,
      subnetConfiguration: [
        // Public: ALB only. Nothing else gets a public route.
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        // Private-with-egress: ECS tasks (need outbound via NAT for LLM/Twilio/etc).
        { name: 'app', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 23 },
        // Isolated: RDS + ElastiCache. No internet route at all (never public).
        { name: 'data', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });

    // VPC endpoints so private ECS tasks reach AWS APIs reliably WITHOUT depending
    // on NAT (fixes the migration task's 'context deadline exceeded' to Secrets
    // Manager). Interface endpoints for SM/ECR/Logs/STS; S3 gateway endpoint for
    // ECR layer pulls.
    //
    // Interface endpoints are ENV-GATED (env-config.ts vpcInterfaceEndpoints): five
    // endpoints x two AZs is ~$73/month whether or not a single request flows. Every
    // environment rides its NAT gateway(s) for this traffic since round 7 WP-8b —
    // same reachability, metered pennies at our volume, and prod was paying for NAT
    // AND the endpoints at once. The construct stays so the flag can bring them back
    // (the documented future option is Secrets Manager alone). The S3 GATEWAY
    // endpoint below is free and stays everywhere.
    if (envConfig.vpcInterfaceEndpoints) {
      const interfaceEndpoints: Record<string, ec2.InterfaceVpcEndpointAwsService> = {
        SecretsManager: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        EcrApi: ec2.InterfaceVpcEndpointAwsService.ECR,
        EcrDkr: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
        CwLogs: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        Sts: ec2.InterfaceVpcEndpointAwsService.STS,
      };
      for (const [id, service] of Object.entries(interfaceEndpoints)) {
        this.vpc.addInterfaceEndpoint(id, {
          service,
          subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
          privateDnsEnabled: true,
        });
      }
    }
    // S3 gateway endpoint (ECR image layers live in S3).
    this.vpc.addGatewayEndpoint('S3', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });
  }
}
