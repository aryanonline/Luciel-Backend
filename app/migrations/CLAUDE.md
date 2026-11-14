# app/migrations — CLAUDE.md

## Purpose
Contains the ordered Alembic schema history that establishes persistence, RLS, billing, audit, and lifecycle structures.

## Key files
`env.py` — Alembic configuration from application settings.
`versions/0001_arc9_foundation.py` through `versions/0031_transcript_source_chunks.py` — ordered changes.
`script.py.mako` — migration template.

## Contracts this module owns
- Use additive, reviewable migrations and retain upgrade/rollback coverage where supported.
- Migration changes must preserve RLS, tenant tables, billing watermark/replay storage, and audit guarantees.
- Treat audit locking as an explicit operations postcondition after upgrade, not a migration shortcut.

## Testing pointers
- `tests/integration/test_migration_*.py` (per-revision upgrade, backfill and round-trip checks) and CI migration apply/rollback.
- `tests/isolation/test_all_tenant_tables.py` plus audit/billing/lifecycle integrations.

## Gotchas from the 2026-08-06 truth delta
- Run `scripts/lock_audit_log.sql` as superuser after `alembic upgrade head`; app-role migrations cannot transfer audit ownership.
- Schema cleanup proof includes `counted_sessions` and `processed_stripe_events`; do not strand them during lifecycle changes.

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
