"""Round 6 WP-F: the operating-context stanza states deployment facts the model must
not get wrong — and never gives Luciel working hours."""

from __future__ import annotations

from app.employee.context import OperatingContext
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
