#!/usr/bin/env bash
# Run the one-off §10 VERIFY ECS task and surface its result (mirrors
# run-migration-task.sh). Reuses the data-plane service's proven network config.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
REGION="${AWS_REGION:-ca-central-1}"
PREFIX="Luciel-${ENVIRONMENT}"

cluster=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-ClusterName'].Value" --output text)
taskdef=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-VerifyTaskArn'].Value" --output text)

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
echo "[verify] started $arn ; waiting..."
aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"

code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].containers[0].exitCode' --output text)
echo "[verify] exit code: $code"

# Print the probe output from CloudWatch (luciel-verify stream).
taskid="${arn##*/}"
sleep 5
aws logs get-log-events --region "$REGION" \
  --log-group-name "$(aws logs describe-log-groups --region "$REGION" \
     --query "logGroups[?contains(logGroupName,'Verify')].logGroupName | [0]" --output text)" \
  --log-stream-name "luciel-verify/VerifyContainer/${taskid}" \
  --query 'events[].message' --output text 2>/dev/null || echo "(logs not yet available)"

[ "$code" = "0" ] || { echo "::error::verify task FAILED (exit $code)"; exit 1; }
echo "[verify] §10 live probe PASSED."
