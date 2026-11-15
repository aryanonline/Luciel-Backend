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
| S3 bucket `luciel-dev-restore-archive-<account-id>` | The files of the two data buckets that had any, one prefix each: `knowledge-originals/` (18 objects) and `export-bundles/` (3 objects). The transcript-archive and voice-audio buckets held no objects when they were deleted (the versioned transcript archive had no older versions or delete markers either), so the archive has no prefix for them. |
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
- The four data buckets: `luciel-dev-knowledge-originals-<account-id>` and
  `luciel-dev-export-bundles-<account-id>`, whose files are in the archive bucket, and
  `luciel-dev-transcript-archive-<account-id>` and `luciel-dev-voice-audio-<account-id>`, which
  held no objects.
- SES receipt rule set `luciel-dev-inbound-v2`, deactivated first (an active rule set cannot be
  deleted) and then deleted with the Compute stack. A restore creates it again, inactive:
  CloudFormation never activates a receipt rule set. The SES domain identity and the verified
  address were set up outside CDK, cost nothing and were kept.
- Two inline policies that had been added by hand, outside CDK, to the Compute stack's task role
  (`Luciel-dev-Compute-TaskRole…`). They went with the role. `LucielExportBundleAccess` served the
  legacy `vm-export-bundles` bucket, which was deleted too, so it is not needed again.
  `LucielSesSend` allowed `ses:SendEmail` and `ses:SendRawEmail` on any identity, in
  `ca-central-1` only. While the account's SES is in the sandbox, SES also checks the recipient's
  verified identity, which CDK's grant (VantageMind identities only) does not cover; step 6
  says how to add it back.
- The log groups.
- The per-tenant `vm/*` secrets. Test tenants must reconnect their own integrations after a
  restore.
- Old orphan resources.

Every fixed resource name is free again, so a restore creates each resource under its old name,
unless someone else takes it first. S3 bucket names are global, and the five buckets a restore
recreates follow a fixed pattern, `luciel-dev-<purpose>-<account-id>` (`knowledge-originals`,
`transcript-archive`, `export-bundles`, `voice-audio` and `widget`), that anyone who knows the
account id can work out. Step 3 checks that the names are still free, and step 4 checks who owns
a bucket before copying files into it.

## What was disabled

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

   First check that the five bucket names are still free: each must answer `404` (`Not Found`),
   and any other answer means the name is in use. A `403` (`Forbidden`) means another account
   owns that name, and the Data or Widget stack would fail on it: give the bucket a new name
   before going on. A data bucket's name is built in `infra/lib/data-stack.ts`; the widget
   bucket's in `infra/lib/widget-stack.ts`, `infra/lib/pipeline-stack.ts` (the frontend deploy
   role's grant) and Luciel-Frontend's `deploy.yml`. A bucket that answers without an error
   still exists in this account and blocks its stack too.

   ```bash
   ACCOUNT=<account-id>
   for b in knowledge-originals transcript-archive export-bundles voice-audio widget; do
     echo "luciel-dev-$b-$ACCOUNT"
     aws s3api head-bucket --bucket "luciel-dev-$b-$ACCOUNT" --region ca-central-1
   done
   ```

   When all five answer `404`, deploy:

   ```bash
   gh workflow run deploy.yml --ref main -f environment=dev -f integration_mode=live -f paused=true
   ```

4. **Files.** Copy the two archive prefixes, `knowledge-originals/` and `export-bundles/`, back
   into the buckets they came from, which step 3 recreated. The transcript-archive and
   voice-audio buckets stay empty: there was nothing to archive. Do it now, while the sweeps are
   still off. The data buckets refuse requests made outside `ca-central-1`.

   Copy into a bucket only after `head-bucket --expected-bucket-owner` has shown that this
   account owns it. A `403` there means it does not: the loop stops, and those customer files
   must not be copied until the bucket is this account's.

   ```bash
   ACCOUNT=<account-id>
   aws s3 ls "s3://luciel-dev-restore-archive-$ACCOUNT/" --region ca-central-1   # the two prefixes
   for b in knowledge-originals export-bundles; do
     aws s3api head-bucket --bucket "luciel-dev-$b-$ACCOUNT" \
       --expected-bucket-owner "$ACCOUNT" --region ca-central-1 ||
       { echo "STOP: luciel-dev-$b-$ACCOUNT is not this account's bucket"; break; }
     aws s3 sync "s3://luciel-dev-restore-archive-$ACCOUNT/$b/" \
       "s3://luciel-dev-$b-$ACCOUNT/" --region ca-central-1
   done
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

   While SES is still in the sandbox, add `LucielSesSend` back to the task role, or every email to
   a verified test address fails with `AccessDenied` (see What was deleted). The policy file is not
   in this repository; write it from that description. Moving it into CDK would mean a wildcard
   identity in `infra/lib/compute-stack.ts`, which that file's `SendPlatformEmail` grant rules out
   on purpose, so leave that choice to the owner.

   ```bash
   ROLE=$(aws iam list-roles --output text \
     --query "Roles[?starts_with(RoleName, 'Luciel-dev-Compute-TaskRole')].RoleName")
   aws iam put-role-policy --role-name "$ROLE" --policy-name LucielSesSend \
     --policy-document file://luciel-ses-send.json
   ```

   Leave every `OTEL_*` variable off the task definitions, console edits included. fastapi 0.162
   reads `OTEL_PROPAGATORS` when it is imported, so a propagator that is not installed (the usual
   X-Ray value, for one) stops the app from starting.
   [`tests/unit/test_fastapi_telemetry_off.py`](../../tests/unit/test_fastapi_telemetry_off.py)
   checks the Dockerfile, the entrypoint and the CDK app, but it cannot see the console.

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

    The walk skips its card step on a live deployment (it needs a real card), and nothing else
    opens Stripe Checkout there, so save a card by hand once, signed in as the walk account. On
    the Billing page choose **Add payment method** and pay with Stripe's test card
    `4242 4242 4242 4242` (any future expiry, any CVC). Expect **Your card was saved.**, then
    **VISA ending 4242** after a reload. This is the first live run of the Checkout request since
    it moved to `allowed_payment_method_types`, because API version 2026-09-30.endive removed
    `payment_method_types` (see `STRIPE_API_VERSION` in
    [`app/billing/stripe_gateway.py`](../../app/billing/stripe_gateway.py)). **We could not open
    secure checkout just now.** means Stripe refused the session, and the API service's log has
    its error. Remove the card again afterwards (**Remove payment method**), so the walk account
    is back to no card on file.

    Then confirm that the primary model answered. The Anthropic SDK moved to 2.x while dev was
    paused: it now sends `anthropic-version: 2026-10-21` with `Authorization: Bearer`, and that
    request has not run live yet. A failure would not fail a turn. Every call would fall over to
    OpenAI, and only `LlmFallbackAlarm` (5 or more fallback turns in 15 minutes) would notice. The
    services' logs must have `llm_usage` lines with `provider=anthropic` and the primary model
    (`claude-sonnet-4-6`, unless the model-currency job has moved it), and the `LLMFallbackUsed`
    metric (namespace `Luciel`) must stay at zero.

    ```bash
    for svc in data-plane control-plane; do
      aws logs filter-log-events --region ca-central-1 --log-group-name "/luciel/dev/$svc" \
        --start-time $(( ($(date +%s) - 3600) * 1000 )) \
        --filter-pattern '"llm_usage" "provider=anthropic"' --query 'length(events)' --output text
    done
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
