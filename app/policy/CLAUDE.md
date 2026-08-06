# app/policy — CLAUDE.md

## Purpose
Centralizes product entitlement and capability decisions consumed by API and runtime surfaces.

## Key files
`entitlements.py` — capability/entitlement rules.
`__init__.py` — package boundary.

## Contracts this module owns
- Preserve the one-account/one-Luciel/no-seat/no-model-picker commercial model.
- Do not add unpublished tiers, allowances, or feature promises through a policy default.
- Return capability state that stays honest about unconfigured or action-needed integrations.

## Testing pointers
- `tests/contract/test_capabilities_api.py` and `test_luciel_invariants.py`.
- Billing, connection, and Luciel contract tests exercise dependent policy paths.

## Gotchas from the 2026-08-06 truth delta
- The 30-day allowance-reduction notice is a legal commitment with no implementation; do not expose it as delivered.
- Scheduling remains one owner-facing capability over Google Calendar or Calendly, not a menu of unsupported variants.

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
