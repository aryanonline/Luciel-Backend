# app/channels — CLAUDE.md

## Purpose
Handles widget, email, SMS, voice, WhatsApp, Messenger, and Instagram ingress/routing plus outbound sends.

## Key files
`pipeline.py` and `runtime_entry.py` — inbound flow and shared pause/runtime gate.
`outbound.py`, `twilio_sender.py`, and `meta_sender.py` — actual delivery boundaries.
`signatures.py` and `sns_verification.py` — inbound authenticity.
`email_routing.py`, `phone_lifecycle.py`, and `number_operability.py` — channel readiness.

## Contracts this module owns
- Validate inbound signatures before tenant lookup; retain proxy-aware Twilio verification.
- Fail closed in live delivery: no fabricated sends/IDs and no mock Null sender on a live path.
- All channels use the shared pause gate and first-message AI disclosure rules.

## Testing pointers
- `tests/integration/test_channels.py`, `test_voice_pipeline.py`, and `test_tenant_meta_sender.py`.
- `tests/unit/test_delivery_honesty.py`, signature, sender, pause, and adapter tests.

## Gotchas from the 2026-08-06 truth delta
- Widget replies from human takeover are persisted and delivered on the next poll, not pushed instantly.
- Customer Twilio accounts/numbers are BYO; number disable/enable must not sever the connection or erase the number.

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
