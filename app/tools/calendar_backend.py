"""Google Calendar as a real ``CalendarBackend`` (§3.3.6, Arc 17).

The four scheduling tools were bound to a stand-in that raised on every call, so a
tenant could *connect* Google Calendar and still have every booking come back
``failed``. This module is the effect half.

**Read-before-write is implemented HERE, not by Google.** The raw Calendar API will
happily insert two events over the same hour: ``events.insert`` has no conflict check
and no precondition to attach one to. So the platform owns the discipline (§3.3.4
rule 2) — every write re-reads the target window immediately before it writes, and a
busy window is a ``failed``/conflict rather than a silent double-booking. The gap
between that read and the write is small but not zero; on a collision Google keeps
both events and the next read surfaces it, which is why the read is immediately
before the write rather than cached from the earlier ``check_availability`` turn.

Two distinct reads, for two distinct questions:
  * ``check_availability`` uses **freeBusy** — the aggregate "is the owner busy",
    which counts events on the calendar that we are not permitted to read.
  * the pre-write conflict re-check uses **events.list** — addressable events with
    ids, which is what lets ``reschedule`` exclude the very event it is moving.
    freeBusy returns anonymous intervals, so it cannot tell "occupied by someone
    else" from "occupied by the booking you are moving".

**Live/mock seam:** ``build_calendar_api`` is the only place that decides whether the
HTTP calls are real. In ``live`` mode it returns :class:`GoogleCalendarApi` (httpx
against googleapis.com); in mock/CI it returns :class:`InMemoryCalendarApi`, a
deterministic fake that stores events in-process. The conflict logic, the token
lookup, and the tool-result mapping are IDENTICAL either way — only the transport
changes — so the whole book/reschedule/cancel discipline is verifiable offline.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

from app.config import get_settings
from app.connections.secrets import get_secret_store, secret_ref_for
from app.tools.contract import ToolContext

# A proposed slot with no explicit end is this long.
SLOT_MINUTES = 30
# How far ahead ``find_bookings`` looks for a lead's existing appointment. A booking
# further out than this is not reachable by reschedule/cancel; two quarters is well
# past any appointment a chat turn is plausibly about.
BOOKING_SEARCH_DAYS = 180
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
# The connected calendar is the owner's own; Google resolves this alias per-token.
DEFAULT_CALENDAR_ID = "primary"


class CalendarUnavailable(RuntimeError):
    """The tenant's calendar cannot be reached — no stored token, or the API refused.

    The catalog wraps every backend call, so this surfaces as a structured ``failed``
    with a non-secret reason. It must never be mistaken for ``empty``, which would read
    to the lead as "no slots" — a false statement about the business's availability.
    """


@dataclass(frozen=True)
class CalendarCredentials:
    access_token: str
    calendar_id: str = DEFAULT_CALENDAR_ID


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    start: datetime
    end: datetime
    email: str


class CalendarApi(Protocol):
    """The Google Calendar HTTP surface, narrowed to what the four tools need.

    This is the live/mock seam: same Protocol, real transport in live mode and an
    in-memory fake otherwise."""

    async def busy(
        self, cred: CalendarCredentials, time_min: datetime, time_max: datetime
    ) -> list[tuple[datetime, datetime]]: ...

    async def events_in(
        self, cred: CalendarCredentials, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]: ...

    async def get_event(self, cred: CalendarCredentials, event_id: str) -> CalendarEvent | None: ...

    async def insert_event(
        self, cred: CalendarCredentials, *, start: datetime, end: datetime, email: str
    ) -> CalendarEvent: ...

    async def patch_event(
        self, cred: CalendarCredentials, event_id: str, *, start: datetime, end: datetime
    ) -> CalendarEvent | None: ...

    async def delete_event(self, cred: CalendarCredentials, event_id: str) -> bool: ...


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def _as_utc(value: datetime) -> datetime:
    """Naive input is read as UTC — a floating local time has no meaning across a
    tenant boundary, and guessing a zone would book the wrong hour."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_instant(text: str) -> datetime:
    return _as_utc(datetime.fromisoformat(text.strip()))


