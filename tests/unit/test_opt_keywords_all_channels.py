"""STOP / START / HELP are consent keywords on EVERY text channel (audit F074).

The SMS carrier answers its own keywords. WhatsApp, Messenger, Instagram and email
have no carrier: a lead who wrote STOP there was routed straight into the LLM, which
cheerfully asked how it could help. Each adapter now classifies the keyword exactly the
way the SMS adapter does (the whole message, trimmed, case-insensitive — never a
sentence that merely contains the word), the pipeline records the consent, and the
edge sends the platform's own confirmation on the channels with no carrier.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import inbound_dispatch, outbound
from app.channels.base import InstanceContext
from app.channels.email_adapter import EmailAdapter
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
from app.channels.pipeline import OptOutHandled
from app.channels.whatsapp_adapter import NullMetaSender, WhatsAppAdapter

pytestmark = [pytest.mark.asyncio]

CTX = InstanceContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4())


class _Sender:
    async def send_email(self, sender, to, subject, body, headers=None):
        return "msg-1"


class _OkVerifier:
    def verify(self, message: dict) -> bool:  # noqa: ANN001
        return True


def _wa_payload(text: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PN123"},
                            "messages": [
                                {"id": "wamid.1", "from": "+14165550143", "text": {"body": text}}
                            ],
                        }
                    }
                ]
            }
        ]
    }


def _meta_payload(text: str) -> dict:
    return {
        "entry": [
            {
                "id": "PAGE_1",
                "messaging": [
                    {"sender": {"id": "PSID_1"}, "message": {"mid": "m.1", "text": text}}
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    ("text", "keyword"),
    [("STOP", "stop"), ("  stop ", "stop"), ("Unsubscribe", "stop"), ("HELP", "help")],
)
async def test_whatsapp_classifies_a_bare_keyword(text: str, keyword: str):
    msg = await WhatsAppAdapter(NullMetaSender()).receive({"payload": _wa_payload(text)}, CTX)
    assert msg.channel_metadata["opt_keyword"] == keyword
    assert msg.channel_metadata["phone_number_id"] == "PN123"  # the reply-as identity


@pytest.mark.parametrize("channel", ["messenger", "instagram"])
async def test_messenger_and_instagram_classify_a_bare_keyword(channel: str):
    adapter = MetaMessagingAdapter(NullMetaSender(), channel=channel)
    msg = await adapter.receive({"payload": _meta_payload("start")}, CTX)
    assert msg.channel_metadata["opt_keyword"] == "start"
    assert msg.channel_metadata["destination_id"] == "PAGE_1"


@pytest.mark.parametrize(
    ("text", "subject", "keyword"),
    [
        ("STOP", "Re: your quote", "stop"),
        ("stop\n\nSent from my phone", "Re: your quote", "stop"),
        ("", "UNSUBSCRIBE", "stop"),
        ("help", None, "help"),
    ],
)
async def test_email_classifies_a_bare_keyword_in_the_first_line_or_subject(
    text: str, subject: str | None, keyword: str
):
    adapter = EmailAdapter(sender=_Sender(), sns_verifier=_OkVerifier())
    msg = await adapter.receive(
        {"from_address": "dana@lead.example", "text": text, "subject": subject}, CTX
    )
    assert msg.channel_metadata["opt_keyword"] == keyword
    assert msg.channel_metadata["subject"] == subject


@pytest.mark.parametrize(
    "text",
    [
        "Please stop sending me quotes",
        "Can you help me pick a deck?",
        "I'd like to start next week",
    ],
)
async def test_a_sentence_that_merely_contains_the_word_is_not_a_keyword(text: str):
    wa = await WhatsAppAdapter(NullMetaSender()).receive({"payload": _wa_payload(text)}, CTX)
    assert "opt_keyword" not in wa.channel_metadata
    meta = await MetaMessagingAdapter(NullMetaSender()).receive(
        {"payload": _meta_payload(text)}, CTX
    )
    assert "opt_keyword" not in meta.channel_metadata
    email = await EmailAdapter(sender=_Sender(), sns_verifier=_OkVerifier()).receive(
        {"from_address": "dana@lead.example", "text": text, "subject": "Re: deck"}, CTX
    )
    assert "opt_keyword" not in email.channel_metadata


# --- the confirmation the platform sends where no carrier does ------------------------ #
@pytest.fixture
def captured_sends(monkeypatch) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []

    async def _send(session, **kwargs):
        sent.append(dict(kwargs))
        return outbound.OutboundResult(channel=kwargs["channel"], delivered=True)

    monkeypatch.setattr(outbound, "send_outbound_reply", _send)
    return sent


def _handled(channel: str, keyword: str, **extra) -> OptOutHandled:
    return OptOutHandled(
        keyword=keyword,
        channel=channel,
        customer_identifier="14165550143",
        admin_id=CTX.admin_id,
        instance_id=CTX.instance_id,
        **extra,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("channel", ["whatsapp", "messenger", "instagram", "email"])
async def test_stop_is_confirmed_on_channels_with_no_carrier(captured_sends, channel: str):
    await inbound_dispatch._confirm_opt_keyword(
        _handled(channel, "stop", sender_id="PN123", subject="Re: deck")
    )
    assert len(captured_sends) == 1
    send = captured_sends[0]
    assert send["channel"] == channel
    assert send["to"] == "14165550143"
    assert send["sender_id"] == "PN123"
    assert "unsubscribed" in str(send["body"]).lower()
    assert "START" in str(send["body"])


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_help_and_start_have_their_own_confirmations(captured_sends):
    await inbound_dispatch._confirm_opt_keyword(_handled("whatsapp", "help"))
    await inbound_dispatch._confirm_opt_keyword(_handled("whatsapp", "start"))
    bodies = [str(s["body"]) for s in captured_sends]
    assert "AI assistant" in bodies[0] and "STOP" in bodies[0]
    assert "opted back in" in bodies[1]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_and_voice_are_left_to_the_carrier(captured_sends):
    await inbound_dispatch._confirm_opt_keyword(_handled("sms", "stop"))
    await inbound_dispatch._confirm_opt_keyword(_handled("voice", "stop"))
    assert captured_sends == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_opt_keyword_with_no_tenant_context_sends_nothing(captured_sends):
    await inbound_dispatch._confirm_opt_keyword(
        OptOutHandled(keyword="stop", channel="whatsapp", customer_identifier="14165550143")
    )
    assert captured_sends == []
