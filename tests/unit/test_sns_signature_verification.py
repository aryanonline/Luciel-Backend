"""Inbound SES/SNS email must be authenticated, not merely shaped like it is (BE P0-7).

The old verifier's entire live-mode check was ``bool(message.get("Signature"))``, so the
literal body ``{"Signature": "x"}`` was admitted. Anyone on the internet could inject a
fabricated inbound email into any tenant's runtime: fake leads, fake escalations,
attacker-chosen text in the owner's dashboard, and LLM spend on the tenant's meter.

These tests sign with a REAL RSA key and a real self-signed certificate, served by a
stubbed transport, so the whole path — canonical string, cert fetch, RSA verify — runs
for real. The attacker cases are the point: each one is a way to produce a message that
LOOKS signed.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.channels.sns_verification import (
    ConfigGatedSnsVerifier,
    SnsSignatureVerifier,
    SnsVerificationError,
    canonical_string,
    validate_cert_url,
)
from app.config import Settings
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from tests.unit.live_settings import live_settings

CERT_URL = "https://sns.ca-central-1.amazonaws.com/SimpleNotificationService-abc123.pem"


@pytest.fixture(scope="module")
def keypair() -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def other_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _notification(**overrides) -> dict[str, str]:
    message = {
        "Type": "Notification",
        "MessageId": "11111111-2222-3333-4444-555555555555",
        "TopicArn": "arn:aws:sns:ca-central-1:123456789012:luciel-inbound",
        "Message": '{"mail": {"source": "dana@lead.example"}}',
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "SignatureVersion": "1",
        "SigningCertURL": CERT_URL,
    }
    message.update(overrides)
    return message


def _sign(message: dict[str, str], key: rsa.RSAPrivateKey) -> dict[str, str]:
    algo = hashes.SHA1() if message.get("SignatureVersion") == "1" else hashes.SHA256()  # noqa: S303
    signature = key.sign(canonical_string(message).encode(), padding.PKCS1v15(), algo)
    return {**message, "Signature": base64.b64encode(signature).decode()}


def _verifier(pem: bytes, *, cert_url: str = CERT_URL) -> SnsSignatureVerifier:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != cert_url:
            return httpx.Response(404)
        return httpx.Response(200, content=pem)

    return SnsSignatureVerifier(client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- the regression itself -------------------------------------------------------- #
def test_a_bare_signature_field_no_longer_passes(keypair):
    """The exact body that used to be admitted in live mode."""
    key, pem = keypair
    assert _verifier(pem).verify({"Type": "Notification", "Signature": "x"}) is False


def test_a_genuinely_signed_notification_passes(keypair):
    key, pem = keypair
    assert _verifier(pem).verify(_sign(_notification(), key)) is True


def test_sha256_signature_version_2_passes(keypair):
    key, pem = keypair
    assert _verifier(pem).verify(_sign(_notification(SignatureVersion="2"), key)) is True


# --- forgery attempts ------------------------------------------------------------- #
def test_a_signature_from_the_wrong_key_is_rejected(keypair, other_key):
    """An attacker who signs with their own key must not be admitted by AWS's cert."""
    _, pem = keypair
    assert _verifier(pem).verify(_sign(_notification(), other_key)) is False


def test_tampering_with_the_body_after_signing_is_rejected(keypair):
    """The injected content lives in Message — changing it must break the signature."""
    key, pem = keypair
    signed = _sign(_notification(), key)
    signed["Message"] = '{"mail": {"source": "attacker@evil.example"}}'
    assert _verifier(pem).verify(signed) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://sns.ca-central-1.amazonaws.com.attacker.test/cert.pem",
        "https://attacker.test/sns.ca-central-1.amazonaws.com.pem",
        "http://sns.ca-central-1.amazonaws.com/cert.pem",
        "https://s3.amazonaws.com/cert.pem",
        "https://evil-sns.ca-central-1.amazonaws.com/cert.pem",
        "",
    ],
)
def test_the_signing_cert_url_is_pinned_to_an_sns_host(url):
    """The load-bearing check. An attacker who chooses the cert URL supplies both the
    signature and the key that approves it, so every other check becomes decorative."""
    with pytest.raises(SnsVerificationError):
        validate_cert_url(url)


