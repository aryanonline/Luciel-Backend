"""Inbound webhook signature verification (Architecture §3.7.2)."""

from __future__ import annotations

from app.channels.signatures import (
    twilio_signature,
    verify_hmac_shared_secret,
    verify_twilio,
)


def test_twilio_signature_roundtrip_valid():
    token = "test_auth_token"  # noqa: S105
    url = "https://api.vantagemind.com/api/v1/chat-widget/sms"
    params = {"To": "+15551230000", "From": "+15559998888", "Body": "hi"}
    sig = twilio_signature(token, url, params)
    assert verify_twilio(token, url, params, sig) is True


def test_twilio_signature_rejects_tampered_params():
    token = "test_auth_token"  # noqa: S105
    url = "https://api.vantagemind.com/sms"
    params = {"To": "+15551230000", "Body": "hi"}
    sig = twilio_signature(token, url, params)
    tampered = {"To": "+15551230000", "Body": "transfer money"}
    assert verify_twilio(token, url, tampered, sig) is False


def test_twilio_signature_rejects_wrong_token():
    url = "https://x/sms"
    params = {"a": "1"}
    sig = twilio_signature("real", url, params)
    assert verify_twilio("attacker", url, params, sig) is False


def test_twilio_missing_signature_rejected():
    assert verify_twilio("t", "https://x", {"a": "1"}, "") is False


def test_meta_hmac_sha256_signature():
    secret = "app_secret"  # noqa: S105
    payload = b'{"object":"page","entry":[]}'
    import hashlib
    import hmac

    good = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_hmac_shared_secret(secret, payload, good) is True
    assert verify_hmac_shared_secret(secret, payload, "sha256=deadbeef") is False
