"""Real SNS message signature verification for inbound SES email (P0-7, §3.7.2).

The previous verifier checked that a ``Signature`` FIELD was present and never looked
at it, so the literal body ``{"Signature": "x"}`` was admitted. Any unauthenticated
caller on the internet could inject a fabricated inbound email into any tenant's
conversation runtime: fake leads, fake escalations, attacker-chosen text in the owner's
dashboard, and LLM spend on the tenant's meter.

What a real verification has to establish, in order:

  1. The message names a signing certificate hosted by AWS SNS itself. This is the load
     -bearing check — an attacker who can point ``SigningCertURL`` at a host they control
     signs the message themselves and every later step passes. The URL must be https and
     its host must match ``sns.<region>.amazonaws.com`` exactly; no suffix matching, which
     ``evil-sns.us-east-1.amazonaws.com.attacker.test`` would satisfy.
  2. The signature verifies, with that certificate's public key, over the CANONICAL
     string AWS defines for the message type — not over the raw body. The canonical
     string covers only known fields in a fixed order, so an attacker cannot smuggle
     signed-looking content through a field AWS does not sign.
  3. The message is recent. AWS certificates are long-lived, so without an age bound a
     single captured notification is replayable forever.

Mock/CI accepts without any of this (hermetic, no network); live mode fails closed.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# ``sns.<region>.amazonaws.com`` and nothing else. Anchored at both ends: an unanchored
# or suffix match is the classic way this check is defeated.
_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

# AWS signs with SHA1 under SignatureVersion 1 and SHA256 under version 2. An unknown
# version is refused rather than defaulted — defaulting picks the weaker one.
_SIGNATURE_HASHES = {"1": "SHA1", "2": "SHA256"}

# Field order is fixed by AWS and differs by message type. A key absent from the message
# is omitted from the canonical string entirely.
_CANONICAL_KEYS: dict[str, tuple[str, ...]] = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
}
_CANONICAL_KEYS["UnsubscribeConfirmation"] = _CANONICAL_KEYS["SubscriptionConfirmation"]

MAX_MESSAGE_AGE = timedelta(hours=1)
_CERT_FETCH_TIMEOUT = 5.0
_MAX_CERT_BYTES = 32 * 1024


class SnsVerificationError(Exception):
    """The message could not be authenticated. Carries a non-secret reason."""


def canonical_string(message: dict[str, Any]) -> str:
    """Build the exact string AWS signed for this message type."""
    keys = _CANONICAL_KEYS.get(str(message.get("Type", "")))
    if keys is None:
        raise SnsVerificationError("unknown_message_type")
    parts: list[str] = []
    for key in keys:
        value = message.get(key)
        if value is None:
            continue
        parts.append(key)
        parts.append(str(value))
    return "\n".join(parts) + "\n"


def validate_cert_url(url: str) -> str:
    """Return ``url`` iff it is an https URL served by SNS itself.

    This is the check the whole scheme rests on: the message tells us where to fetch the
    key that validates it, so an unpinned host means the attacker supplies both the
    signature and the key that approves it.
    """
    parsed = httpx.URL(url or "")
    if parsed.scheme != "https":
        raise SnsVerificationError("cert_url_not_https")
    if not _SNS_HOST.match(parsed.host or ""):
        raise SnsVerificationError("cert_url_host_not_allowed")
    return url


def _check_age(message: dict[str, Any], *, now: datetime | None = None) -> None:
    raw = message.get("Timestamp")
    if not raw:
        raise SnsVerificationError("missing_timestamp")
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise SnsVerificationError("unparsable_timestamp") from None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if abs((now or datetime.now(UTC)) - stamp) > MAX_MESSAGE_AGE:
        raise SnsVerificationError("message_too_old")


class SnsSignatureVerifier:
    """Verifies SNS messages against the AWS-hosted signing certificate.

    Certificates are cached by URL for the process lifetime: AWS rotates them rarely,
    the URL changes when it does, and re-fetching per message would put a synchronous
    network call on the inbound path.
    """

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client
        self._certs: dict[str, Any] = {}

    def _load_cert(self, url: str) -> Any:  # cryptography's Certificate (lazy import)
        from cryptography import x509  # noqa: PLC0415 — keep the mock path import-free

        if url in self._certs:
            return self._certs[url]
        client = self._client or httpx.Client(timeout=_CERT_FETCH_TIMEOUT)
        try:
            response = client.get(url)
            response.raise_for_status()
            pem = response.content[:_MAX_CERT_BYTES]
        except httpx.HTTPError as exc:
            raise SnsVerificationError(f"cert_fetch_failed: {type(exc).__name__}") from None
        finally:
            if self._client is None:
                client.close()
        try:
            cert = x509.load_pem_x509_certificate(pem)
        except ValueError:
            raise SnsVerificationError("cert_not_parsable") from None
        self._certs[url] = cert
        return cert

    def verify_or_raise(self, message: dict[str, Any], *, now: datetime | None = None) -> None:
        """Authenticate ``message``, or raise ``SnsVerificationError`` saying why."""
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: PLC0415

        version = str(message.get("SignatureVersion", ""))
        algorithm = _SIGNATURE_HASHES.get(version)
        if algorithm is None:
            raise SnsVerificationError("unsupported_signature_version")

        signature_b64 = message.get("Signature")
        if not signature_b64:
            raise SnsVerificationError("missing_signature")
        try:
            signature = base64.b64decode(str(signature_b64), validate=True)
        except (ValueError, TypeError):
            raise SnsVerificationError("signature_not_base64") from None

        _check_age(message, now=now)
        signed = canonical_string(message).encode("utf-8")
        cert = self._load_cert(validate_cert_url(str(message.get("SigningCertURL", ""))))

        public_key = cert.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise SnsVerificationError("cert_key_not_rsa")
        try:
            public_key.verify(
                signature,
                signed,
                padding.PKCS1v15(),
                hashes.SHA1() if algorithm == "SHA1" else hashes.SHA256(),  # noqa: S303
            )
        except InvalidSignature:
            raise SnsVerificationError("signature_mismatch") from None

    def verify(self, message: dict[str, Any]) -> bool:
        """Protocol-shaped wrapper: True iff the message authenticates."""
        try:
            self.verify_or_raise(message)
        except SnsVerificationError as exc:
            logger.warning("sns_signature_rejected reason=%s", exc)
            return False
        return True


class ConfigGatedSnsVerifier:
    """Real verification in live mode; accept in mock/CI so tests stay hermetic.

    The mode gate is the ONLY thing that varies. In live mode this is the real verifier
    with no fallback: a message that cannot be authenticated is rejected, never admitted
    with a warning.
    """

    def __init__(self, *, real: SnsSignatureVerifier | None = None) -> None:
        self._real = real or SnsSignatureVerifier()

    def verify(self, message: dict[str, Any]) -> bool:
        if get_settings().integration_mode != "live":
            return True
        return self._real.verify(message)


def confirm_subscription(message: dict[str, Any], *, client: httpx.Client | None = None) -> bool:
    """Complete the SNS subscription handshake by fetching ``SubscribeURL``.

    Only ever called for a message whose signature ALREADY verified — an unauthenticated
    confirmation would let anyone subscribe our endpoint to a topic they control, which
    is the same injection this module exists to prevent. The URL is pinned to an SNS host
    for the same reason the certificate URL is.
    """
    try:
        url = validate_cert_url(str(message.get("SubscribeURL", "")))
    except SnsVerificationError as exc:
        logger.warning("sns_subscribe_url_rejected reason=%s", exc)
        return False
    owned = client or httpx.Client(timeout=_CERT_FETCH_TIMEOUT)
    try:
        owned.get(url).raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("sns_subscribe_failed error=%s", type(exc).__name__)
        return False
    finally:
        if client is None:
            owned.close()
    return True
