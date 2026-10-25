/**
 * Per-environment hostnames (owner decision D6) on one ALB per environment (D7) —
 * round 7 WP-8b.
 *
 * Before this, one flat DOMAINS const was read unqualified by every stack, so dev,
 * preprod and prod all claimed api.vantagemind.ai (the ephemeral preprod could not
 * even create its records while dev held them), the web tier ran a second ALB, and
 * the per-environment origins (SeedTest, the smoke probe, APP_BASE_URL) could only
 * ever name the public host. These tests pin the D6 naming, the one-owner rule for
 * the public names, the certificates, the host-based listener rules and the records.
 */
import { Template, Match } from 'aws-cdk-lib/assertions';
import { App, Stack } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import {
  CONTROL_PLANE_PATH_PATTERNS,
  LISTENER_RULE_PRIORITY,
  MAX_CONDITION_VALUES_PER_RULE,
  controlPlaneRuleGroups,
} from '../lib/compute-stack';
import { WidgetCertStack, WidgetStack, EMBED_CERT_DOMAIN } from '../lib/widget-stack';
import {
  CLOUDFRONT_CERT_REGION,
  EnvName,
  PUBLIC_HOSTNAMES,
  apexOwner,
  baseUrls,
  getEnvConfig,
  hostnames,
  hostnamesFor,
  recordIdFor,
  servesUnownedPublicNames,
} from '../lib/env-config';
import { ENV, buildComputeAndWeb } from './support/stacks';

const ENVS: EnvName[] = ['dev', 'preprod', 'prod'];
type Res = { Type: string; Properties: Record<string, any>; DependsOn?: string[] };

function synth(envName: EnvName) {
  const built = buildComputeAndWeb(envName, { publicDns: true });
  return {
    compute: Template.fromStack(built.compute),
    web: Template.fromStack(built.web),
  };
}

function rules(t: Template): Res[] {
  return Object.values(t.findResources('AWS::ElasticLoadBalancingV2::ListenerRule')) as Res[];
}

function hostValues(rule: Res): string[] {
  return (rule.Properties.Conditions ?? []).flatMap((c: any) => c.HostHeaderConfig?.Values ?? []);
}

function pathValues(rule: Res): string[] {
  return (rule.Properties.Conditions ?? []).flatMap((c: any) => c.PathPatternConfig?.Values ?? []);
}

function recordNames(t: Template, type: 'A' | 'AAAA'): string[] {
  return (Object.values(t.findResources('AWS::Route53::RecordSet')) as Res[])
    .filter((r) => r.Properties.Type === type)
    .map((r) => r.Properties.Name)
    .sort();
}

