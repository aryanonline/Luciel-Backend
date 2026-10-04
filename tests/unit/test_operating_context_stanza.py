"""Round 6 WP-F: the operating-context stanza states deployment facts the model must
not get wrong — and never gives Luciel working hours."""

from __future__ import annotations

from datetime import UTC, datetime

from app.employee.context import OperatingContext, business_timezone, local_today_phrase
from app.runtime.input_safety import build_system_prompt, compose_operating_context_stanza


def _ctx(**over: object) -> OperatingContext:
    base = dict(
        business_short_name="Acme Patios",
        live_channels=("widget", "sms"),
        escalation_channel="sms",
        escalation_configured=True,
        team_reachable_now=None,
        next_reachable_phrase=None,
    )
    base.update(over)
    return OperatingContext(**base)  # type: ignore[arg-type]


def test_none_keeps_the_prompt_byte_identical():
    assert compose_operating_context_stanza(None) == ""
    assert build_system_prompt("ctx") == build_system_prompt("ctx", operating_context=None)


def test_the_stanza_names_the_business_channels_and_notification_path():
    stanza = compose_operating_context_stanza(_ctx())
    assert "You work for Acme Patios." in stanza
    assert "available to customers around the clock" in stanza
    assert "never say you are closed, offline, or unavailable" in stanza
    assert "reach the business right now on: the website chat, text message." in stanza
    assert "notified by text message" in stanza
    assert "never promise an immediate or real-time human reply" in stanza
    assert "If a capability is not among your tools, you cannot do it" in stanza
    # Nothing about a person is claimed when the owner never described the team.
    assert "reachable right now" not in stanza


def test_unconfigured_escalation_is_said_plainly():
    stanza = compose_operating_context_stanza(
        _ctx(escalation_configured=False, escalation_channel="email", business_short_name=None)
    )
    assert "You work for this business." in stanza
    assert "notified by email to the account owner (no dedicated contact is set)" in stanza


def test_team_reachability_is_about_people_only():
    reachable = compose_operating_context_stanza(_ctx(team_reachable_now=True))
    assert "Someone on the team is reachable right now." in reachable
    away = compose_operating_context_stanza(
        _ctx(team_reachable_now=False, next_reachable_phrase="tomorrow at 9:00 am (EDT)")
    )
    assert "Nobody on the team is reachable right now; a person is next expected" in away
    assert "next expected tomorrow at 9:00 am (EDT)." in away
    # Luciel itself stays around the clock in the same stanza.
    assert "available to customers around the clock" in away


def test_the_stanza_sits_under_the_invariants_and_above_personality():
    prompt = build_system_prompt("Acme sells patios", operating_context=_ctx())
    invariants = prompt.index("AI IDENTITY")
    context = prompt.index("Operating context")
    personality = prompt.index("Personality")
    business = prompt.index("Acme sells patios")
    assert invariants < context < personality < business


# --- the business's clock (round 7 WP-2 remainder, item 11) ------------------------- #
# The prompt carried no date and no zone, so "3pm Tuesday" had no anchor: the model
# could not turn a weekday into a date, and it read every tool time back as UTC.


def test_the_stanza_names_the_business_zone_and_todays_date_there():
    stanza = compose_operating_context_stanza(
        _ctx(timezone="America/Toronto", local_today="Tuesday 06 October 2026")
    )
    assert (
        "The business keeps time in America/Toronto; today there is Tuesday 06 October 2026."
    ) in stanza
    assert "A time a customer gives is in that zone unless they name another." in stanza
    assert "a date or time you pass without a UTC offset is read in that zone" in stanza
    assert "Pass a slot exactly as check_availability returned it" in stanza
    assert "in the business's local time — never as UTC" in stanza
    # One clock line, never both.
    assert "has not set a time zone" not in stanza


def test_without_a_zone_the_date_is_utc_and_times_are_said_to_be_utc():
    stanza = compose_operating_context_stanza(_ctx(local_today="Wednesday 07 October 2026"))
    assert "Today's date is Wednesday 07 October 2026 (UTC)." in stanza
    assert "The business has not set a time zone" in stanza
    assert "say that it is UTC" in stanza
    assert "keeps time in" not in stanza


def test_a_context_built_without_a_clock_adds_no_clock_line():
    stanza = compose_operating_context_stanza(_ctx())
    assert "keeps time in" not in stanza
    assert "Today's date" not in stanza


def test_todays_date_is_the_business_date_not_the_utc_date():
    # 02:00 UTC on the 7th is still 10 pm on Tuesday the 6th in Toronto.
    late_evening = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    assert local_today_phrase("America/Toronto", late_evening) == "Tuesday 06 October 2026"
    assert local_today_phrase(None, late_evening) == "Wednesday 07 October 2026"
    # A naive clock reading is UTC, as everywhere else in the runtime.
    assert local_today_phrase("America/Toronto", datetime(2026, 10, 7, 2, 0)) == (
        "Tuesday 06 October 2026"
    )


def test_only_a_real_iana_zone_is_handed_to_the_model_and_the_tools():
    assert business_timezone(" America/Toronto ") == "America/Toronto"
    assert business_timezone("Mars/Olympus_Mons") is None
    assert business_timezone("") is None
    assert business_timezone(None) is None
