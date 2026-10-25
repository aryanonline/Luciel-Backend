/**
 * Round 7 WP-7 — what the app needs from the stack to talk to external providers.
 *
 * The public API origin (`public_api_base_url`) was left to the platform secret's
 * API_BASE_URL alone. Absent there, Twilio webhook auto-config and the health
 * sweep's drift repair silently fell back to manual guidance and customer-facing
 * SMS/export links lost their host — with nothing on /healthz to say why. The
 * stack now injects LUCIEL_PUBLIC_API_BASE_URL into every plane that builds such a
 * URL, from the same constant its SNS subscriptions and the SeedTest harness use.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack, CONTROL_PLANE_PATH_PATTERNS } from '../lib/compute-stack';
import { getEnvConfig, OFFERING_REGION, hostnames, baseUrls } from '../lib/env-config';
import type { EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function buildCompute(envName: EnvName): Template {
  const app = new App();
  const envConfig = getEnvConfig(envName);
  const net = new NetworkingStack(app, 'Net', { env: ENV, envConfig });
  const sec = new SecurityStack(app, 'Sec', { env: ENV, envConfig });
  const data = new DataStack(app, 'Data', { env: ENV, envConfig, vpc: net.vpc, cmk: sec.cmk });
  return Template.fromStack(
    new ComputeStack(app, 'Compute', {
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
    }),
  );
}

/** The environment entry `name` on container `container`, or undefined. */
function envValue(template: Template, container: string, name: string): unknown {
  const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
  for (const res of Object.values(taskDefs)) {
    const containers = (res as { Properties: { ContainerDefinitions: unknown[] } }).Properties
      .ContainerDefinitions as { Name: string; Environment?: { Name: string; Value: unknown }[] }[];
    for (const c of containers) {
      if (c.Name !== container) continue;
      const entry = (c.Environment ?? []).find((e) => e.Name === name);
      if (entry) return entry.Value;
    }
  }
  return undefined;
}

describe.each<EnvName>(['dev', 'prod'])('provider-facing origin (%s)', (envName) => {
  const template = buildCompute(envName);

  test.each(['data-planeContainer', 'control-planeContainer', 'SweepContainer'])(
    '%s carries LUCIEL_PUBLIC_API_BASE_URL = the environment API base URL',
    (container) => {
      expect(envValue(template, container, 'LUCIEL_PUBLIC_API_BASE_URL')).toBe(
        baseUrls(envName).api,
      );
    },
  );

  test('the SES SNS subscriptions are registered against an origin the environment serves (the one that does not move)', () => {
    // SNS replaces a subscription whose endpoint changes, and the routes confirm only in
    // live mode (Phase 3 infra review): the public api name while this environment
    // serves it, its own name otherwise - always one of LUCIEL_API_ORIGINS.
    const subscriptions = Object.values(template.findResources('AWS::SNS::Subscription')).map(
      (s) => (s as { Properties: { Endpoint: string } }).Properties.Endpoint,
    );
    expect(subscriptions.length).toBeGreaterThan(0);
    const served = String(envValue(template, 'data-planeContainer', 'LUCIEL_API_ORIGINS')).split(',');
    const stable = served[served.length - 1];
    for (const endpoint of subscriptions) {
      expect(endpoint.startsWith(`${stable}/`)).toBe(true);
    }
    if (envName === 'dev') expect(stable).toBe('https://api.vantagemind.ai');
  });
});

describe.each<EnvName>(['dev', 'prod'])('API origins the environment serves (%s)', (envName) => {
  const template = buildCompute(envName);

  test.each(['data-planeContainer', 'control-planeContainer', 'SweepContainer'])(
    '%s carries LUCIEL_API_ORIGINS = every API hostname, so a redirect URI registered on any of them is valid',
    (container) => {
      const expected = hostnames(envName).apiNames.map((n) => `https://${n}`).join(',');
      expect(envValue(template, container, 'LUCIEL_API_ORIGINS')).toBe(expected);
      expect(expected.split(',')).toContain(baseUrls(envName).api);
    },
  );
});

describe('scripts/merge_platform_secret.py judges redirect URIs against the origins this helper serves', () => {
  // The pre-deploy --check carries a hand-written ENV_API_ORIGINS table (no Node
  // toolchain at the owner's keyboard). It must say exactly what hostnames() says, or
  // the check diverges from the deployed task again at the D6 cutover.
  const source = readFileSync(join(__dirname, '..', '..', 'scripts', 'merge_platform_secret.py'), 'utf8');
  const table = source.slice(source.indexOf('ENV_API_ORIGINS: dict'), source.indexOf('}', source.indexOf('ENV_API_ORIGINS: dict')));

  test.each<EnvName>(['dev', 'preprod', 'prod'])('%s', (envName) => {
    const line = table.split('\n').find((l) => l.trim().startsWith(`"${envName}":`));
    expect(line).toBeDefined();
    const listed = Array.from(line!.matchAll(/"(https:\/\/[^"]+)"/g)).map((m) => m[1]);
    expect(listed).toEqual(hostnames(envName).apiNames.map((n) => `https://${n}`));
    expect(listed[0]).toBe(baseUrls(envName).api);
  });
});

describe.each<EnvName>(['dev', 'prod'])('frontend runtime config (%s)', (envName) => {
  const template = buildCompute(envName);

  test.each(['data-planeContainer', 'control-planeContainer'])(
    '%s carries the widget embed URL GET /api/v1/public/config serves: the PUBLIC host while the environment serves it',
    (container) => {
      // widget-stack.ts (D6): the customer-facing snippet names the public host until
      // a production environment claims it — a pasted dev-only host would keep
      // loading dev's bundle after the cutover (Phase 3 infra review).
      const embeds = hostnames(envName).embedNames;
      expect(envValue(template, container, 'LUCIEL_WIDGET_EMBED_URL')).toBe(
        `https://${embeds[embeds.length - 1]}/v1/luciel.js`,
      );
      if (envName === 'dev') expect(embeds[embeds.length - 1]).toBe('embed.vantagemind.ai');
    },
  );

  test('GET /api/v1/public/config is forwarded to the control plane', () => {
    // The route is mounted under app/main.py CONTROL_PLANE_PREFIXES; without its own
    // pattern the ALB default (the data plane) would answer it.
    expect(CONTROL_PLANE_PATH_PATTERNS.flat()).toContain('/api/v1/public/*');
    const rules = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::ListenerRule'));
    const publicRules = rules.filter((r) =>
      JSON.stringify((r as { Properties: unknown }).Properties).includes('/api/v1/public/*'),
    );
    expect(publicRules.length).toBeGreaterThanOrEqual(1);
  });
});