describe('hostnames(envName) — the D6 naming', () => {
  test('dev answers on its own names AND, while it owns them, the public names (dual-host)', () => {
    expect(hostnames('dev')).toEqual({
      app: 'dev.vantagemind.ai',
      api: 'api-dev.vantagemind.ai',
      embed: 'embed-dev.vantagemind.ai',
      aliases: ['vantagemind.ai', 'www.vantagemind.ai', 'api.vantagemind.ai', 'embed.vantagemind.ai'],
      appNames: ['dev.vantagemind.ai', 'vantagemind.ai', 'www.vantagemind.ai'],
      apiNames: ['api-dev.vantagemind.ai', 'api.vantagemind.ai'],
      embedNames: ['embed-dev.vantagemind.ai', 'embed.vantagemind.ai'],
    });
  });

  test('preprod rehearses on its own names and never touches the public ones', () => {
    const h = hostnames('preprod');
    expect([h.app, h.api, h.embed]).toEqual([
      'preprod.vantagemind.ai',
      'api-preprod.vantagemind.ai',
      'embed-preprod.vantagemind.ai',
    ]);
    expect(h.aliases).toEqual([]);
  });

  test("prod's names are the public names (apex + www / api / embed)", () => {
    const h = hostnames('prod');
    expect([h.app, h.api, h.embed]).toEqual([
      PUBLIC_HOSTNAMES.app,
      PUBLIC_HOSTNAMES.api,
      PUBLIC_HOSTNAMES.embed,
    ]);
    expect(h.appNames).toEqual(['vantagemind.ai', 'www.vantagemind.ai']);
    expect(h.aliases).toEqual(['www.vantagemind.ai']);
  });

  test('after the cutover dev keeps only its own names (the shape the flag flip produces)', () => {
    const h = hostnamesFor('dev', false);
    expect(h.aliases).toEqual([]);
    expect(h.appNames).toEqual(['dev.vantagemind.ai']);
    expect(h.apiNames).toEqual(['api-dev.vantagemind.ai']);
    expect(h.embedNames).toEqual(['embed-dev.vantagemind.ai']);
  });

  test('no two environments share a primary name', () => {
    const primaries = ENVS.flatMap((e) => {
      const h = hostnames(e);
      return [h.app, h.api, h.embed];
    });
    expect(new Set(primaries).size).toBe(primaries.length);
  });

  test('the base URLs are https origins of the primary names', () => {
    expect(baseUrls('dev')).toEqual({
      app: 'https://dev.vantagemind.ai',
      api: 'https://api-dev.vantagemind.ai',
    });
    expect(baseUrls('prod')).toEqual({
      app: 'https://vantagemind.ai',
      api: 'https://api.vantagemind.ai',
    });
  });

  test('record ids keep the deployed public records in place and derive the rest', () => {
    expect(recordIdFor('vantagemind.ai')).toBe('Apex');
    expect(recordIdFor('www.vantagemind.ai')).toBe('Www');
    expect(recordIdFor('api.vantagemind.ai')).toBe('Api');
    expect(recordIdFor('embed.vantagemind.ai')).toBe('Widget');
    expect(recordIdFor('api-dev.vantagemind.ai')).toBe('ApiDev');
    expect(recordIdFor('embed-preprod.vantagemind.ai')).toBe('EmbedPreprod');
    expect(() => recordIdFor('example.com')).toThrow(/not under vantagemind.ai/);
  });
});

describe('exactly ONE environment owns the public apex (D6)', () => {
  test('the config names exactly one owner, and it is dev until prod claims the names', () => {
    const owners = ENVS.filter((e) => getEnvConfig(e).ownsPublicApex);
    expect(owners).toEqual(['dev']);
    expect(apexOwner()).toBe('dev');
  });

  test('the ephemeral preprod may never own them', () => {
    expect(getEnvConfig('preprod').ownsPublicApex).toBe(false);
  });

  test('prod serves names it does not own yet — the case deploy.yml refuses', () => {
    expect(servesUnownedPublicNames('prod')).toBe(true);
    expect(servesUnownedPublicNames('dev')).toBe(false);
    expect(servesUnownedPublicNames('preprod')).toBe(false);
  });
});

describe('certificates cover every name the environment answers on', () => {
  test.each(ENVS)('%s: the ONE regional certificate names every api and app host', (envName) => {
    const { compute, web } = synth(envName);
    const h = hostnames(envName);
    compute.resourceCountIs('AWS::CertificateManager::Certificate', 1);
    web.resourceCountIs('AWS::CertificateManager::Certificate', 0);
    const [cert] = Object.values(compute.findResources('AWS::CertificateManager::Certificate')) as Res[];
    const covered = [cert.Properties.DomainName, ...(cert.Properties.SubjectAlternativeNames ?? [])];
    expect(covered[0]).toBe(h.api);
    expect([...covered].sort()).toEqual([...h.apiNames, ...h.appNames].sort());
    expect(cert.Properties.ValidationMethod).toBe('DNS');
    // The HTTPS listener presents it.
    compute.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 443,
      Certificates: [{ CertificateArn: Match.anyValue() }],
    });
  });

  test('the CloudFront certificate covers every embed host of every environment', () => {
    const app = new App();
    const t = Template.fromStack(
      new WidgetCertStack(app, 'WidgetCert', {
        env: { account: ENV.account, region: CLOUDFRONT_CERT_REGION },
        envConfig: getEnvConfig('prod'),
      }),
    );
    t.resourceCountIs('AWS::CertificateManager::Certificate', 1);
    t.hasResourceProperties('AWS::CertificateManager::Certificate', { DomainName: EMBED_CERT_DOMAIN });
    const wildcard = EMBED_CERT_DOMAIN.replace('*.', '');
    for (const e of ENVS) {
      for (const name of [...hostnames(e).embedNames, ...hostnamesFor(e, !getEnvConfig(e).ownsPublicApex).embedNames]) {
        // A single-label wildcard: <label>.vantagemind.ai, no deeper.
        expect(name.endsWith(`.${wildcard}`) && name.split('.').length === wildcard.split('.').length + 1).toBe(
          true,
        );
      }
    }
  });

  test("dev holds only the wildcard embed cert now - the retiring embed.vantagemind.ai cert is gone", () => {
    // Kept through the Phase 3 deploy (the distribution could not drop a cert still in
    // use); retired on 2026-10-25 once the distribution was confirmed on EmbedCert.
    const app = new App();
    const t = Template.fromStack(
      new WidgetCertStack(app, 'WidgetCert', {
        env: { account: ENV.account, region: CLOUDFRONT_CERT_REGION },
        envConfig: getEnvConfig('dev'),
      }),
    );
    t.resourceCountIs('AWS::CertificateManager::Certificate', 1);
    t.hasResourceProperties('AWS::CertificateManager::Certificate', { DomainName: '*.vantagemind.ai' });
  });
});

