"""Real SMS delivery via Twilio (Architecture §3.1.4 SMS / §3.1.2 voice send-side).

Closes the SMS/voice SEND-path deferred stub. Two implementations behind the
``SmsSender`` Protocol the adapter already expects (mirrors the
``app/notifications/email_sender.py`` SES pattern — ``EmailSender`` Protocol,
``NullEmailSender`` mock/CI default, real sender lazy-imports its SDK):

- ``TwilioSmsSender`` — live: sends through the Twilio REST API. Bound in live
  mode by ``build_sms_sender`` from the platform config.
- ``NullSmsSender``   — mock/CI: records sends in-memory, makes NO network call,
  so the suite stays hermetic and deterministic. This is the default.

An Account SID (``AC...``) is always mandatory: an API-Key SID (``SK...``) + secret is
an alternative to the Account auth token, not a replacement for the Account SID —
Twilio authenticates API-Key pairs *against* an account. In the BYO model (§3.1.4) the
tenant supplies their own Account SID with their ``sms_sender`` connection, so the
customer-facing path (``app.channels.tenant_twilio``) is fully satisfied. The
platform-level ``build_sms_sender()`` below still degrades to ``NullSmsSender`` when no
platform Account SID is configured — honest degradation, matching the codebase's
integration-mode seam pattern (see ``build_email_sender`` in
``app/notifications/email_sender.py``).

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

    Default sender when Twilio is not configured for live sending (§3.1.4).
    """

    def __init__(self) -> None:
        self.sent: list[SentSms] = []

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        from app.config import live_mode  # noqa: PLC0415 — avoids an import cycle

        sid = f"SMnull{len(self.sent) + 1:022d}"
        self.sent.append(SentSms(from_number=from_number, to=to, body=body, message_sid=sid))
        # Every adapter above reads a non-None id as an accepted delivery. In live mode
        # nothing left the system, so handing back the fake sid would tell the owner a
        # lead was answered when they were never reached.
        return None if live_mode() else sid


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
    # Round 7 WP-4b: an absolute URL Twilio posts each message's delivery receipt to
    # (``MessageStatus`` delivered/failed/undelivered). Set by the notification leg only
    # — a page's ``delivered`` must come from the carrier, not from the API accepting
    # the request. None keeps the send byte-identical.
    status_callback: str | None = None
    _client: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Fail closed at construction, never at send time mid-conversation —
        # matches RealStripeGateway's fail-loudly-at-construction posture.
        if not self.account_sid:
            raise RuntimeError(
                "TwilioSmsSender requires a Twilio Account SID (AC...); only an "
                "API-Key SID/secret was configured."
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
        import asyncio  # noqa: PLC0415

        client = self._get_client()
        extra: dict[str, str] = {}
        if self.status_callback:
            extra["status_callback"] = self.status_callback
        # The Twilio REST client is synchronous: off the event loop (round 7 WP-2,
        # item 5), like the tenant webhook configurator already is.
        msg = await asyncio.to_thread(
            lambda: client.messages.create(from_=from_number, to=to, body=body, **extra)  # type: ignore[attr-defined]
        )
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
    secret, AND a Twilio Account SID. With any of them absent this factory returns
    ``NullSmsSender`` even in live mode, mirroring ``build_email_sender``'s honest
    degradation to Null rather than crashing when a live prerequisite is absent.

    Config field names read (getattr-guarded so this module never requires the
    corresponding config.py fields to already exist):
      - ``twilio_account_sid``    (AC...)
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
