"""Tests for the two core-loop fixes: lead-capture cognition (§3.4.4/§3.4.9) and
the in-code AI-identity disclosure backstop (§3.4.16)."""

from __future__ import annotations

from app.runtime.input_safety import (
    disclosure_present,
    ensure_ai_identity_disclosure,
)
from app.runtime.lead_capture import extract_lead_signals


def test_signals_qualify_on_contact_info():
    s = extract_lead_signals("Sure, my email is jordan@acme.com")
    assert s.email == "jordan@acme.com"
    assert s.strong_identifier == "jordan@acme.com"
    assert s.is_qualified


def test_signals_qualify_on_phone():
    s = extract_lead_signals("call me at 416-555-0143")
    assert s.phone is not None
    assert s.is_qualified


def test_signals_qualify_on_intent_or_budget_without_contact():
    assert extract_lead_signals("how much does it cost to get started?").is_qualified
    assert extract_lead_signals("my budget is around $5000").is_qualified


def test_anonymous_question_does_not_qualify():
    s = extract_lead_signals("what are your hours?")
    assert not s.is_qualified
    assert s.strong_identifier is None


def test_disclosure_present_detection():
    assert disclosure_present("Hi, I'm Ava, the AI assistant for GTA Premier.")
    assert not disclosure_present("Sure, our hours are 9 to 5.")


def test_backstop_prepends_when_missing():
    out = ensure_ai_identity_disclosure(
        "Our hours are 9 to 5.", business_name="GTA Premier", assistant_name="Ava"
    )
    assert disclosure_present(out)
    assert "GTA Premier" in out
    assert "Our hours are 9 to 5." in out


def test_backstop_noop_when_already_disclosed():
    original = "Hi — I'm Ava, the AI assistant for GTA Premier. Our hours are 9 to 5."
    out = ensure_ai_identity_disclosure(original, business_name="GTA Premier", assistant_name="Ava")
    assert out == original  # surfaced once; not duplicated


def test_backstop_never_doubles_assistant_when_name_already_ends_in_assistant():
    """LIVE-VERIFIED (Harmony wave 2, item 1): a Luciel/business name that already
    ends in the word "Assistant" (e.g. a front-desk name an owner actually typed,
    "Aryan's Front-Desk Assistant") must never produce "...Assistant's assistant" in
    the composed greeting. The old template appended ", the AI assistant for
    {business}" directly after the speaker name, and the old default-assistant
    fallback separately appended "'s assistant" onto the business name — either
    path doubles the word when the configured name already ends in "Assistant".
    """
    name = "Aryan's Front-Desk Assistant"
    out = ensure_ai_identity_disclosure(
        "Our hours are 9 to 5.", business_name=name, assistant_name=name
    )
    lowered = out.lower()
    assert "assistant's assistant" not in lowered
    assert "assistant assistant" not in lowered
    # The configured name is used VERBATIM as the speaker.
    assert out.startswith(f"Hi — I'm {name}.")
    # AI-disclosure requirement stays explicit and intact.
    assert disclosure_present(out)
    assert "Our hours are 9 to 5." in out


def test_disclosure_clause_names_business_when_luciel_name_ends_in_assistant():
    """LIVE-VERIFIED regression: the disclosure clause must name the BUSINESS, never
    the Luciel, even when the Luciel/speaker name ends in the word "Assistant" (the
    exact live-failing shape: "Hi — I'm Aryan's Front-Desk Assistant. I'm an AI
    assistant for Aryan's Front-Desk Assistant..." incorrectly repeated the Luciel
    name in the disclosure clause instead of naming the business).
    """
    luciel_name = "Aryan's Front-Desk Assistant"
    business_name = "Aryan's Detailing Co"
    out = ensure_ai_identity_disclosure(
        "", business_name=business_name, assistant_name=luciel_name
    )
    assert out.startswith(f"Hi — I'm {luciel_name}.")
    assert f"an AI assistant for {business_name}" in out
    # The Luciel name must never appear in the disclosure clause itself (only as
    # the speaker at the very start of the line).
    after_speaker = out[len(f"Hi — I'm {luciel_name}."):]
    assert luciel_name not in after_speaker


def test_disclosure_clause_falls_back_to_this_business_when_no_business_name():
    """No usable business name → the disclosure clause falls back to the generic
    "this business" rather than ever substituting the Luciel name."""
    luciel_name = "Nova"
    out = ensure_ai_identity_disclosure(
        "", business_name="this business", assistant_name=luciel_name
    )
    assert "an AI assistant for this business" in out
    assert luciel_name not in out.split(".", 1)[1]


def test_disclosure_never_contains_the_luciel_name_even_when_it_ends_in_assistant():
    """Hard invariant: whatever business_name is supplied, the disclosure clause
    (the part after the speaker introduction) must never contain the Luciel name."""
    luciel_name = "Aryan's Front-Desk Assistant"
    for business_name in ("this business", "Aryan's Detailing Co", "GTA Premier"):
        out = ensure_ai_identity_disclosure(
            "", business_name=business_name, assistant_name=luciel_name
        )
        clause = out.split(".", 1)[1]
        assert luciel_name not in clause
