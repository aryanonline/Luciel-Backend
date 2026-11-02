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

# Print the probe output from CloudWatch. The group is the one ComputeStack
# declares — /luciel/<env>/verify (round 7 WP-8: the old lookup searched for
# 'Verify' case-sensitively, matched nothing, and always printed "(logs not yet
# available)" — the same defect run-seedtest-task.sh fixed under F174, so the
# §10 probe's own output never reached the workflow log).
taskid="${arn##*/}"
log_group="/luciel/${ENVIRONMENT}/verify"
echo "[verify] task definition: $taskdef"
echo "[verify] log group: $log_group  stream: luciel-verify/VerifyContainer/${taskid}"
sleep 5
# The read's error is kept (as run-seedtest-task.sh keeps it): a stream that does not
# exist yet is expected, but a refused read must say so, or the probe's output goes
# missing with no reason given. Either way this only reports: the verdict below is the
# task's exit code.
log_err_file=$(mktemp)
if ! aws logs get-log-events --region "$REGION" \
  --log-group-name "$log_group" \
  --log-stream-name "luciel-verify/VerifyContainer/${taskid}" \
  --query 'events[].message' --output text 2>"$log_err_file"; then
  log_err=$(tr '\r\n' '  ' <"$log_err_file" | cut -c1-400)
  case "$log_err" in
    *ResourceNotFoundException*) echo "(logs not yet available for $log_group)" ;;
    *) echo "::warning::[verify] cannot read the probe log: ${log_err}" ;;
  esac
fi
rm -f "$log_err_file"

[ "$code" = "0" ] || { echo "::error::verify task FAILED (exit $code)"; exit 1; }
echo "[verify] §10 live probe PASSED."
