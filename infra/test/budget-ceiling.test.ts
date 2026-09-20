/**
 * Monthly cost ceiling (AWS Budgets). Every other cost test in this suite bounds
 * one resource; this is the account-level backstop that catches what they miss —
 * above all a failed preprod teardown quietly running a prod-shaped stack. Pin
 * that each environment declares a ceiling and that the alert thresholds exist.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { BudgetStack } from '../lib/budget-stack';
import { SecurityStack } from '../lib/security-stack';
import { applyCostAllocationTags, environmentCostFilter } from '../lib/cost-tags';
import { getEnvConfig, OFFERING_REGION, EnvName } from '../lib/env-config';

const ENV = { account: '111111111111', region: OFFERING_REGION };

function build(envName: EnvName, alertEmail?: string) {
  const app = new App();
  return Template.fromStack(
    new BudgetStack(app, 'Budget', { env: ENV, envConfig: getEnvConfig(envName), alertEmail }),
  );
}

describe('Monthly cost ceiling (AWS Budgets)', () => {
  test.each([['dev'], ['preprod'], ['prod']])(
    '%s declares a monthly USD ceiling',
    (envName) => {
      const cfg = getEnvConfig(envName as EnvName);
      expect(cfg.monthlyBudgetUsd).toBeGreaterThan(0);
      // No BudgetName assertion: the name is deliberately GENERATED. Budgets are
      // replaced when notifications change, and a replacement under a fixed name
      // deadlocks against itself (create-before-delete + "same name, different
      // internalId") — which a real deploy hit the day the alert email landed.
      build(envName as EnvName).hasResourceProperties('AWS::Budgets::Budget', {
        Budget: Match.objectLike({
          BudgetType: 'COST',
          TimeUnit: 'MONTHLY',
          BudgetLimit: { Amount: cfg.monthlyBudgetUsd, Unit: 'USD' },
        }),
      });
    },
  );

  test('with an alert email: 80% actual, 100% actual, 100% forecast all notify', () => {
    build('prod', 'owner@example.com').hasResourceProperties('AWS::Budgets::Budget', {
      NotificationsWithSubscribers: Match.arrayWith([
        Match.objectLike({
          Notification: Match.objectLike({ NotificationType: 'ACTUAL', Threshold: 80 }),
          Subscribers: [{ SubscriptionType: 'EMAIL', Address: 'owner@example.com' }],
        }),
        Match.objectLike({
          Notification: Match.objectLike({ NotificationType: 'ACTUAL', Threshold: 100 }),
        }),
        Match.objectLike({
          Notification: Match.objectLike({ NotificationType: 'FORECASTED', Threshold: 100 }),
        }),
      ]),
    });
  });

  test('without an alert email the budget still synthesizes (ceiling visible)', () => {
    build('dev').resourceCountIs('AWS::Budgets::Budget', 1);
  });

  test('dev ceiling clears the measured running run-rate (≈$195/mo) so 80% means "top of normal"', () => {
    // Cost Explorer 2026-09-01..13: ≈ $195/mo running incl. tax, ≈ $110 paused.
    // A ceiling at or below the running rate fires every month and trains
    // alert-blindness (the prod entry's reasoning, applied to dev).
    expect(getEnvConfig('dev').monthlyBudgetUsd).toBeGreaterThanOrEqual(250);
    expect(getEnvConfig('prod').monthlyBudgetUsd).toBe(1000);
  });
});

describe('Per-environment cost attribution (round 7 WP-8, D7)', () => {
  test.each([['dev'], ['preprod'], ['prod']])(
    "%s's budget filters on its own Environment tag, not the account total",
    (envName) => {
      const cfg = getEnvConfig(envName as EnvName);
      expect(environmentCostFilter(cfg)).toBe(`user:Environment$${envName}`);
      build(envName as EnvName).hasResourceProperties('AWS::Budgets::Budget', {
        Budget: Match.objectLike({
          CostFilters: { TagKeyValue: [`user:Environment$${envName}`] },
        }),
      });
    },
  );

  test('a tagged stack carries Environment + Component at the stack level and on its resources', () => {
    // The stack-level tags are what CloudFormation propagates to every taggable
    // resource, and the ones the budget filter matches on. Checked on a stack
    // with ordinary taggable resources (KMS key, secret) — the bin applies the
    // same call to every stack in the app.
    const app = new App();
    const cfg = getEnvConfig('prod');
    const sec = new SecurityStack(app, 'Luciel-prod-Security', { env: ENV, envConfig: cfg });
    applyCostAllocationTags(sec, cfg, 'Security');
    const t = Template.fromStack(sec);
    expect(sec.tags.tagValues()).toEqual({ Environment: 'prod', Component: 'Security' });
    t.hasResourceProperties('AWS::KMS::Key', {
      Tags: Match.arrayWith([
        { Key: 'Component', Value: 'Security' },
        { Key: 'Environment', Value: 'prod' },
      ]),
    });
  });
});
