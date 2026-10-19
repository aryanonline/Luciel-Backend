#!/usr/bin/env bash
# Migrate with the NEW image BEFORE the Compute stack rolls the services
# (round 7 migrations review, 2026-10-11).
#
#   scripts/migrate-before-roll.sh <env> <new-image-uri>
#
# WHY: the MigrationTask definition lives in the Compute stack and carries the image,
# so the only migration the pipeline used to run came AFTER `cdk deploy` had already
# rolled both planes onto the new image. A refused migration (0067 refuses on
# duplicate claims by design) then left the NEW code serving the OLD schema until the
# rollback put the services back. This script closes that window: it registers a
# revision of the CURRENT MigrationTask definition with only the image swapped for the
# release's gated tag (describe-task-definition -> swap -> register-task-definition),
# runs it through scripts/run-migration-task.sh, and deregisters the revision again.
# A refusal now fails the deploy while the old code is still serving the old schema —
# nothing to roll back. The post-deploy migration stays as a no-op safety (alembic is
# idempotent), so every migration must STILL be expand-first: for the length of the
# roll the OLD code runs against the NEW schema.
#
# First deploy of an environment (no MigrationTask export yet): nothing to register
# from — exit 0 with a notice; the post-deploy migration migrates.
#
# Needs (PipelineStack deploy role, MigrateBeforeRoll + PassTaskRoles): ecs:
# DescribeTaskDefinition / RegisterTaskDefinition / DeregisterTaskDefinition /
# TagResource, and iam:PassRole on the Compute task + execution roles.
set -euo pipefail
ENVIRONMENT="${1:?environment: dev|preprod|prod}"
IMAGE="${2:?the new image URI (<registry>/<repo>:<tag>)}"
REGION="${AWS_REGION:-ca-central-1}"
PREFIX="Luciel-${ENVIRONMENT}"
CONTAINER="MigrationContainer"

current=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${PREFIX}-MigrationTaskArn'].Value" --output text)
if [ -z "$current" ] || [ "$current" = "None" ]; then
  echo "::notice::[pre-roll] no ${PREFIX}-MigrationTaskArn export yet (first deploy) — the post-deploy migration runs instead"
  exit 0
fi

work="$(mktemp -d)"
aws ecs describe-task-definition --region "$REGION" --task-definition "$current" \
  --include TAGS --output json > "$work/current.json"

# Copy every REGISTERABLE field of the current revision (describe also returns
# read-only ones — ARN, revision, status, registeredAt/By, compatibilities,
# requiresAttributes — that register refuses), swap the one container's image, keep
# the cost-allocation tags. Exactly one container named MigrationContainer, or stop:
# swapping the wrong container's image would migrate with the OLD code.
python3 - "$work/current.json" "$IMAGE" "$CONTAINER" > "$work/register.json" <<'PY'
import json
import sys

described = json.load(open(sys.argv[1]))
image, container_name = sys.argv[2], sys.argv[3]
td = described["taskDefinition"]
matches = [c for c in td["containerDefinitions"] if c.get("name") == container_name]
if len(matches) != 1:
    sys.exit(f"[pre-roll] expected exactly one {container_name} in {td['taskDefinitionArn']}, found {len(matches)}")
old_image = matches[0]["image"]
matches[0]["image"] = image
registerable = (
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "runtimePlatform", "ephemeralStorage", "pidMode", "ipcMode", "proxyConfiguration",
    "inferenceAccelerators",
)
out = {k: td[k] for k in registerable if td.get(k) not in (None, [], {})}
tags = described.get("tags") or []
if tags:
    out["tags"] = tags
print(f"[pre-roll] {container_name}: {old_image} -> {image}", file=sys.stderr)
json.dump(out, sys.stdout)
PY

new=$(aws ecs register-task-definition --region "$REGION" \
  --cli-input-json "file://$work/register.json" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "[pre-roll] registered $new (from $current)"

# The revision exists only for this run: deregister it on EVERY exit path so the
# family's ACTIVE list keeps naming only what CloudFormation manages.
deregister() {
  aws ecs deregister-task-definition --region "$REGION" --task-definition "$new" \
    --query 'taskDefinition.status' --output text \
    || echo "::warning::[pre-roll] could not deregister $new"
}
trap deregister EXIT

bash "$(dirname "$0")/run-migration-task.sh" "$ENVIRONMENT" "$new"
echo "[pre-roll] schema migrated with the new image; the services may roll."
