"""WhatsApp + Instagram/Messenger adapters (Architecture §3.1.2/§3.1.3)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from app.channels.base import InboundRejected, InstanceContext
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
from app.channels.meta_sender import build_meta_sender
from app.channels.whatsapp_adapter import NullMetaSender, WhatsAppAdapter

APP_SECRET = "meta-app-secret"
ADMIN = uuid.uuid4()
INSTANCE = uuid.uuid4()


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _resolve(connection_type: str, destination: str) -> InstanceContext:
    assert connection_type == "channel_auth"
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


async def test_meta_messaging_channel_id_is_combined():
    assert MetaMessagingAdapter().channel_id == "instagram_messenger"


async def test_meta_messaging_verifies_and_normalizes():
    adapter = MetaMessagingAdapter(NullMetaSender())
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
    assert msg.channel == "instagram_messenger"
    assert msg.customer_identifier == "PSID_1"
    assert msg.body == "Do you deliver?"


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


def test_build_meta_sender_defaults_to_null_in_mock():
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert isinstance(build_meta_sender(), NullMetaSender)
