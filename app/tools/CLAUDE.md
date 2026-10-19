# app/tools — CLAUDE.md

## Purpose
Controls external tool authorization, selection, execution, normalized outcomes, and scheduling backend behavior.

## Key files
`contract.py` — normalized tool outcome contract.
`broker.py` and `registry.py` — resolution and execution.
`authorization.py` and `catalog.py` — tenant/tool permission surface.
`calendar_backend.py` — scheduling operations.

## Contracts this module owns
- Use only `ok`, `empty`, `ambiguous`, `failed`, and `missing_input`; read results back faithfully.
- Authorize tools per tenant and do not substitute an unconfigured provider.
- Scheduling reads before it writes; reschedule/cancel must locate the booking and reveal ambiguity/conflict.

## Testing pointers
- `tests/integration/test_tool_broker.py`, `test_tool_registry_execution.py`, and `test_scheduling_capability.py`.
- `tests/unit/test_google_calendar_backend.py`, registry resolver, and contract pillar tests.

## Gotchas from the 2026-08-06 truth delta
- Live tool delivery fails closed and must never fabricate a send ID or successful side effect.
- Google Calendar or Calendly provides one scheduling capability; unsupported status must be truthful.

## Round 6 WP-H (2026-09-08)
- `calendly_backend.py`: Calendly never receives a write — `book_appointment` returns the
  slot's scheduling link (`state: scheduling_link` + note), `reschedule` the invitee's
  reschedule link, `cancel` a real cancellation. `_TenantCalendarBackend` dispatches by
  provider; the event type comes from the connection row (`eventTypeUri`, chosen via
  PUT /connections/{id}/settings).
- `record_backend.LiveRecordBackend` (provider `api`): GET `<url>?q=` through the
  sandbox pin + egress allowlist, bearer `api_key`, 10 s, ≤256 KB, JSON array; the
  credential submit probes `?q=ping` first. Mock: `MockRecordTransport.seed(url, rows)`.
- `_EmailMessengerBackend` sends through `channels.outbound.send_outbound_email` — the
  tenant's mailbox, never a platform address; no mailbox → provider_rejected.

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

## Always-on cognition tool (round 7 WP-2 item 2, 2026-09-20)
- `schemas.HAND_TO_TEAM_TOOL_ID` / `HAND_TO_TEAM_DEF` / `with_cognition_tools(defs)`:
  Luciel's own "a person should step in" door. Deliberately NOT in `catalog.ALL_TOOL_IDS`
  or `TOOL_SCHEMAS` (`test_tool_schemas` pins that equality), so it never reaches the
  Configure surface, `DEFAULT_TOOLS`, an authorization row, a capability snapshot or a
  connection gate; `registry.build_tool("hand_to_team")` is None. The orchestrator
  appends it to every turn's tool defs and executes it itself as a standard escalation
  (`luciel_requested_handoff`). Do not add it to the catalog to "make it configurable".

## Required inputs from the confirmed record (round 7 WP-2 remainder item 10, 2026-09-28)
- `ToolBroker.dispatch(..., gathered=)`: the loop passes the lead's CONFIRMED
  email/phone keyed by input name (`app/runtime/channel_switch.confirmed_inputs_on_file`);
  a required input the model left out (absent or blank) is filled from it BEFORE the
  missing_input check, an explicit input always wins, and the fill rides on the
  in-memory trace as `filled_from_record` (field names; the durable row is unchanged).
  `ToolContext.gathered` finally has a writer. Never fill from an unconfirmed
  address — the bar is the same as sending to it.

## Booking mini-audit (round 7 WP-2 remainder item 11, 2026-09-28)
- `booking_events` (the brief's "appointments booked", the analytics card) gets a row for
  a CONFIRMED booking only: a `book_appointment` ok that carries `bookingRef`. Calendly's
  ok is a scheduling LINK with none (held only once the customer completes it) and writes
  nothing; a move or a cancel never writes. `tests/integration/test_round7_booking_audit.py`
  walks read-before-write, move, cancel and the counted row on both providers through the
  broker over the real in-memory transports.
- `CalendlyBackend._find_bookings` by reference requires `status == "active"` like the
  search path, so a cancelled booking answers `empty` on a move or a second cancel —
  never `slot_conflict` about a time nobody holds.
- The business's clock (2026-10-04): `ToolContext.timezone` (written by
  `ToolBroker.dispatch(timezone=)`, which the loop feeds from the turn's operating
  context — `instances.timezone` when it is a real IANA zone) is the clock both backends
  read and answer on. `calendar_backend.parse_interval(tz=)` reads an offset-less time
  and a bare `YYYY-MM-DD` in it (the LOCAL day, 23/25 h across a clock change) and
  honours an explicit offset; every returned time (`open_slots`, `_booking`, the
  Calendly link/reschedule payloads) is rendered in it with the offset on the string,
  and confirming payloads carry `timezone` (M10: `UTC` when no zone is set).
  `schemas._WINDOW_DESC` says the same thing to the model.
- A time already gone (2026-10-04): `calendar_backend.now_utc()` is the one clock seam.
  `open_slots(not_before=)` never offers a started slot ("today" used to offer this
  morning); a book or a move into the past is `slot_in_past` (M10), no write.
  Calendly's available-times API answers a past `start_time` with a 400 (it read as a
  calendar outage on every "anything today?"), so the window is asked from
  now + `AVAILABILITY_LEAD`, and a window wholly behind us is `empty` without a call.
  Pinned in `tests/unit/test_round7_booking_clock.py` and
  `tests/integration/test_round7_booking_timezone.py`.

## Round 7 M10 scheduling follow-ups
- `slot_in_past`: `calendar_backend.ensure_not_past` (both backends, before any read or
  write; the start is read on the business's clock; `PAST_SLOT_TOLERANCE` = 2 min of clock
  skew) raises `SlotInPast`, which Book/Reschedule map to `failed("slot_in_past")`. A slot
  that started inside the tolerance books on Google and stays a `slot_conflict` on Calendly
  (Calendly no longer offers it). `catalog.FAILURE_READBACK` is the reason → read-back copy
  catalog; the orchestrator's `_tool_result_feedback` adds it as `means` beside `reason`.
- Calendly link/reschedule end = the chosen event type's OWN length: the connection row's
  `eventTypeDurationMinutes` (written by PUT /connections/{id}/settings), else Calendly's
  event types, else NO end on the payload (never the 30-minute default).
- No business zone: `stamp_zone` labels every confirming payload `timezone: "UTC"`,
  check_availability carries `timezone` (zone name or `UTC`), and schedule_callback's ok
  adds `fireAtBusinessTime` + `timezone` beside the raw `fireAt` (`_callback_readback`;
  a naive instant is UTC, as the deferred queue reads it) — never a bare hour. The Today
  card's `timezone_unset` need (app/employee/status.py) tells the owner; never guessed.
  Tests: `tests/unit/test_m10_scheduling.py`, `tests/integration/test_m10_timezone_need.py`.
