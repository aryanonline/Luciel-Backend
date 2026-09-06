#!/usr/bin/env bash
# Run the one-off §10 SEED + LIVE-BEHAVIOR TEST ECS task and surface its result
# (mirrors run-verify-task.sh). Reuses the data-plane service's proven network
# config so the task can reach the private API + RDS from inside the VPC.
#
#   scripts/run-seedtest-task.sh dev
#
set -euo pipefail
ENVIRONMENT="${1:-dev}"
REGION="${AWS_REGION:-ca-central-1}"
PREFIX="Luciel-${ENVIRONMENT}"

cluster=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-ClusterName'].Value" --output text)
taskdef=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-SeedTestTaskArn'].Value" --output text)

svc=$(aws ecs list-services --region "$REGION" --cluster "$cluster" \
  --query 'serviceArns[0]' --output text)
netcfg=$(aws ecs describe-services --region "$REGION" --cluster "$cluster" --services "$svc" \
  --query 'services[0].networkConfiguration.awsvpcConfiguration' --output json)
subnets=$(echo "$netcfg" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['subnets']))")
sgs=$(echo "$netcfg" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['securityGroups']))")

arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" \
  --task-definition "$taskdef" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sgs],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
echo "[seedtest] started $arn ; waiting..."
aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"

code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].containers[0].exitCode' --output text)
echo "[seedtest] exit code: $code"

# Print the harness output from CloudWatch. The group is the one ComputeStack
# declares — /luciel/<env>/seedtest (2026-09-05 audit F174: the old lookup searched
# for 'SeedTest' case-sensitively, matched nothing, and always printed
# "(logs not yet available)" — the harness output never reached the workflow log).
taskid="${arn##*/}"
log_group="/luciel/${ENVIRONMENT}/seedtest"
echo "[seedtest] task definition: $taskdef"
echo "[seedtest] log group: $log_group  stream: luciel-seedtest/SeedTestContainer/${taskid}"
sleep 5
aws logs get-log-events --region "$REGION" \
  --log-group-name "$log_group" \
  --log-stream-name "luciel-seedtest/SeedTestContainer/${taskid}" \
  --query 'events[].message' --output text 2>/dev/null || echo "(logs not yet available for $log_group)"

[ "$code" = "0" ] || { echo "::error::seedtest task FAILED (exit $code)"; exit 1; }
echo "[seedtest] §10 live-behavior probe PASSED."
