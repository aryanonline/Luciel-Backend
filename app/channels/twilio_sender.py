"""Real SMS delivery via Twilio (Architecture §3.1.4 SMS / §3.1.2 voice send-side).

Closes the SMS/voice SEND-path deferred stub. Two implementations behind the
``SmsSender`` Protocol the adapter already expects (mirrors the
``app/notifications/email_sender.py`` SES pattern — ``EmailSender`` Protocol,
``NullEmailSender`` mock/CI default, real sender lazy-imports its SDK):

- ``TwilioSmsSender`` — live: sends through the Twilio REST API. Bound in live
  mode by ``build_sms_sender`` from the platform config.
- ``NullSmsSender``   — mock/CI: records sends in-memory, makes NO network call,
  so the suite stays hermetic and deterministic. This is the default.

KNOWN GAP (see gap_twilio.md): the provided Twilio credential is an API-Key SID
(``SK...``) + secret + a Client ID, but the Twilio REST client (``twilio.rest.Client``)
REQUIRES an Account SID (``AC...``) — an API-Key SID cannot substitute for it (Twilio
authenticates API-Key/secret pairs *against* an Account SID, it does not replace it).
So ``TwilioSmsSender`` accepts an ``account_sid`` field and ``build_sms_sender()``
only binds the real sender when one is configured; live SMS sending is BLOCKED until
the owner supplies the Twilio Account SID. Until then ``build_sms_sender()`` returns
``NullSmsSender`` even if ``integration_mode == \"live\"`` — honest degradation,
matching the codebase's existing integration-mode seam pattern (see
``build_email_sender`` in ``app/notifications/email_sender.py``).

Twilio magic test numbers (https://www.twilio.com/docs/iam/test-credentials) are
used in tests/mock mode (e.g. ``from=+15005550006``) so the send *shape* is
exercised without any real network call or real credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# Twilio magic test number that always "succeeds" against the test credential set
# (https://www.twilio.com/docs/iam/test-credentials). Used by NullSmsSender/tests
# ONLY — never a real send destination or origin.
MAGIC_TEST_FROM_NUMBER = "+15005550006"


@dataclass(frozen=True)
class SentSms:
    from_number: str
    to: str
    body: str
    message_sid: str


class SmsSenderProtocol(Protocol):
    """Same shape as ``app.channels.sms_adapter.SmsSender`` — kept as a local
    alias so this module has no import-time dependency on the adapter module."""

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None: ...


class NullSmsSender:
    """Mock/CI sender — records intent, no network. Inspectable in tests.

    Default sender when Twilio is not configured for live sending (§3.1.4) —
    including the current Account-SID-missing blocker (see module docstring).
    """

    def __init__(self) -> None:
        self.sent: list[SentSms] = []

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        sid = f"SMnull{len(self.sent) + 1:022d}"
        self.sent.append(SentSms(from_number=from_number, to=to, body=body, message_sid=sid))
        return sid


@dataclass
class TwilioSmsSender:
    """Live Twilio REST sender. Lazy import of the ``twilio`` SDK so CI without
    network/credentials never imports it (mirrors ``SesEmailSender``'s lazy boto3
    import in ``app/notifications/email_sender.py``).

    Twilio's REST client authenticates as an Account (``account_sid``); an
    API-Key SID (``SK...``) + secret is a valid alternative to the Account auth
    token but the Account SID itself is still mandatory — there is no Twilio
    REST call that omits it. Either credential shape is accepted, because a
    customer bringing their own account (§3.1.4) may hand us either one.
    """

    account_sid: str
    api_key_sid: str = ""
    api_key_secret: str = ""
    auth_token: str = ""
    _client: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Fail closed at construction, never at send time mid-conversation —
        # matches RealStripeGateway's fail-loudly-at-construction posture.
        if not self.account_sid:
            raise RuntimeError(
                "TwilioSmsSender requires a Twilio Account SID (AC...); only an "
                "API-Key SID/secret was configured. See gap_twilio.md."
            )
        if not self.auth_token and not (self.api_key_sid and self.api_key_secret):
            raise RuntimeError("TwilioSmsSender requires an Auth Token or an API Key SID + secret.")

    def _get_client(self) -> object:
        if self._client is None:
            from twilio.rest import Client  # noqa: PLC0415 — lazy/guarded (CI has no network)

            if self.api_key_sid and self.api_key_secret:
                self._client = Client(self.api_key_sid, self.api_key_secret, self.account_sid)
            else:
                self._client = Client(self.account_sid, self.auth_token)
        return self._client

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        client = self._get_client()
        msg = client.messages.create(from_=from_number, to=to, body=body)  # type: ignore[attr-defined]
        return str(getattr(msg, "sid", "")) or None


def build_sms_sender() -> SmsSenderProtocol:
    """PLATFORM Twilio sender — dev/mock fallback ONLY (§3.1.4).

    The customer-facing send path goes through
    ``app.channels.tenant_twilio.build_tenant_sms_sender``, which binds the TENANT's own
    Twilio credentials from their ``sms_sender`` connection. This factory reads the
    platform's account, which must never be the sender in the BYO model — VantageMind is
    not in the telephony billing path. It survives as the no-tenant-context fallback
    (returns Null in mock/CI, which is every non-live environment).

    Requires ALL of: ``integration_mode == \"live\"``, a Twilio API-Key SID +
    secret, AND a Twilio Account SID. The Account SID is the current known gap
    (see module docstring / gap_twilio.md) — the provided credential has no
    ``AC...`` value, so this factory returns ``NullSmsSender`` even in live mode
    until the owner supplies one. This mirrors ``build_email_sender``'s honest
    degradation to Null rather than crashing when a live prerequisite is absent.

    Config field names read (getattr-guarded so this module never requires the
    parent's config.py change to already exist — see gap_twilio.md for the exact
    field names the parent must add):
      - ``twilio_account_sid``    (AC... — MISSING today, the blocker)
      - ``twilio_api_key_sid``    (SK...)
      - ``twilio_api_key_secret``
    """
    from app.config import get_settings

    s = get_settings()
    account_sid = getattr(s, "twilio_account_sid", "") or ""
    api_key_sid = getattr(s, "twilio_api_key_sid", "") or ""
    api_key_secret = getattr(s, "twilio_api_key_secret", "") or ""

    if (
        getattr(s, "integration_mode", "mock") == "live"
        and account_sid
        and api_key_sid
        and api_key_secret
    ):
        return TwilioSmsSender(
            account_sid=account_sid, api_key_sid=api_key_sid, api_key_secret=api_key_secret
        )
    return NullSmsSender()
