#!/usr/bin/env bash
# Run the one-off §10 SEED + LIVE-BEHAVIOR TEST ECS task and surface its result
# (mirrors run-verify-task.sh). Reuses the data-plane service's proven network
# config so the task can reach the private API + RDS from inside the VPC.
#
#   scripts/run-seedtest-task.sh dev                 # strict single run (T1-T7)
#   scripts/run-seedtest-task.sh dev --matrix        # the configuration matrix (M1-M8)
#   scripts/run-seedtest-task.sh dev --matrix --only M3,M8
#   scripts/run-seedtest-task.sh dev --persist e2e-walk   # keep the walk tenant walkable
#   scripts/run-seedtest-task.sh dev --no-strict     # the same, any skip tolerated
#
# Every argument after the environment is handed to scripts/seed_and_livetest.py as the
# container command (an ECS container override). --strict is added unless --strict or
# --no-strict is already given (round 7 WP-12): a [SKIP] outside the owner-gated
# allowlist fails the run instead of reading as PASS. The owner-gated flags
# LUCIEL_SEEDTEST_LIVE_CAP / LUCIEL_SEEDTEST_WEBHOOK_URL / LUCIEL_SEEDTEST_WEBHOOK_AUTH
# and LUCIEL_SEEDTEST_KEEP ride along as container environment when they are set in the
# caller's environment (nothing could reach the task with them before). Note: container
# overrides are visible to anyone who can ecs:DescribeTasks in the account.
#
set -euo pipefail
# Git Bash on Windows: keep /luciel/<env>/seedtest a log-group name, not a file path.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
ENVIRONMENT="${1:-dev}"
if [ "$#" -gt 0 ]; then shift; fi
REGION="${AWS_REGION:-ca-central-1}"
PREFIX="Luciel-${ENVIRONMENT}"

overrides=$(python3 - "$@" <<'PY_OVERRIDES'
"""The ECS run-task --overrides JSON for the SeedTest container (argv = harness flags)."""
import json
import os
import sys

PASSTHROUGH_ENV = (
    "LUCIEL_SEEDTEST_LIVE_CAP",
    "LUCIEL_SEEDTEST_WEBHOOK_URL",
    "LUCIEL_SEEDTEST_WEBHOOK_AUTH",
    "LUCIEL_SEEDTEST_KEEP",
)
args = sys.argv[1:]
if "--strict" not in args and "--no-strict" not in args:
    args = ["--strict", *args]
container = {
    "name": "SeedTestContainer",
    "command": ["python3", "scripts/seed_and_livetest.py", *args],
}
env = [{"name": k, "value": os.environ[k]} for k in PASSTHROUGH_ENV if os.environ.get(k)]
if env:
    container["environment"] = env
print(json.dumps({"containerOverrides": [container]}))
PY_OVERRIDES
)

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

echo "[seedtest] container command override: $overrides"
arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" \
  --task-definition "$taskdef" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sgs],assignPublicIp=DISABLED}" \
  --overrides "$overrides" \
  --query 'tasks[0].taskArn' --output text)
echo "[seedtest] started $arn ; waiting..."
# `aws ecs wait tasks-stopped` gives up after 10 minutes (100 polls x 6 s) and set -e
# turned that give-up into a failed probe. A matrix run outlives it: wait in rounds
# until the task stops or SEEDTEST_MAX_WAIT_MINUTES (default 90) has passed.
max_wait_minutes="${SEEDTEST_MAX_WAIT_MINUTES:-90}"
deadline=$(( $(date +%s) + max_wait_minutes * 60 ))
until aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn" 2>/dev/null; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "::error::seedtest task still running after ${max_wait_minutes} min: $arn"
    exit 1
  fi
  echo "[seedtest] still running ($(date -u +%H:%M:%SZ)) ..."
  sleep 5
done

code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].containers[0].exitCode' --output text)
echo "[seedtest] exit code: $code"

# Print the harness output from CloudWatch. The group is the one ComputeStack
# declares — /luciel/<env>/seedtest (2026-09-05 audit F174: the old lookup searched
# for 'SeedTest' case-sensitively, matched nothing, and always printed
# "(logs not yet available)" — the harness output never reached the workflow log).
taskid="${arn##*/}"
log_group="/luciel/${ENVIRONMENT}/seedtest"
stream="luciel-seedtest/SeedTestContainer/${taskid}"
echo "[seedtest] task definition: $taskdef"
echo "[seedtest] log group: $log_group  stream: $stream"
# CloudWatch ingests a few seconds behind the container's exit, and the harness's LAST
# line is its one-line SUMMARY: fetch (filter-log-events auto-paginates; get-log-events
# returned one page of a long matrix log) until that line is in, bounded, then render
# once — every message on its own line, the SUMMARY again at the end, and the same
# SUMMARY plus every [FAIL]/[SKIP] into the GitHub job summary when there is one.
logs='{"events": []}'
for _ in 1 2 3 4 5 6; do
  sleep 5
  logs=$(aws logs filter-log-events --region "$REGION" --log-group-name "$log_group" \
    --log-stream-names "$stream" --output json 2>/dev/null || echo '{"events": []}')
  case "$logs" in *"[seedtest] SUMMARY"*) break ;; esac
done
report_py=$(cat <<'PY_REPORT'
"""Render the harness log (stdin: filter-log-events JSON); argv = exit code, environment."""
import json
import os
import sys

PREFIX = "[seedtest] SUMMARY"
code, environment = sys.argv[1], sys.argv[2]
try:
    events = json.loads(sys.stdin.read() or "{}").get("events") or []
except ValueError:
    events = []
lines = [str(e.get("message", "")).rstrip("\r\n") for e in events if isinstance(e, dict)]
for line in lines:
    print(line)
if not lines:
    print("(no harness output in CloudWatch for this task yet)")
summary = next((line for line in reversed(lines) if line.startswith(PREFIX)), None)
if summary is None:
    summary = f"{PREFIX} missing: the harness did not finish (task exit code {code})"
print(summary)
target = os.environ.get("GITHUB_STEP_SUMMARY")
if target:
    notable = [line for line in lines if line.startswith(("[FAIL]", "[SKIP]", "::error::"))]
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(f"### SeedTest ({environment})\n\n`{summary}`\n\nTask exit code: `{code}`\n\n")
        for line in notable:
            fh.write("- " + line[:400].replace("`", "'") + "\n")
        fh.write("\n")
PY_REPORT
)
printf '%s' "$logs" | python3 -c "$report_py" "$code" "$ENVIRONMENT"

[ "$code" = "0" ] || { echo "::error::seedtest task FAILED (exit $code)"; exit 1; }
echo "[seedtest] §10 live-behavior probe PASSED."
