# app/auth — CLAUDE.md

## Purpose
Implements account access, credential recovery, session tokens, CAPTCHA checks, and unverified-account cleanup.

## Key files
`access.py` — account/session access decisions.
`passwords.py` and `tokens.py` — credential and token flows.
`captcha.py`, `lookup.py`, and `reaper.py` — public-entry safeguards and cleanup.

## Contracts this module owns
- Keep the single-account, single-login product model; do not introduce seats or role-based tenant expansion.
- Preserve session-epoch invalidation when an account closes.
- Public pre-auth lookup is narrow and must not become a general RLS bypass.

## Testing pointers
- `tests/contract/test_auth_flow.py`.
- `tests/contract/test_closed_account_gate.py` and `tests/integration/test_lifecycle.py`.

## Gotchas from the 2026-08-06 truth delta
- Unverified accounts are purged after seven days by lifecycle sweeps.
- Account closure and pause are different states; login/access behavior must remain honest about the distinction.

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
