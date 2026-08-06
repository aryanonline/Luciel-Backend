# app/schemas_api — CLAUDE.md

## Purpose
Provides typed API wire schemas for authentication, billing, connections, conversations, knowledge, Luciel, usage, and widget flows.

## Key files
`common.py` — shared aliases/base shapes.
`billing.py`, `connections.py`, and `luciel.py` — owner control-plane contracts.
`conversations.py`, `knowledge.py`, `usage.py`, and `widget.py` — runtime/data-plane contracts.

## Contracts this module owns
- Maintain compatible field names, aliases, and explicit state values for frontend/API callers.
- Expose honest connection, evidence, billing, and delivery status rather than inferring success.
- Schema changes must update contract tests and, if routes change, the route registry deliberately.

## Testing pointers
- `tests/contract/` is the primary wire-contract suite.
- Unit and integration tests exercise schema serialization through endpoints.

## Gotchas from the 2026-08-06 truth delta
- Evidence state is three-valued plus legacy “Not scored”; do not collapse unavailable into no sources.
- A destination not bound after consent is Action needed, not Connected.

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
