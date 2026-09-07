"""When the HUMAN team is reachable — never when Luciel works (round 6 WP-E, F054).

Luciel is on duty around the clock on every channel; nothing in this module changes
what it answers or when. The owner may optionally describe when their people can pick
up a hand-off, and that description is used for exactly two things:

* honest follow-up wording — "someone will follow up with you tomorrow at 9:00 am"
  instead of a "shortly" that a Sunday-night visitor would rightly doubt;
* after-hours escalation routing — a hot lead at 2 am can page the on-call person
  instead of the office inbox (``escalation_dispatch.resolve_routing``).

Everything is a pure function of the stored setting and a clock, so it is pinned
without a database. Windows are read in the owner's IANA timezone; DST is whatever
``zoneinfo`` says for that instant; an overnight window (22:00 → 06:00) wraps past
midnight; a closure date beats the weekly grid for the windows that START on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: How far ahead ``next_reachable`` looks before giving up (a fortnight of closures
#: is the realistic worst case; beyond that the honest phrase is "when they're next
#: available").
LOOKAHEAD_DAYS = 14

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Window:
    """One weekly window: ``day`` 0=Monday … 6=Sunday, minutes since local midnight.

    ``end`` may be 1440 ("24:00", until the end of the day); ``end < start`` means the
    window wraps past midnight into the next day."""

    day: int
    start: int
    end: int

    @property
    def overnight(self) -> bool:
        return self.end < self.start


@dataclass(frozen=True)
class AfterHoursEscalation:
    """Who an escalation reaches while the team is unreachable (routing only)."""

    channel: str  # "email" | "sms"
    contact_email: str | None
    contact_sms: str | None
    cc_owner_email: bool | None

    @property
    def contact(self) -> str | None:
        return self.contact_sms if self.channel == "sms" else self.contact_email


@dataclass(frozen=True)
class TeamAvailability:
    enabled: bool
    timezone: str
    weekly: tuple[Window, ...]
    closures: frozenset[date]
    after_hours: AfterHoursEscalation | None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def valid_timezone(name: object) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    try:
        ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_hhmm(raw: object) -> int | None:
    """``"09:30"`` → 570 minutes; ``"24:00"`` → 1440 (end of day). None when malformed."""
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if minutes > 59 or hours > 24 or (hours == 24 and minutes != 0):
        return None
    return hours * 60 + minutes


def _text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _items(value: object) -> list[object]:
    """A stored list, or nothing — a blob hand-edited into a string is not a grid."""
    return list(value) if isinstance(value, (list, tuple)) else []


def parse(raw: Mapping[str, object] | None, timezone: str | None) -> TeamAvailability | None:
    """Read the stored wire blob back. Returns None when unset, off, or unreadable —
    every consumer then behaves exactly as it did before the setting existed."""
    if not isinstance(raw, Mapping) or not raw.get("enabled"):
        return None
    tz_name = (timezone or "").strip()
    if not valid_timezone(tz_name):
        return None
    weekly: list[Window] = []
    for item in _items(raw.get("weekly")):
        if not isinstance(item, Mapping):
            continue
        day = item.get("day")
        start = parse_hhmm(item.get("start"))
        end = parse_hhmm(item.get("end"))
        if not isinstance(day, int) or not 0 <= day <= 6 or start is None or end is None:
            continue
        if start == end or start >= MINUTES_PER_DAY:
            continue  # a zero-length window is nothing, not "all day"
        weekly.append(Window(day=day, start=start, end=end))
    closures: set[date] = set()
    for item in _items(raw.get("closures")):
        value = item.get("date") if isinstance(item, Mapping) else item
        try:
            closures.add(date.fromisoformat(str(value)))
        except (TypeError, ValueError):
            continue
    after_hours: AfterHoursEscalation | None = None
    ah = raw.get("afterHours")
    esc = ah.get("escalation") if isinstance(ah, Mapping) else None
    if isinstance(esc, Mapping) and esc.get("channel") in ("email", "sms"):
        cc = esc.get("ccOwnerEmail")
        candidate = AfterHoursEscalation(
            channel=str(esc["channel"]),
            contact_email=_text(esc.get("contactEmail")),
            contact_sms=_text(esc.get("contactSms")),
            cc_owner_email=bool(cc) if cc is not None else None,
        )
        # An after-hours rule with nobody on the other end routes nowhere — drop it so
        # the regular contacts keep being paged rather than a blank address.
        if candidate.contact:
            after_hours = candidate
    return TeamAvailability(
        enabled=True,
        timezone=tz_name,
        weekly=tuple(weekly),
        closures=frozenset(closures),
        after_hours=after_hours,
    )


def _local(ta: TeamAvailability, now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ta.tz)


def _covering(ta: TeamAvailability, local: datetime) -> list[tuple[Window, date]]:
    """The windows covering the local instant, each with the local date it STARTED on
    (an overnight window's tail belongs to the day before)."""
    minute = local.hour * 60 + local.minute
    weekday = local.weekday()
    out: list[tuple[Window, date]] = []
    for w in ta.weekly:
        if w.day == weekday and (
            (w.start <= minute < w.end) if not w.overnight else minute >= w.start
        ):
            out.append((w, local.date()))
        elif w.overnight and w.day == (weekday - 1) % 7 and minute < w.end:
            out.append((w, local.date() - timedelta(days=1)))
    return out


def team_reachable_now(ta: TeamAvailability | None, now: datetime | None = None) -> bool:
    """True when a person is reachable at ``now``. An unset/off setting is True as
    well: the platform makes no claim, and callers keep today's generic wording."""
    if ta is None:
        return True
    local = _local(ta, now)
    return any(started_on not in ta.closures for _, started_on in _covering(ta, local))


def next_reachable(ta: TeamAvailability, now: datetime | None = None) -> datetime | None:
    """The next instant (in the owner's zone) a person becomes reachable, or None when
    no window opens within ``LOOKAHEAD_DAYS``. Returns ``now`` itself when reachable."""
    local = _local(ta, now)
    if team_reachable_now(ta, local):
        return local
    for offset in range(LOOKAHEAD_DAYS + 1):
        day = local.date() + timedelta(days=offset)
        if day in ta.closures:
            continue
        for start in sorted(w.start for w in ta.weekly if w.day == day.weekday()):
            candidate = datetime.combine(
                day, time(hour=start // 60, minute=start % 60), tzinfo=ta.tz
            )
            if candidate > local:
                return candidate
    return None


def _clock(moment: datetime) -> str:
    hour12 = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    zone = moment.tzname() or ""
    return f"{hour12}:{moment.minute:02d} {suffix}" + (f" ({zone})" if zone else "")


def describe_next_window(ta: TeamAvailability, now: datetime | None = None) -> str:
    """A phrase for a follow-up promise: "today at 2:00 pm (EDT)", "tomorrow at
    9:00 am (PST)", "on Monday at 9:00 am (CET)", "on 27 Dec at 9:00 am (GMT)", or
    "when they're next available" when nothing opens within the lookahead."""
    local = _local(ta, now)
    opens = next_reachable(ta, local)
    if opens is None:
        return "when they're next available"
    delta_days = (opens.date() - local.date()).days
    when = _clock(opens)
    if delta_days == 0:
        return f"today at {when}"
    if delta_days == 1:
        return f"tomorrow at {when}"
    if delta_days < 7:
        return f"on {opens.strftime('%A')} at {when}"
    return f"on {opens.day} {opens.strftime('%b')} at {when}"


def follow_up_phrase(ta: TeamAvailability | None, now: datetime | None = None) -> str | None:
    """None while the team is reachable (or the setting is off) → callers keep their
    existing wording; otherwise the honest "when" for a hand-off line."""
    if ta is None or team_reachable_now(ta, now):
        return None
    return describe_next_window(ta, now)
