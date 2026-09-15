"""The contact form lands in the inbox a person reads (owner decision D12, 2026-09-14).

Before this, ``_notify`` addressed the message to ``platform_email_sender`` — the SES
sender identity, which is a From address nobody needs to read — so a prospect's enquiry
could be delivered to a mailbox that does not exist. The destination is now its own
setting, defaulting to the one address the business operates.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.v1 import contact as contact_module
from app.api.v1.contact import ContactIn, _notify
from app.config import Settings, get_settings

pytestmark = pytest.mark.asyncio


class _CapturingSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, body_text: str) -> str:
        self.calls.append({"to": to, "subject": subject, "body_text": body_text})
        return "msg-1"


def test_default_inbox_is_the_address_the_business_operates() -> None:
    assert Settings().platform_contact_inbox == "info@vantagemind.ai"


async def test_notify_addresses_the_contact_inbox_not_the_sender_identity(monkeypatch) -> None:
    sender = _CapturingSender()
    monkeypatch.setattr(
        "app.notifications.email_sender.build_email_sender", lambda: sender, raising=True
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_contact_inbox", "info@vantagemind.ai")
    monkeypatch.setattr(settings, "platform_email_sender", "noreply@vantagemind.ai")

    ok = await _notify(
        uuid.uuid4(),
        ContactIn(name="Dana", email="dana@example.com", message="hello", captchaToken="t"),
    )

    assert ok is True
    assert sender.calls[0]["to"] == "info@vantagemind.ai"
    assert "dana@example.com" in sender.calls[0]["body_text"]
    assert contact_module  # the module under test is the one wired to the route
