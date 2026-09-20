"""Blocking provider SDK calls run off the event loop (round 7 WP-2, item 5).

``SesEmailSender.send`` (boto3) and ``TwilioSmsSender.send_sms`` (twilio REST) are
synchronous SDK calls that used to run on the event-loop thread, stalling every other
conversation the process was serving for the duration of one HTTP round trip. Both now
go through ``asyncio.to_thread``: the fakes here record the thread the SDK call ran on
and assert it was not the loop's.
"""

from __future__ import annotations

import threading

import pytest
from app.channels.twilio_sender import TwilioSmsSender
from app.notifications.email_sender import SesEmailSender

pytestmark = [pytest.mark.asyncio]


class _FakeSesClient:
    def __init__(self) -> None:
        self.thread: int | None = None
        self.kwargs: dict[str, object] = {}

    def send_email(self, **kwargs: object) -> dict[str, str]:
        self.thread = threading.get_ident()
        self.kwargs = kwargs
        return {"MessageId": "ses-1"}


class _FakeMessages:
    def __init__(self) -> None:
        self.thread: int | None = None

    def create(self, *, from_: str, to: str, body: str) -> object:
        self.thread = threading.get_ident()

        class _Msg:
            sid = "SM1"

        return _Msg()


class _FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


async def test_ses_send_runs_off_the_event_loop_thread() -> None:
    sender = SesEmailSender(sender_identity="noreply@vantagemind.ai", region_name="ca-central-1")
    client = _FakeSesClient()
    sender._client = client
    mid = await sender.send(to="owner@acme.example", subject="s", body_text="b")
    assert mid == "ses-1"
    assert client.thread is not None and client.thread != threading.get_ident()
    assert client.kwargs["Destination"] == {"ToAddresses": ["owner@acme.example"]}


async def test_twilio_send_runs_off_the_event_loop_thread() -> None:
    sender = TwilioSmsSender(
        account_sid="ACfixturefixturefixturefixture01",
        auth_token="not-a-real-token",  # noqa: S106
    )
    client = _FakeTwilioClient()
    sender._client = client
    sid = await sender.send_sms("+15005550006", "+15551234567", "hello")
    assert sid == "SM1"
    assert client.messages.thread is not None
    assert client.messages.thread != threading.get_ident()
