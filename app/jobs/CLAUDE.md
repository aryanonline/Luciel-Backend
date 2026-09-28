# app/jobs — CLAUDE.md

## Purpose
Runs scheduled platform sweeps for lifecycle, retention, reconciliation, dunning, and deferred-delivery health.

## Key files
`sweeps.py` — sweep implementations and scheduling candidates.
`__main__.py` — executable entrypoint.

## Contracts this module owns
- Use advisory locking so concurrent runs do not duplicate destructive or billing work.
- Keep sweeps idempotent and tenant-safe.
- Surface failed escalation dispatch through deferred work and `/healthz`; do not hide it.

## Testing pointers
- `tests/integration/test_sweep_jobs.py`, `test_sweep_locking.py`, and `test_sweep_deferred_health.py`.
- `tests/unit/test_sweep_scheduling.py`.

## Gotchas from the 2026-08-06 truth delta
- Lifecycle sweeps prove unverified purge, retention, delete/close grace, lead prune, dunning, audit retention, usage reconcile, session finalization (hourly close + §3.4.7 summarize), the 90-day transcript hot→cold archive (Commitment #38), and the 5-minute BYO-mailbox delta poll (`mailbox_poll` — a TRANSPORT, not a compliance sweep; candidates are only CONNECTED outlook rows, migration 0038).
- “Freed” pages may be reused; files shrink only after explicit `VACUUM FULL`.

## deferred (round 7 WP-4b item 8, 2026-09-28)
- After its per-tenant drain, `run_deferred_retry` publishes `EscalationBacklog` (the
  escalation pages still queued platform-wide, via `app.notifications.backlog`) — the datum
  the ObservabilityStack's hourly alarm reads. Emitted AFTER the drain so a page this run
  delivered is not counted; nothing is emitted when the count could not be taken.
  `DEFERRED_SWEEP_INTERVAL` there is a copy of this sweep's cron and a unit test refuses drift.

## daily_brief (round 6 WP-F, 2026-09-07)
- Hourly sweep; a tenant is due once its LOCAL clock (instances.timezone, UTC when unset)
  passes 07:00 and no `brief:<date>` row exists in `notice_deliveries`. Body from
  `app.employee.brief.render_daily_brief` (counts + top-3 needs, never narration).
  `instances.daily_brief_enabled` is the opt-out; candidates via migration 0062.

## Change checklist
- Read the root `CLAUDE.md` and trace callers and dependents before editing.
- Add or update a test that reproduces the real condition being changed.
- Keep tenant, live/mock, and externally visible status behavior explicit.
- If persistence, routes, or deployment posture changes, update the related migration, registry, or infrastructure test deliberately.
- Do not replace an error/unknown state with fabricated success.

## Review prompt
Before merge, ask whether this change could hide an unconfigured, failed,
or tenant-crossing condition behind a successful-looking result. If so,
make that state explicit and test it.
