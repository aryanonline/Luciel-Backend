# Pause / Resume (cost control)

Two tiers for parking an environment when it isn't needed. Region is **ca-central-1**
(CloudFront/widget cert is global/us-east-1). Substitute `<env>` = `dev` | `staging`
| `prod`. The OIDC deploy role / your admin profile must be assumed first.

The recurring cost split (dev): the **compute** tier (Fargate tasks + 2 ALBs + NAT)
is the bulk and is what these tiers shed; the **data** floor (RDS instance + storage
+ backups) only goes away in the near-zero tier.

---

## Tier 1 — Cheap-idle (reversible in one command, keeps data)

Scale **every ECS service** (backend control-plane + data-plane, frontend web) to
`desiredCount 0`. Kills Fargate task cost; ALBs/NAT/RDS keep their hourly floor.

### Option A — the `paused` context flag (preferred; single source of truth)

`paused` is an env-config switch wired through `bin/luciel-infra.ts`. Redeploy the
compute + web stacks with it set:

```bash
cd infra
# PAUSE: all ECS services -> desiredCount 0
npx cdk deploy "Luciel-<env>-Compute" "Luciel-<env>-Web" \
  -c env=<env> -c paused=true -c buildImage=true --require-approval never

# RESUME: back to normal counts
npx cdk deploy "Luciel-<env>-Compute" "Luciel-<env>-Web" \
  -c env=<env> -c buildImage=true --require-approval never
```

### Option B — direct service scale (no deploy; fastest, but drifts from IaC)

```bash
CLUSTER_BACKEND=$(aws cloudformation list-exports \
  --query "Exports[?Name=='Luciel-<env>-ClusterName'].Value" --output text)

# Backend services
for svc in $(aws ecs list-services --cluster "$CLUSTER_BACKEND" --query 'serviceArns' --output text); do
  aws ecs update-service --cluster "$CLUSTER_BACKEND" --service "$svc" --desired-count 0
done

# Frontend web cluster (find it by name prefix)
CLUSTER_WEB=$(aws ecs list-clusters --query "clusterArns[?contains(@,'Luciel-<env>-Web')]" --output text)
for svc in $(aws ecs list-services --cluster "$CLUSTER_WEB" --query 'serviceArns' --output text); do
  aws ecs update-service --cluster "$CLUSTER_WEB" --service "$svc" --desired-count 0
done
```

Resume = same loop with the normal counts (`1` in dev; `2` for multi-AZ services).
> NOTE: Option B drifts from IaC — the next `cdk deploy` resets counts to config.
> For anything longer than a few hours, prefer Option A so state stays in code.

---

## Tier 2 — Near-zero (destroy compute/edge/web; data optional)

Tear down everything with an hourly floor. ALBs, NAT gateways, Fargate, CloudFront
all stop billing. Keep the data layer (RDS) unless you also want zero data cost.

```bash
cd infra
# Destroy the billable compute/edge/web + widget CDN (keeps Networking/Security/Data).
npx cdk destroy \
  "Luciel-<env>-Web" "Luciel-<env>-Widget" "Luciel-<env>-WidgetCert" \
  "Luciel-<env>-Edge" "Luciel-<env>-Observability" "Luciel-<env>-Compute" \
  -c env=<env> --force

# (Optional, dev/staging only) drop the rest INCLUDING data. RDS takes a final
# snapshot unless deletion is forced; dev RDS removalPolicy is DESTROY.
npx cdk destroy "Luciel-<env>-Data" "Luciel-<env>-Networking" "Luciel-<env>-Dns" \
  "Luciel-<env>-Security" -c env=<env> --force
```

### Restore path

```bash
cd infra
# Bring it all back (image must exist in ECR; build via the pipeline or buildImage=true).
npx cdk deploy --all -c env=<env> -c buildImage=true --require-approval never
# Then run DB migrations as the one-off ECS task (only if Data was recreated):
bash ../scripts/run-migration-task.sh <env>
```

Notes:
- **prod** RDS has `deletionProtection` + `RemovalPolicy.RETAIN` — Tier 2 will not
  silently drop prod data. Don't destroy prod data without a deliberate snapshot.
- The widget S3 bucket retains in prod (RETAIN); dev/staging auto-delete objects.
- DNS records (api/apex/embed) live in the Compute/Web/Widget stacks, so they come
  and go with those stacks. The hosted zone itself is never created or destroyed
  here (it pre-exists: `Z0203683JUDTD67YTV93`).
- ACM certs are free; leaving the `Dns`/`WidgetCert` stacks up costs nothing and
  avoids re-validation churn — prefer keeping them.
