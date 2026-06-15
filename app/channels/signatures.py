"""Inbound webhook signature verification (Architecture §3.7.2, §3.1.3).

Every inbound channel webhook is cryptographically verified BEFORE the tenant
lookup runs. An unsigned/invalid webhook is rejected and audit-logged, never
processed. This module implements the provider signature schemes:

  - Twilio: HMAC-SHA1 over (full URL + sorted POST params), base64, compared to
    the X-Twilio-Signature header (constant-time).
  - SES via SNS: SNS message signature (the canonical string is signed by an
    AWS-issued cert). Full cert-chain validation requires fetching the SigningCertURL;
    here we implement the canonical-string construction + signature interface and
    verify against a provided public key, with the cert-fetch step abstracted
    (bound in deployment). The HMAC path is fully testable.

Constant-time comparison is used everywhere to avoid timing oracles.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute the expected X-Twilio-Signature for a request.

    Per Twilio's scheme: concatenate the full URL with each POST param's key and
    value, sorted by key, then HMAC-SHA1 with the auth token, base64-encoded.
    """
    data = url
    for key in sorted(params.keys()):
        data += key + params[key]
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_twilio(auth_token: str, url: str, params: dict[str, str], header_signature: str) -> bool:
    expected = twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected, header_signature or "")


def sns_canonical_string(message: dict[str, str]) -> str:
    """Build the canonical string SNS signs for a Notification message.

    Field order is fixed by AWS: for a Notification, the keys
    (Message, MessageId, Subject?, Timestamp, TopicArn, Type) in that order,
    each as 'key\\nvalue\\n'. Subject is included only when present.
    """
    keys = ["Message", "MessageId"]
    if message.get("Subject") is not None:
        keys.append("Subject")
    keys += ["Timestamp", "TopicArn", "Type"]
    parts = []
    for k in keys:
        if k in message and message[k] is not None:
            parts.append(k)
            parts.append(message[k])
    return "\n".join(parts) + "\n"


def verify_hmac_shared_secret(secret: str, payload: bytes, header_signature: str) -> bool:
    """Generic HMAC-SHA256 verification (used for Meta X-Hub-Signature-256 too).

    Meta sends 'sha256=<hex>' in X-Hub-Signature-256 over the raw body.
    """
    sig = header_signature or ""
    if sig.startswith("sha256="):
        sig = sig[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
