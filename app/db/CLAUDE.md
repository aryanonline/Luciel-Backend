# app/db — CLAUDE.md

## Purpose
Sets up SQLAlchemy connections, transactions, declarative base, and tenant RLS context.

## Key files
`engine.py` — asynchronous and synchronous engine configuration.
`session.py` — scoped session lifecycle.
`rls.py` — tenant context and RLS helpers.
`base.py` — model base.

## Contracts this module owns
- Application database access uses the non-superuser, non-BYPASSRLS app role.
- Every tenant operation carries the correct RLS context; never “fix” a query by bypassing it.
- Maintain safe transaction/session cleanup around request and job boundaries.

## Testing pointers
- `tests/isolation/test_all_tenant_tables.py` and `test_tenant_isolation.py`.
- Integration tests exercise RLS through real persistence paths.

## Gotchas from the 2026-08-06 truth delta
- The audit owner is deliberately separate from the app role; do not grant that role to `luciel_app`.
- Lifecycle proof expects orphan-free cleanup across tables including sessions and processed Stripe events.

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
