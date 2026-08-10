# app/connections — CLAUDE.md

## Purpose
Manages BYO external-provider identities, OAuth, lifecycle state, health, deferred work, registry, and secrets.

## Key files
`service.py` — connection lifecycle operations.
`registry.py` — supported provider definitions.
`oauth_flow.py` — signed OAuth state and callback exchange.
`verify.py` — post-exchange provider verification (Graph /me for the Outlook mailbox).
`secrets.py` — tenant secret storage.
`health.py`, `gate.py`, and `deferred_queue.py` — readiness and follow-up.

## Contracts this module owns
- Connection lifecycle: Connect → healthy / Action needed / Reconnect → Disconnect → Switch; swap keeps old credentials serving until verification.
- Store tenant secrets under `vm/`; disconnect clears credentials and is audited.
- Be honestly disabled/action-needed for unconfigured providers or unbound channel destinations.

## Testing pointers
- `tests/contract/test_connection_lifecycle.py`, `test_connections.py`, and OAuth callback tests.
- `tests/integration/test_connections.py`, swap, provisioning, operability, and deferred-health tests.

## Gotchas from the 2026-08-06 truth delta (updated 2026-08-10)
- One Meta connection covers WhatsApp/Messenger; Instagram is separate and each channel needs its own destination binding.
- BYO email mailbox IS now built (Outlook first, owner decision 2026-08-10): `outlook`
  registers under the existing `email_sender` type beside the platform `ses` modes.
  Connect-verify-bind-live runs through `complete_oauth_connect`'s verify seam
  (`verify.py`); a ses→outlook swap derives its flow shape from the STAGED provider
  and cuts over (provider + auth_class) only in `confirm_reconnect`. The SES sender
  keeps serving on any verify failure — never silently replaced.
- `provision_email` binds the address as the inbound-routing `destination` (with the
  cross-tenant uniqueness check); before migration 0037 no email_sender row carried
  one and SES inbound resolved no tenant at all.
- Microsoft tokens live ~1h and refresh tokens ROTATE: `app/channels/tenant_outlook.py`
  owns refresh (on-demand + the health sweep's provider-dispatching refresher). Live
  mode only; mock falls back to the honest no-refresher.

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