def test_an_attacker_hosted_cert_is_never_fetched(keypair, other_key):
    """End to end on the same point: a self-consistent message signed by the attacker
    and pointing at the attacker's own cert must still fail."""
    _, pem = keypair
    evil = _notification(SigningCertURL="https://attacker.test/cert.pem")
    assert _verifier(pem).verify(_sign(evil, other_key)) is False


def test_an_unknown_signature_version_is_refused_not_defaulted(keypair):
    key, pem = keypair
    signed = _sign(_notification(), key)
    signed["SignatureVersion"] = "99"
    assert _verifier(pem).verify(signed) is False


def test_a_replayed_old_notification_is_rejected(keypair):
    """AWS certs are long-lived, so without an age bound one captured notification is
    replayable forever."""
    key, pem = keypair
    stale = _notification(
        Timestamp=(datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )
    assert _verifier(pem).verify(_sign(stale, key)) is False


@pytest.mark.parametrize("field", ["Signature", "SignatureVersion", "Timestamp"])
def test_a_missing_required_field_is_rejected(keypair, field):
    key, pem = keypair
    signed = _sign(_notification(), key)
    signed.pop(field)
    assert _verifier(pem).verify(signed) is False


def test_an_unfetchable_cert_fails_closed(keypair):
    key, _ = keypair
    verifier = SnsSignatureVerifier(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    assert verifier.verify(_sign(_notification(), key)) is False


# --- canonical string ------------------------------------------------------------- #
def test_canonical_string_orders_notification_fields_as_aws_signs_them():
    msg = _notification(Subject="Patio quote")
    assert canonical_string(msg) == (
        "Message\n"
        f"{msg['Message']}\n"
        "MessageId\n"
        f"{msg['MessageId']}\n"
        "Subject\n"
        "Patio quote\n"
        "Timestamp\n"
        f"{msg['Timestamp']}\n"
        "TopicArn\n"
        f"{msg['TopicArn']}\n"
        "Type\n"
        "Notification\n"
    )


def test_an_absent_subject_is_omitted_entirely():
    assert "Subject" not in canonical_string(_notification())


def test_subscription_confirmation_uses_its_own_field_order():
    msg = _notification(Type="SubscriptionConfirmation", Token="tok", SubscribeURL="https://x/y")
    canonical = canonical_string(msg)
    assert canonical.index("SubscribeURL") < canonical.index("Timestamp")
    assert canonical.index("Token") < canonical.index("TopicArn")


def test_an_unknown_message_type_has_no_canonical_form():
    with pytest.raises(SnsVerificationError):
        canonical_string({"Type": "SomethingNew"})


# --- mode gate -------------------------------------------------------------------- #
def test_mock_mode_accepts_so_ci_stays_hermetic(monkeypatch):
    from app.channels import sns_verification

    monkeypatch.setattr(sns_verification, "get_settings", lambda: Settings(integration_mode="mock"))
    assert ConfigGatedSnsVerifier().verify({"Type": "Notification"}) is True


def test_live_mode_has_no_accept_fallback(monkeypatch, keypair):
    """The whole defect was a live-mode path that admitted on a weaker check. There must
    be no branch in live mode that accepts an unauthenticated message."""
    from app.channels import sns_verification

    key, pem = keypair
    monkeypatch.setattr(sns_verification, "get_settings", live_settings)
    gated = ConfigGatedSnsVerifier(real=_verifier(pem))

    assert gated.verify({"Type": "Notification", "Signature": "x"}) is False
    assert gated.verify(_sign(_notification(), key)) is True
