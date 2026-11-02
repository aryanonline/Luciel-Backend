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
- `/docs`, `/redoc` and `/openapi.json` are served only when `settings.environment` is
  `LOCAL_ENVIRONMENT` (`development`, the unset default); deployed tasks get dev, preprod
  or prod and answer 404. `app.openapi()` keeps working in tests in either mode.
- An array-returning GET takes `limit` (ceiling `PAGE_MAX`, `paging.py`) and `offset`, or
  names why not in `tests/unit/test_list_endpoints_bounded.py`, which also accounts for
  every top-level array in an object response. A list the dashboard reads whole defaults
  to its ceiling until the client pages. Two of those are not a plain first page:
  - The transcript is windowed from the newest end, and `offset` counts back from the
    newest message, because the takeover view polls it. Past 500 rows the oldest are out.
  - Knowledge sources have their own ceiling (`SOURCES_MAX`, 10,000), since that page is
    the only place to view, rename or delete a source. Past it, rows removed at origin go
    first.
  Both answer `X-Total-Count` (CORS-exposed on the control plane). The dashboard does not
  read it yet, so nothing on screen says a list was cut. Connections and sync connections
  stay at 500 with no total: each row is a connection an owner made.

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

## The closed-account surface (round 7 WP-5, owner decision D5)
- Three gates in `deps.py`: `current_session_subject` (any valid session), `current_account_owner`
  (verified; open, or closed inside the 30-day reopen window), `current_admin` (verified and open).
- ONLY `GET /admin/account`, `POST /admin/account/reopen` and `POST /admin/account/export` depend
  on `current_account_owner`. Every other owner route stays on `current_admin`, which refuses a
  closed account with the typed 403 `{"message", "code": "account_closed", "detail": {"closedAt",
  "reopenUntil"}}` — the frontend renders the reduced page from it. `ApiError.code/detail` are
  optional; an untyped error's body is unchanged.
- `tests/unit/test_closed_account_surface.py` walks every registered route and pins those sets by
  dependency: putting a new route on the wrong gate fails there, by name.

## The Website chat toggle (round 7 owner decision D9)
- `chat_widget.py` honours the channels pillar's `widget` entry: an explicit `enabled: false`
  holds NEW visitors — bootstrap answers `renderState: "channel_off"` (no greeting, no budget
  read, no LLM) and `/messages` opens no session and answers `channel_off`. A missing widget
  entry reads as ON (legacy rows; the frontend uses the same rule).
- The carve-out is the pause/delete drain's: a supplied session id that resolves to an OPEN
  widget session of the instance keeps being served.
- Paused AND off → `paused` WINS. Both render nothing, but the widget keeps re-checking the
  bootstrap on `channel_off` (so it appears when the owner switches it back on) and stops on
  `paused`; re-polling a Luciel that serves nobody would be wasted load.
- Ordering is unchanged: embed key → per-IP gate → origin allowlist → held states → budget
  (bootstrap) / stage-two session gate (messages). Tests: `tests/contract/test_widget_channel_off.py`.

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
