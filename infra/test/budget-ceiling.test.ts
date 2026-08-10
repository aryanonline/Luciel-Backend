/**
 * Monthly cost ceiling (AWS Budgets). Every other cost test in this suite bounds
 * one resource; this is the account-level backstop that catches what they miss —
 * above all a failed preprod teardown quietly running a prod-shaped stack. Pin
 * that each environment declares a ceiling and that the alert thresholds exist.
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { BudgetStack } from '../lib/budget-stack';
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
});
