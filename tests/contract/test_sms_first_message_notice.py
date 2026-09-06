"""The FIRST text of an SMS conversation carries the AI-identity + STOP notice (audit F064).

§3.4.16 and the carrier rules both require it. The adapter waited for a metadata flag
(``is_first_of_conversation``) that no caller ever set, so no conversation ever got the
notice. The Twilio route now keys it on the runtime's ``is_new_session`` — once per
conversation, never on the follow-up turns.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import routing
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.channels.signatures import twilio_signature
from app.channels.sms_adapter import SMS_FIRST_MESSAGE_NOTICE
from app.config import get_settings
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

BYO_NUMBER = "+14155550123"
URL = "http://test/api/v1/webhooks/twilio/sms"


async def _seed_active_instance() -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Sarah Chen",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": "sms", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )
    async with tenant_session(admin) as s:
        ctx = await routing.resolve_destination(s, "sms_sender", BYO_NUMBER)
        assert ctx.admin_id == admin
    return admin, iid


async def _text(client, auth_token: str, body: str, sender: str = "+14155559999") -> str:
    params = {"From": sender, "To": BYO_NUMBER, "Body": body}
    r = await client.post(
        "/api/v1/webhooks/twilio/sms",
        data=params,
        headers={"X-Twilio-Signature": twilio_signature(auth_token, URL, params)},
    )
    assert r.status_code == 200, r.text
    return r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_first_text_of_a_conversation_carries_the_notice_and_the_second_does_not(
    client, monkeypatch
):
    auth_token = "authtok-live"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    await _seed_active_instance()

    first = await _text(client, auth_token, "hello, are you open today?")
    assert "<Message>" in first
    assert SMS_FIRST_MESSAGE_NOTICE in first
    assert first.index(SMS_FIRST_MESSAGE_NOTICE) < first.index("</Message>")

    second = await _text(client, auth_token, "and on sundays?")
    assert "<Message>" in second  # same conversation, Luciel still answers
    assert SMS_FIRST_MESSAGE_NOTICE not in second

    # A different person is a different conversation: their first text gets the notice.
    other = await _text(client, auth_token, "hi there", sender="+14155550001")
    assert SMS_FIRST_MESSAGE_NOTICE in other
