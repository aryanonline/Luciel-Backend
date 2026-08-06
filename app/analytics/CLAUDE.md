# app/analytics — CLAUDE.md

## Purpose
Computes owner-facing analytics and usage views from persisted product activity.

## Key files
`service.py` — analytics calculations and read models.
`__init__.py` — package boundary.

## Contracts this module owns
- Keep usage semantics aligned with Billing and Overview: a conversation is one billed session per billing period.
- Do not invent a separate counting rule for multi-turn sessions or a new pricing tier.
- Expose only tenant-scoped data through the API layer and preserve RLS assumptions.

## Testing pointers
- `tests/unit/test_analytics_service.py`.
- `tests/contract/test_usage_analytics.py` and `tests/contract/test_dashboard.py`.

## Gotchas from the 2026-08-06 truth delta
- The free-50 window follows the Stripe billing period, with calendar-month fallback before a card.
- Overview, Billing, and Analytics must share the same conversation meaning.

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
