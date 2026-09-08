"""Calendly backend for the scheduling capability (round 6 WP-H, audit F055).

Calendly's contract differs from a calendar: the platform never writes an event.
The owner's availability comes from an EVENT TYPE's available times, and a booking is
made by the CUSTOMER completing a scheduling link. So this backend is honest about
what each tool actually does:

* ``check_availability`` — the event type's open start times in the window.
* ``book_appointment``   — ``ok`` carries the scheduling link for that exact slot; the
  payload says plainly that Calendly confirms the booking when the customer completes
  it. A slot no longer offered is a ``slot_conflict``.
* ``find/reschedule/cancel`` — read the customer's scheduled events by invitee email;
  reschedule hands back the invitee's reschedule link (Calendly moves the booking when
  they complete it); cancel is a real cancellation through the API.

The live/mock seam is the ``CalendlyApi`` Protocol: real HTTP in live mode, an
in-memory fake otherwise, so the discipline under test is the production path.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

from sqlalchemy import select

from app.config import get_settings
from app.models.connection import InstanceConnection
from app.tools.calendar_backend import BOOKING_SEARCH_DAYS, parse_interval
from app.tools.contract import ToolContext

CALENDLY_API = "https://api.calendly.com"
#: Calendly limits an available-times query to a 7-day range.
AVAILABILITY_MAX_DAYS = 7

_T = TypeVar("_T")


class CalendlyUnavailable(RuntimeError):
    """Calendly cannot be used right now — no token, no event type chosen, or the API
    refused. Surfaces as a structured ``failed``; never as an empty availability."""


@dataclass(frozen=True)
class CalendlyCredentials:
    access_token: str
    user_uri: str
    event_type_uri: str


@dataclass(frozen=True)
class CalendlyEventType:
    uri: str
    name: str
    active: bool
    duration_minutes: int
    scheduling_url: str


@dataclass(frozen=True)
class CalendlyEvent:
    """One scheduled event with its (single) invitee."""

    event_uuid: str
    name: str
    start: datetime
    end: datetime
    invitee_email: str
    reschedule_url: str
    cancel_url: str
    status: str = "active"


class CalendlyApi(Protocol):
    async def event_types(
        self, cred: CalendlyCredentials, user_uri: str
    ) -> list[CalendlyEventType]: ...

    async def available_times(
        self, cred: CalendlyCredentials, event_type_uri: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        """(start_time, scheduling_url) for each open slot in [start, end)."""
        ...

    async def scheduled_events(
        self,
        cred: CalendlyCredentials,
        user_uri: str,
        *,
        invitee_email: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendlyEvent]: ...

    async def get_event(
        self, cred: CalendlyCredentials, event_uuid: str
    ) -> CalendlyEvent | None: ...

    async def cancel_event(
        self, cred: CalendlyCredentials, event_uuid: str, reason: str
    ) -> bool: ...


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _booking(event: CalendlyEvent) -> dict[str, Any]:
    return {
        "bookingRef": event.event_uuid,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "email": event.invitee_email,
        "name": event.name,
    }


BOOKING_LINK_NOTE = (
    "Calendly confirms the booking when the customer completes this link — share it "
    "with them; the time is only held once they confirm."
)
RESCHEDULE_LINK_NOTE = (
    "Calendly moves the booking when the customer completes this link — share it with "
    "them; the current time stands until they do."
)


class CalendlyBackend:
    """``CalendarBackend`` over the tenant's connected Calendly account."""

    def __init__(self, api: CalendlyApi | None = None) -> None:
        self._api = api if api is not None else build_calendly_api()

    async def _credentials(self, ctx: ToolContext) -> CalendlyCredentials:
        if ctx.session is None:
            raise CalendlyUnavailable("calendly_session_unavailable")
        from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

        row = (
            await ctx.session.execute(
                select(InstanceConnection.non_secret_config).where(
                    InstanceConnection.admin_id == ctx.admin_id,
                    InstanceConnection.instance_id == ctx.instance_id,
                    InstanceConnection.connection_type == "calendar",
                    InstanceConnection.status != "revoked",
                )
            )
        ).scalar_one_or_none()
        cfg = dict(row) if isinstance(row, dict) else {}
        user_uri = str(cfg.get("userUri") or "")
        event_type_uri = str(cfg.get("eventTypeUri") or "")
        if not user_uri:
            raise CalendlyUnavailable("calendly_user_unknown")
        if not event_type_uri:
            # The owner has no active event type (or has not chosen one) — say so
            # rather than reporting "no availability".
            raise CalendlyUnavailable("calendly_event_type_not_chosen")
        token = await ensure_fresh_access_token(
            ctx.session, ctx.admin_id, ctx.instance_id, connection_type="calendar"
        )
        if not token:
            raise CalendlyUnavailable("calendly_token_unavailable")
        return CalendlyCredentials(
            access_token=token, user_uri=user_uri, event_type_uri=event_type_uri
        )

    async def _guarded(self, ctx: ToolContext, call: Awaitable[_T]) -> _T:
        """A 401/403 from Calendly is a dead grant: flip the row (WP-C) and fail honestly."""
        try:
            return await call
        except CalendlyUnavailable as exc:
            if ctx.session is not None:
                from app.connections import health  # noqa: PLC0415

                await health.flag_if_provider_auth_failure(
                    ctx.session,
                    admin_id=ctx.admin_id,
                    instance_id=ctx.instance_id,
                    connection_type="calendar",
                    reason=str(exc),
                )
            raise

    async def free_busy(self, ctx: ToolContext, window: str) -> list[str]:
        return await self._guarded(ctx, self._free_busy(ctx, window))

    async def _free_busy(self, ctx: ToolContext, window: str) -> list[str]:
        cred = await self._credentials(ctx)
        start, end = parse_interval(window)
        end = min(end, start + timedelta(days=AVAILABILITY_MAX_DAYS))
        times = await self._api.available_times(cred, cred.event_type_uri, start, end)
        return [t.isoformat() for t, _url in sorted(times, key=lambda pair: pair[0])]

    async def book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None:
        return await self._guarded(ctx, self._book(ctx, slot, email))

    async def _book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None:
        cred = await self._credentials(ctx)
        start, end = parse_interval(slot)
        times = await self._api.available_times(
            cred, cred.event_type_uri, start, start + timedelta(minutes=1)
        )
        match = next((url for t, url in times if _as_utc(t) == start), None)
        if match is None:
            return None  # the slot is no longer offered -> failed("slot_conflict")
        return {
            "state": "scheduling_link",
            "schedulingLink": match,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "email": email,
            "note": BOOKING_LINK_NOTE,
        }

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
            if event is None or event.invitee_email.lower() != email.lower():
                return []
            return [_booking(event)]
        now = datetime.now(UTC)
        events = await self._api.scheduled_events(
            cred,
            cred.user_uri,
            invitee_email=email,
            start=now,
            end=now + timedelta(days=BOOKING_SEARCH_DAYS),
        )
        return [_booking(e) for e in events if e.status == "active"]

    async def reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None:
        return await self._guarded(ctx, self._reschedule(ctx, booking_ref, slot))

    async def _reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None:
        cred = await self._credentials(ctx)
        event = await self._api.get_event(cred, booking_ref)
        if event is None or event.status != "active":
            return None
        start, end = parse_interval(slot)
        return {
            **_booking(event),
            "state": "reschedule_link",
            "rescheduleLink": event.reschedule_url,
            "requestedStart": start.isoformat(),
            "requestedEnd": end.isoformat(),
            "note": RESCHEDULE_LINK_NOTE,
        }

    async def cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None:
        return await self._guarded(ctx, self._cancel(ctx, booking_ref))

    async def _cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None:
        cred = await self._credentials(ctx)
        event = await self._api.get_event(cred, booking_ref)
        if event is None or event.status != "active":
            return None
        if not await self._api.cancel_event(cred, booking_ref, "Cancelled via the assistant"):
            return None
        return {**_booking(event), "status": "cancelled"}


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class InMemoryCalendlyApi:
    """Deterministic in-process Calendly for mock/CI. Seeded per event type / user so
    a test controls exactly what the owner's account offers."""

    _availability: dict[str, list[datetime]] = {}
    _events: dict[str, CalendlyEvent] = {}
    _event_types: dict[str, list[CalendlyEventType]] = {}

    @classmethod
    def reset(cls) -> None:
        cls._availability.clear()
        cls._events.clear()
        cls._event_types.clear()

    @classmethod
    def seed_availability(cls, event_type_uri: str, times: list[datetime]) -> None:
        cls._availability[event_type_uri] = [_as_utc(t) for t in times]

    @classmethod
    def seed_event_types(cls, user_uri: str, types: list[CalendlyEventType]) -> None:
        cls._event_types[user_uri] = list(types)

    @classmethod
    def seed_event(cls, event: CalendlyEvent) -> None:
        cls._events[event.event_uuid] = event

    async def event_types(
        self, cred: CalendlyCredentials, user_uri: str
    ) -> list[CalendlyEventType]:
        return list(
            self._event_types.get(user_uri)
            or [
                CalendlyEventType(
                    uri=cred.event_type_uri or "https://api.calendly.com/event_types/mock",
                    name="30 minute meeting",
                    active=True,
                    duration_minutes=30,
                    scheduling_url="https://calendly.com/mock/30min",
                )
            ]
        )

    async def available_times(
        self, cred: CalendlyCredentials, event_type_uri: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        start, end = _as_utc(start), _as_utc(end)
        return [
            (t, f"https://calendly.com/mock/30min/{t.strftime('%Y-%m-%dT%H:%M')}")
            for t in self._availability.get(event_type_uri, [])
            if start <= t < end
        ]

    async def scheduled_events(
        self,
        cred: CalendlyCredentials,
        user_uri: str,
        *,
        invitee_email: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendlyEvent]:
        start, end = _as_utc(start), _as_utc(end)
        return [
            e
            for e in self._events.values()
            if e.invitee_email.lower() == invitee_email.lower() and start <= e.start < end
        ]

    async def get_event(self, cred: CalendlyCredentials, event_uuid: str) -> CalendlyEvent | None:
        return self._events.get(event_uuid)

    async def cancel_event(self, cred: CalendlyCredentials, event_uuid: str, reason: str) -> bool:
        event = self._events.get(event_uuid)
        if event is None or event.status != "active":
            return False
        self._events[event_uuid] = CalendlyEvent(
            event_uuid=event.event_uuid,
            name=event.name,
            start=event.start,
            end=event.end,
            invitee_email=event.invitee_email,
            reschedule_url=event.reschedule_url,
            cancel_url=event.cancel_url,
            status="canceled",
        )
        return True


class CalendlyHttpApi:
    """The real transport: Calendly v2 over httpx. Selected in ``live`` only."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    def _client(self, cred: CalendlyCredentials) -> Any:
        import httpx  # noqa: PLC0415 — keeps httpx off the mock-mode import path

        return httpx.AsyncClient(
            base_url=CALENDLY_API,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {cred.access_token}"},
        )

    @staticmethod
    def _check(response: Any) -> Any:
        if response.status_code >= 400:
            # The body can echo the owner's calendar; keep it out of the reason.
            raise CalendlyUnavailable(f"calendly_http_{response.status_code}")
        return response

    @staticmethod
    def _uuid_of(uri: str) -> str:
        return uri.rstrip("/").rsplit("/", 1)[-1]

    async def event_types(
        self, cred: CalendlyCredentials, user_uri: str
    ) -> list[CalendlyEventType]:
        async with self._client(cred) as http:
            body = self._check(
                await http.get(
                    "/event_types", params={"user": user_uri, "active": "true", "count": 50}
                )
            ).json()
        out: list[CalendlyEventType] = []
        for item in body.get("collection") or []:
            if not isinstance(item, dict) or not item.get("uri"):
                continue
            out.append(
                CalendlyEventType(
                    uri=str(item["uri"]),
                    name=str(item.get("name") or ""),
                    active=bool(item.get("active", True)),
                    duration_minutes=int(item.get("duration") or 0),
                    scheduling_url=str(item.get("scheduling_url") or ""),
                )
            )
        return out

    async def available_times(
        self, cred: CalendlyCredentials, event_type_uri: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        async with self._client(cred) as http:
            body = self._check(
                await http.get(
                    "/event_type_available_times",
                    params={
                        "event_type": event_type_uri,
                        "start_time": _as_utc(start).isoformat(),
                        "end_time": _as_utc(end).isoformat(),
                    },
                )
            ).json()
        out: list[tuple[datetime, str]] = []
        for item in body.get("collection") or []:
            if not isinstance(item, dict) or item.get("status") != "available":
                continue
            raw = item.get("start_time")
            if not raw:
                continue
            out.append(
                (
                    _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))),
                    str(item.get("scheduling_url") or ""),
                )
            )
        return out

    async def _with_invitee(self, http: Any, resource: dict[str, Any]) -> CalendlyEvent | None:
        uri = str(resource.get("uri") or "")
        start, end = resource.get("start_time"), resource.get("end_time")
        if not uri or not start or not end:
            return None
        event_uuid = self._uuid_of(uri)
        invitees = self._check(
            await http.get(f"/scheduled_events/{event_uuid}/invitees", params={"count": 1})
        ).json()
        first = next((i for i in (invitees.get("collection") or []) if isinstance(i, dict)), {})
        return CalendlyEvent(
            event_uuid=event_uuid,
            name=str(resource.get("name") or ""),
            start=_as_utc(datetime.fromisoformat(str(start).replace("Z", "+00:00"))),
            end=_as_utc(datetime.fromisoformat(str(end).replace("Z", "+00:00"))),
            invitee_email=str(first.get("email") or ""),
            reschedule_url=str(first.get("reschedule_url") or ""),
            cancel_url=str(first.get("cancel_url") or ""),
            status=str(resource.get("status") or "active"),
        )

    async def scheduled_events(
        self,
        cred: CalendlyCredentials,
        user_uri: str,
        *,
        invitee_email: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendlyEvent]:
        async with self._client(cred) as http:
            body = self._check(
                await http.get(
                    "/scheduled_events",
                    params={
                        "user": user_uri,
                        "invitee_email": invitee_email,
                        "status": "active",
                        "min_start_time": _as_utc(start).isoformat(),
                        "max_start_time": _as_utc(end).isoformat(),
                        "count": 20,
                    },
                )
            ).json()
            events: list[CalendlyEvent] = []
            for item in body.get("collection") or []:
                if isinstance(item, dict):
                    event = await self._with_invitee(http, item)
                    if event is not None:
                        events.append(event)
        return events

    async def get_event(self, cred: CalendlyCredentials, event_uuid: str) -> CalendlyEvent | None:
        async with self._client(cred) as http:
            response = await http.get(f"/scheduled_events/{event_uuid}")
            if response.status_code == 404:
                return None
            resource = self._check(response).json().get("resource") or {}
            return await self._with_invitee(http, resource)

    async def cancel_event(self, cred: CalendlyCredentials, event_uuid: str, reason: str) -> bool:
        async with self._client(cred) as http:
            response = await http.post(
                f"/scheduled_events/{event_uuid}/cancellation", json={"reason": reason}
            )
            if response.status_code in (404, 409):
                return False
            self._check(response)
            return True


def build_calendly_api() -> CalendlyApi:
    """The ONE live/mock decision point for Calendly HTTP (mirrors build_calendar_api)."""
    if get_settings().integration_mode == "live":
        return CalendlyHttpApi()
    return InMemoryCalendlyApi()


def new_mock_event(
    *,
    start: datetime,
    invitee_email: str,
    minutes: int = 30,
    name: str = "30 minute meeting",
) -> CalendlyEvent:
    """Test helper: a seeded scheduled event with mock reschedule/cancel links."""
    event_uuid = uuid.uuid4().hex
    start = _as_utc(start)
    return CalendlyEvent(
        event_uuid=event_uuid,
        name=name,
        start=start,
        end=start + timedelta(minutes=minutes),
        invitee_email=invitee_email,
        reschedule_url=f"https://calendly.com/reschedulings/{event_uuid}",
        cancel_url=f"https://calendly.com/cancellations/{event_uuid}",
    )
