"""Capability-URL inbound verification (audit round 3, C11).

An OAuth tenant's webhooks can never pass X-Twilio-Signature (Twilio signs with
the Account Auth Token, which the platform never holds for them) — the
registered URLs carry a per-connection 256-bit ``cap`` parameter instead. These
tests pin the acceptance rule: signature first and byte-identical to before;
the capability path is ADDITIVE, constant-time, and never a fallback for a
tenant whose signature simply failed.
"""

from __future__ import annotations

from app.channels.signatures import (
    capability_url_ok,
    twilio_signature,
    verify_twilio_inbound,
)

TOKEN = "u" * 43  # token_urlsafe(32) shape
URL = f"https://api.vantagemind.ai/api/v1/webhooks/twilio/sms?cap={TOKEN}"


def test_capability_match_accepts():
    assert capability_url_ok(TOKEN, URL) is True


def test_capability_mismatch_and_absence_reject():
    assert capability_url_ok(TOKEN, URL.replace(TOKEN, "x" * 43)) is False
    no_cap_url = "https://api.vantagemind.ai/api/v1/webhooks/twilio/sms"
    assert capability_url_ok(TOKEN, no_cap_url) is False
    assert capability_url_ok(None, URL) is False
    assert capability_url_ok("", URL) is False


def test_capability_check_survives_hostile_encodings():
    # Non-ASCII / oversized values must compare False, never raise.
    assert capability_url_ok(TOKEN, "https://x/api?cap=%C3%A9%C3%A9") is False
    assert capability_url_ok(TOKEN, f"https://x/api?cap={'A' * 5000}") is False


def test_signature_path_still_accepts_and_is_checked_first():
    params = {"To": "+16045550100", "Body": "hi"}
    url = "https://api.vantagemind.ai/api/v1/webhooks/twilio/sms"
    signature = twilio_signature("tenant-token", url, params)
    assert verify_twilio_inbound("tenant-token", url, params, signature) is True
    # A capability token being configured must not break the signature path.
    assert (
        verify_twilio_inbound("tenant-token", url, params, signature, capability_token=TOKEN)
        is True
    )


def test_capability_accepts_when_signature_cannot():
    params = {"To": "+16045550100", "Body": "hi"}
    # The platform-token fallback fails for an OAuth tenant (Twilio signed with
    # a token nobody here holds) — the cap parameter in the registered URL is
    # what verifies.
    assert (
        verify_twilio_inbound("platform-token", URL, params, "bogus", capability_token=TOKEN)
        is True
    )


def test_forged_requests_are_rejected():
    params = {"To": "+16045550100", "Body": "hi"}
    forged_url = URL.replace(TOKEN, "y" * 43)
    assert (
        verify_twilio_inbound("platform-token", forged_url, params, "bogus", capability_token=TOKEN)
        is False
    )
    # No capability token stored (credential tenant): the cap parameter is inert
    # and the signature requirement stands.
    assert verify_twilio_inbound("platform-token", URL, params, "bogus") is False
