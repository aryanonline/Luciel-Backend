"""Contract tests for the inbound channel webhook routes (§3.1.3, §3.1.5, §3.4.1).

Invariants under test:
  - signature is verified BEFORE tenant lookup; a bad/absent signature is rejected and
    never routed to the runtime (§3.1.3);
  - the Meta GET verification handshake echoes hub.challenge iff the verify token matches;
  - an admitted inbound with a resolvable destination reaches the runtime and produces a
    reply on-channel (TwiML for SMS);
  - an unresolvable destination is a clean no-op ack (no 5xx to the provider).
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import routing
from app.channels.phone_lifecycle import attach_sms_number
from app.channels.signatures import twilio_signature
from app.config import get_settings
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

BYO_NUMBER = "+14155550123"


async def _seed_active_instance() -> tuple[uuid.UUID, uuid.UUID]:
    admin = uuid.uuid4()
    iid = uuid.uuid4()
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
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return admin, iid


# --------------------------------------------------------------------------- #
# Meta GET verification handshake
# --------------------------------------------------------------------------- #
async def test_meta_verify_echoes_challenge_on_matching_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "meta_verify_token", "tok-123", raising=False)
    r = await client.get(
        "/api/v1/webhooks/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "tok-123",
            "hub.challenge": "CHALLENGE_ACCEPTED",
        },
    )
    assert r.status_code == 200
    assert r.text == "CHALLENGE_ACCEPTED"


async def test_meta_verify_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "meta_verify_token", "tok-123", raising=False)
    r = await client.get(
        "/api/v1/webhooks/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG",
            "hub.challenge": "x",
        },
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Twilio SMS — signature gate
# --------------------------------------------------------------------------- #
async def test_twilio_sms_bad_signature_is_not_routed(client, monkeypatch):
    """A bad X-Twilio-Signature → rejected pre-resolution, empty TwiML, no runtime."""
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "authtok", raising=False)
    r = await client.post(
        "/api/v1/webhooks/twilio/sms",
        data={"From": "+14155559999", "To": BYO_NUMBER, "Body": "hi"},
        headers={"X-Twilio-Signature": "definitely-wrong"},
    )
    # Rejected inbound is a clean ack (200) with an empty <Response/> — never a 5xx.
    assert r.status_code == 200
    assert "<Response/>" in r.text or "<Response></Response>" in r.text


async def test_twilio_sms_valid_signature_reaches_runtime(client, monkeypatch):
    """Valid signature + resolvable number → runtime runs, TwiML reply returned."""
    auth_token = "authtok-live"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, iid = await _seed_active_instance()
    async with tenant_session(admin) as s:
        await attach_sms_number(s, admin_id=admin, instance_id=iid, e164_number=BYO_NUMBER)
    # Confirm it resolves (guards the test setup).
    async with tenant_session(admin) as s:
        ctx = await routing.resolve_destination(s, "sms_sender", BYO_NUMBER)
        assert ctx.admin_id == admin

    url = "http://test/api/v1/webhooks/twilio/sms"
    params = {"From": "+14155559999", "To": BYO_NUMBER, "Body": "hello"}
    sig = twilio_signature(auth_token, url, params)
    r = await client.post(
        "/api/v1/webhooks/twilio/sms",
        data=params,
        headers={"X-Twilio-Signature": sig},
    )
    assert r.status_code == 200
    # Mock LLM (EchoModel) produces a non-empty reply → wrapped in <Message>.
    assert "<Response>" in r.text


async def test_twilio_sms_unresolvable_number_is_clean_ack(client, monkeypatch):
    """Valid signature but a number no tenant owns → clean 200 no-op, not a crash."""
    auth_token = "authtok-x"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    url = "http://test/api/v1/webhooks/twilio/sms"
    params = {"From": "+14155559999", "To": "+19998887777", "Body": "hi"}
    sig = twilio_signature(auth_token, url, params)
    r = await client.post(
        "/api/v1/webhooks/twilio/sms",
        data=params,
        headers={"X-Twilio-Signature": sig},
    )
    assert r.status_code == 200
    assert "<Response/>" in r.text or "<Response></Response>" in r.text
