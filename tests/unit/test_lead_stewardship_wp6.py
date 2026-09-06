"""Leads and CRM stewardship, deterministic half (2026-09-05 audit WP6).

* A volunteered phone is E.164 or nothing (F111/F127).
* Identifiers have a kind and a normal form (F125).
* HubSpot receives its STANDARD property names, never ours (F128).
* Salesforce's Description merges the record's facts with the change (F130).
"""

from __future__ import annotations

from app.runtime.lead_capture import (
    crm_phone_key,
    extract_lead_signals,
    identifier_kind,
    normalize_identifier,
    normalize_phone,
)
from app.tools.crm_backend import HubSpotCrmApi, SalesforceCrmApi


# --- F111 / F127 --------------------------------------------------------------- #
def test_phones_normalise_to_e164_or_to_nothing():
    assert normalize_phone("604-555-0100") == "+16045550100"
    assert normalize_phone("(604) 555 0100") == "+16045550100"
    assert normalize_phone("1 604 555 0100") == "+16045550100"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("+1 (604) 555-0100") == "+16045550100"
    # Ambiguous digit runs are never guessed into a number.
    assert normalize_phone("555 0100") is None
    assert normalize_phone("44 20 7946 0958") is None
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_a_typed_phone_signal_is_already_e164():
    s = extract_lead_signals("call me at 604-555-0100 about a quote")
    assert s.phone == "+16045550100"
    assert s.strong_identifier == "+16045550100"


# --- F125 ------------------------------------------------------------------------ #
def test_identifier_kinds_and_normal_forms():
    assert identifier_kind("Jane@Acme.com") == "email"
    assert identifier_kind("+16045550100") == "phone"
    assert identifier_kind("psid:1234567890") == "transport"
    assert normalize_identifier(" Jane@Acme.com ") == "jane@acme.com"
    assert normalize_identifier(" +16045550100 ") == "+16045550100"


def test_the_webhook_phone_key_comes_from_the_phone_or_the_transport():
    class Row:
        phone = None
        contact_identifier = "+16045550100"

    assert crm_phone_key(Row()) == "+16045550100"
    Row.phone = "+14165550143"
    assert crm_phone_key(Row()) == "+14165550143"
    Row.phone = None
    Row.contact_identifier = "jane@acme.com"
    assert crm_phone_key(Row()) is None


# --- F128 ------------------------------------------------------------------------ #
def test_hubspot_receives_standard_property_names_only():
    props = HubSpotCrmApi._properties(
        "jane@acme.com",
        {"name": "Jane Miller", "phone": "+16045550100", "intent": "reno quote", "outcome": "lost"},
        notes={"lead_score": "0.7", "intent": "old intent"},
    )
    assert props["email"] == "jane@acme.com"
    assert props["firstname"] == "Jane" and props["lastname"] == "Miller"
    assert props["phone"] == "+16045550100"
    # Everything non-standard rides the standard free-text property, with this
    # push's facts on top of the record's current ones.
    assert props["message"] == "intent: reno quote\nlead_score: 0.7\noutcome: lost"
    assert "name" not in props and "intent" not in props and "outcome" not in props


def test_hubspot_single_token_name_and_company_mapping():
    props = HubSpotCrmApi._mapped({"name": "Jane", "business": "Acme Decks"})
    assert props == {"firstname": "Jane", "company": "Acme Decks"}
    assert HubSpotCrmApi._mapped({}) == {}


# --- F130 ------------------------------------------------------------------------ #
def test_salesforce_update_merges_the_records_facts_into_description():
    payload = SalesforceCrmApi._update_payload(
        {"outcome": "converted"}, notes={"intent": "reno quote", "outcome": "in_progress"}
    )
    # The change wins over the stale note, and the untouched intent survives.
    assert payload == {"Description": "intent: reno quote\noutcome: converted"}
    # Without notes the delta alone is sent, exactly as before.
    assert SalesforceCrmApi._update_payload({"intent": "x"}) == {"Description": "intent: x"}


def test_salesforce_create_carries_the_notes_too():
    payload = SalesforceCrmApi._create_payload(
        "jane@acme.com", {"name": "Jane Miller"}, notes={"intent": "reno quote"}
    )
    assert payload["LastName"] == "Jane Miller"
    assert payload["Description"] == "intent: reno quote"
