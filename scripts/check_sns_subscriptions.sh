#!/usr/bin/env bash
# Prove that each SES topic of an environment still has a CONFIRMED https subscriber
# after a deploy (round 7, Phase 3 infra review).
#
# SNS REPLACES a subscription whose endpoint changes: CloudFormation creates a new
# PendingConfirmation subscription and deletes the confirmed one in the stack's
# cleanup. The SES routes confirm only in live mode (channels.py), and nothing else
# retries. A topic left with no confirmed subscriber drops every customer reply to a
# *.reply.vantagemind.ai address and every bounce/complaint event - silently. This
# step makes that state a red run instead.
#
#   bash scripts/check_sns_subscriptions.sh <dev|preprod|prod>
#
# Topic names are the CDK's (`luciel-<env>-ses-events`, `luciel-<env>-ses-inbound-v2`);
# no hostname is written here.
set -euo pipefail

ENVIRONMENT="${1:?environment name (dev|preprod|prod)}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ca-central-1}}"

status=0
for topic in "luciel-${ENVIRONMENT}-ses-events" "luciel-${ENVIRONMENT}-ses-inbound-v2"; do
  arn=$(aws sns list-topics --region "$REGION" \
    --query "Topics[?ends_with(TopicArn, ':${topic}')].TopicArn | [0]" --output text)
  if [ -z "$arn" ] || [ "$arn" = "None" ]; then
    echo "::error::[sns] topic ${topic} not found in ${REGION}"
    status=1
    continue
  fi
  confirmed=$(aws sns list-subscriptions-by-topic --region "$REGION" --topic-arn "$arn" \
    --query "length(Subscriptions[?Protocol=='https' && SubscriptionArn!='PendingConfirmation'])" \
    --output text)
  pending=$(aws sns list-subscriptions-by-topic --region "$REGION" --topic-arn "$arn" \
    --query "length(Subscriptions[?SubscriptionArn=='PendingConfirmation'])" --output text)
  if [ "$confirmed" = "0" ]; then
    echo "::error::[sns] ${topic} has NO confirmed https subscriber (${pending} pending) - inbound mail and bounce events are being dropped; confirm the subscription or re-subscribe"
    status=1
  else
    echo "[sns] ${topic}: ${confirmed} confirmed https subscriber(s), ${pending} pending"
  fi
done
exit $status
