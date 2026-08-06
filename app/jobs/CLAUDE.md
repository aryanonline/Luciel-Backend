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
- Lifecycle sweeps prove unverified purge, retention, delete/close grace, lead prune, dunning, audit retention, and usage reconcile.
- “Freed” pages may be reused; files shrink only after explicit `VACUUM FULL`.

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
