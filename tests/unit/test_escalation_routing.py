"""Escalation routing resolution + notification rendering (Architecture §3.5.1 / §3.5.3).

These are the decisions that determine WHO gets paged and WHAT they can act on, and they
are pure functions over the admin's escalation pillar config — so they are pinned here
without a database. The config is read back exactly as the admin PUT it (camelCase wire
keys), which is the part most likely to silently drift.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.notifications.base import EscalationNotification
from app.notifications.email_notifier import EmailNotifier
from app.notifications.escalation_dispatch import (
    has_configured_escalation_contact,
    resolve_routing,
)
from app.notifications.sms_notifier import SmsNotifier


def _notification(**overrides: object) -> EscalationNotification:
    defaults: dict[str, object] = {
        "admin_id": uuid.uuid4(),
        "instance_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "signal": "high_value_lead",
        "gate": "outcome",
        "business_name": "Acme Patios",
        "lead_name": "Dana Lee",
        "lead_contact": "dana@lead.example",
        "lead_intent": "Wants a quote for a 20x20 patio, hoping to start in March",
        "lead_score": 0.91,
    }
    return EscalationNotification(**(defaults | overrides))  # type: ignore[arg-type]


def test_no_config_falls_back_to_email_with_no_contact():
    """An admin who never configured escalation contacts must not resolve to a bogus
    target — the dispatcher needs to see 'nothing configured' so it can owner-fallback."""
    routing = resolve_routing({}, "high_value_lead")
    assert routing.channel == "email"
    assert routing.primary is None and routing.secondary is None
    assert routing.cc_owner_email is False


def test_preferred_channel_selects_the_matching_contact_pair():
    """preferredChannel=sms must page the SMS contacts, not the email ones."""
    routing = resolve_routing(
        {
            "primaryEmail": "owner@acme.example",
            "primarySms": "+15550001111",
            "secondarySms": "+15552223333",
            "preferredChannel": "sms",
        },
        "cannot_answer",
    )
    assert routing.channel == "sms"
    assert routing.primary == "+15550001111"
    assert routing.secondary == "+15552223333"


def test_per_signal_rule_overrides_the_preferred_channel():
    """§3.5.3: routing is a per-signal map — the rule for THIS signal wins over the
    account-wide preference."""
    config = {
        "primaryEmail": "owner@acme.example",
        "primarySms": "+15550001111",
        "preferredChannel": "sms",
        "routing": [{"signal": "high_value_lead", "channel": "email", "ccOwnerEmail": True}],
    }
    hot = resolve_routing(config, "high_value_lead")
    assert hot.channel == "email"
    assert hot.primary == "owner@acme.example"
    assert hot.cc_owner_email is True

    # A signal with no rule of its own still follows the account-wide preference.
    other = resolve_routing(config, "cannot_answer")
    assert other.channel == "sms"
    assert other.primary == "+15550001111"
    assert other.cc_owner_email is False


def test_snake_case_config_reads_identically():
    """The pillar is stored camelCase, but the wire model accepts either spelling — a
    hand-seeded or fixture config must not resolve to 'nothing configured'."""
    routing = resolve_routing(
        {"primary_email": "owner@acme.example", "preferred_channel": "email"}, "cannot_answer"
    )
    assert routing.channel == "email"
    assert routing.primary == "owner@acme.example"


def test_email_body_carries_summary_contact_and_next_step():
    """§3.5.1: the notification must be actionable WITHOUT logging in — so the lead's
    contact info and the recommended next step have to be in the body."""
    body = EmailNotifier().format(_notification())
    assert "Acme Patios" in body
    assert "Dana Lee" in body
    assert "dana@lead.example" in body
    assert "20x20 patio" in body
    assert "0.91" in body
    assert "follow up" in body.lower()


def test_email_body_is_explicit_when_contact_was_not_captured():
    """A blank contact line reads like a rendering bug; say it was not captured."""
    body = EmailNotifier().format(_notification(lead_name=None, lead_contact=None))
    assert "not captured" in body


def test_sms_body_is_condensed_but_still_carries_the_contact():
    """SMS is the condensed form (§3.5.1) — but paging someone without the lead's
    contact info makes the page useless."""
    body = SmsNotifier().format(_notification())
    assert "dana@lead.example" in body
    assert "Acme Patios" in body
    assert "\n" not in body


def test_notifier_config_validation_rejects_a_contact_for_the_wrong_channel():
    """A phone number in the email field (or vice versa) is an actionable config error,
    not something to hand to SES/Twilio."""
    assert EmailNotifier().verify_config("owner@acme.example") is True
    assert EmailNotifier().verify_config("+15550001111") is False
    assert SmsNotifier().verify_config("+15550001111") is True
    assert SmsNotifier().verify_config("owner@acme.example") is False


def test_next_step_is_specific_per_signal():
    """The 'what do I do now' line is the difference between a useful page and noise."""
    at_cap = _notification(signal="budget_exhausted").next_step
    assert "payment method" in at_cap
    assert _notification(signal="explicit_human_request").next_step != at_cap


def test_no_configured_contact_is_reported_honestly():
    """Harmony fix 2026-08-04 item 4: a brand-new account (empty escalation config, the
    JSONB column's own default) must report False, not None-is-falsy-by-luck -- this is
    what the customer-facing ack copy and the dashboard nudge both key off of."""
    assert has_configured_escalation_contact({}) is False
    assert has_configured_escalation_contact(None) is False


def test_any_single_configured_contact_counts():
    """Only one of the four contact fields needs to be set for the platform to actually
    have somewhere real to send an escalation -- preferredChannel/routing rules alone
    (with no contact) do not count."""
    assert has_configured_escalation_contact({"primaryEmail": "owner@acme.example"}) is True
    assert has_configured_escalation_contact({"primarySms": "+15550001111"}) is True
    assert has_configured_escalation_contact({"secondaryEmail": "backup@acme.example"}) is True
    assert has_configured_escalation_contact({"secondarySms": "+15552223333"}) is True
    assert has_configured_escalation_contact({"preferredChannel": "sms"}) is False


def test_email_body_carries_channel_when_quote_and_deep_link():
    """2026-08-18 enrichment: the live email named nothing but a bare UUID while the
    caller's number sat in the session. Channel, time, the triggering message, the
    #ref, and a deep link that opens the exact conversation are all in the body."""
    from datetime import UTC, datetime

    sid = uuid.uuid4()
    body = EmailNotifier().format(
        _notification(
            session_id=sid,
            channel="voice",
            fired_at=datetime(2026, 8, 18, 2, 29, tzinfo=UTC),
            customer_identifier="+16045550107",
            triggering_message="How much is a new water heater installed?",
            dashboard_url=f"https://www.vantagemind.ai/dashboard/conversations?open={sid}",
        )
    )
    assert "Phone call" in body
    assert "2026-08-18 02:29 UTC" in body
    assert "How much is a new water heater installed?" in body
    assert f"#{str(sid)[:8]}" in body
    assert f"https://www.vantagemind.ai/dashboard/conversations?open={sid}" in body


def test_email_contact_falls_back_to_the_transport_identity():
    """A voice/SMS caller with no Lead row enrichment still has a KNOWN number —
    'not captured' is reserved for the genuinely anonymous widget visitor."""
    body = EmailNotifier().format(
        _notification(lead_name=None, lead_contact=None, customer_identifier="+16045550107")
    )
    assert "+16045550107" in body
    assert "Contact: not captured" not in body


def test_sms_body_carries_the_short_ref():
    """The SMS variant previously carried NO conversation handle at all."""
    sid = uuid.uuid4()
    body = SmsNotifier().format(_notification(session_id=sid))
    assert f"#{str(sid)[:8]}" in body
    assert "\n" not in body  # still the condensed single-line form


# --- Round 6 WP-E (F054): after-hours routing ----------------------------------------

_CONTACTS = {
    "primaryEmail": "office@acme.example",
    "primarySms": "+16045550001",
    "routing": [{"signal": "high_value_lead", "channel": "email", "ccOwnerEmail": True}],
}
_HOURS = {
    "enabled": True,
    "weekly": [{"day": d, "start": "09:00", "end": "17:00"} for d in range(5)],
    "afterHours": {"escalation": {"channel": "sms", "contactSms": "+16045559999"}},
}
_OFFICE_TIME = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)  # Tue 10:30 EDT
_NIGHT_TIME = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)  # Tue 23:00 EDT


