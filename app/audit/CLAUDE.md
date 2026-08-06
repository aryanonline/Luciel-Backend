# app/audit — CLAUDE.md

## Purpose
Appends durable audit events and records tightly controlled internal access.

## Key files
`events.py` — audit event definitions.
`log.py` — append/read log operations.
`internal_access.py` — internal-access log operations.

## Contracts this module owns
- Audit records are append-only and retained for one year.
- Tenant context applies to audit reads/writes; do not use an unscoped administrative shortcut.
- The app can append/select but cannot alter audit history after the ownership lock.

## Testing pointers
- `tests/integration/test_audit_chain.py`, `test_audit_immutability.py`, and `test_audit_retention.py`.
- `tests/integration/test_internal_access_log.py`.

## Gotchas from the 2026-08-06 truth delta
- The audit ownership/trigger lock is a required superuser post-migration operation, not an Alembic-only guarantee.
- One-year retention is the narrowly allowed deletion path; ordinary update/delete/truncate is forbidden.

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
