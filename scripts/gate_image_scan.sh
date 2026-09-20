#!/usr/bin/env bash
# Governed CVE gate over a MULTI-ARCH image tag (round 7 WP-8, owner decision D7:
# Fargate on Graviton).
#
#   scripts/gate_image_scan.sh <ecr-repository> <image-tag> [check_image_scan.py args]
#
# The deploy pushes ONE tag that is an OCI image index (manifest list) carrying a
# linux/amd64 and a linux/arm64 child; the ECS task definitions run the arm64 child
# on Graviton. ECR scan-on-push scans each pushed IMAGE manifest, and the index
# itself is not an image (there is nothing in it to scan), so the gate resolves the
# platform children through the tag's manifest and runs scripts/check_image_scan.py
# against each child's findings. Fails CLOSED when:
#   * the tag is not an index carrying every platform in REQUIRED_PLATFORMS — an
#     amd64-only image (a build from before the Graviton move, reused by the
#     deploy's skip-if-present path) would deploy a task ECS can never start on
#     ARM64 (CannotPullContainerError: no match for platform);
#   * any child's scan never reaches COMPLETE within the wait;
#   * any child fails the governed gate (non-allowlisted CRITICAL/HIGH, expired waiver).
# Findings land beside the caller as scan-findings-<os>-<arch>.json.
#
# IAM: ecr:BatchGetImage (the tag's manifest) and ecr:DescribeImageScanFindings —
# both already on the deploy roles (PipelineStack). ecr:StartImageScan is tried only
# as a fallback when a child's scan never registered, and a denial is tolerated.
set -euo pipefail

REPO="${1:?ecr repository name}"
TAG="${2:?image tag}"
shift 2
REGION="${AWS_REGION:-ca-central-1}"
REQUIRED_PLATFORMS="${REQUIRED_PLATFORMS:-linux/amd64 linux/arm64}"
ALLOWLIST="${ALLOWLIST:-.security/cve-allowlist.json}"
SCAN_WAIT_ATTEMPTS="${SCAN_WAIT_ATTEMPTS:-60}"

manifest=$(aws ecr batch-get-image --region "$REGION" --repository-name "$REPO" \
  --image-ids imageTag="$TAG" \
  --accepted-media-types application/vnd.oci.image.index.v1+json \
    application/vnd.docker.distribution.manifest.list.v2+json \
    application/vnd.oci.image.manifest.v1+json \
    application/vnd.docker.distribution.manifest.v2+json \
  --query 'images[0].imageManifest' --output text)
if [ -z "$manifest" ] || [ "$manifest" = "None" ]; then
  echo "::error::image-scan gate: $REPO:$TAG has no manifest in ECR — failing closed"
  exit 1
fi

# One "<os>/<arch> <digest>" line per platform child. buildx attestation manifests
# (platform unknown/unknown) are not images and are skipped.
children=$(python3 - "$manifest" "$REQUIRED_PLATFORMS" <<'PY'
import json
import sys

manifest = json.loads(sys.argv[1])
required = set(sys.argv[2].split())
hint = "rebuild with docker buildx --platform " + ",".join(sorted(required))
entries = manifest.get("manifests")
if not entries:
    print(
        "::error::image-scan gate: the tag is a single-architecture image, not a "
        f"multi-arch index; the ECS task definitions run ARM64 — {hint}",
        file=sys.stderr,
    )
    sys.exit(1)
found: dict[str, str] = {}
for entry in entries:
    platform = entry.get("platform") or {}
    if platform.get("os") == "unknown" or platform.get("architecture") == "unknown":
        continue
    found[f"{platform.get('os')}/{platform.get('architecture')}"] = entry["digest"]
missing = sorted(required - set(found))
if missing:
    print(
        f"::error::image-scan gate: the index lacks {missing} (has {sorted(found)}); "
        f"the ECS task definitions run ARM64 — {hint}",
        file=sys.stderr,
    )
    sys.exit(1)
for key in sorted(found):
    print(key, found[key])
PY
)

failed=0
while read -r platform digest; do
  [ -n "$platform" ] || continue
  out="scan-findings-${platform//\//-}.json"
  status=""
  for i in $(seq 1 "$SCAN_WAIT_ATTEMPTS"); do
    # Scan-on-push takes a few seconds to even REGISTER, so poll until the scan
    # exists and completes — the ECR waiter errors on ScanNotFound instead of
    # retrying. If it never completes, the findings file is absent or non-COMPLETE
    # and check_image_scan.py fails closed.
    if aws ecr describe-image-scan-findings --region "$REGION" --repository-name "$REPO" \
         --image-id imageDigest="$digest" --max-results 1000 --output json > "$out" 2>/dev/null; then
      status=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('imageScanStatus',{}).get('status',''))" "$out")
      [ "$status" = "COMPLETE" ] && break
    elif [ "$i" -eq 6 ]; then
      # No scan registered after 30s: ask for one. A denial (the role has no
      # ecr:StartImageScan) or an already-running scan both just mean keep polling.
      aws ecr start-image-scan --region "$REGION" --repository-name "$REPO" \
        --image-id imageDigest="$digest" >/dev/null 2>&1 || true
    fi
    echo "[gate] $platform scan not ready (attempt $i, status '${status:-none}') - waiting 5s"
    sleep 5
  done
  echo "[gate] $platform ($digest): scan status '${status:-absent}'"
  if ! python3 scripts/check_image_scan.py "$out" "$ALLOWLIST" "$@"; then
    failed=1
  fi
done <<< "$children"

if [ "$failed" != "0" ]; then
  echo "::error::image-scan gate: at least one platform child of $REPO:$TAG failed the gate"
  exit 1
fi
echo "[gate] PASS — every platform child of $REPO:$TAG cleared the governed gate."
