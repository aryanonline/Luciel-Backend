# Dev teardown and restore

On 2026-11-14 the dev environment in `ca-central-1` was paused to stop AWS charges, which were
about $4.70 a day. This went further than the deploy workflow's `paused=true` switch, which only
scales the services to zero and turns the sweeps off: most of the environment was deleted, and
what a restore needs was kept. The public hostnames (`vantagemind.ai`, `www`, `api` and `embed`,
and the `dev`, `api-dev` and `embed-dev` names) serve nothing until dev is restored.

Below, `<account-id>` stands for the AWS account id.

## What was kept

| Kept | Why |
| --- | --- |
| Route 53 hosted zone `vantagemind.ai` | A new zone would get new name servers, which would have to be set again at the registrar. |
| Stack `Luciel-dev-Security`: KMS key `alias/luciel-dev` and secret `luciel/dev/platform` | The key encrypts the database snapshot. The secret holds every provider key. |
| Stack `Luciel-dev-Pipeline`: the GitHub OIDC provider, the deploy roles and ECR repository `luciel-backend-dev` (emptied) | The deploy workflows reach AWS through these roles. |
| Stack `CDKToolkit` in `ca-central-1` and in `us-east-1` | Every `cdk deploy` needs the CDK bootstrap. The CloudFront certificate's stack deploys to `us-east-1`. |
| RDS snapshot `luciel-dev-final-2026-11-14` (PostgreSQL 16, encrypted with the KMS key above) | The database. [`infra/lib/env-config.ts`](../../infra/lib/env-config.ts) names it as dev's `dbSnapshotIdentifier`, so the next deploy restores it. |
| S3 bucket `luciel-dev-restore-archive-<account-id>` | The files of the four data buckets, under one prefix per bucket. |
| Budget `luciel-paused-guard` (account-wide, $5 a month, alerts at 80% and 100% actual and at 100% forecast) | It catches anything left running or started by accident. |

> [!WARNING]
> Never destroy `Luciel-dev-Security` while the snapshot matters. Dev's KMS key has the `DESTROY`
> removal policy, so deleting the stack schedules the key for deletion, and RDS cannot restore a
> snapshot without the key it was encrypted with. If it happens, cancel the key's deletion within
> its 7-day waiting period.

## What was deleted

- Stacks `Luciel-dev-Networking`, `Luciel-dev-Data`, `Luciel-dev-Compute`, `Luciel-dev-Edge`,
  `Luciel-dev-Web`, `Luciel-dev-Widget`, `Luciel-dev-WidgetCert` (in `us-east-1`),
  `Luciel-dev-Dns`, `Luciel-dev-Observability` and `Luciel-dev-Budget`.
- The database instance and Redis.
- The load balancer, the NAT gateway, the WAF and the CloudFront widget CDN.
- ECR repository `luciel-dev-web`.
- The four data buckets: `luciel-dev-knowledge-originals-<account-id>`,
  `luciel-dev-transcript-archive-<account-id>`, `luciel-dev-export-bundles-<account-id>` and
  `luciel-dev-voice-audio-<account-id>`. Their files are in the archive bucket.
- The log groups.
- The per-tenant `vm/*` secrets. Test tenants must reconnect their own integrations after a
  restore.
- Old orphan resources.

Every fixed resource name is free again, so a restore creates each resource under its old name.

## What was disabled

- SES receipt rule set `luciel-dev-inbound-v2` was deactivated. It belongs to the Compute stack,
  so a restore creates it again, inactive: CloudFormation never activates a receipt rule set.
- GitHub workflows `dev-cost-guard`, `image-rebuild` and `dependency-review` in this repository,
  and `image-rebuild` and `dependency-review` in Luciel-Frontend.
- The monthly steward routine, the cloud routine outside this repository that reads the month's
  dependency review issue, is paused.

## Cost while paused

About $2 to $4 a month: the hosted zone ($0.50), the KMS key ($1), the platform secret ($0.40),
snapshot storage and the archive bucket.

## Restore

A restore brings back the running cost of about $4.70 a day. Run the steps in order, from this
repository on `main`, with AWS credentials for the account and the virtual environment active for
the Python scripts.

1. **Provider keys.** Rotate the provider keys and write the new values into
   `luciel/dev/platform`: run `python scripts/merge_platform_secret.py --env dev`, paste the
   `KEY=VALUE` lines and end the input. It prints key names, never values. Leave out
   `--redeploy`, because no service exists yet. Then
   `python scripts/merge_platform_secret.py --env dev --check --live` reports what a live boot
   would refuse or mark degraded
   ([`scripts/merge_platform_secret.py`](../../scripts/merge_platform_secret.py)).
