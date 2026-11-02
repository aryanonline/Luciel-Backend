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
- Lifecycle sweeps prove unverified purge, retention, delete/close grace, lead prune, dunning, audit retention, usage reconcile, session finalization (hourly close + §3.4.7 summarize), the 90-day transcript hot→cold archive (Commitment #38), and the 5-minute BYO-mailbox delta poll (`mailbox_poll` — a TRANSPORT, not a compliance sweep; candidates are CONNECTED outlook rows plus QUIESCING ones stashed as connected, migrations 0038 + 0071 — the poll is the only inbound door a draining mailbox has, so the delete drain is unreachable on email without it).
- “Freed” pages may be reused; files shrink only after explicit `VACUUM FULL`.

## deferred (round 7 WP-4b item 8, 2026-09-28)
- After its per-tenant drain, `run_deferred_retry` publishes `EscalationBacklog` (the
  escalation pages still queued platform-wide, via `app.notifications.backlog`) — the datum
  the ObservabilityStack's hourly alarm reads. Emitted AFTER the drain so a page this run
  delivered is not counted; nothing is emitted when the count could not be taken.
  `DEFERRED_SWEEP_INTERVAL` there is a copy of this sweep's cron and a unit test refuses drift.

## The process drains what it scheduled (round 7 phase 2 review, 2026-10-04)
- A sweep that writes an escalation row only QUEUES the owner notification (a background task
  started after the tenant's commit). `__main__._run_sweep` awaits
  `escalation_dispatch.drain_background_notifications()` for at most 60 s after the sweep, in a
  `finally`; under the old bare `asyncio.run(sweep())` the page was cancelled at process exit and
  the row read `pending` forever. A timeout logs `sweep_notification_drain_timeout` at ERROR
  (SweepErrors alarms) and keeps the exit code. Notifications only — never the retry ladder, and
  never inside `_per_tenant` (the API process's in-process mailbox poller shares it).

## One event loop per sweep process (round 7 fact check, 2026-10-25)
- `main` makes exactly ONE `asyncio.run(_main(name))`: the audit-lock preflight, the sweep,
  the notification drain and `dispose_engine()` all run on that loop. The engine is a module
  singleton whose asyncpg pool belongs to the loop that opened it; the audit sweep used to
  preflight under one `asyncio.run` and sweep under a second, and died on its first query
  (`got Future ... attached to a different loop`) every Sunday 2026-09-27..10-25.
  `app.db.engine` now raises `EngineLoopMismatchError` on a second loop instead.
  09-27 is only the oldest run /luciel/dev/sweep still holds (30-day retention): the
  two-`asyncio.run` shape dates from 2f2af24 (2026-07-31, the preflight's introduction), and
  no `{"sweep": "audit_retention", ...}` result line exists in the retained window, so treat
  the weekly audit prune as never having completed on dev before this fix.
- A sweep that raises is logged as `ERROR app.jobs sweep_failed sweep=<name>` (with the
  traceback) and exits 1. That is a named log line on top of an alert that was already there:
  the F034 EventBridge rule `SweepTaskExitedNonZero` (`infra/lib/observability-stack.ts`) has
  always emailed on any non-zero sweep exit, logged or not, so the audit crashes were never
  silent. A failed pass therefore sends TWO emails for one failure: a SweepErrorAlarm email
  (the filter counts `sweep_failed` itself, not whatever a library logged on the way down) and
  a "stopped with a non-zero exit code" email. The audit-lock refusal exits 1 with no ERROR
  token, so it sends only the exit-code email.
  Tests: `tests/unit/test_sweep_entrypoint_one_loop.py` (every sweep in SWEEPS),
  `tests/integration/test_sweep_entrypoint_one_loop.py` (the real DB).

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
