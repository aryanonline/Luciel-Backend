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

## After-hours routing (round 6 WP-E, 2026-09-07)
- `resolve_routing(..., team_availability=, timezone=, now=)`: with the owner's team
  availability on, nobody reachable now and an after-hours contact named, that contact
  takes the page on its channel and the regular contact for that channel becomes the
  backup; `Routing.after_hours` is True and `escalation_after_hours_routing` is audited.
  The owner-email last resort is untouched. Setting off/reachable → unchanged.

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

## Delivery off the customer's turn (round 7 WP-2 item 5, 2026-09-20)
- `escalation_dispatch.schedule_notification_after_commit(session, ...)` is what
  `orchestrator._record_escalation` calls now (never `notify_escalation_safely` inline):
  the notification starts as a background task on the SQLAlchemy `after_commit` of the
  turn's session, in its own `tenant_session`, so the row is durable before anything
  reads it back and a rolled-back turn pages nobody. Tracked in
  `_BACKGROUND_NOTIFICATIONS`; `drain_background_notifications()` (tests) and
  `drain_background_work()` (the FastAPI lifespan shutdown in `app/main.py`) await it.
- A test that asserts on delivery through the loop or `_record_escalation` must exit
  the session (commit) and then `await escalation_dispatch.drain_background_notifications()`;
  `tests/conftest.py` drains leftovers at teardown so no task outlives its loop.
- `SesEmailSender.send` and `TwilioSmsSender.send_sms` run the SDK call via
  `asyncio.to_thread`; the notification body also carries the row's `reasons` (email
  "Details:", SMS "Why:"), read at send time like everything else in `_build_notification`.