describe('one ALB per environment, routed by host (D7)', () => {
  test.each(ENVS)('%s: exactly one load balancer, and it is the Compute one', (envName) => {
    const { compute, web } = synth(envName);
    compute.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    web.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 0);
    web.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 0);
  });

  test.each(ENVS)('%s: the app hosts reach the web target group ahead of every api rule', (envName) => {
    const { compute, web } = synth(envName);
    const h = hostnames(envName);
    const [appRule] = rules(web);
    expect(rules(web)).toHaveLength(1);
    expect(hostValues(appRule)).toEqual(h.appNames);
    expect(appRule.Properties.Priority).toBe(LISTENER_RULE_PRIORITY.appHost);
    const webTg = Object.keys(web.findResources('AWS::ElasticLoadBalancingV2::TargetGroup'));
    expect(webTg).toHaveLength(1);
    expect(appRule.Properties.Actions[0]).toEqual(
      expect.objectContaining({ Type: 'forward', TargetGroupArn: { Ref: webTg[0] } }),
    );
    // Every api-host rule sits behind it.
    for (const r of rules(compute).filter((x) => hostValues(x).includes(h.api))) {
      expect(r.Properties.Priority).toBeGreaterThan(LISTENER_RULE_PRIORITY.appHost);
    }
    // The web target group keeps the SSR health check.
    web.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      Port: 3000,
      HealthCheckPath: '/',
      Matcher: { HttpCode: '200-399' },
    });
  });

  test.each(ENVS)(
    '%s: every api host routes the control-plane paths on BOTH listeners; data is its catch-all',
    (envName) => {
      const { compute } = synth(envName);
      const h = hostnames(envName);
      const listeners = Object.keys(compute.findResources('AWS::ElasticLoadBalancingV2::Listener'));
      expect(listeners).toHaveLength(2);
      for (const listener of listeners) {
        const onListener = rules(compute).filter((r) => r.Properties.ListenerArn.Ref === listener);
        const apiRules = onListener.filter((r) => hostValues(r).length && hostValues(r)[0] === h.api);
        for (const r of apiRules) expect(hostValues(r)).toEqual(h.apiNames);
        const control = apiRules.filter((r) => pathValues(r).length > 0);
        expect(control.flatMap(pathValues).sort()).toEqual(CONTROL_PLANE_PATH_PATTERNS.flat().slice().sort());
        const catchAll = apiRules.filter((r) => pathValues(r).length === 0);
        expect(catchAll).toHaveLength(1);
        expect(catchAll[0].Properties.Priority).toBe(LISTENER_RULE_PRIORITY.apiHost);
        for (const r of control) {
          expect(r.Properties.Priority).toBeGreaterThanOrEqual(LISTENER_RULE_PRIORITY.controlPlane);
          expect(r.Properties.Priority).toBeLessThan(LISTENER_RULE_PRIORITY.apiHost);
        }
      }
    },
  );

  test.each(ENVS)('%s: no rule exceeds the ALB quota of five condition values', (envName) => {
    const { compute, web } = synth(envName);
    for (const r of [...rules(compute), ...rules(web)]) {
      expect(hostValues(r).length + pathValues(r).length).toBeLessThanOrEqual(MAX_CONDITION_VALUES_PER_RULE);
    }
  });

  test('the control-plane regrouping is exact and refuses a host list that leaves no room', () => {
    for (const hosts of [1, 2, 3, 4]) {
      const groups = controlPlaneRuleGroups(hosts);
      expect(groups.flat()).toEqual(CONTROL_PLANE_PATH_PATTERNS.flat());
      for (const g of groups) expect(g.length + hosts).toBeLessThanOrEqual(MAX_CONDITION_VALUES_PER_RULE);
    }
    expect(() => controlPlaneRuleGroups(MAX_CONDITION_VALUES_PER_RULE)).toThrow(/no room/);
  });

  test('the listeners keep the data plane as the default action (an unnamed host is never dropped)', () => {
    const { compute } = synth('dev');
    for (const l of Object.values(compute.findResources('AWS::ElasticLoadBalancingV2::Listener')) as Res[]) {
      expect(l.Properties.DefaultActions[0].Type).toBe('forward');
    }
  });
});

