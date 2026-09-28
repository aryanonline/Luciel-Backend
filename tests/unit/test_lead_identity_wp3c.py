"""Lead identity, deterministic half (round 7 WP-3c items 2 and 7).

* An identifier the customer claims as THEIR OWN ("my email is", "call me at", a
  bare address) is the lead's; one framed as somebody else's ("copy my colleague
  sarah@…") or as a reference ("invoice 4165550143") is a MENTION — never the
  lead's identity, never a CRM key (item 2).
* The native CRM payload builders never rewrite the Email property, even when
  the identity moved — the new address is a note (item 2).
"""

from __future__ import annotations

from app.runtime.lead_capture import extract_lead_signals
from app.tools.crm_backend import HubSpotCrmApi, SalesforceCrmApi


def test_a_colleagues_address_is_a_mention_not_the_leads_email():
    s = extract_lead_signals("please copy my colleague sarah@acme.com on this")
    assert s.email is None
    assert s.mentions == ("sarah@acme.com",)
    assert s.strong_identifier is None
    assert not s.is_qualified  # a mention alone never promotes a visitor


def test_a_third_persons_number_is_a_mention():
    s = extract_lead_signals("you can reach her at 416-555-0143 if I'm out")
    assert s.phone is None
    assert s.mentions == ("+14165550143",)


def test_reference_numbers_are_mentions():
    assert extract_lead_signals("invoice 4165550143 hasn't arrived").phone is None
    assert extract_lead_signals("invoice 4165550143 hasn't arrived").mentions == ("+14165550143",)
    assert extract_lead_signals("order #4165550143 please").phone is None
    assert extract_lead_signals("ref: 416 555 0143").phone is None


def test_own_claims_are_the_leads_and_the_rest_are_mentions():
    s = extract_lead_signals("my number is 416-555-0143 and my colleague's is 416-555-0199")
    assert s.phone == "+14165550143"
    assert s.mentions == ("+14165550199",)
    s = extract_lead_signals("cc me at jane@acme.com and my boss bob@acme.com")
    assert s.email == "jane@acme.com"
    assert s.mentions == ("bob@acme.com",)


def test_a_bare_or_unframed_address_is_the_customers_own():
    # The answer to "where can we reach you?" is just the address.
    assert extract_lead_signals("jane@acme.com").email == "jane@acme.com"
    assert extract_lead_signals("I need a quote, jane@acme.com").email == "jane@acme.com"
    assert extract_lead_signals("It's me again — jane@acme.com").email == "jane@acme.com"
    assert extract_lead_signals("416-555-0143").phone == "+14165550143"
    # The everyday self-claims keep working exactly as before.
    assert extract_lead_signals("Sure, my email is jordan@acme.com").email == "jordan@acme.com"
    assert extract_lead_signals("call me at 416-555-0143").phone == "+14165550143"
    assert extract_lead_signals("you can also text me on 416 555 0143").phone == "+14165550143"


def test_the_nearest_marker_decides_and_a_tie_goes_to_the_claim():
    # "my number" contains the word "number" — the self-claim wins the tie.
    assert extract_lead_signals("my number 416-555-0143").phone == "+14165550143"
    # A third-party marker far back is overridden by a nearer self-claim.
    s = extract_lead_signals("my colleague suggested I write; reach me at jane@acme.com")
    assert s.email == "jane@acme.com" and s.mentions == ()
    # …and a self-claim far back is overridden by a nearer third-party marker.
    s = extract_lead_signals("I'm Jane; please also send it to my assistant amy@acme.com")
    assert s.email is None and s.mentions == ("amy@acme.com",)


def test_mentions_never_duplicate_the_leads_own_identifier():
    s = extract_lead_signals("my email is jane@acme.com — yes, jane@acme.com, that one")
    assert s.email == "jane@acme.com"
    assert s.mentions == ()


# --- item 2, CRM half: the Email property is never rewritten --------------------- #
def test_salesforce_update_never_writes_email_even_when_the_identity_moved():
    payload = SalesforceCrmApi._update_payload({"phone": "+16045550100"}, {"email": "new@acme.com"})
    assert "Email" not in payload
    assert payload["Phone"] == "+16045550100"
    assert payload["Description"] == "email: new@acme.com"
    # A stray "email" field lands in Description, never in Email.
    stray = SalesforceCrmApi._update_payload({"email": "x@acme.com"})
    assert "Email" not in stray and stray["Description"] == "email: x@acme.com"


def test_hubspot_mapping_never_sets_the_email_property_outside_create():
    props = HubSpotCrmApi._mapped({"name": "Jane"}, {"email": "new@acme.com", "intent": "quote"})
    assert "email" not in props
    assert props["message"] == "email: new@acme.com\nintent: quote"
    assert "email" not in HubSpotCrmApi._mapped({"email": "x@acme.com"})
    # Create still keys the contact on the email property — the one legitimate write.
    assert HubSpotCrmApi._properties("jane@acme.com", {})["email"] == "jane@acme.com"
