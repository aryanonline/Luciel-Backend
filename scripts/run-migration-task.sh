#!/usr/bin/env bash
# Run the one-off DB migration ECS task and wait for it to finish (DEVOPS §5.3/§5.9).
# Invoked by the deploy pipeline AFTER the Compute stack is deployed. Pulls cluster,
# task def, subnets, and SG from the deployed CloudFormation stacks.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
REGION="${AWS_REGION:-ca-central-1}"
PREFIX="Luciel-${ENVIRONMENT}"

cluster=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-ClusterName'].Value" --output text)
taskdef=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-MigrationTaskArn'].Value" --output text)

# App-tier private subnets + SG from the networking/compute stacks.
subnets=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:aws-cdk:subnet-name,Values=app" "Name=vpc-id,Values=$(
    aws ec2 describe-vpcs --region "$REGION" --filters "Name=tag:Name,Values=${PREFIX}-Networking/Vpc" --query 'Vpcs[0].VpcId' --output text)" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')

echo "[run-migration] cluster=$cluster taskdef=$taskdef"
arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" \
  --task-definition "$taskdef" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
echo "[run-migration] started $arn ; waiting..."
aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"

code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].containers[0].exitCode' --output text)
echo "[run-migration] exit code: $code"
[ "$code" = "0" ] || { echo "::error::migration task failed (exit $code)"; exit 1; }
