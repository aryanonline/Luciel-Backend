#!/usr/bin/env bash
# Run the one-off DB migration ECS task and wait for it (DEVOPS §5.3/§5.9).
# Reuses the EXACT network config (subnets + security group) of the running
# data-plane service, so the migration task has the same SM/NAT reachability the
# app already proved. Hand-rolling subnet/SG discovery was unreliable (tasks
# timed out reaching Secrets Manager); copying the proven config is robust.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
REGION="${AWS_REGION:-ca-central-1}"
PREFIX="Luciel-${ENVIRONMENT}"

cluster=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-ClusterName'].Value" --output text)
taskdef=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-MigrationTaskArn'].Value" --output text)

# Pull subnets + SG from a running service in the cluster (the data plane).
svc=$(aws ecs list-services --region "$REGION" --cluster "$cluster" \
  --query 'serviceArns[0]' --output text)
netcfg=$(aws ecs describe-services --region "$REGION" --cluster "$cluster" --services "$svc" \
  --query 'services[0].networkConfiguration.awsvpcConfiguration' --output json)
subnets=$(echo "$netcfg" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['subnets']))")
sgs=$(echo "$netcfg" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['securityGroups']))")

echo "[run-migration] cluster=$cluster taskdef=$taskdef"
echo "[run-migration] subnets=$subnets sg=$sgs"
arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" \
  --task-definition "$taskdef" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sgs],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
echo "[run-migration] started $arn ; waiting..."
aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"

code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].containers[0].exitCode' --output text)
reason=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].stoppedReason' --output text)
echo "[run-migration] exit code: $code ; reason: $reason"
[ "$code" = "0" ] || { echo "::error::migration task failed (exit $code; $reason)"; exit 1; }
