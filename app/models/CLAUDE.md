# app/models — CLAUDE.md

## Purpose
Defines SQLAlchemy persistence records for tenants, conversations, billing, knowledge, connections, tools, audit, and lifecycle data.

## Key files
`instance.py`, `conversation.py`, and `lead.py` — core owner/runtime records.
`billing.py`, `connection.py`, and `knowledge.py` — commercial/provider/retrieval state.
`audit.py`, `deferred.py`, and `internal_access.py` — immutable/deferred operational state.

## Contracts this module owns
- Keep tenant ownership explicit and compatible with RLS for each persisted record.
- Persist facts that enforce invariants: grounding scores, billing count/watermark/replay IDs, destination readiness, and lifecycle states.
- Model changes require a matching migration and cross-tenant isolation review.

## Testing pointers
- `tests/isolation/` and integration tests covering each persistence flow.
- `tests/integration/test_lifecycle_storage_proof.py` for schema-wide cleanup expectations.

## Gotchas from the 2026-08-06 truth delta
- Transcript evidence supports explicit source states and legacy “Not scored” treatment.
- Per-tenant secrets are pointers/metadata here; secret material belongs in Secrets Manager under `vm/`.

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
