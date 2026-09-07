"""Round 6 WP-E (F054): team availability is about the HUMAN team, read in the owner's
zone, and never touches what Luciel answers. Pure functions over the stored blob."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.runtime.team_availability import (
    describe_next_window,
    follow_up_phrase,
    next_reachable,
    parse,
    parse_hhmm,
    team_reachable_now,
    valid_timezone,
)

TORONTO = "America/Toronto"

OFFICE_HOURS = {
    "enabled": True,
    "weekly": [{"day": d, "start": "09:00", "end": "17:00"} for d in range(5)],
    "closures": [{"date": "2026-12-25", "label": "Christmas"}],
    "afterHours": {"escalation": {"channel": "sms", "contactSms": "+16045550000"}},
}


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --- parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("09:00", 540), ("17:30", 1050), ("00:00", 0), ("24:00", 1440), (" 9:05 ", 545)],
)
def test_hhmm_parses_24_hour_times(raw, expected):
    assert parse_hhmm(raw) == expected


@pytest.mark.parametrize("raw", ["9", "25:00", "24:01", "09:60", "9am", "", None, "09:00:00"])
def test_hhmm_refuses_anything_else(raw):
    assert parse_hhmm(raw) is None


def test_valid_timezone_is_an_iana_name():
    assert valid_timezone(TORONTO)
    assert valid_timezone("Europe/Berlin")
    assert not valid_timezone("EST5EDT-ish")
    assert not valid_timezone("")
    assert not valid_timezone(None)


@pytest.mark.parametrize(
    ("raw", "timezone"),
    [
        (None, TORONTO),
        ({}, TORONTO),
        ({**OFFICE_HOURS, "enabled": False}, TORONTO),
        (OFFICE_HOURS, None),
        (OFFICE_HOURS, "Mars/Olympus_Mons"),
    ],
)
def test_unset_off_or_unreadable_parses_to_none(raw, timezone):
    """None is the 'no claim' value: every consumer then behaves as before the setting."""
    assert parse(raw, timezone) is None
    assert team_reachable_now(parse(raw, timezone)) is True
    assert follow_up_phrase(parse(raw, timezone)) is None


def test_parse_drops_malformed_windows_and_a_contactless_after_hours_rule():
    ta = parse(
        {
            "enabled": True,
            "weekly": [
                {"day": 0, "start": "09:00", "end": "17:00"},
                {"day": 7, "start": "09:00", "end": "17:00"},  # no such day
                {"day": 1, "start": "09:00", "end": "09:00"},  # zero length
                {"day": 2, "start": "nine", "end": "17:00"},
                "not a window",
            ],
            "closures": [{"date": "2026-12-25"}, {"date": "yesterday"}, "2026-01-01"],
            "afterHours": {"escalation": {"channel": "email"}},  # nobody on the other end
        },
        TORONTO,
    )
    assert ta is not None
    assert [(w.day, w.start, w.end) for w in ta.weekly] == [(0, 540, 1020)]
    assert {d.isoformat() for d in ta.closures} == {"2026-12-25", "2026-01-01"}
    assert ta.after_hours is None


def test_parse_keeps_an_after_hours_rule_with_a_contact():
    ta = parse(OFFICE_HOURS, TORONTO)
    assert ta is not None and ta.after_hours is not None
    assert ta.after_hours.channel == "sms"
    assert ta.after_hours.contact == "+16045550000"
    assert ta.after_hours.cc_owner_email is None


# --- reachability ----------------------------------------------------------------


def test_weekday_window_in_the_owners_zone():
    ta = parse(OFFICE_HOURS, TORONTO)
    # Tuesday 2026-09-08 14:30 UTC = 10:30 EDT → reachable
    assert team_reachable_now(ta, _utc("2026-09-08T14:30:00"))
    # Tuesday 2026-09-08 22:30 UTC = 18:30 EDT → not
    assert not team_reachable_now(ta, _utc("2026-09-08T22:30:00"))
    # Saturday → not
    assert not team_reachable_now(ta, _utc("2026-09-12T15:00:00"))


def test_dst_is_the_zones_business_not_ours():
    """2026-03-08 is the US/Canada spring-forward: 09:00 local is 13:00 UTC (EST→EDT)."""
    ta = parse(
        {
            "enabled": True,
            "weekly": [{"day": d, "start": "09:00", "end": "17:00"} for d in range(7)],
        },
        "America/New_York",
    )
    assert not team_reachable_now(ta, _utc("2026-03-08T12:30:00"))  # 07:30 EST
    assert team_reachable_now(ta, _utc("2026-03-08T13:30:00"))  # 09:30 EDT
    opens = next_reachable(ta, _utc("2026-03-08T12:30:00"))
    assert opens is not None and opens.isoformat() == "2026-03-08T09:00:00-04:00"


def test_overnight_window_wraps_past_midnight_and_belongs_to_the_day_it_started():
    ta = parse(
        {
            "enabled": True,
            "weekly": [{"day": 4, "start": "22:00", "end": "06:00"}],  # Friday night shift
            "closures": [{"date": "2026-09-12"}],  # the Saturday — NOT the Friday
        },
        "Europe/Berlin",
    )
    assert team_reachable_now(ta, _utc("2026-09-11T21:30:00"))  # Fri 23:30 CEST
    assert team_reachable_now(ta, _utc("2026-09-12T01:30:00"))  # Sat 03:30 CEST (tail)
    assert not team_reachable_now(ta, _utc("2026-09-12T05:30:00"))  # Sat 07:30 CEST
    assert not team_reachable_now(ta, _utc("2026-09-10T21:30:00"))  # Thu night


def test_a_closure_date_blocks_the_windows_that_start_on_it():
    ta = parse(OFFICE_HOURS, TORONTO)
    # Friday 2026-12-25 15:00 UTC = 10:00 EST — a workday by the grid, closed by the list.
    assert not team_reachable_now(ta, _utc("2026-12-25T15:00:00"))
    opens = next_reachable(ta, _utc("2026-12-25T15:00:00"))
    assert opens is not None and opens.isoformat() == "2026-12-28T09:00:00-05:00"


def test_end_of_day_window_reads_until_midnight():
    ta = parse({"enabled": True, "weekly": [{"day": 0, "start": "09:00", "end": "24:00"}]}, "UTC")
    assert team_reachable_now(ta, _utc("2026-09-07T23:59:00"))  # Monday
    assert not team_reachable_now(ta, _utc("2026-09-08T00:00:00"))  # Tuesday


# --- wording ---------------------------------------------------------------------


def test_follow_up_phrase_is_none_while_reachable_and_names_the_next_window_otherwise():
    ta = parse(OFFICE_HOURS, TORONTO)
    assert follow_up_phrase(ta, _utc("2026-09-08T14:30:00")) is None
    # Tuesday 18:30 EDT → tomorrow 9:00 am
    assert follow_up_phrase(ta, _utc("2026-09-08T22:30:00")) == "tomorrow at 9:00 am (EDT)"
    # Tuesday 06:00 EDT → later today
    assert describe_next_window(ta, _utc("2026-09-08T10:00:00")) == "today at 9:00 am (EDT)"
    # Friday 18:30 EDT → on Monday
    assert describe_next_window(ta, _utc("2026-09-11T22:30:00")) == "on Monday at 9:00 am (EDT)"


def test_a_long_closure_names_the_date_and_no_window_says_so_honestly():
    ta = parse(
        {
            "enabled": True,
            "weekly": [{"day": d, "start": "09:00", "end": "17:00"} for d in range(7)],
            "closures": [{"date": f"2026-12-{day:02d}"} for day in range(20, 32)],
        },
        "Europe/London",
    )
    assert describe_next_window(ta, _utc("2026-12-20T12:00:00")) == "on 1 Jan at 9:00 am (GMT)"
    nothing = parse({"enabled": True, "weekly": []}, TORONTO)
    assert (
        describe_next_window(nothing, _utc("2026-09-08T22:30:00")) == "when they're next available"
    )
    assert next_reachable(nothing, _utc("2026-09-08T22:30:00")) is None
