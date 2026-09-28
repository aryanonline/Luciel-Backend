"""A page's ``delivered`` comes from the carrier, not from Twilio accepting it (round 7
WP-4b).

The escalation SMS leg asks Twilio to post the message's delivery receipt to
``/api/v1/webhooks/twilio/notifications/status`` (with the tenant's capability token
on the URL when that is how their inbound verifies) and reports ``dlr_pending`` so the
dispatcher stamps the row ``sent`` rather than ``delivered``. Where no receipt can come
(the Null sender, no public API origin) the receipt is what it always was.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import tenant_twilio
from app.channels.twilio_sender import NullSmsSender, TwilioSmsSender
from app.config import get_settings
from app.notifications import sms_notifier
from app.notifications.sms_notifier import (
    DLR_PENDING,
    NOTIFICATION_STATUS_PATH,
    SmsNotifier,
    notification_status_callback_url,
)

pytestmark = [pytest.mark.asyncio]

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


async def test_without_a_callback_the_send_is_byte_identical(monkeypatch, from_number):
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
    assert receipt.accepted and receipt.detail is None
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
