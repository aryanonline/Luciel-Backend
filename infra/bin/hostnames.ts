#!/usr/bin/env node
/**
 * Print an environment's hostnames (env-config.ts `hostnames`, owner decision D6) for
 * tooling that is not CDK: deploy.yml derives its smoke-probe URL from it, and an
 * operator can see what an environment answers on before it is deployed.
 *
 *   npx ts-node bin/hostnames.ts <dev|preprod|prod>            -> JSON
 *   npx ts-node bin/hostnames.ts <env> <field>                 -> one value
 *
 * Fields: app, api, embed, appBaseUrl, apiBaseUrl, aliases, appNames, apiNames,
 * embedNames (lists print space-separated), ownsPublicApex, servesUnownedPublicNames,
 * stripeIntegrationPosture (the smoke probe's expected Stripe binding).
 * Exits non-zero on an unknown environment or field — a workflow must never fall
 * back to a guessed hostname.
 */
import {
  EnvName,
  apexOwner,
  baseUrls,
  getEnvConfig,
  hostnames,
  servesUnownedPublicNames,
} from '../lib/env-config';

const ENVS: readonly EnvName[] = ['dev', 'preprod', 'prod'];

function main(argv: string[]): number {
  const [envArg, field] = argv;
  if (!envArg || !ENVS.includes(envArg as EnvName)) {
    process.stderr.write(`usage: hostnames.ts <${ENVS.join('|')}> [field]\n`);
    return 2;
  }
  const envName = envArg as EnvName;
  // The same refusal the CDK app makes: a config with two owners (or none) has no
  // trustworthy answer to print.
  apexOwner();
  const names = hostnames(envName);
  const urls = baseUrls(envName);
  const facts: Record<string, string | boolean | readonly string[]> = {
    envName,
    ...names,
    appBaseUrl: urls.app,
    apiBaseUrl: urls.api,
    ownsPublicApex: getEnvConfig(envName).ownsPublicApex,
    servesUnownedPublicNames: servesUnownedPublicNames(envName),
    stripeIntegrationPosture: getEnvConfig(envName).stripeIntegrationPosture,
  };
  if (field === undefined) {
    process.stdout.write(`${JSON.stringify(facts, null, 2)}\n`);
    return 0;
  }
  if (!(field in facts)) {
    process.stderr.write(`unknown field '${field}'; one of: ${Object.keys(facts).join(', ')}\n`);
    return 2;
  }
  const value = facts[field];
  process.stdout.write(`${Array.isArray(value) ? value.join(' ') : String(value)}\n`);
  return 0;
}

process.exitCode = main(process.argv.slice(2));