2. **GitHub settings.** Set the repository variable `AWS_ACCOUNT_ID` in both repositories, and
   the secret `BUDGET_ALERT_EMAIL` (the operator inbox) in this one. Delete the old
   `BUDGET_ALERT_EMAIL` variable if it is still there.

   ```bash
   gh variable set AWS_ACCOUNT_ID --body <account-id>
   gh variable set AWS_ACCOUNT_ID --body <account-id> --repo aryanonline/Luciel-Frontend
   gh secret set BUDGET_ALERT_EMAIL      # prompts for the address
   ```

3. **Backend, paused.** This recreates the deleted stacks with every service at zero. Because
   `infra/lib/env-config.ts` sets `dbSnapshotIdentifier` for dev, the Data stack restores the
   database from `luciel-dev-final-2026-11-14`, with a new master password. The run builds no
   image: a paused run reuses the newest tag in `luciel-backend-dev`, which is empty, so its task
   definitions name an image that does not exist yet. Nothing starts while paused.

   ```bash
   gh workflow run deploy.yml --ref main -f environment=dev -f integration_mode=live -f paused=true
   ```

4. **Files.** Copy each archive prefix back into the bucket it came from, which step 3 recreated.
   Do it now, while the sweeps are still off. The data buckets refuse requests made outside
   `ca-central-1`.

   ```bash
   aws s3 ls s3://luciel-dev-restore-archive-<account-id>/ --region ca-central-1
   aws s3 sync s3://luciel-dev-restore-archive-<account-id>/<prefix>/ \
     s3://luciel-dev-<bucket>-<account-id>/ --region ca-central-1
   ```

5. **Frontend.** This pushes the web image into the recreated `luciel-dev-web` repository and the
   widget bundle into the widget bucket.

   ```bash
   gh workflow run deploy.yml --repo aryanonline/Luciel-Frontend --ref main -f environment=dev
   ```

6. **Backend, running.** This builds and gates the image, migrates, starts the services and runs
   every gate. The first migration after a restore also re-sets the app role's password from the
   new master password. Expect the last step, the SNS check, to fail until step 8; that check
   never rolls anything back. If an earlier gate fails, the automatic rollback returns the services
   to step 3's task definitions, whose image does not exist: fix the cause and run this step again.

   ```bash
   gh workflow run deploy.yml --ref main -f environment=dev -f integration_mode=live -f paused=false
   ```

7. **Workflows.** Re-enable them now, not before step 6: `dev-cost-guard` stops dev's database
   whenever every service is at zero.

   ```bash
   gh workflow enable dev-cost-guard.yml
   gh workflow enable image-rebuild.yml
   gh workflow enable dependency-review.yml
   gh workflow enable image-rebuild.yml --repo aryanonline/Luciel-Frontend
   gh workflow enable dependency-review.yml --repo aryanonline/Luciel-Frontend
   ```

8. **Subscriptions.** `bash scripts/check_sns_subscriptions.sh dev` must find a confirmed https
   subscriber on `luciel-dev-ses-events` and on `luciel-dev-ses-inbound-v2`. Both subscriptions
   were created in step 3, when nothing answered, so request confirmation again for each pending
   one (SNS console, Request confirmation); the live routes confirm it. Also confirm the email that
   AWS sent the operator inbox for the alarms topic
   ([`scripts/check_sns_subscriptions.sh`](../../scripts/check_sns_subscriptions.sh)).
9. **Inbound email.** Activate the receipt rule set only after step 8: mail that reaches a topic
   with no confirmed subscriber is dropped.

   ```bash
   aws ses set-active-receipt-rule-set --rule-set-name luciel-dev-inbound-v2 --region ca-central-1
   ```

10. **Live walk.** Run it from the frontend repository. The walk account comes back with the
    snapshot.

    ```bash
    gh workflow run live-walk.yml --repo aryanonline/Luciel-Frontend --ref main \
      -f base_url=https://vantagemind.ai -f confirm=walk-account-only
    ```

11. **Steward routine.** Resume the monthly steward routine.
12. **Guard budget.** Delete `luciel-paused-guard`, or raise its limit: a running dev costs about
    $4.70 a day, so at $5 a month it alerts on the first day. Dev's own ceiling comes back with
    the `Luciel-dev-Budget` stack.

## After the restore

Keep `dbSnapshotIdentifier` set, to the same value, for as long as the restored database lives.
CloudFormation itself lets `DBSnapshotIdentifier` be removed after a restore; the danger is what
the Data stack does without it. With the field unset in `infra/lib/env-config.ts`, the stack
builds a fresh database instead, which adds `DBName`, `MasterUsername`, `KmsKeyId` and
`StorageEncrypted` under the same logical id. Changing any of those requires replacement, so
CloudFormation replaces the restored database with a new, empty one. Naming a different snapshot
restores a new instance from that snapshot instead. Either way the current instance is kept
outside the stack (`RETAIN`), with its data, and the app is pointed at the new one
([`infra/test/db-snapshot-restore.test.ts`](../../infra/test/db-snapshot-restore.test.ts)).
