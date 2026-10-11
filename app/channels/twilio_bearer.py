"""Bearer-token Twilio REST access for one-click OAuth tenants (audit round 3, C10).

The ``twilio`` SDK's ``Client`` authenticates with Account SID + Auth Token or an
API key pair — the credential-form shapes. An OAuth tenant holds neither: their
connection's secret material is an access/refresh token pair, and every REST call
the PLATFORM makes on their behalf (list numbers for the picker, point the
designated number's webhooks, send a message, verify an inbound resource exists)
authenticates with ``Authorization: Bearer``. This module is that thin surface —
plain ``httpx`` against the same ``2010-04-01`` endpoints the SDK wraps, one
class per protocol the credential-shape implementations already satisfy, so the
factories in ``tenant_twilio`` can return either shape behind one interface.

Credential-form tenants never route through here; their ``twilio.rest.Client``
paths are untouched (C10's additive rule).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.channels.number_operability import OperabilityResult

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_TIMEOUT_SECONDS = 15.0


class _BearerTransport(Protocol):
    """The httpx.AsyncClient subset used — injectable for hermetic tests."""

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> Any: ...

    async def post(self, url: str, *, headers: dict[str, str], data: dict[str, str]) -> Any: ...


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _request(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    transport: _BearerTransport | None = None,
) -> Any | None:
    """One bearer call; None on ANY failure (callers degrade honestly)."""
    import httpx  # noqa: PLC0415 — keep import-time cost off hermetic paths

    try:
        if transport is not None:
            if method == "GET":
                response = await transport.get(
                    url, headers=_headers(access_token), params=params or {}
                )
            else:
                response = await transport.post(
                    url, headers=_headers(access_token), data=data or {}
                )
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                if method == "GET":
                    response = await client.get(
                        url, headers=_headers(access_token), params=params or {}
                    )
                else:
                    response = await client.post(
                        url, headers=_headers(access_token), data=data or {}
                    )
    except Exception as exc:  # noqa: BLE001 — network/transport failures all degrade
        logger.warning("twilio_bearer_transport_error error=%s", type(exc).__name__)
        return None
    status = getattr(response, "status_code", 0)
    if not (200 <= int(status) < 300):
        logger.warning("twilio_bearer_http_error status=%s", status)
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — a non-JSON 2xx is still not usable data
        return None


async def list_incoming_numbers(
    *,
    access_token: str,
    account_sid: str,
    phone_number: str | None = None,
    limit: int = 50,
    transport: _BearerTransport | None = None,
) -> list[dict[str, str]] | None:
    """IncomingPhoneNumbers for the account; None when the call failed.

    Returns dicts with ``phone_number``/``friendly_name``/``sid`` — the fields
    the picker (C9) and the webhook configurator consume."""
    params: dict[str, str] = {"PageSize": str(limit)}
    if phone_number:
        params["PhoneNumber"] = phone_number
    body = await _request(
        "GET",
        f"{TWILIO_API_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json",
        access_token=access_token,
        params=params,
        transport=transport,
    )
    if not isinstance(body, dict):
        return None
    rows = body.get("incoming_phone_numbers")
    if not isinstance(rows, list):
        return None
    return [
        {
            "phone_number": str(r.get("phone_number") or ""),
            "friendly_name": str(r.get("friendly_name") or ""),
            "sid": str(r.get("sid") or ""),
        }
        for r in rows
        if isinstance(r, dict) and r.get("phone_number")
    ]


async def resource_exists(
    *,
    access_token: str,
    account_sid: str,
    kind: str,
    resource_sid: str,
    transport: _BearerTransport | None = None,
) -> bool | None:
    """Does this Message/Call SID exist in the account? (C11 defense-in-depth.)

    ``kind`` is ``"Messages"`` or ``"Calls"``. Returns True/False on a definitive
    answer, None when Twilio could not be asked (callers must not treat None as
    proof either way)."""
    if kind not in ("Messages", "Calls") or not resource_sid:
        return None
    import httpx  # noqa: PLC0415

    url = f"{TWILIO_API_BASE}/Accounts/{account_sid}/{kind}/{resource_sid}.json"
    try:
        if transport is not None:
            response = await transport.get(url, headers=_headers(access_token), params={})
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=_headers(access_token), params={})
    except Exception:  # noqa: BLE001
        return None
    status = int(getattr(response, "status_code", 0))
    if status == 200:
        return True
    if status == 404:
        return False
    return None


@dataclass
class TwilioBearerSmsSender:
    """SmsSenderProtocol over bearer auth — the OAuth tenant's send path.

    Same honesty contract as ``TwilioSmsSender``: the returned SID is Twilio's
    own, and None means NOT delivered (the delivery layer refuses to fabricate)."""

    account_sid: str
    access_token: str
    transport: _BearerTransport | None = None
    # The same seam ``TwilioSmsSender.status_callback`` is (round 7 delivery
    # hardening): an absolute URL Twilio posts the message's delivery receipt to. Set
    # by the notification leg only; None keeps the send byte-identical. An OAuth
    # tenant's page used to go out with no receipt requested at all, so it was
    # stamped delivered the moment the API accepted it.
    status_callback: str | None = None

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        data = {"From": from_number, "To": to, "Body": body}
        if self.status_callback:
            data["StatusCallback"] = self.status_callback
        payload = await _request(
            "POST",
            f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Messages.json",
            access_token=self.access_token,
            data=data,
            transport=self.transport,
        )
        if not isinstance(payload, dict):
            return None
        return str(payload.get("sid") or "") or None


@dataclass
class TwilioBearerOperabilityProbe:
    """NumberOperabilityProbe over bearer auth (§3.1.4 BYO-number honesty).

    Same boundary as ``TwilioOperabilityProbe``: presence in the account's
    IncomingPhoneNumbers, never a claim about A2P/10DLC registration; provider
    errors are UNKNOWN, never silently operable."""

    account_sid: str
    access_token: str
    transport: _BearerTransport | None = None

    async def check(self, e164_number: str) -> OperabilityResult:
        rows = await list_incoming_numbers(
            access_token=self.access_token,
            account_sid=self.account_sid,
            phone_number=e164_number,
            limit=1,
            transport=self.transport,
        )
        if rows is None:
            return OperabilityResult.UNKNOWN
        return OperabilityResult.OPERABLE if rows else OperabilityResult.NOT_OPERABLE


@dataclass
class TwilioBearerWebhookConfigurator:
    """TwilioWebhookConfigurator over bearer auth (c20 auto-config for OAuth tenants).

    Same fail-open contract: True on success, False on ANY failure — the caller
    appends manual guidance to status_detail, never blocks the attach."""

    account_sid: str
    access_token: str
    transport: _BearerTransport | None = None

    async def configure(self, e164_number: str, urls: Any) -> bool:
        rows = await list_incoming_numbers(
            access_token=self.access_token,
            account_sid=self.account_sid,
            phone_number=e164_number,
            limit=1,
            transport=self.transport,
        )
        if not rows:
            return False
        number_sid = rows[0].get("sid") or ""
        if not number_sid:
            return False
        updated = await _request(
            "POST",
            f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/IncomingPhoneNumbers/{number_sid}.json",
            access_token=self.access_token,
            data={
                "VoiceUrl": urls.voice_url,
                "SmsUrl": urls.sms_url,
                "StatusCallback": urls.status_callback_url,
            },
            transport=self.transport,
        )
        return isinstance(updated, dict)
