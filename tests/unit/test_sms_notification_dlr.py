"""A page's ``delivered`` comes from the carrier, not from Twilio accepting it (round 7
WP-4b).

The escalation SMS leg asks Twilio to post the message's delivery receipt to
``/api/v1/webhooks/twilio/notifications/status`` (with the tenant's capability token
on the URL when that is how their inbound verifies) and reports ``dlr_pending`` so the
dispatcher stamps the row ``sent`` rather than ``delivered``.

Round 7 delivery hardening: the bearer sender of a one-click OAuth tenant asks for the
same receipt (it used to send with none requested, so its pages read ``delivered`` on
API accept), and an accepted send through ANY real provider sender is ``dlr_pending``
even when no callback could be requested — only the mock/Null sender reports what it
always did.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import tenant_twilio
from app.channels.twilio_bearer import TwilioBearerSmsSender
from app.channels.twilio_sender import NullSmsSender, TwilioSmsSender
from app.config import get_settings
from app.notifications import sms_notifier
from app.notifications.sms_notifier import (
    DLR_PENDING,
    NOTIFICATION_STATUS_PATH,
    SmsNotifier,
    notification_status_callback_url,
)

ADMIN, INSTANCE = uuid.uuid4(), uuid.uuid4()
CALLBACK = "https://api.example/api/v1/webhooks/twilio/notifications/status"


class _FakeMessages:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs

        class _Msg:
            sid = "SM-real"

        return _Msg()


class _FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


@pytest.fixture
def from_number(monkeypatch) -> None:
    async def _resolve(*a, **kw):
        return "+15559990000"

    monkeypatch.setattr(sms_notifier, "resolve_sms_number", _resolve)


async def test_a_live_sender_is_asked_for_the_receipt_and_the_receipt_says_dlr_pending(
    monkeypatch, from_number
):
    client = _FakeTwilioClient()
    sender = TwilioSmsSender(account_sid="AC" + "1" * 32, auth_token="tok")  # noqa: S106
    sender._client = client

    async def _sender(*a, **kw):
        return sender

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)
    receipt = await SmsNotifier().send_text(
        None,  # type: ignore[arg-type] — the fakes never touch the session
        admin_id=ADMIN,
        instance_id=INSTANCE,
        to="+15550001111",
        body="page",
        status_callback=CALLBACK,
    )
    assert receipt.accepted and receipt.provider_message_id == "SM-real"
    assert receipt.detail == DLR_PENDING
    assert client.messages.kwargs["status_callback"] == CALLBACK
    assert sender.status_callback is None  # the tenant's sender itself is untouched


async def test_without_a_callback_the_request_is_unchanged_but_never_reads_delivered(
    monkeypatch, from_number
):
    """No public API origin (or a tenant whose receipts could not be verified): the
    request carries no callback, and the receipt STILL says the carrier has not spoken
    — Twilio accepting a request was read as ``delivered`` here."""
    client = _FakeTwilioClient()
    sender = TwilioSmsSender(account_sid="AC" + "1" * 32, auth_token="tok")  # noqa: S106
    sender._client = client

    async def _sender(*a, **kw):
        return sender

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)
    receipt = await SmsNotifier().send_text(
        None,  # type: ignore[arg-type]
        admin_id=ADMIN,
        instance_id=INSTANCE,
        to="+15550001111",
        body="page",
    )
    assert receipt.accepted and receipt.detail == DLR_PENDING
    assert "status_callback" not in client.messages.kwargs


async def test_the_null_sender_never_claims_a_receipt_is_coming(monkeypatch, from_number):
    async def _sender(*a, **kw):
        return NullSmsSender()

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)
    receipt = await SmsNotifier().send_text(
        None,  # type: ignore[arg-type]
        admin_id=ADMIN,
        instance_id=INSTANCE,
        to="+15550001111",
        body="page",
        status_callback=CALLBACK,
    )
    assert receipt.accepted and receipt.detail == "recorded"  # mock: recorded, not sent


async def test_the_callback_url_carries_the_capability_token_and_needs_a_public_origin(
    monkeypatch,
):
    async def _verification(*a, **kw):
        return None, "cap/tok+1"

    monkeypatch.setattr(tenant_twilio, "tenant_inbound_verification", _verification)
    monkeypatch.setattr(get_settings(), "public_api_base_url", "https://api.example/")
    url = await notification_status_callback_url(None, ADMIN, INSTANCE)  # type: ignore[arg-type]
    assert url == f"https://api.example{NOTIFICATION_STATUS_PATH}?cap=cap%2Ftok%2B1"

    async def _no_cap(*a, **kw):
        return "auth-token", None

    monkeypatch.setattr(tenant_twilio, "tenant_inbound_verification", _no_cap)
    url = await notification_status_callback_url(None, ADMIN, INSTANCE)  # type: ignore[arg-type]
    assert url == f"https://api.example{NOTIFICATION_STATUS_PATH}"

    monkeypatch.setattr(get_settings(), "public_api_base_url", "")
    assert await notification_status_callback_url(None, ADMIN, INSTANCE) is None  # type: ignore[arg-type]


async def test_no_callback_is_requested_for_a_tenant_whose_receipts_could_never_verify(
    monkeypatch,
):
    """Neither an auth token nor a capability token is held: Twilio's receipt could
    only ever be answered 403. Verification is not loosened for them — no receipt is
    asked for, and the page stays ``sent`` until the sweep's status fetch."""

    async def _nothing(*a, **kw):
        return None, None

    monkeypatch.setattr(tenant_twilio, "tenant_inbound_verification", _nothing)
    monkeypatch.setattr(get_settings(), "public_api_base_url", "https://api.example/")
    assert await notification_status_callback_url(None, ADMIN, INSTANCE) is None  # type: ignore[arg-type]