def test_after_hours_contact_takes_the_page_when_nobody_is_reachable():
    r = resolve_routing(
        _CONTACTS,
        "high_value_lead",
        team_availability=_HOURS,
        timezone="America/Toronto",
        now=_NIGHT_TIME,
    )
    assert r.after_hours is True
    assert (r.channel, r.primary) == ("sms", "+16045559999")
    # The regular SMS contact stays as the backup; the rule's cc-owner choice is kept.
    assert r.secondary == "+16045550001"
    assert r.cc_owner_email is True
    assert r.next_reachable is not None and r.next_reachable.isoformat() == (
        "2026-09-09T09:00:00-04:00"
    )


def test_office_hours_routing_is_unchanged_while_the_team_is_reachable():
    plain = resolve_routing(_CONTACTS, "high_value_lead")
    r = resolve_routing(
        _CONTACTS,
        "high_value_lead",
        team_availability=_HOURS,
        timezone="America/Toronto",
        now=_OFFICE_TIME,
    )
    assert r == plain
    assert r.after_hours is False and r.channel == "email"


def test_the_setting_off_or_without_a_contact_never_reroutes():
    plain = resolve_routing(_CONTACTS, "high_value_lead")
    off = resolve_routing(
        _CONTACTS,
        "high_value_lead",
        team_availability={**_HOURS, "enabled": False},
        timezone="America/Toronto",
        now=_NIGHT_TIME,
    )
    nobody = resolve_routing(
        _CONTACTS,
        "high_value_lead",
        team_availability={**_HOURS, "afterHours": {"escalation": {"channel": "sms"}}},
        timezone="America/Toronto",
        now=_NIGHT_TIME,
    )
    assert off == plain
    assert nobody == plain


def test_after_hours_cc_owner_override_and_same_contact_dedupe():
    hours = {
        **_HOURS,
        "afterHours": {
            "escalation": {
                "channel": "email",
                "contactEmail": "office@acme.example",
                "ccOwnerEmail": False,
            }
        },
    }
    r = resolve_routing(
        _CONTACTS,
        "high_value_lead",
        team_availability=hours,
        timezone="America/Toronto",
        now=_NIGHT_TIME,
    )
    assert r.after_hours is True
    assert (r.channel, r.primary, r.secondary) == ("email", "office@acme.example", None)
    assert r.cc_owner_email is False
