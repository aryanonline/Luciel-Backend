# app/notifications — CLAUDE.md

## Purpose
Delivers internal/customer notifications and runs the deferred escalation-dispatch boundary.

## Key files
`escalation_dispatch.py` — deferred escalation sends and retry work.
`email_dispatcher.py`, `email_sender.py`, and `email_notifier.py` — email delivery layers.
`sms_notifier.py` and `notifier.py` — alternate notification adapters.

## Contracts this module owns
- Escalation dispatch failure queues a retry and contributes to health visibility.
- Do not claim an escalation is reaching a person in real time when only deferred/pass-to-team behavior exists.
- Live notification delivery is honest and fail-closed; mock sends cannot masquerade as provider success.

## Testing pointers
- `tests/integration/test_escalation_retry.py` and `test_sweep_deferred_health.py`.
- `tests/unit/test_escalation_delivery.py`, `test_escalation_routing.py`, and notifications tests.

## Gotchas from the 2026-08-06 truth delta
- Missing contacts and grounding failures say the request was passed to the team, never “connecting you right now”.
- Human replies to widget users are later poll delivery, not notification-driven real-time push.
- The 2026-08-18 enrichment (channel/when/identity/quote/deep-link on
  EscalationNotification) is DERIVED at send time in _build_notification from rows
  already written — the deferred-queue payload is unchanged, replays render
  identically, and no PII enters the queue. Keep it that way.

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
