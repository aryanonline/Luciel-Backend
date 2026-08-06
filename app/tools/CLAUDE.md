# app/tools — CLAUDE.md

## Purpose
Controls external tool authorization, selection, execution, normalized outcomes, and scheduling backend behavior.

## Key files
`contract.py` — normalized tool outcome contract.
`broker.py` and `registry.py` — resolution and execution.
`authorization.py` and `catalog.py` — tenant/tool permission surface.
`calendar_backend.py` — scheduling operations.

## Contracts this module owns
- Use only `ok`, `empty`, `ambiguous`, `failed`, and `missing_input`; read results back faithfully.
- Authorize tools per tenant and do not substitute an unconfigured provider.
- Scheduling reads before it writes; reschedule/cancel must locate the booking and reveal ambiguity/conflict.

## Testing pointers
- `tests/integration/test_tool_broker.py`, `test_tool_registry_execution.py`, and `test_scheduling_capability.py`.
- `tests/unit/test_google_calendar_backend.py`, registry resolver, and contract pillar tests.

## Gotchas from the 2026-08-06 truth delta
- Live tool delivery fails closed and must never fabricate a send ID or successful side effect.
- Google Calendar or Calendly provides one scheduling capability; unsupported status must be truthful.

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
