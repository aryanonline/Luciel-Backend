/**
 * 2026-09-05 audit F067: the public-ingress data plane holds a narrower task role
 * than the control plane and the sweeps. Pin the split, and pin what the data-plane
 * role may and may not do with tenant connection secrets.
 */
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { NetworkingStack } from "../lib/networking-stack";
import { SecurityStack } from "../lib/security-stack";
import { DataStack } from "../lib/data-stack";
import { ComputeStack } from "../lib/compute-stack";
import { getEnvConfig, OFFERING_REGION } from "../lib/env-config";
import type { EnvName } from "../lib/env-config";

const ENV = { account: "111111111111", region: OFFERING_REGION };

function buildCompute(envName: EnvName) {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, "Net", { env: ENV, envConfig });
  const sec = new SecurityStack(app, "Sec", { env: ENV, envConfig });
  const data = new DataStack(app, "Data", {
    env: ENV,
    envConfig,
    vpc: net.vpc,
    cmk: sec.cmk,
  });
  const compute = new ComputeStack(app, "Compute", {
    env: ENV,
    envConfig,
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
  return Template.fromStack(compute);
}

type Resource = { Type: string; Properties: Record<string, any> };

/** The logical id of the IAM role a service's task definition runs as. */
function taskRoleOfService(template: Template, serviceId: string): string {
  const services = template.findResources("AWS::ECS::Service");
  const [id, service] =
    Object.entries(services).find(([k]) => k.startsWith(serviceId)) ?? [];
  if (!id || !service) throw new Error(`service ${serviceId} not found`);
  const tdRef = (service as Resource).Properties.TaskDefinition.Ref as string;
  const td = template.findResources("AWS::ECS::TaskDefinition")[
    tdRef
  ] as Resource;
  return td.Properties.TaskRoleArn["Fn::GetAtt"][0] as string;
}

/** The logical id of the IAM role a stand-alone task definition runs as. */
function taskRoleOfTaskDef(template: Template, taskDefId: string): string {
  const defs = template.findResources("AWS::ECS::TaskDefinition");
  const [id, td] =
    Object.entries(defs).find(([k]) => k.startsWith(taskDefId)) ?? [];
  if (!id || !td) throw new Error(`task definition ${taskDefId} not found`);
  return (td as Resource).Properties.TaskRoleArn["Fn::GetAtt"][0] as string;
}

/** Every statement in every policy attached to the given role logical id. */
function statementsOfRole(template: Template, roleId: string): any[] {
  return Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((p) =>
      ((p as Resource).Properties.Roles ?? []).some(
        (r: any) => r.Ref === roleId,
      ),
    )
    .flatMap((p) => (p as Resource).Properties.PolicyDocument.Statement);
}

const actionsOf = (statement: any): string[] =>
  [statement.Action].flat().sort();

describe.each<EnvName>(["dev", "prod"])(
  "plane task-role split (%s)",
  (envName) => {
    test("the data plane and the control plane run as different roles", () => {
      const template = buildCompute(envName);
      const dataRole = taskRoleOfService(template, "DataPlane");
      const controlRole = taskRoleOfService(template, "ControlPlane");
      expect(dataRole).not.toEqual(controlRole);
      expect(dataRole).toMatch(/^DataPlaneTaskRole/);
      expect(controlRole).toMatch(/^TaskRole/);
    });

    test("only the sweep role may rotate the platform secret or restart the app services (F115)", () => {
      const template = buildCompute(envName);
      const sweepRole = taskRoleOfTaskDef(template, "SweepTask");
      expect(sweepRole).toMatch(/^SweepTaskRole/);
      const sweepSids = statementsOfRole(template, sweepRole).map((s) => s.Sid);
      expect(sweepSids).toContain("RotatePlatformSecret");
      expect(sweepSids).toContain("RestartAppServices");
      // The sweeps keep the full app-plane set (retention deletes, tenant secrets, email).
      expect(sweepSids).toContain("ManageTenantConnectionSecrets");
      expect(sweepSids).toContain("SendPlatformEmail");
      for (const serviceId of ["ControlPlane", "DataPlane"]) {
        const sids = statementsOfRole(
          template,
          taskRoleOfService(template, serviceId),
        ).map((s) => s.Sid);
        expect(sids).not.toContain("RotatePlatformSecret");
        expect(sids).not.toContain("RestartAppServices");
      }
      // The scheduler may pass ONLY the sweep role (and the execution role) to ECS.
      const passRole = Object.values(template.findResources("AWS::IAM::Policy"))
        .flatMap(
          (p) => (p as Resource).Properties.PolicyDocument.Statement as any[],
        )
        .find((s) => [s.Action].flat().includes("iam:PassRole"));
      expect(passRole).toBeDefined();
      const passed = JSON.stringify(passRole.Resource);
      expect(passed).toMatch(/SweepTaskRole/);
      expect(passed).not.toMatch(/"TaskRole[A-F0-9]/);
    });

    test("the data-plane role reads and rotates tenant secrets but never creates or deletes them", () => {
      const template = buildCompute(envName);
      const statements = statementsOfRole(
        template,
        taskRoleOfService(template, "DataPlane"),
      );
      const tenant = statements.find(
        (s) => s.Sid === "UseTenantConnectionSecrets",
      );
      expect(tenant).toBeDefined();
      expect(actionsOf(tenant)).toEqual([
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
      ]);
      expect(tenant.Resource).toEqual(
        `arn:aws:secretsmanager:${ENV.region}:${ENV.account}:secret:vm/*`,
      );
      const sids = statements.map((s) => s.Sid).filter(Boolean);
      expect(sids).not.toContain("ManageTenantConnectionSecrets");
      expect(sids).not.toContain("RotatePlatformSecret");
      expect(sids).not.toContain("RestartAppServices");
    });

    test("the data-plane role touches neither the export bucket nor the knowledge originals", () => {
      const template = buildCompute(envName);
      const statements = statementsOfRole(
        template,
        taskRoleOfService(template, "DataPlane"),
      );
      // Buckets are cross-stack imports named after their construct ids
      // (Data:ExportsOutputFnGetAttResidency<Name>...); match the construct name.
      const resources = JSON.stringify(statements.map((s) => s.Resource));
      expect(resources).not.toMatch(/ResidencyExport[A-F0-9]/);
      expect(resources).not.toMatch(/ResidencyKnowledgeOriginals/);
      expect(resources).toMatch(/ResidencyVoiceAudio/);
      expect(resources).toMatch(/ResidencyTranscriptArchive/);
    });

    test("the control plane keeps the full tenant-secret lifecycle", () => {
      const template = buildCompute(envName);
      const statements = statementsOfRole(
        template,
        taskRoleOfService(template, "ControlPlane"),
      );
      const tenant = statements.find(
        (s) => s.Sid === "ManageTenantConnectionSecrets",
      );
      expect(tenant).toBeDefined();
      expect(actionsOf(tenant)).toContain("secretsmanager:CreateSecret");
      expect(actionsOf(tenant)).toContain("secretsmanager:DeleteSecret");
    });
  },
);
