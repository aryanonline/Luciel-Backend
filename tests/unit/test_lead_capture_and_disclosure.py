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
    out = ensure_ai_identity_disclosure(
        original, business_name="GTA Premier", assistant_name="Ava"
    )
    assert out == original  # surfaced once; not duplicated
