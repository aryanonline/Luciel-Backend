"""The always-on ``hand_to_team`` cognition tool — surface invariants (round 7 WP-2, item 2).

Luciel decides when to escalate (Vision §3.4). The model's own judgment reaches the team
through ONE door, ``hand_to_team``, which is cognition rather than a catalog tool: it is
never on the Configure surface, never needs an authorization row or a connection, and is
described to the model on every turn. Its signal has a wire value and owner-facing
labels, and the owner's page carries the model's reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.api.v1.admin.luciel import DEFAULT_TOOLS
from app.notifications.base import EscalationNotification
from app.notifications.email_notifier import EmailNotifier
from app.notifications.sms_notifier import SmsNotifier
from app.runtime.escalation import Signal
from app.runtime.input_safety import build_system_prompt
from app.schemas_api.conversations import EscalationEventOut
from app.tools import catalog
from app.tools.registry import build_tool
from app.tools.schemas import (
    HAND_TO_TEAM_DEF,
    HAND_TO_TEAM_TOOL_ID,
    TOOL_SCHEMAS,
    build_tool_defs,
    with_cognition_tools,
)


# --- cognition, not a catalog tool -------------------------------------------------- #
def test_hand_to_team_is_never_an_admin_configurable_tool() -> None:
    assert HAND_TO_TEAM_TOOL_ID == "hand_to_team"
    assert HAND_TO_TEAM_TOOL_ID not in catalog.ALL_TOOL_IDS
    assert HAND_TO_TEAM_TOOL_ID not in TOOL_SCHEMAS
    assert HAND_TO_TEAM_TOOL_ID not in catalog.TOOL_CONNECTION_TYPE
    assert HAND_TO_TEAM_TOOL_ID not in catalog.TOOL_DISPLAY_NAMES
    # A fresh Luciel's tools pillar (the Configure checklist) never lists it.
    assert all(t["id"] != HAND_TO_TEAM_TOOL_ID for t in DEFAULT_TOOLS)
    # The production resolver does not build it: the loop dispatches it itself.
    assert build_tool(HAND_TO_TEAM_TOOL_ID) is None


def test_the_cognition_def_rides_after_the_gated_catalog_defs() -> None:
    defs = with_cognition_tools(build_tool_defs({"lookup_record", "check_availability"}))
    assert [d["tool_id"] for d in defs] == ["check_availability", "lookup_record", "hand_to_team"]
    assert defs[-1] is HAND_TO_TEAM_DEF
    assert HAND_TO_TEAM_DEF["input_schema"]["required"] == ["reason"]
    # No catalog tools at all: the model still hears about the one door to the team.
    assert [d["tool_id"] for d in with_cognition_tools(None)] == ["hand_to_team"]


# --- the model is told, on every turn, how (and how not) to reach the team ---------- #
def test_the_escalation_contract_names_the_tool_and_forbids_an_unbacked_claim() -> None:
    prompt = build_system_prompt(None, tool_catalog=with_cognition_tools(None))
    contract = prompt[prompt.index("4. ESCALATION CONTRACT") : prompt.index("5. MODERATION")]
    assert "hand_to_team" in contract
    assert "only way to reach the team" in contract
    assert "Never tell a customer that you have flagged" in contract
    assert "never suppress an escalation" in contract.lower()
    # The stanza still lists it as a callable tool, with its description.
    assert "- hand_to_team:" in prompt
    assert "Always available" in prompt


# --- the signal is a first-class row: wire value, next step, labels ------------------ #
def test_the_signal_has_a_wire_value_and_owner_facing_copy() -> None:
    assert Signal.luciel_requested_handoff.value == "luciel_requested_handoff"
    out = EscalationEventOut(
        escalation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        signal="luciel_requested_handoff",
        gate="outcome",
        fired_at=datetime.now(UTC),
        reasons=["Luciel asked for a person", "wants a refund the policy does not cover"],
    )
    assert out.signal == "luciel_requested_handoff"
    # The platform's undeliverable-reply signal (F077) parses on the same list.
    assert (
        EscalationEventOut(
            escalation_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            signal="reply_undeliverable",
            gate="outcome",
            fired_at=datetime.now(UTC),
        ).signal
        == "reply_undeliverable"
    )


def _notification(signal: str, reasons: tuple[str, ...]) -> EscalationNotification:
    return EscalationNotification(
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        signal=signal,
        gate="outcome",
        business_name="Acme Patios",
        lead_name="Dana Lee",
        lead_contact="dana@lead.example",
        reasons=reasons,
    )


def test_the_owner_reads_luciels_reason_on_email_and_sms() -> None:
    reasons = ("Luciel asked for a person", "wants a refund the policy does not cover")
    n = _notification("luciel_requested_handoff", reasons)
    email = EmailNotifier()
    assert email.subject(n) == "Luciel needs you: Luciel asked for a person"
    body = email.format(n)
    assert "Why: Luciel asked for a person" in body
    assert "Details: Luciel asked for a person; wants a refund the policy does not cover" in body
    assert "read its reason" in body  # the recommended next step
    sms = SmsNotifier().format(n)
    assert "Why: Luciel asked for a person; wants a refund the policy does not cover." in sms
    assert "(luciel_requested_handoff)" in sms


def test_a_row_without_reasons_renders_exactly_as_before() -> None:
    n = _notification("high_value_lead", ())
    assert "Details:" not in EmailNotifier().format(n)
    assert "Why:" not in SmsNotifier().format(n)


def test_every_signal_has_owner_facing_copy() -> None:
    """A row the loop can write must render on the owner's page (email subject / next
    step) instead of falling back to the raw enum value."""
    from app.notifications.base import _NEXT_STEP
    from app.notifications.email_notifier import _SIGNAL_LABELS

    for signal in Signal:
        assert signal.value in _SIGNAL_LABELS, signal
        assert signal.value in _NEXT_STEP, signal
