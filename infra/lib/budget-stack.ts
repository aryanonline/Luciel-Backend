/**
 * Cost ceiling alarm — AWS Budgets (cost hygiene, Architecture §4.1 "cost ceilings").
 *
 * Every other cost control in this app bounds ONE resource (log retention, S3
 * lifecycle, ECR expiry, Redis snapshots). This is the control that catches
 * everything those miss — an unnoticed always-on resource, a runaway NAT bill,
 * and above all a FAILED preprod teardown quietly running a prod-shaped stack
 * for a month. Three alerts per environment:
 *
 *   1. 80% of the monthly ceiling, ACTUAL      — early warning.
 *   2. 100% of the monthly ceiling, ACTUAL     — the ceiling is breached.
 *   3. 100% of the monthly ceiling, FORECASTED — trending to breach before it does.
 *
 * Notifications go to the alert email supplied at deploy time via the
 * `-c budgetAlertEmail=...` context (never hardcoded in the repo). When no email
 * is supplied, the stack still synthesizes the budget with no subscribers rather
 * than failing — the ceiling itself remains visible in Cost Explorer/Budgets.
 * AWS Budgets is a global (us-east-1-backed) service but the resource is declared
 * in-region like everything else; it carries no data-residency surface (billing
 * metadata only, no customer data).
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import { Construct } from 'constructs';
import { EnvConfig } from './env-config';

export interface BudgetStackProps extends StackProps {
  readonly envConfig: EnvConfig;
  /** Alert recipient (deploy-time context; optional so synth never blocks). */
  readonly alertEmail?: string;
}

export class BudgetStack extends Stack {
  constructor(scope: Construct, id: string, props: BudgetStackProps) {
    super(scope, id, props);
    const { envConfig, alertEmail } = props;

    const subscribers = alertEmail
      ? [{ subscriptionType: 'EMAIL', address: alertEmail }]
      : [];

    const notifications: budgets.CfnBudget.NotificationWithSubscribersProperty[] = [
      {
        notification: {
          notificationType: 'ACTUAL',
          comparisonOperator: 'GREATER_THAN',
          threshold: 80,
          thresholdType: 'PERCENTAGE',
        },
        subscribers,
      },
      {
        notification: {
          notificationType: 'ACTUAL',
          comparisonOperator: 'GREATER_THAN',
          threshold: 100,
          thresholdType: 'PERCENTAGE',
        },
        subscribers,
      },
      {
        notification: {
          notificationType: 'FORECASTED',
          comparisonOperator: 'GREATER_THAN',
          threshold: 100,
          thresholdType: 'PERCENTAGE',
        },
        subscribers,
      },
    ];

    new budgets.CfnBudget(this, 'MonthlyCostCeiling', {
      budget: {
        budgetName: `luciel-${envConfig.envName}-monthly-cost-ceiling`,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: envConfig.monthlyBudgetUsd, unit: 'USD' },
      },
      // AWS Budgets requires at least one subscriber per notification; with no
      // alert email the budget is declared without notifications (still visible
      // in the console) rather than failing synth.
      notificationsWithSubscribers: alertEmail ? notifications : undefined,
    });
  }
}
