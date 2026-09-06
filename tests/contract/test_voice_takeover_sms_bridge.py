"""A voice takeover reply reaches the caller as a TEXT — labelled, gated, never silent
(2026-09-05 audit F150; owner decision: keep the SMS bridge).

A call has no text transport of its own. The admin's reply on a voice conversation
goes out as an SMS on the tenant's number, which is only honest when the dashboard
says so (``voice_bridged_to_sms``) and refuses when it cannot: SMS switched off in the
pillar, or the number not connected + attested (``voice_reply_requires_sms``).
"""

from __future__ import annotations

import uuid

import pytest
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead, LeadState

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

CALLER = "+15557778888"
TENANT_NUMBER = "+15550001111"


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"voice-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_voice_conversation(
    admin: uuid.UUID, *, sms_enabled: bool, sms_status: str | None
) -> uuid.UUID:
    iid, sid, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[
                    {"id": "voice", "enabled": True},
                    {"id": "sms", "enabled": sms_enabled},
                ],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Caller",
                contact_identifier=CALLER,
                state=LeadState.active,
            )
        )
        s.add(
            SessionModel(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="voice",
                mode="ai",
                resolved_lead_id=lead_id,
                summary="Called about a quote",
            )
        )
        if sms_status is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="sms_sender",
                    auth_class="api_key",
                    provider="twilio",
                    status=sms_status,
                    secret_ref=secret_ref_for(admin, iid, "sms_sender"),
                    non_secret_config={"destination": TENANT_NUMBER},
                )
            )
        await s.flush()
    return sid


@pytest.fixture
def twilio(monkeypatch) -> dict[str, str]:
    sent: dict[str, str] = {}

    class FakeSender:
        async def send_sms(self, from_number, to, body):
            sent.update(from_number=from_number, to=to, body=body)
            return "SM-voice-1"

    from app.channels import tenant_twilio

    async def _tenant_sender(*a, **kw):
        return FakeSender()

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return sent


async def _reply(client, sid: uuid.UUID) -> dict:
    await client.post(f"/api/v1/dashboard/conversations/{sid}/take-over")
    r = await client.post(
        f"/api/v1/dashboard/conversations/{sid}/messages",
        json={"text": "Ana here — I can do Tuesday at 10."},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_voice_reply_goes_out_as_a_labelled_text_when_sms_is_on_and_connected(
    client, twilio
):
    admin = await _signup_verify_login(client)
    sid = await _seed_voice_conversation(admin, sms_enabled=True, sms_status="connected")

    body = await _reply(client, sid)
    assert body["delivered"] is True
    assert body["deliveryDetail"] == "voice_bridged_to_sms"
    assert twilio == {
        "from_number": TENANT_NUMBER,
        "to": CALLER,
        "body": "Ana here — I can do Tuesday at 10.",
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_voice_reply_is_refused_when_the_sms_channel_is_off(client, twilio):
    admin = await _signup_verify_login(client)
    sid = await _seed_voice_conversation(admin, sms_enabled=False, sms_status="connected")

    body = await _reply(client, sid)
    assert body["delivered"] is False
    assert body["deliveryDetail"] == "voice_reply_requires_sms"
    assert twilio == {}  # nothing left the building


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("status", ["action_needed", "pending_carrier_registration"])
async def test_a_voice_reply_is_refused_until_the_number_is_attested(client, twilio, status):
    """A business-initiated text is exactly what A2P registration governs: operable-
    but-pending is not enough here, the owner must have attested (`connected`)."""
    admin = await _signup_verify_login(client)
    sid = await _seed_voice_conversation(admin, sms_enabled=True, sms_status=status)

    body = await _reply(client, sid)
    assert body["delivered"] is False
    assert body["deliveryDetail"] == "voice_reply_requires_sms"
    assert twilio == {}
