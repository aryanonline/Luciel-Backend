# app/lifecycle — CLAUDE.md

## Purpose
Owns account/Luciel state transitions, delete grace, cascade cleanup, retention, and export production/storage.

## Key files
`state_machine.py` — account and Luciel states.
`cascade.py`, `close_purge.py`, and `grace_expiry.py` — close/delete cleanup.
`reopen.py` — reopen a closed account inside its 30-day window (owner decision D5).
`export_bundle.py` and `export_store.py` — export assembly/storage.
`retention.py` and `audit_retention.py` — scheduled deletion rules.
`transcript_archive.py` and `transcript_store.py` — 90-day hot→cold transcript offload to S3 + read-through (Commitment #38).

## Contracts this module owns
- Account close is delete Luciel (offer export) then close account; deletion has 30-day grace and can restore active.
- A closed account can be REOPENED for 30 days (D5): back to the state before closure — the Luciel
  included iff the closure deleted it (paused again if it was paused); billing rebinds lazily.
- Apply retention/cascade operations idempotently and without orphaned tenant data.
- Preserve the seven-day export-link expiry and the one-year session-content/summary deletion.

## Testing pointers
- `tests/integration/test_lifecycle.py`, `test_lifecycle_storage_proof.py`, and `test_deletion_cascade_completeness.py`.
- `tests/contract/test_luciel_lifecycle.py`, `test_export_link.py`, and export unit tests.

## Gotchas from the 2026-08-06 truth delta
- Pause is not deletion and must not be represented as account closure.
- Per-lead archive and irreversible confirmed prune exist; stale-lead views, bulk prune, and auto-prune opt-in must not be promised as complete.

## Gotchas from round 7 WP-5 (2026-10)
- A Luciel delete writes connection status `quiescing`, not `revoked`: the resolver still routes
  inbound (the drain works on every channel), the claim gate and the broker treat it as revoked.
  Restore and the day-30 cascade handle both (`NOT_SERVING_STATUSES`).
- The closure clock is `accounts.closed_at` (stamped at close, cleared at reopen, equal to the
  Luciel's `grace_window_started_at` when the closure deleted it). `close_purge.reopen_deadline`
  is the ONE deadline the reduced surface and the purge share; the first closure audit row is
  only the fallback for rows closed before the column.
- Boundary races are serialized, not hoped away: reopen and the purge both take `FOR UPDATE` on
  the account row; restore and grace expiry on the instance row; an export build and the
  hard-delete cascade share `export_bundle.hold_export_lock` (the cascade takes it FIRST).
- Restore re-qualifies what rode through the grace window: `crm_lead_links` survive only for a
  CRM connection revived on its stashed credential, and a synced document re-ingested by a sync
  that was in flight at the delete is removed before the original comes back.

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
