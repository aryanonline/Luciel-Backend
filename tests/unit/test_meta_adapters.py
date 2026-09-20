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


def _echo_payload() -> dict:
    """A real-shaped ``message_echoes`` webhook: the PAGE is the sender and the
    customer the recipient, ``message.is_echo`` is true, and ``app_id`` names the app
    that sent it — this is the page's own outbound reflected back."""
    return {
        "object": "page",
        "entry": [
            {
                "id": "PAGE_or_IG_id_1",
                "time": 1757937600000,
                "messaging": [
                    {
                        "sender": {"id": "PAGE_or_IG_id_1"},
                        "recipient": {"id": "PSID_1"},
                        "timestamp": 1757937599000,
                        "message": {
                            "is_echo": True,
                            "app_id": 1517776481860111,
                            "metadata": "DEVELOPER_DEFINED_METADATA_STRING",
                            "mid": "m.echo.1",
                            "text": "Yes, we deliver within the city. This is an AI assistant.",
                        },
                    }
                ],
            }
        ],
    }


def _standby_payload() -> dict:
    """A real-shaped handover-protocol ``standby`` webhook: another app holds the
    thread; this app only observes."""
    return {
        "object": "page",
        "entry": [
            {
                "id": "PAGE_or_IG_id_1",
                "time": 1757937600000,
                "standby": [
                    {
                        "sender": {"id": "PSID_1"},
                        "recipient": {"id": "PAGE_or_IG_id_1"},
                        "timestamp": 1757937599000,
                        "message": {"mid": "m.standby.1", "text": "Is anyone there?"},
                    }
                ],
            }
        ],
    }


def _signed(payload: dict, resolve=_resolve) -> dict:
    body = json.dumps(payload).encode()
    return {
        "app_secret": APP_SECRET,
        "raw_body": body,
        "x_hub_signature_256": _sig(body),
        "payload": payload,
        "resolve": resolve,
    }


async def test_an_echo_of_the_pages_own_outbound_is_never_a_customer_message():
    """Round 7 WP-3a: ``message.is_echo`` events (the page's own replies, sender.id =
    the page) were ingested as customer messages keyed on the PAGE id — Luciel would
    open a conversation with itself and answer its own words. Skipped with a reason
    that names the echo, and the tenant is never even resolved for it."""
    resolved: list[str] = []

    async def _resolve_and_count(connection_type: str, destination: str) -> InstanceContext:
        resolved.append(destination)
        return InstanceContext(admin_id=ADMIN, instance_id=INSTANCE)

    adapter = MetaMessagingAdapter(NullMetaSender())
    with pytest.raises(InboundRejected) as e:
        await adapter.verify_inbound(_signed(_echo_payload(), _resolve_and_count))
    assert e.value.reason == "meta_echo_ignored"
    assert resolved == []


async def test_a_standby_event_is_ignored_because_handover_is_not_supported():
    """``entry.standby`` is the handover protocol's feed for an app that does NOT hold
    the thread. Luciel implements no handover, so answering a standby event would
    speak over whichever app does. Ignored with a reason that names it."""
    adapter = MetaMessagingAdapter(NullMetaSender(), channel="instagram")
    with pytest.raises(InboundRejected) as e:
        await adapter.verify_inbound(_signed(_standby_payload()))
    assert e.value.reason == "meta_standby_ignored"


async def test_a_customer_message_batched_after_an_echo_is_still_received():
    """Meta batches events: an echo of Luciel's last reply can precede the customer's
    next message in one delivery. The echo is skipped, the customer's turn is the one
    received — keyed on the PSID, never on the page id."""
    payload = _echo_payload()
    payload["entry"][0]["messaging"].append(
        {
            "sender": {"id": "PSID_1"},
            "recipient": {"id": "PAGE_or_IG_id_1"},
            "timestamp": 1757937601000,
            "message": {"mid": "m.2", "text": "Great, how much is delivery?"},
        }
    )
    adapter = MetaMessagingAdapter(NullMetaSender())
    request = _signed(payload)
    ctx = await adapter.verify_inbound(request)
    msg = await adapter.receive(request, ctx)
    assert msg.customer_identifier == "PSID_1"
    assert msg.body == "Great, how much is delivery?"
    assert msg.channel_metadata["mid"] == "m.2"


async def test_a_messaging_event_with_no_sender_is_refused_not_admitted_blank():
    """A messaging event with no ``sender.id`` yielded customer_identifier "" — which
    the runtime then turned into a fresh, unkeyable, unanswerable session per message
    (round 7 WP-3a). The adapter refuses it with a reason instead."""
    payload = _meta_payload()
    del payload["entry"][0]["messaging"][0]["sender"]
    adapter = MetaMessagingAdapter(NullMetaSender())
    request = _signed(payload)
    ctx = await adapter.verify_inbound(request)  # the destination still resolves
    with pytest.raises(InboundRejected) as e:
        await adapter.receive(request, ctx)
    assert e.value.reason == "meta_no_sender"


async def test_a_payload_with_no_messaging_at_all_keeps_the_plain_reason():
    adapter = MetaMessagingAdapter(NullMetaSender())
    with pytest.raises(InboundRejected) as e:
        await adapter.verify_inbound(_signed({"object": "page", "entry": [{"id": "PAGE_1"}]}))
    assert e.value.reason == "meta_no_message"


def test_an_adapter_built_without_a_sender_never_reaches_the_network():
    """The Graph token is per-tenant (P1-1), so a session-less adapter has no way to
    resolve one. It must default to Null rather than to a platform credential."""
    assert isinstance(WhatsAppAdapter()._sender, NullMetaSender)
    assert isinstance(MetaMessagingAdapter()._sender, NullMetaSender)
