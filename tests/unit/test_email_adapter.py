"""Email channel adapter — per-tenant sender identity + inbound resolution (§3.1.6a).

The adapter must send FROM the tenant's provisioned address (threaded via
``OutboundMessage.metadata["sender_identity"]``); there is no platform fallback
address, so a message without one fails closed before the provider is called. Two
tenants therefore send from two different addresses. Inbound
resolution keys on the recipient address, resolved to exactly one tenant (§3.1.3).
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.base import InstanceContext, OutboundMessage
from app.channels.email_adapter import EmailAdapter


class _CapturingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    async def send_email(
        self,
        sender: str,
        to: str,
        subject: str,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        self.sent.append((sender, to, subject, body))
        return f"msg-{len(self.sent)}"


class _OkVerifier:
    def verify(self, message: dict) -> bool:  # noqa: ANN001
        return True


def _adapter() -> tuple[EmailAdapter, _CapturingSender]:
    sender = _CapturingSender()
    return EmailAdapter(sender=sender, sns_verifier=_OkVerifier()), sender


async def test_send_uses_per_tenant_identity_not_fallback():
    adapter, sender = _adapter()
    await adapter.send(
        OutboundMessage(
            channel="email",
            to="lead@customer.com",
            body="hi",
            metadata={"sender_identity": "acme@acme.example"},
        )
    )
    assert sender.sent[0][0] == "acme@acme.example"


async def test_two_tenants_send_from_two_addresses():
    adapter, sender = _adapter()
    for addr in ("t1@one.example", "t2@two.example"):
        await adapter.send(
            OutboundMessage(
                channel="email", to="lead@x.com", body="hi", metadata={"sender_identity": addr}
            )
        )
    senders = {s[0] for s in sender.sent}
    assert senders == {"t1@one.example", "t2@two.example"}


@pytest.mark.parametrize("metadata", [{}, {"sender_identity": ""}, {"sender_identity": None}])
async def test_send_without_a_sender_identity_fails_closed(metadata: dict[str, object]) -> None:
    adapter, sender = _adapter()
    receipt = await adapter.send(
        OutboundMessage(channel="email", to="lead@x.com", body="hi", metadata=metadata)
    )
    # No platform fallback address: nothing reaches the provider and the receipt says
    # why, with no provider message id to mistake for a send.
    assert sender.sent == []
    assert receipt.accepted is False
    assert receipt.provider_message_id is None
    assert receipt.detail == "no_sender_identity"


async def test_inbound_resolves_recipient_to_tenant():
    adapter, _ = _adapter()
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    seen: dict[str, str] = {}

    async def resolve(connection_type: str, dest: str) -> InstanceContext:
        seen["ct"] = connection_type
        seen["dest"] = dest
        return InstanceContext(admin_id=admin_id, instance_id=instance_id)

    ctx = await adapter.verify_inbound(
        {"sns_message": {}, "resolve": resolve, "recipient": "acme@acme.example"}
    )
    # Resolution keys the recipient (own-domain or subdomain address) on email_sender.
    assert seen == {"ct": "email_sender", "dest": "acme@acme.example"}
    assert ctx.admin_id == admin_id and ctx.instance_id == instance_id
