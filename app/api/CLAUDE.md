# app/api — CLAUDE.md

## Purpose
Defines FastAPI v1 routes, dependencies, error behavior, and the control/data-plane HTTP boundary.

## Key files
`deps.py` — shared request dependencies.
`errors.py` — registered API error handling.
`v1/admin/*.py` — owner control-plane routes.
`v1/chat_widget.py` and `v1/webhooks/*.py` — public data-plane and provider ingress.

## Contracts this module owns
- Keep dashboard/admin routes cookie-authenticated and widget routes free of ambient cookie authority.
- Verify provider signatures before tenant lookup and before runtime work.
- Add every intentional route to the frozen route registry test.

## Testing pointers
- `tests/contract/` for wire contracts and public flows.
- `tests/unit/test_route_registry.py`, `test_cors_headers.py`, and webhook tests.

## Gotchas from the 2026-08-06 truth delta
- Widget CORS is deliberately wildcard without credentials; control-plane CORS is allow-listed with credentials.
- OAuth callbacks are public only because their signed single-use state authorizes them.

## Configuration maintainability (round 6 WP-D, 2026-09-07)
- Everything connected can be switched or disconnected: DELETE /admin/connections/sms/number
  (keeps the Twilio account), DELETE /admin/connections/sms/attest-registration, DELETE
  /admin/connections/record-source/csv (hands the record_source row back → lookup_record off),
  DELETE /admin/luciel/voice-consent and /sms-compliance-ack (the channel goes off in the
  same write; re-enabling re-asks). PUT /channels cascades the dependent send tool off
  server-side with `disabledReason: <channel>_channel_disabled` (F163). POST
  /admin/account/close needs `confirmDeleteLuciel: true` while a Luciel is live (F156).
  POST /knowledge/sync-connections returns the existing crawl row (200) for the same URLs.

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
