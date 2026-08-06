# app/connections — CLAUDE.md

## Purpose
Manages BYO external-provider identities, OAuth, lifecycle state, health, deferred work, registry, and secrets.

## Key files
`service.py` — connection lifecycle operations.
`registry.py` — supported provider definitions.
`oauth_flow.py` — signed OAuth state and callback exchange.
`secrets.py` — tenant secret storage.
`health.py`, `gate.py`, and `deferred_queue.py` — readiness and follow-up.

## Contracts this module owns
- Connection lifecycle: Connect → healthy / Action needed / Reconnect → Disconnect → Switch; swap keeps old credentials serving until verification.
- Store tenant secrets under `vm/`; disconnect clears credentials and is audited.
- Be honestly disabled/action-needed for unconfigured providers or unbound channel destinations.

## Testing pointers
- `tests/contract/test_connection_lifecycle.py`, `test_connections.py`, and OAuth callback tests.
- `tests/integration/test_connections.py`, swap, provisioning, operability, and deferred-health tests.

## Gotchas from the 2026-08-06 truth delta
- One Meta connection covers WhatsApp/Messenger; Instagram is separate and each channel needs its own destination binding.
- Email is ruled BYO but not built; current platform-provisioned address/own-domain reverify state must not be presented as completed BYO email.

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