class _Resp:
    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body


class _FakeTwilioRest:
    """The httpx subset the bearer surface uses, recording what it was sent."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def post(self, url: str, *, headers: dict[str, str], data: dict[str, str]) -> _Resp:
        self.posts.append({"url": url, "headers": headers, "data": dict(data)})
        return _Resp(201, {"sid": "SM-oauth"})

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> _Resp:
        raise AssertionError("no GET expected")


async def test_an_oauth_tenants_bearer_sender_is_asked_for_the_receipt_too(
    monkeypatch, from_number
):
    """The one-click OAuth grant sends through ``TwilioBearerSmsSender``. It was not
    the SDK sender class, so the page went out with From/To/Body only — no receipt
    requested, ``delivered`` stamped on API accept, and a carrier failure that nobody
    was ever told about."""
    rest = _FakeTwilioRest()
    sender = TwilioBearerSmsSender(
        account_sid="AC" + "2" * 32,
        access_token="bearer-tok",  # noqa: S106
        transport=rest,
    )

    async def _sender(*a, **kw):
        return sender

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)
    receipt = await SmsNotifier().send_text(
        None,  # type: ignore[arg-type] — the fakes never touch the session
        admin_id=ADMIN,
        instance_id=INSTANCE,
        to="+15550001111",
        body="page",
        status_callback=CALLBACK + "?cap=tok",
    )
    assert receipt.accepted and receipt.provider_message_id == "SM-oauth"
    assert receipt.detail == DLR_PENDING
    (post,) = rest.posts
    assert post["url"].endswith("/Accounts/AC" + "2" * 32 + "/Messages.json")
    assert post["headers"] == {"Authorization": "Bearer bearer-tok"}
    assert post["data"] == {
        "From": "+15559990000",
        "To": "+15550001111",
        "Body": "page",
        "StatusCallback": CALLBACK + "?cap=tok",
    }
    assert sender.status_callback is None  # the tenant's sender itself is untouched


async def test_a_bearer_send_without_a_callback_is_unchanged_but_never_reads_delivered(
    monkeypatch, from_number
):
    rest = _FakeTwilioRest()

    async def _sender(*a, **kw):
        return TwilioBearerSmsSender(
            account_sid="AC" + "2" * 32,
            access_token="bearer-tok",  # noqa: S106
            transport=rest,
        )

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)
    receipt = await SmsNotifier().send_text(
        None,  # type: ignore[arg-type]
        admin_id=ADMIN,
        instance_id=INSTANCE,
        to="+15550001111",
        body="code",
    )
    assert receipt.accepted and receipt.detail == DLR_PENDING
    assert rest.posts[0]["data"] == {"From": "+15559990000", "To": "+15550001111", "Body": "code"}
