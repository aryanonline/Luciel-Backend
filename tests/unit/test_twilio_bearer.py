"""Bearer-token Twilio REST surface (audit round 3, C10).

An OAuth tenant holds an access token, not the SDK's credential shapes — these
tests pin the thin httpx wrapper the factories bind for them: number listing,
webhook pointing, the send path, the operability probe, and the C11
resource-existence check. Every failure degrades to the honest value (None /
UNKNOWN / False), never a fabricated success.
"""

from __future__ import annotations

import pytest
from app.channels.number_operability import OperabilityResult
from app.channels.tenant_twilio import WebhookUrls
from app.channels.twilio_bearer import (
    TwilioBearerOperabilityProbe,
    TwilioBearerSmsSender,
    TwilioBearerWebhookConfigurator,
    list_incoming_numbers,
    resource_exists,
)

pytestmark = [pytest.mark.asyncio]

ACCOUNT = "AC" + "9" * 32


class _Resp:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Transport:
    """Scripted transport: each call pops the next response; records requests."""

    def __init__(self, *responses: _Resp) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def _next(self) -> _Resp:
        return self._responses.pop(0)

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> _Resp:
        self.requests.append(("GET", url, dict(params)))
        assert headers["Authorization"].startswith("Bearer ")
        return self._next()

    async def post(self, url: str, *, headers: dict[str, str], data: dict[str, str]) -> _Resp:
        self.requests.append(("POST", url, dict(data)))
        assert headers["Authorization"].startswith("Bearer ")
        return self._next()


_NUMBER_ROW = {
    "phone_number": "+16045550100",
    "friendly_name": "Shopline",
    "sid": "PN" + "1" * 32,
}


async def test_list_numbers_returns_the_picker_fields():
    t = _Transport(_Resp(200, {"incoming_phone_numbers": [_NUMBER_ROW]}))
    rows = await list_incoming_numbers(access_token="tok", account_sid=ACCOUNT, transport=t)
    assert rows == [
        {"phone_number": "+16045550100", "friendly_name": "Shopline", "sid": "PN" + "1" * 32}
    ]
    method, url, _ = t.requests[0]
    assert method == "GET"
    assert url == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/IncomingPhoneNumbers.json"


async def test_list_numbers_is_none_on_provider_refusal():
    t = _Transport(_Resp(401, {}))
    assert await list_incoming_numbers(access_token="tok", account_sid=ACCOUNT, transport=t) is None


async def test_sender_returns_the_provider_sid_and_never_fabricates():
    ok = _Transport(_Resp(201, {"sid": "SM" + "2" * 32}))
    sender = TwilioBearerSmsSender(account_sid=ACCOUNT, access_token="tok", transport=ok)
    assert await sender.send_sms("+16045550100", "+16045550111", "hi") == "SM" + "2" * 32
    _, url, data = ok.requests[0]
    assert url.endswith(f"/Accounts/{ACCOUNT}/Messages.json")
    assert data == {"From": "+16045550100", "To": "+16045550111", "Body": "hi"}

    refused = _Transport(_Resp(400, {"message": "nope"}))
    sender = TwilioBearerSmsSender(account_sid=ACCOUNT, access_token="tok", transport=refused)
    assert await sender.send_sms("+16045550100", "+16045550111", "hi") is None


async def test_probe_maps_presence_absence_and_error_honestly():
    present = TwilioBearerOperabilityProbe(
        account_sid=ACCOUNT,
        access_token="tok",
        transport=_Transport(_Resp(200, {"incoming_phone_numbers": [_NUMBER_ROW]})),
    )
    assert await present.check("+16045550100") == OperabilityResult.OPERABLE

    absent = TwilioBearerOperabilityProbe(
        account_sid=ACCOUNT,
        access_token="tok",
        transport=_Transport(_Resp(200, {"incoming_phone_numbers": []})),
    )
    assert await absent.check("+16045550100") == OperabilityResult.NOT_OPERABLE

    erroring = TwilioBearerOperabilityProbe(
        account_sid=ACCOUNT, access_token="tok", transport=_Transport(_Resp(500, {}))
    )
    # NEVER operable on error — that would reintroduce the silent-failure bug.
    assert await erroring.check("+16045550100") == OperabilityResult.UNKNOWN


async def test_webhook_configurator_updates_the_number_by_sid():
    urls = WebhookUrls(
        voice_url="https://api.x/api/v1/webhooks/twilio/voice?cap=T",
        sms_url="https://api.x/api/v1/webhooks/twilio/sms?cap=T",
        status_callback_url="https://api.x/api/v1/webhooks/twilio/voice/status?cap=T",
    )
    t = _Transport(
        _Resp(200, {"incoming_phone_numbers": [_NUMBER_ROW]}),
        _Resp(200, {"sid": _NUMBER_ROW["sid"]}),
    )
    cfg = TwilioBearerWebhookConfigurator(account_sid=ACCOUNT, access_token="tok", transport=t)
    assert await cfg.configure("+16045550100", urls) is True
    method, url, data = t.requests[1]
    assert method == "POST"
    assert url.endswith(f"/IncomingPhoneNumbers/{_NUMBER_ROW['sid']}.json")
    assert data == {
        "VoiceUrl": urls.voice_url,
        "SmsUrl": urls.sms_url,
        "StatusCallback": urls.status_callback_url,
    }


async def test_webhook_configurator_is_false_when_the_number_is_not_in_the_account():
    t = _Transport(_Resp(200, {"incoming_phone_numbers": []}))
    cfg = TwilioBearerWebhookConfigurator(account_sid=ACCOUNT, access_token="tok", transport=t)
    urls = WebhookUrls(voice_url="v", sms_url="s", status_callback_url="c")
    assert await cfg.configure("+16045550100", urls) is False


async def test_resource_exists_is_three_valued():
    assert (
        await resource_exists(
            access_token="tok",
            account_sid=ACCOUNT,
            kind="Messages",
            resource_sid="SM" + "3" * 32,
            transport=_Transport(_Resp(200, {"sid": "SM" + "3" * 32})),
        )
        is True
    )
    assert (
        await resource_exists(
            access_token="tok",
            account_sid=ACCOUNT,
            kind="Calls",
            resource_sid="CA" + "3" * 32,
            transport=_Transport(_Resp(404, {})),
        )
        is False
    )
    # A provider error is NOT an answer — callers must not treat it as proof.
    assert (
        await resource_exists(
            access_token="tok",
            account_sid=ACCOUNT,
            kind="Messages",
            resource_sid="SM" + "3" * 32,
            transport=_Transport(_Resp(503, {})),
        )
        is None
    )
    # Unknown kinds are refused outright (no URL construction from bad input).
    assert (
        await resource_exists(
            access_token="tok",
            account_sid=ACCOUNT,
            kind="Accounts",
            resource_sid="AC" + "3" * 32,
            transport=_Transport(_Resp(200, {})),
        )
        is None
    )
