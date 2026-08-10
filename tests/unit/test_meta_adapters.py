"""WhatsApp + Instagram/Messenger adapters (Architecture §3.1.2/§3.1.3)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from app.channels.base import InboundRejected, InstanceContext
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
from app.channels.whatsapp_adapter import NullMetaSender, WhatsAppAdapter

APP_SECRET = "meta-app-secret"
ADMIN = uuid.uuid4()
INSTANCE = uuid.uuid4()


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _resolve(connection_type: str, destination: str) -> InstanceContext:
    # Since the Instagram Login split, an IG-account id is bound on ``instagram_auth`` and a
    # Page id on ``channel_auth``, and the webhook entry does not say which it carries.
    assert connection_type in ("channel_auth", "instagram_auth")
    assert destination  # non-empty resolution key
    return InstanceContext(admin_id=ADMIN, instance_id=INSTANCE)


def _wa_payload() -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PN123"},
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "+14165550143",
                                    "text": {"body": "Hi, are you open?"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def _meta_payload() -> dict:
    return {
        "entry": [
            {
                "id": "PAGE_or_IG_id_1",
                "messaging": [
                    {
                        "sender": {"id": "PSID_1"},
                        "message": {"mid": "m.1", "text": "Do you deliver?"},
                    }
                ],
            }
        ]
    }


# --- WhatsApp -------------------------------------------------------------------- #


async def test_whatsapp_verifies_signature_and_normalizes():
    adapter = WhatsAppAdapter(NullMetaSender())
    body = json.dumps(_wa_payload()).encode()
    request = {
        "app_secret": APP_SECRET,
        "raw_body": body,
        "x_hub_signature_256": _sig(body),
        "payload": _wa_payload(),
        "resolve": _resolve,
    }
    ctx = await adapter.verify_inbound(request)
    assert ctx.admin_id == ADMIN
    msg = await adapter.receive(request, ctx)
    assert msg.channel == "whatsapp"
    assert msg.customer_identifier == "+14165550143"
    assert msg.body == "Hi, are you open?"


async def test_whatsapp_bad_signature_rejected():
    adapter = WhatsAppAdapter(NullMetaSender())
    body = json.dumps(_wa_payload()).encode()
    request = {
        "app_secret": APP_SECRET,
        "raw_body": body,
        "x_hub_signature_256": "sha256=deadbeef",
        "payload": _wa_payload(),
        "resolve": _resolve,
    }
    with pytest.raises(InboundRejected) as e:
        await adapter.verify_inbound(request)
    assert e.value.reason == "meta_signature_invalid"


async def test_whatsapp_send_via_null_sender():
    sender = NullMetaSender()
    adapter = WhatsAppAdapter(sender)
    from app.channels.base import OutboundMessage

    receipt = await adapter.send(
        OutboundMessage(
            channel="whatsapp",
            to="+14165550143",
            body="Yes, we are open!",
            metadata={"phone_number_id": "PN123"},
        )
    )
    assert receipt.accepted
    assert sender.sent[0]["to"] == "+14165550143"
    assert sender.sent[0]["channel"] == "whatsapp"


# --- Instagram / Messenger ------------------------------------------------------- #


async def test_meta_messaging_carries_the_split_channel_ids():
    # §3.1.2: Messenger and Instagram are separate channels on one adapter class; the
    # webhook edge names the channel from the payload's ``object``.
    assert MetaMessagingAdapter().channel_id == "messenger"
    assert MetaMessagingAdapter(channel="instagram").channel_id == "instagram"


@pytest.mark.parametrize("channel", ["messenger", "instagram"])
async def test_meta_messaging_verifies_and_normalizes(channel: str):
    adapter = MetaMessagingAdapter(NullMetaSender(), channel=channel)
    body = json.dumps(_meta_payload()).encode()
    request = {
        "app_secret": APP_SECRET,
        "raw_body": body,
        "x_hub_signature_256": _sig(body),
        "payload": _meta_payload(),
        "resolve": _resolve,
    }
    ctx = await adapter.verify_inbound(request)
    msg = await adapter.receive(request, ctx)
    assert msg.channel == channel
    assert msg.customer_identifier == "PSID_1"
    assert msg.body == "Do you deliver?"


async def test_meta_messaging_falls_back_to_the_facebook_grant():
    """An IG account is bound on the Instagram Login connection and a Page on the
    Facebook one, and the webhook entry does not say which kind of id it carries. The
    adapter must try both, or a tenant who connected only Facebook stops receiving
    Messenger."""
    tried: list[str] = []

    async def _resolve_page_only(connection_type: str, destination: str) -> InstanceContext:
        tried.append(connection_type)
        if connection_type != "channel_auth":
            raise InboundRejected(f"unresolvable_destination:{connection_type}")
        return InstanceContext(admin_id=ADMIN, instance_id=INSTANCE)

    body = json.dumps(_meta_payload()).encode()
    ctx = await MetaMessagingAdapter(NullMetaSender()).verify_inbound(
        {
            "app_secret": APP_SECRET,
            "raw_body": body,
            "x_hub_signature_256": _sig(body),
            "payload": _meta_payload(),
            "resolve": _resolve_page_only,
        }
    )
    assert ctx.admin_id == ADMIN
    assert tried == ["instagram_auth", "channel_auth"]


async def test_meta_messaging_rejects_when_no_grant_owns_the_destination():
    async def _resolve_nobody(connection_type: str, destination: str) -> InstanceContext:
        raise InboundRejected(f"unresolvable_destination:{connection_type}")

    body = json.dumps(_meta_payload()).encode()
    with pytest.raises(InboundRejected) as e:
        await MetaMessagingAdapter(NullMetaSender()).verify_inbound(
            {
                "app_secret": APP_SECRET,
                "raw_body": body,
                "x_hub_signature_256": _sig(body),
                "payload": _meta_payload(),
                "resolve": _resolve_nobody,
            }
        )
    assert e.value.reason == "unresolvable_destination:messenger"


async def test_meta_messaging_bad_signature_rejected():
    adapter = MetaMessagingAdapter(NullMetaSender())
    body = json.dumps(_meta_payload()).encode()
    with pytest.raises(InboundRejected):
        await adapter.verify_inbound(
            {
                "app_secret": APP_SECRET,
                "raw_body": body,
                "x_hub_signature_256": "sha256=wrong",
                "payload": _meta_payload(),
                "resolve": _resolve,
            }
        )


def test_an_adapter_built_without_a_sender_never_reaches_the_network():
    """The Graph token is per-tenant (P1-1), so a session-less adapter has no way to
    resolve one. It must default to Null rather than to a platform credential."""
    assert isinstance(WhatsAppAdapter()._sender, NullMetaSender)
    assert isinstance(MetaMessagingAdapter()._sender, NullMetaSender)
