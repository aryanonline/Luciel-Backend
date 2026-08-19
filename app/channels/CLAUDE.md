# app/channels — CLAUDE.md

## Purpose
Handles widget, email, SMS, voice, WhatsApp, Messenger, and Instagram ingress/routing plus outbound sends.

## Key files
`pipeline.py` and `runtime_entry.py` — inbound flow and shared pause/runtime gate.
`inbound_dispatch.py` — pipeline→runtime→reply, shared by webhooks AND the mailbox poll.
`outbound.py`, `twilio_sender.py`, `meta_sender.py`, and `outlook_sender.py` — delivery boundaries.
`tenant_outlook.py` — Microsoft token refresh (rotating refresh tokens, live-gated).
`mailbox_poll.py` — BYO-mailbox inbound via Graph delta polling (5-min sweep).
`signatures.py` and `sns_verification.py` — inbound authenticity.
`email_routing.py`, `phone_lifecycle.py`, and `number_operability.py` — channel readiness.

## Contracts this module owns
- Validate inbound signatures before tenant lookup; retain proxy-aware Twilio verification.
- Fail closed in live delivery: no fabricated sends/IDs and no mock Null sender on a live path.
- All channels use the shared pause gate and first-message AI disclosure rules.

## Testing pointers
- `tests/integration/test_channels.py`, `test_voice_pipeline.py`, and `test_tenant_meta_sender.py`.
- `tests/unit/test_delivery_honesty.py`, signature, sender, pause, and adapter tests.

## Gotchas from the 2026-08-06 truth delta (updated 2026-08-10)
- Widget replies from human takeover are persisted and delivered on the next poll, not pushed instantly.
- Customer Twilio accounts/numbers are BYO; number disable/enable must not sever the connection or erase the number.
- A BYO Outlook mailbox has NO silent SES fallback on the send path: an expired or
  unrefreshable mailbox refuses with `mailbox_reconnect_needed` (falling back would
  swap the FROM identity behind the owner's back). Graph's sendMail answers 202 with
  no message id — never fabricate one.
- THE BASELINE RULE (`mailbox_poll.py`): the first poll after a mailbox connects
  drains the entire delta chain WITHOUT processing — the customer's historical inbox
  is never conversation backlog. Do not weaken this to satisfy a test.
- Webhook auto-config (c20, `tenant_twilio.TwilioWebhookConfigurator` +
  `phone_lifecycle.configure_number_webhooks`): points the designated number's
  Voice/SMS/StatusCallback at the platform using the TENANT's credentials. Fail-open
  to manual guidance (appended to status_detail + audited) — a configuration failure
  must never fail the attach or the credential submit. Silent no-op when
  public_api_base_url is unset (mock/CI).
- Destination-claim hardening (c21): every claim point (attach, pending→destination
  promotion on re-verify, email provision, OAuth mailbox bind, Meta bind) runs
  `routing.lock_destination_claim` (blocking per-destination advisory lock) around
  the ownership check; only a CONFIRMED-operable number becomes the routable
  `destination` (a failed probe parks it under `pending_destination` — no
  squatting); the 0045 partial unique index makes duplicate live sms claims
  structurally impossible; claim refusals audit `destination_claim_rejected`
  under the claimant + rate-limit (`destination_claim` bucket) against
  enumeration. Revoked rows cannot re-enter service outside `lifecycle.restore`,
  which strips destinations another tenant claimed during grace.

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
