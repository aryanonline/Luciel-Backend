"""The sweep's one status fetch for a page whose delivery receipt never came (round 7
delivery hardening).

``escalation_dispatch.sweep_stale_escalations`` asks the tenant's OWN Twilio for the
Message resource — the same credential shape the page went out on: the SDK client for
an Auth-Token / API-Key tenant, the bearer surface for a one-click OAuth tenant — and
``sms_notifier.fetch_sms_status`` turns the answer into data. Every failure is None,
"could not be asked", never a status; the caller settles the row as ``unconfirmed``
rather than reading None as anything.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.twilio_bearer import TwilioBearerSmsSender
from app.channels.twilio_sender import NullSmsSender, TwilioSmsSender
from app.notifications import sms_notifier
from app.notifications.sms_notifier import SmsStatus, fetch_sms_status

ACCOUNT = "AC" + "9" * 32
SID = "SM" + "a" * 32
ADMIN, INSTANCE = uuid.uuid4(), uuid.uuid4()


class _Resp:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Transport:
    def __init__(self, *responses: _Resp) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> _Resp:
        self.requests.append(("GET", url, dict(params)))
        assert headers["Authorization"] == "Bearer tok"
        return self._responses.pop(0)

    async def post(self, url: str, *, headers: dict[str, str], data: dict[str, str]) -> _Resp:
        raise AssertionError("a status fetch never posts")


# --- the bearer surface (OAuth tenants) ---------------------------------------------- #
async def test_the_bearer_sender_fetches_the_message_resource_over_bearer_auth():
    t = _Transport(_Resp(200, {"sid": SID, "status": "delivered", "error_code": None}))
    sender = TwilioBearerSmsSender(account_sid=ACCOUNT, access_token="tok", transport=t)  # noqa: S106
    assert await sender.fetch_message_status(SID) == ("delivered", None)
    assert t.requests == [
        ("GET", f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{SID}.json", {})
    ]


async def test_the_bearer_sender_carries_the_carriers_error_code():
    t = _Transport(_Resp(200, {"status": "undelivered", "error_code": 30003}))
    sender = TwilioBearerSmsSender(account_sid=ACCOUNT, access_token="tok", transport=t)  # noqa: S106
    assert await sender.fetch_message_status(SID) == ("undelivered", "30003")


@pytest.mark.parametrize(
    "response",
    [_Resp(401, {}), _Resp(404, {"code": 20404}), _Resp(200, {"sid": SID}), _Resp(200, [])],
)
async def test_the_bearer_sender_answers_none_when_the_provider_gives_no_status(response):
    sender = TwilioBearerSmsSender(
        account_sid=ACCOUNT,
        access_token="tok",  # noqa: S106
        transport=_Transport(response),
    )
    assert await sender.fetch_message_status(SID) is None


# --- the SDK client (Auth-Token / API-Key tenants) ----------------------------------- #
class _FakeMessage:
    def __init__(self, status: str, error_code: int | None, fail: Exception | None) -> None:
        self._status, self._error_code, self._fail = status, error_code, fail

    def fetch(self) -> object:
        if self._fail is not None:
            raise self._fail

        class _Resource:
            status = self._status
            error_code = self._error_code

        return _Resource()


class _FakeClient:
    def __init__(self, status: str = "", error_code: int | None = None, fail=None) -> None:
        self.fetched: list[str] = []
        self._message = _FakeMessage(status, error_code, fail)

    def messages(self, sid: str) -> _FakeMessage:
        self.fetched.append(sid)
        return self._message


async def test_the_sdk_sender_fetches_by_sid_and_reports_status_and_error_code():
    client = _FakeClient("undelivered", 30005)
    sender = TwilioSmsSender(account_sid=ACCOUNT, auth_token="tok")  # noqa: S106
    sender._client = client
    assert await sender.fetch_message_status(SID) == ("undelivered", "30005")
    assert client.fetched == [SID]


async def test_the_sdk_sender_answers_none_without_a_status_and_lets_a_transport_error_through():
    sender = TwilioSmsSender(account_sid=ACCOUNT, auth_token="tok")  # noqa: S106
    sender._client = _FakeClient("")
    assert await sender.fetch_message_status(SID) is None
    sender._client = _FakeClient(fail=ConnectionError("twilio unreachable"))
    with pytest.raises(ConnectionError):
        await sender.fetch_message_status(SID)


# --- the notifier's seam: an unanswerable provider is None, never a status ---------- #
async def _bind(monkeypatch, sender: object) -> None:
    async def _sender(*a, **kw):
        return sender

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)


async def test_fetch_sms_status_normalises_the_providers_word(monkeypatch):
    client = _FakeClient("Delivered", None)
    sender = TwilioSmsSender(account_sid=ACCOUNT, auth_token="tok")  # noqa: S106
    sender._client = client
    await _bind(monkeypatch, sender)
    assert await fetch_sms_status(None, ADMIN, INSTANCE, SID) == SmsStatus("delivered", None)  # type: ignore[arg-type]
    sender._client = _FakeClient("undelivered", 30003)
    assert await fetch_sms_status(None, ADMIN, INSTANCE, SID) == SmsStatus(  # type: ignore[arg-type]
        "undelivered", "30003"
    )


async def test_fetch_sms_status_is_none_when_the_provider_cannot_be_asked(monkeypatch):
    # The mock/Null sender has no status resource (nothing ever left the system).
    await _bind(monkeypatch, NullSmsSender())
    assert await fetch_sms_status(None, ADMIN, INSTANCE, SID) is None  # type: ignore[arg-type]
    # A transport failure is data, not a raise — and not a status.
    sender = TwilioSmsSender(account_sid=ACCOUNT, auth_token="tok")  # noqa: S106
    sender._client = _FakeClient(fail=ConnectionError("twilio unreachable"))
    await _bind(monkeypatch, sender)
    assert await fetch_sms_status(None, ADMIN, INSTANCE, SID) is None  # type: ignore[arg-type]
    # No status in the answer is no answer.
    sender._client = _FakeClient("")
    assert await fetch_sms_status(None, ADMIN, INSTANCE, SID) is None  # type: ignore[arg-type]
