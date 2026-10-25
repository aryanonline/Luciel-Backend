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
- SQLAlchemy 2.0 dispatches `after_commit` on a SAVEPOINT release too; the hook ignores
  it (`in_nested_transaction()`), so a `begin_nested()` inside a turn never starts a page
  before the outer commit. `relabel_scheduled_notification` re-keys a still-queued page
  when its row is relabelled in the same transaction (M3 one-row fix); it never schedules.
- A test that asserts on delivery through the loop or `_record_escalation` must exit
  the session (commit) and then `await escalation_dispatch.drain_background_notifications()`;
  `tests/conftest.py` drains leftovers at teardown so no task outlives its loop.
- `SesEmailSender.send` and `TwilioSmsSender.send_sms` run the SDK call via
  `asyncio.to_thread`; the notification body also carries the row's `reasons` (email
  "Details:", SMS "Why:"), read at send time like everything else in `_build_notification`.

## Escalation delivery truth (round 7 WP-4b, 2026-09-21 → 2026-09-28)
- Contact confirmation is channel-agnostic (`escalation_contact_confirmations`, migration
  0069): saving an SMS contact sends a 6-digit code through the tenant's own number (honest
  `sms_channel_not_provisioned` refusal when none is connected); the owner enters it on
  `POST /admin/luciel/escalation/contacts/confirm-sms` (rate-limited, audited) or the
  contact texts it back. Until confirmed, every page to that number is ALSO copied to the
  account owner's email, exactly like an unverified address. The after-hours contact runs
  the same loop and is in contact health + resend.
- "Delivered" means delivered: a page through a real Twilio sender asks for a DLR
  (`/webhooks/twilio/notifications/status`, signature-verified) and the row reads `sent`
  (`dlr_pending`) until the carrier stamps `delivered` / `failed` by
  `delivery_provider_message_id`.
- An inbound text from an escalation number is a contact reply ONLY when it correlates with
  a page to that number within 24h or carries a confirmation code; otherwise it is a
  customer and runs the runtime.
- `malformed_contact` / `sms_channel_not_provisioned` are terminal: stamped now (never queued
  for 24h); a malformed address is filed as a hard-bounce incident, which status.py already
  reads as the `escalation_bouncing` need.
- A rung that reaches the owner re-points the queued item to contacts-only; a replay that
  finds the row at `owner_fallback` stands down from the owner.
- The payload carries `lead.email` / `lead.phone` on top of the transport identity. A
  moderation take-over (first reason `moderation:self_harm` / `moderation:threats_violence`,
  or the audited `content_moderation_event`) is a SAFETY page on both channels — never
  "asked to speak to a person"; `content_flagged` names what was flagged; the ask bound
  lists the field names still missing.
- Backlog visibility (`backlog.py`): `/healthz` adds `escalation_backlog:<n>` to `degraded`
  once the queue has held pages for longer than one deferred-sweep interval (the "since"
  is `oldestQueuedAt` from `platform_escalation_backlog()`, persisted — never a process
  clock); the deferred sweep emits `EscalationBacklog` after its drain and the
  ObservabilityStack alarms on ≥ 1 over the sweep's hourly period. An unknown backlog
  emits nothing — a fabricated 0 would read as all clear.

## Delivery hardening (round 7 Phase 2 review, 2026-10-18; migration 0072)
- Every accepted page through a real Twilio sender reads `sent` (`dlr_pending`), BOTH
  credential shapes: the OAuth bearer sender asks for the same StatusCallback. Twilio signs
  receipts with the ACCOUNT auth token, so a tenant without one verifies on the C11
  capability token in the URL; a tenant with neither gets no callback (never accepted unsigned).
- The status route verifies BEFORE charging its flood bucket, and re-looks-up an unknown SID
  for a few seconds (a receipt can beat the sender's commit).
- `sweep_stale_escalations` (inside the deferred sweep, no new process): `pending` > 10 min
  (the off-turn task died) is audited `notification_lost:pending_stale` and retried once
  through `notify_escalation_safely`; `sent` > 15 min from `sent_at` gets ONE status fetch
  with the tenant's own credentials — terminal → stamped like a receipt, else `unconfirmed`
  and the owner is told. Both bounds are copied into 0072's SQL (candidates + backlog).
- The owner is emailed at most once per escalation: the `+owner` marker on
  `delivery_detail` covers the cc fan-out, the unverified copy and a re-stamped fallback.
  A contacts-only replay with nothing retryable completes its queue item
  (`settled:owner_heard`) instead of living 24h.
- Code texts spend a per-tenant `escalation_code_sms` budget (refusal
  `code_send_rate_limited`, audited); dashboard code entries spend `escalation_code_entry`,
  sized to 3 SMS contacts × `MAX_CODE_ATTEMPTS` — the per-code counter is the brute-force guard.