describe('records: every name aliases to the right target', () => {
  test.each(ENVS)('%s: api names on Compute, app names on Web — A and AAAA, all to the one ALB', (envName) => {
    const { compute, web } = synth(envName);
    const h = hostnames(envName);
    const fqdn = (names: readonly string[]) => names.map((n) => `${n}.`).sort();
    expect(recordNames(compute, 'A')).toEqual(fqdn(h.apiNames));
    expect(recordNames(compute, 'AAAA')).toEqual(fqdn(h.apiNames));
    expect(recordNames(web, 'A')).toEqual(fqdn(h.appNames));
    expect(recordNames(web, 'AAAA')).toEqual(fqdn(h.appNames));
    // The web records alias the COMPUTE ALB (a cross-stack reference), never a local one.
    for (const r of Object.values(web.findResources('AWS::Route53::RecordSet')) as Res[]) {
      expect(JSON.stringify(r.Properties.AliasTarget.DNSName)).toMatch(/Compute|GetStackOutput/);
    }
  });

  test("the app records move only once the replacement web service is up behind the host rule", () => {
    const { web } = synth('dev');
    const service = Object.keys(web.findResources('AWS::ECS::Service'));
    const hostRule = Object.keys(web.findResources('AWS::ElasticLoadBalancingV2::ListenerRule'));
    expect(service).toHaveLength(1);
    expect(service[0]).toMatch(/^WebAppService/);
    for (const r of Object.values(web.findResources('AWS::Route53::RecordSet')) as Res[]) {
      expect(r.DependsOn).toEqual(expect.arrayContaining([service[0], hostRule[0]]));
    }
  });

  test('the public records keep their deployed logical ids (updated in place, never re-created)', () => {
    const { compute, web } = synth('dev');
    const ids = (t: Template) => Object.keys(t.findResources('AWS::Route53::RecordSet'));
    for (const prefix of ['ApiA', 'ApiAaaa']) expect(ids(compute).some((i) => i.startsWith(prefix))).toBe(true);
    for (const prefix of ['ApexA', 'ApexAaaa', 'WwwA']) expect(ids(web).some((i) => i.startsWith(prefix))).toBe(true);
  });

  test('dev widget: every embed host aliases to the distribution', () => {
    const app = new App();
    const holder = new Stack(app, 'Holder', { env: ENV });
    const cert = acm.Certificate.fromCertificateArn(
      holder,
      'Cert',
      `arn:aws:acm:${CLOUDFRONT_CERT_REGION}:111111111111:certificate/abc`,
    );
    const t = Template.fromStack(new WidgetStack(app, 'Widget', { env: ENV, envConfig: getEnvConfig('dev'), cert }));
    expect(recordNames(t, 'A')).toEqual(['embed-dev.vantagemind.ai.', 'embed.vantagemind.ai.']);
    expect(recordNames(t, 'AAAA')).toEqual(['embed-dev.vantagemind.ai.', 'embed.vantagemind.ai.']);
    expect(Object.keys(t.findResources('AWS::Route53::RecordSet')).some((i) => i.startsWith('WidgetA'))).toBe(true);
  });

  test('the SES webhook subscriptions wait for the api host to exist before SNS confirms them', () => {
    const { compute } = synth('dev');
    const apiRecords = Object.keys(compute.findResources('AWS::Route53::RecordSet'));
    for (const sub of Object.values(compute.findResources('AWS::SNS::Subscription')) as Res[]) {
      // The endpoint stays on the public api name while dev serves it (SNS would
      // replace a subscription whose endpoint moved - Phase 3 infra review); the
      // records it waits for include that name's own.
      expect(sub.Properties.Endpoint).toMatch(/^https:\/\/api\.vantagemind\.ai\//);
      expect(sub.DependsOn).toEqual(expect.arrayContaining(apiRecords));
    }
  });
});

describe('the per-environment origins reach the containers', () => {
  function containerEnv(t: Template, idPrefix: string): Record<string, string> {
    const [, td] = Object.entries(t.findResources('AWS::ECS::TaskDefinition')).find(([k]) =>
      k.startsWith(idPrefix),
    )!;
    const c = (td as Res).Properties.ContainerDefinitions[0];
    return Object.fromEntries(c.Environment.map((e: any) => [e.Name, e.Value]));
  }

  test.each(ENVS)('%s: app planes and sweeps carry APP_BASE_URL / API_BASE_URL / CORS origins', (envName) => {
    const { compute, web } = synth(envName);
    const urls = baseUrls(envName);
    const h = hostnames(envName);
    for (const plane of ['dataplaneTask', 'controlplaneTask']) {
      const env = containerEnv(compute, plane);
      expect(env.LUCIEL_APP_BASE_URL).toBe(urls.app);
      expect(env.LUCIEL_PUBLIC_API_BASE_URL).toBe(urls.api);
      expect(JSON.parse(env.LUCIEL_DASHBOARD_ORIGINS)).toEqual(h.appNames.map((n) => `https://${n}`));
    }
    const sweep = containerEnv(compute, 'SweepTask');
    expect(sweep.LUCIEL_APP_BASE_URL).toBe(urls.app);
    expect(sweep.LUCIEL_PUBLIC_API_BASE_URL).toBe(urls.api);
    expect(containerEnv(compute, 'SeedTestTask').LUCIEL_API_BASE).toBe(urls.api);
    expect(containerEnv(web, 'WebTask').NEXT_PUBLIC_API_BASE_URL).toBe(urls.api);
  });
});

describe('bin/hostnames.ts — the one source tooling outside CDK reads', () => {
  const { execFileSync, spawnSync } = require('node:child_process') as typeof import('node:child_process');
  const path = require('node:path') as typeof import('node:path');
  const infraRoot = path.resolve(__dirname, '..');
  const tsNode = require.resolve('ts-node/dist/bin.js');
  const run = (...args: string[]) =>
    spawnSync(process.execPath, [tsNode, '--prefer-ts-exts', 'bin/hostnames.ts', ...args], {
      cwd: infraRoot,
      encoding: 'utf-8',
    });

  test('prints the per-environment api origin deploy.yml probes', () => {
    const out = execFileSync(
      process.execPath,
      [tsNode, '--prefer-ts-exts', 'bin/hostnames.ts', 'dev', 'apiBaseUrl'],
      { cwd: infraRoot, encoding: 'utf-8' },
    );
    expect(out.trim()).toBe(baseUrls('dev').api);
    expect(run('prod', 'servesUnownedPublicNames').stdout.trim()).toBe('true');
  }, 60_000);

  test('refuses an unknown environment or field instead of guessing', () => {
    expect(run('qa').status).toBe(2);
    expect(run('dev', 'nope').status).toBe(2);
  }, 60_000);
});
