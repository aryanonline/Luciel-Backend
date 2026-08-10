# app/lifecycle — CLAUDE.md

## Purpose
Owns account/Luciel state transitions, delete grace, cascade cleanup, retention, and export production/storage.

## Key files
`state_machine.py` — account and Luciel states.
`cascade.py`, `close_purge.py`, and `grace_expiry.py` — close/delete cleanup.
`export_bundle.py` and `export_store.py` — export assembly/storage.
`retention.py` and `audit_retention.py` — scheduled deletion rules.
`transcript_archive.py` and `transcript_store.py` — 90-day hot→cold transcript offload to S3 + read-through (Commitment #38).

## Contracts this module owns
- Account close is delete Luciel (offer export) then close account; deletion has 30-day grace and can restore active.
- Apply retention/cascade operations idempotently and without orphaned tenant data.
- Preserve the seven-day export-link expiry and the one-year session-content/summary deletion.

## Testing pointers
- `tests/integration/test_lifecycle.py`, `test_lifecycle_storage_proof.py`, and `test_deletion_cascade_completeness.py`.
- `tests/contract/test_luciel_lifecycle.py`, `test_export_link.py`, and export unit tests.

## Gotchas from the 2026-08-06 truth delta
- Pause is not deletion and must not be represented as account closure.
- Per-lead archive and irreversible confirmed prune exist; stale-lead views, bulk prune, and auto-prune opt-in must not be promised as complete.

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