def parse_interval(text: str, *, default_minutes: int = SLOT_MINUTES) -> tuple[datetime, datetime]:
    """``"<start>/<end>"`` or a bare ISO instant (given ``default_minutes`` of length).

    Also accepts a bare ``YYYY-MM-DD`` as that whole UTC day, which is what a caller
    asking to "check Tuesday" produces. Raises ``ValueError`` on anything else; the
    catalog maps that to ``failed`` rather than guessing a window."""
    raw = text.strip()
    if "/" in raw:
        start_text, _, end_text = raw.partition("/")
        start, end = _parse_instant(start_text), _parse_instant(end_text)
        if end <= start:
            raise ValueError(f"window ends before it starts: {raw!r}")
        return start, end
    if len(raw) == 10:  # bare calendar date
        start = _parse_instant(raw)
        return start, start + timedelta(days=1)
    start = _parse_instant(raw)
    return start, start + timedelta(minutes=default_minutes)


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open overlap: back-to-back appointments do NOT collide."""
    return a_start < b_end and b_start < a_end


def open_slots(
    busy: list[tuple[datetime, datetime]],
    *,
    window_start: datetime,
    window_end: datetime,
    minutes: int = SLOT_MINUTES,
) -> list[str]:
    """Fixed-width candidate slots in the window that no busy interval overlaps."""
    step = timedelta(minutes=minutes)
    out: list[str] = []
    cursor = window_start
    while cursor + step <= window_end:
        slot_end = cursor + step
        if not any(_overlaps(cursor, slot_end, b_start, b_end) for b_start, b_end in busy):
            out.append(cursor.isoformat())
        cursor = slot_end
    return out


def _booking(event: CalendarEvent) -> dict[str, Any]:
    """The confirming payload for an ``ok`` result — the exact time that was written,
    never the time that was requested (§3.3.4 rule 1)."""
    return {
        "bookingRef": event.event_id,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "email": event.email,
    }


# --------------------------------------------------------------------------- #
# the backend
# --------------------------------------------------------------------------- #
_T = TypeVar("_T")


class GoogleCalendarBackend:
    """``CalendarBackend`` over Google Calendar for the tenant's connected account.

    The credential is the OAuth token the connect flow stored under the tenant's
    ``calendar`` secret_ref (§3.8.3), read per call so a reconnect takes effect without
    rebuilding the tool.
    """

    def __init__(
        self, api: CalendarApi | None = None, *, calendar_id: str = DEFAULT_CALENDAR_ID
    ) -> None:
        self._api = api if api is not None else build_calendar_api()
        self._calendar_id = calendar_id

    async def _credentials(self, ctx: ToolContext) -> CalendarCredentials:
        # Call-time freshness (§3.8.5, the tenant_outlook pattern generalized):
        # refresh a stale/unknown-expiry token BEFORE the booking call, so a
        # customer's "book me in" never fails on a token that merely aged out
        # between health sweeps. Live-gated inside the helper; in mock (and
        # when no session is available) it returns the stored token untouched.
        if ctx.session is not None:
            from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

            token = await ensure_fresh_access_token(
                ctx.session, ctx.admin_id, ctx.instance_id, connection_type="calendar"
            )
            if not token:
                raise CalendarUnavailable("calendar_token_unavailable")
            return CalendarCredentials(access_token=token, calendar_id=self._calendar_id)
        ref = secret_ref_for(ctx.admin_id, ctx.instance_id, "calendar")
        material = await get_secret_store().get(ref)
        token = (material or {}).get("access_token")
        if not token:
            # Connected-looking row, no usable credential: fail honestly rather than
            # reporting an empty calendar.
            raise CalendarUnavailable("calendar_token_unavailable")
        return CalendarCredentials(access_token=token, calendar_id=self._calendar_id)

    async def _guarded(self, ctx: ToolContext, call: Awaitable[_T]) -> _T:
        """Round 6 WP-C: a 401/403 from Google is a dead grant. The calendar row
        flips to error through the sweep's own transition the moment it happens
        (audit + one attention email); the tool call still fails honestly."""
        try:
            return await call
        except CalendarUnavailable as exc:
            if ctx.session is not None:
                from app.connections import health  # noqa: PLC0415 — import cycle

                await health.flag_if_provider_auth_failure(
                    ctx.session,
                    admin_id=ctx.admin_id,
                    instance_id=ctx.instance_id,
                    connection_type="calendar",
                    reason=str(exc),
                )
            raise

    async def _is_free(
        self,
        cred: CalendarCredentials,
        start: datetime,
        end: datetime,
        *,
        ignore_event_id: str | None = None,
    ) -> bool:
        """The pre-write conflict re-check (§3.3.6). ``ignore_event_id`` is the event
        being moved, which must not count as its own conflict."""
        events = await self._api.events_in(cred, start, end)
        return not any(
            event.event_id != ignore_event_id and _overlaps(start, end, event.start, event.end)
            for event in events
        )

    async def free_busy(self, ctx: ToolContext, window: str) -> list[str]:
        return await self._guarded(ctx, self._free_busy(ctx, window))

    async def _free_busy(self, ctx: ToolContext, window: str) -> list[str]:
        cred = await self._credentials(ctx)
        window_start, window_end = parse_interval(window, default_minutes=SLOT_MINUTES)
        busy = await self._api.busy(cred, window_start, window_end)
        return open_slots(busy, window_start=window_start, window_end=window_end)

    async def book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None:
        return await self._guarded(ctx, self._book(ctx, slot, email))

    async def _book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None:
        cred = await self._credentials(ctx)
        start, end = parse_interval(slot)
        if not await self._is_free(cred, start, end):
            return None  # -> failed("slot_conflict")
        return _booking(await self._api.insert_event(cred, start=start, end=end, email=email))

    async def find_bookings(
        self, ctx: ToolContext, *, email: str, booking_ref: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._guarded(
            ctx, self._find_bookings(ctx, email=email, booking_ref=booking_ref)
        )

    async def _find_bookings(
        self, ctx: ToolContext, *, email: str, booking_ref: str | None = None
    ) -> list[dict[str, Any]]:
        cred = await self._credentials(ctx)
        if booking_ref:
            event = await self._api.get_event(cred, booking_ref)
            # An id that belongs to someone else is not this lead's booking to move.
            return [_booking(event)] if event is not None and event.email == email else []
        now = datetime.now(UTC)
        events = await self._api.events_in(cred, now, now + timedelta(days=BOOKING_SEARCH_DAYS))
        return [_booking(event) for event in events if event.email == email]

    async def reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None:
        return await self._guarded(ctx, self._reschedule(ctx, booking_ref, slot))

    async def _reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None:
        cred = await self._credentials(ctx)
        start, end = parse_interval(slot)
        existing = await self._api.get_event(cred, booking_ref)
        if existing is None:
            return None  # gone since the read -> failed, never a fabricated move
        if not await self._is_free(cred, start, end, ignore_event_id=booking_ref):
            return None  # -> failed("slot_conflict")
        moved = await self._api.patch_event(cred, booking_ref, start=start, end=end)
        return _booking(moved) if moved is not None else None

    async def cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None:
        return await self._guarded(ctx, self._cancel(ctx, booking_ref))

    async def _cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None:
        cred = await self._credentials(ctx)
        event = await self._api.get_event(cred, booking_ref)
        if event is None or not await self._api.delete_event(cred, booking_ref):
            return None  # -> failed("cancel_rejected")
        return {**_booking(event), "status": "cancelled"}


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class InMemoryCalendarApi:
    """Deterministic in-process calendar for mock/CI (§ the same posture as
    ``LocalSecretStore``): the fake is the TRANSPORT only, so the conflict discipline
    under test is the production code path, not a simulation of it.

    Keyed by (access_token, calendar_id), so two tenants in one test never see each
    other's events."""

    _store: dict[tuple[str, str], dict[str, CalendarEvent]] = {}
    _lock = threading.Lock()

    def _calendar(self, cred: CalendarCredentials) -> dict[str, CalendarEvent]:
        return type(self)._store.setdefault((cred.access_token, cred.calendar_id), {})

    async def busy(
        self, cred: CalendarCredentials, time_min: datetime, time_max: datetime
    ) -> list[tuple[datetime, datetime]]:
        events = await self.events_in(cred, time_min, time_max)
        return [(event.start, event.end) for event in events]

    async def events_in(
        self, cred: CalendarCredentials, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]:
        with self._lock:
            events = list(self._calendar(cred).values())
        return sorted(
            (e for e in events if _overlaps(time_min, time_max, e.start, e.end)),
            key=lambda e: e.start,
        )

    async def get_event(self, cred: CalendarCredentials, event_id: str) -> CalendarEvent | None:
        with self._lock:
            return self._calendar(cred).get(event_id)

    async def insert_event(
        self, cred: CalendarCredentials, *, start: datetime, end: datetime, email: str
    ) -> CalendarEvent:
        event = CalendarEvent(uuid.uuid4().hex, start, end, email)
        with self._lock:
            self._calendar(cred)[event.event_id] = event
        return event

    async def patch_event(
        self, cred: CalendarCredentials, event_id: str, *, start: datetime, end: datetime
    ) -> CalendarEvent | None:
        with self._lock:
            calendar = self._calendar(cred)
            existing = calendar.get(event_id)
            if existing is None:
                return None
            moved = CalendarEvent(event_id, start, end, existing.email)
            calendar[event_id] = moved
            return moved

    async def delete_event(self, cred: CalendarCredentials, event_id: str) -> bool:
        with self._lock:
            return self._calendar(cred).pop(event_id, None) is not None

    @classmethod
    def reset(cls) -> None:
        """Test hook: clear the process-global calendar between tests."""
        with cls._lock:
            cls._store.clear()


