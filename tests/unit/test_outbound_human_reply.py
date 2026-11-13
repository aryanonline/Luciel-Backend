"""Outbound dispatch for admin takeover replies (Architecture §3.4.12 / §11.5-11.7).

An admin who takes a conversation over must reach the customer on the channel they
wrote in on. These tests pin the two things that are easy to get wrong: a reply that
cannot be pushed must say so instead of claiming delivery, and a reply that CAN be
pushed must actually go through the channel adapter with the tenant's own sender.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import outbound
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection


async def _seed_sms_sender(admin: uuid.UUID, instance: uuid.UUID, number: str) -> None:
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=instance,
                connection_type="sms_sender",
                auth_class="api_key",
                provider="twilio",
                status="connected",
                secret_ref=secret_ref_for(admin, instance, "sms_sender"),
                non_secret_config={"destination": number},
            )
        )
        await s.flush()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_widget_reply_is_poll_only_not_a_claimed_send():
    """The browser holds the widget connection, not us — there is nothing to push to."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s, admin_id=admin, instance_id=instance, channel="widget", to=None, body="hi"
        )
    assert result.delivered is False
    assert result.detail == "widget_poll_only"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_reply_goes_through_the_adapter_with_the_tenant_number(monkeypatch):
    """A pushable channel must really be pushed — and from the tenant's OWN number
    (§3.1.4 BYO), not a platform default."""
    sent: dict[str, str] = {}

    class FakeSender:
        async def send_sms(self, from_number, to, body):
            sent.update(from_number=from_number, to=to, body=body)
            return "SM-123"

    from app.channels import tenant_twilio

    async def _tenant_sender(*a, **kw):
        return FakeSender()

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)

    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_sms_sender(admin, instance, "+15550001111")
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=instance,
            channel="sms",
            to="+15557778888",
            body="Ana here, we open at 9.",
        )
    assert result.delivered is True
    assert result.provider_message_id == "SM-123"
    assert sent == {
        "from_number": "+15550001111",
        "to": "+15557778888",
        "body": "Ana here, we open at 9.",
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_reply_without_a_provisioned_number_is_reported_not_raised():
    """A delivery problem is data, not an exception: the transcript row is already
    written by the time we get here, so failing the request would lose the reply."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s, admin_id=admin, instance_id=instance, channel="sms", to="+15557778888", body="hi"
        )
    assert result.delivered is False
    assert result.detail == "channel_not_provisioned"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reply_with_no_recipient_on_file_is_reported():
    admin, instance = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s, admin_id=admin, instance_id=instance, channel="sms", to=None, body="hi"
        )
    assert result.delivered is False
    assert result.detail == "no_recipient"
