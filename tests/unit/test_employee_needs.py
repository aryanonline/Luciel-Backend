"""Round 6 WP-F: the employee's needs and capabilities are pure rules over a snapshot.

One test per rule, no database. The doctrine pinned here: ``ready`` only for a live
connection; Luciel is on duty around the clock regardless of any state; the owner's
team availability never appears as a capability of Luciel's; unset optional settings
are ``info``, never ``attention``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.employee.status import (
    ON_DUTY,
    Snapshot,
    derive_capabilities,
    derive_needs,
)

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)


def _snap(**overrides: object) -> Snapshot:
    base = Snapshot(
        assistant_name="Aurora",
        business_short_name="Acme",
        luciel_state="active",
        channels_enabled={"widget": True, "email": False, "sms": False},
        channel_status={},
        tools_enabled={"schedule_callback": True, "push_to_crm": False},
        tool_status={},
        tool_provider_available={},
        knowledge_sources=3,
        knowledge_errors=0,
        knowledge_oldest_sync=NOW - timedelta(days=1),
        escalation_configured=True,
        escalation_bouncing=False,
        escalation_unconfirmed=False,
        conversations_used=12,
        free_allowance=50,
        billed_blocks=0,
        billable=True,
        at_cap=False,
        near_next_block=False,
        billing_state="free_cap",
        dunning_state="none",
        period_resets_at=NOW + timedelta(days=20),
        leads_failed_crm=0,
        callbacks_abandoned_recent=0,
        team_availability_set=True,
        team_reachable_now=True,
        next_reachable=None,
        timezone="America/Toronto",
        allowed_origins_set=True,
        daily_brief_enabled=True,
        now=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _codes(snap: Snapshot) -> list[str]:
    return [n.code for n in derive_needs(snap)]


def _by_id(snap: Snapshot) -> dict[str, tuple[str, str | None]]:
    return {c.id: (c.state, c.detail) for c in derive_capabilities(snap)}


def test_a_healthy_deployment_needs_nothing_and_is_ready_everywhere():
    snap = _snap()
    assert _codes(snap) == []
    caps = _by_id(snap)
    assert caps["widget"] == ("ready", "on your website")
    assert caps["schedule_callback"] == ("ready", None)  # needs no connection
    assert caps["knowledge"] == ("ready", "3 source(s)")
    assert caps["escalation"] == ("ready", None)
    assert caps["budget"] == ("ready", "12 of 50 free used")
    assert caps["email"] == ("off", None)
    assert ON_DUTY == "around the clock"


def test_email_on_without_a_mailbox_is_the_named_need():
    snap = _snap(channels_enabled={"widget": True, "email": True}, channel_status={})
    needs = derive_needs(snap)
    assert needs[0].code == "email_no_mailbox"
    assert needs[0].severity == "attention"
    assert needs[0].title == "Email channel is on but no mailbox is connected"
    assert _by_id(snap)["email"] == ("attention", "nothing connected")


def test_a_channel_on_with_a_broken_connection_names_the_state():
    snap = _snap(
        channels_enabled={"widget": True, "sms": True},
        channel_status={"sms": "pending_carrier_registration"},
    )
    assert _codes(snap) == ["channel_attention:sms"]
    assert derive_needs(snap)[0].title == "Text messages: carrier registration pending"
    assert _by_id(snap)["sms"] == ("attention", "carrier registration pending")


def test_dormant_sends_the_owner_to_billing_never_to_a_connect_button():
    """``dormant`` is billing-suspended (Legal §A3): Connect refuses to re-arm it
    (DormantConnection), so "dormant — reconnect" pointed the owner at a button that
    fails. The need says what the Configure page says — add a card and it resumes —
    and links the Billing page (round 7 WP-3a). Channels (sms_sender / email_sender
    are action-tool connections) and tools alike."""
    snap = _snap(
        channels_enabled={"widget": True, "sms": True},
        channel_status={"sms": "dormant"},
        tools_enabled={"push_to_crm": True},
        tool_status={"push_to_crm": "dormant"},
        tool_provider_available={"push_to_crm": True},
    )
    needs = {n.code: n for n in derive_needs(snap)}
    assert set(needs) == {"channel_dormant:sms", "tool_dormant:push_to_crm"}
    for need in needs.values():
        assert need.severity == "attention"
        assert need.detail == "Add a card on the Billing page and it resumes."
        assert need.href == "/dashboard/billing"
        assert "reconnect" not in need.title.lower()
    assert needs["channel_dormant:sms"].title == "Text messages: paused until a card is on file"
    caps = _by_id(snap)
    assert caps["sms"] == ("attention", "paused until a card is on file")
    assert caps["push_to_crm"] == ("attention", "paused until a card is on file")


def test_a_connected_channel_is_ready_and_a_disconnected_one_is_never_ready():
    connected = _snap(
        channels_enabled={"widget": True, "sms": True}, channel_status={"sms": "connected"}
    )
    assert _by_id(connected)["sms"] == ("ready", None)
    assert _codes(connected) == []
    for status in ("unconfigured", "not_connected", "revoked", "expired", "error"):
        snap = _snap(channels_enabled={"widget": True, "sms": True}, channel_status={"sms": status})
        assert _by_id(snap)["sms"][0] == "attention", status


def test_tools_follow_their_connection_and_an_unavailable_provider_is_info():
    on = _snap(
        tools_enabled={"push_to_crm": True, "book_appointment": True, "lookup_record": True},
        tool_status={"push_to_crm": "connected", "book_appointment": None, "lookup_record": None},
        tool_provider_available={
            "push_to_crm": True,
            "book_appointment": True,
            "lookup_record": False,
        },
    )
    caps = _by_id(on)
    assert caps["push_to_crm"] == ("ready", None)
    assert caps["book_appointment"] == ("attention", "nothing connected")
    assert caps["lookup_record"] == ("unavailable", "not available yet")
    needs = {n.code: n.severity for n in derive_needs(on)}
    assert needs == {
        "tool_disconnected:book_appointment": "attention",
        "tool_unavailable:lookup_record": "info",
    }


def test_escalation_contact_rules():
    assert _codes(_snap(escalation_configured=False)) == ["no_escalation_contact"]
    assert _codes(_snap(escalation_bouncing=True)) == ["escalation_bouncing"]
    assert _codes(_snap(escalation_unconfirmed=True)) == ["escalation_unconfirmed"]
    assert derive_needs(_snap(escalation_unconfirmed=True))[0].severity == "info"
    assert _by_id(_snap(escalation_configured=False))["escalation"] == (
        "attention",
        "no contact set",
    )


def test_knowledge_rules():
    assert _codes(_snap(knowledge_sources=0)) == ["knowledge_empty"]
    assert _codes(_snap(knowledge_errors=2)) == ["knowledge_sync_error"]
    stale = _snap(knowledge_oldest_sync=NOW - timedelta(days=9))
    assert _codes(stale) == ["knowledge_stale"]
    assert derive_needs(stale)[0].severity == "info"
    assert _by_id(stale)["knowledge"] == ("attention", "not synced lately")
    uploads_only = _snap(knowledge_oldest_sync=None)
    assert _codes(uploads_only) == []


def test_budget_rules_never_stop_luciel_only_ask_for_a_card():
    assert _codes(_snap(at_cap=True, conversations_used=50)) == ["at_cap"]
    assert _by_id(_snap(at_cap=True))["budget"] == ("attention", "at capacity")
    near = _snap(conversations_used=41)
    assert _codes(near) == ["near_cap"]
    assert derive_needs(near)[0].severity == "info"
    payg = _snap(billing_state="payg_enabled", billed_blocks=2, near_next_block=True)
    assert _codes(payg) == ["near_next_block"]
    assert _by_id(payg)["budget"] == ("ready", "12 of 50 free used; 2 paid block(s)")
    assert _codes(_snap(dunning_state="retrying")) == ["payment_failed"]
    assert _codes(_snap(billable=False, billed_blocks=1, billing_state="free_cap")) == [
        "card_removed"
    ]
    assert _codes(_snap(billable=False, billed_blocks=0)) == []


def test_leads_callbacks_and_pause():
    assert _codes(_snap(leads_failed_crm=2)) == ["leads_not_in_crm"]
    assert derive_needs(_snap(leads_failed_crm=2))[0].href == "/dashboard/leads"
    assert _codes(_snap(callbacks_abandoned_recent=1)) == ["callbacks_abandoned"]
    paused = derive_needs(_snap(luciel_state="paused"))
    assert paused[0].code == "paused" and paused[0].severity == "attention"


def test_optional_settings_are_info_and_sorted_after_attention():
    snap = _snap(
        team_availability_set=False, allowed_origins_set=False, escalation_configured=False
    )
    needs = derive_needs(snap)
    assert [n.code for n in needs] == [
        "no_escalation_contact",
        "team_availability_unset",
        "allowed_origins_unset",
    ]
    assert needs[1].severity == "info"
    assert "around the clock" in needs[1].detail
    # Team availability is never a capability of Luciel's.
    assert "team" not in {c.id for c in derive_capabilities(snap)}