class GoogleCalendarApi:
    """The real transport: Google Calendar v3 over httpx. Selected in ``live`` only."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    def _client(self, cred: CalendarCredentials) -> Any:
        import httpx  # noqa: PLC0415 — keeps httpx off the mock-mode import path

        return httpx.AsyncClient(
            base_url=GOOGLE_CALENDAR_API,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {cred.access_token}"},
        )

    @staticmethod
    def _event(payload: dict[str, Any]) -> CalendarEvent | None:
        start = (payload.get("start") or {}).get("dateTime")
        end = (payload.get("end") or {}).get("dateTime")
        if not payload.get("id") or not start or not end:
            # All-day events carry `date` not `dateTime` and are not bookable slots.
            return None
        attendees = payload.get("attendees") or []
        email = next((str(a.get("email")) for a in attendees if a.get("email")), "")
        return CalendarEvent(str(payload["id"]), _parse_instant(start), _parse_instant(end), email)

    @staticmethod
    def _check(response: Any) -> Any:
        if response.status_code >= 400:
            # The provider's body can echo calendar contents; keep it out of the reason.
            raise CalendarUnavailable(f"google_calendar_http_{response.status_code}")
        return response

    async def busy(
        self, cred: CalendarCredentials, time_min: datetime, time_max: datetime
    ) -> list[tuple[datetime, datetime]]:
        async with self._client(cred) as http:
            body = self._check(
                await http.post(
                    "/freeBusy",
                    json={
                        "timeMin": time_min.isoformat(),
                        "timeMax": time_max.isoformat(),
                        "items": [{"id": cred.calendar_id}],
                    },
                )
            ).json()
        periods = ((body.get("calendars") or {}).get(cred.calendar_id) or {}).get("busy") or []
        return [(_parse_instant(p["start"]), _parse_instant(p["end"])) for p in periods]

    async def events_in(
        self, cred: CalendarCredentials, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]:
        async with self._client(cred) as http:
            body = self._check(
                await http.get(
                    f"/calendars/{cred.calendar_id}/events",
                    params={
                        "timeMin": time_min.isoformat(),
                        "timeMax": time_max.isoformat(),
                        # Expand recurrences: a weekly standup must conflict on each
                        # instance, not only on the series' first occurrence.
                        "singleEvents": "true",
                        "orderBy": "startTime",
                        "maxResults": 250,
                    },
                )
            ).json()
        parsed = (self._event(item) for item in body.get("items") or [])
        return [event for event in parsed if event is not None]

    async def get_event(self, cred: CalendarCredentials, event_id: str) -> CalendarEvent | None:
        async with self._client(cred) as http:
            response = await http.get(f"/calendars/{cred.calendar_id}/events/{event_id}")
            if response.status_code == 404:
                return None
            return self._event(self._check(response).json())

    async def insert_event(
        self, cred: CalendarCredentials, *, start: datetime, end: datetime, email: str
    ) -> CalendarEvent:
        async with self._client(cred) as http:
            body = self._check(
                await http.post(
                    f"/calendars/{cred.calendar_id}/events",
                    json={
                        "start": {"dateTime": start.isoformat()},
                        "end": {"dateTime": end.isoformat()},
                        "attendees": [{"email": email}],
                    },
                )
            ).json()
        event = self._event(body)
        if event is None:
            raise CalendarUnavailable("google_calendar_insert_unparseable")
        return event

    async def patch_event(
        self, cred: CalendarCredentials, event_id: str, *, start: datetime, end: datetime
    ) -> CalendarEvent | None:
        async with self._client(cred) as http:
            response = await http.patch(
                f"/calendars/{cred.calendar_id}/events/{event_id}",
                json={
                    "start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": end.isoformat()},
                },
            )
            if response.status_code == 404:
                return None
            return self._event(self._check(response).json())

    async def delete_event(self, cred: CalendarCredentials, event_id: str) -> bool:
        async with self._client(cred) as http:
            response = await http.delete(f"/calendars/{cred.calendar_id}/events/{event_id}")
            if response.status_code == 404:
                return False
            # A cancelled event deletes idempotently; 410 Gone means already cancelled.
            if response.status_code == 410:
                return True
            self._check(response)
            return True


def build_calendar_api() -> CalendarApi:
    """The ONE live/mock decision point for Google Calendar HTTP.

    Mirrors ``build_email_sender`` / ``build_sms_sender``: mock is the default, so no
    test or local run can reach googleapis.com by omission."""
    if get_settings().integration_mode == "live":
        return GoogleCalendarApi()
    return InMemoryCalendarApi()
