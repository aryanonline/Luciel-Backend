"""Round 6 WP-H (F141): the send_email tool sends AS the tenant, through the same seam
a reply uses — never from a platform address behind the owner's back."""

from __future__ import annotations

import uuid

import pytest
from app.channels import outbound
from app.channels.base import DeliveryReceipt
from app.tools.contract import ToolContext
from app.tools.registry import _EmailMessengerBackend


class _Session:
    """Only its presence matters: with a session the tenant seam is used."""


async def test_with_a_session_the_email_goes_out_through_the_tenant_seam(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_send(session, admin_id, instance_id, *, to, body, subject=None):
        seen.update({"to": to, "body": body, "subject": subject})
        return DeliveryReceipt(accepted=True, provider_message_id=None)

    monkeypatch.setattr(outbound, "send_outbound_email", fake_send)
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), session=_Session())
    marker = await _EmailMessengerBackend().send(ctx, "dana@lead.example", "Here are the details.")
    assert marker == "mailbox:accepted"
    assert seen == {
        "to": "dana@lead.example",
        "body": "Here are the details.",
        "subject": _EmailMessengerBackend.SUBJECT,
    }


@pytest.mark.parametrize("detail", ["channel_not_provisioned", "mailbox_reconnect_needed"])
async def test_no_usable_mailbox_is_provider_rejected_never_a_fabricated_send(monkeypatch, detail):
    async def refused(session, admin_id, instance_id, *, to, body, subject=None):
        return DeliveryReceipt(accepted=False, detail=detail)

    monkeypatch.setattr(outbound, "send_outbound_email", refused)
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), session=_Session())
    assert await _EmailMessengerBackend().send(ctx, "dana@lead.example", "hi") is None
