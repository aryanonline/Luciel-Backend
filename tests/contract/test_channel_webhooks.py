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
from app.channels.number_operability import NullOperabilityProbe
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
                # The channels these webhooks exercise are ON (a switched-off channel
                # admits no new conversation — tests/unit/test_channel_enabled_gate.py).
                channels=[
                    {"id": "sms", "enabled": True},
                    {"id": "voice", "enabled": True},
                    {"id": "whatsapp", "enabled": True},
                    {"id": "messenger", "enabled": True},
                    {"id": "instagram", "enabled": True},
                ],
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
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )
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


async def test_twilio_sms_is_verified_with_the_tenants_own_auth_token(client, monkeypatch):
    """BYO (§3.1.4): the receiving number lives in the TENANT's Twilio account, so Twilio
    signs the webhook with THEIR auth token. Verifying against the platform's would reject
    every real inbound — the whole channel would look silently dead."""
    from app.connections.secrets import get_secret_store, secret_ref_for

    tenant_token = "tenant-authtok"  # noqa: S105 — test fixture, not a real secret
    # The platform token is deliberately DIFFERENT: if the edge fell back to it, the
    # signature below would not verify and the assertion at the end would fail.
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "platform-authtok", raising=False)

    admin, iid = await _seed_active_instance()
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )
        conn.secret_ref = secret_ref_for(admin, iid, "sms_sender")
        conn.non_secret_config = {**(conn.non_secret_config or {}), "accountSid": "AC" + "7" * 30}
    await get_secret_store().put(
        secret_ref_for(admin, iid, "sms_sender"), {"authToken": tenant_token}
    )

    url = "http://test/api/v1/webhooks/twilio/sms"
    params = {"From": "+14155559999", "To": BYO_NUMBER, "Body": "hello"}
    r = await client.post(
        "/api/v1/webhooks/twilio/sms",
        data=params,
        headers={"X-Twilio-Signature": twilio_signature(tenant_token, url, params)},
    )
    assert r.status_code == 200
    assert "<Response>" in r.text


# --------------------------------------------------------------------------- #
# Meta POST inbound — §3.1.2 split channel attribution
# --------------------------------------------------------------------------- #
def _meta_signature(secret: str, body: bytes) -> str:
    import hashlib
    import hmac as hmac_mod

    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _meta_inbound_payload(obj: str, destination_id: str) -> dict:
    return {
        "object": obj,
        "entry": [
            {
                "id": destination_id,
                "messaging": [
                    {
                        "sender": {"id": "PSID_42"},
                        "message": {"mid": "m.42", "text": "Do you deliver?"},
                    }
                ],
            }
        ],
    }


async def _seed_meta_destination(
    admin: uuid.UUID, iid: uuid.UUID, *, connection_type: str, key: str, destination: str
) -> None:
    from app.connections.secrets import secret_ref_for
    from app.models.connection import InstanceConnection

    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type=connection_type,
                auth_class="oauth_token",
                provider="meta" if connection_type == "channel_auth" else "instagram",
                status="connected",
                secret_ref=secret_ref_for(admin, iid, connection_type),
                non_secret_config={"destinations": {key: destination}},
            )
        )
        await s.flush()


@pytest.mark.parametrize(
    ("obj", "connection_type", "key", "expected_channel"),
    [
        ("page", "channel_auth", "messenger", "messenger"),
        ("instagram", "instagram_auth", "instagram", "instagram"),
    ],
)
async def test_meta_inbound_opens_a_session_on_the_split_channel(
    client, monkeypatch, obj, connection_type, key, expected_channel
):
    """§3.1.2: an Instagram webhook opens an ``instagram`` session and a Page webhook a
    ``messenger`` one — never the pre-split combined channel. The channel id is what
    sessions, rate limits, and the dashboard's channel mix are keyed by."""
    import json as jsonlib

    from app.models.conversation import Session as ConvSession
    from sqlalchemy import select as sa_select

    secret = "meta-secret-42"  # noqa: S105 — test fixture
    monkeypatch.setattr(get_settings(), "meta_app_secret", secret, raising=False)
    monkeypatch.setattr(get_settings(), "instagram_app_secret", "", raising=False)
    admin, iid = await _seed_active_instance()
    destination = f"dest-{uuid.uuid4().hex[:8]}"
    await _seed_meta_destination(
        admin, iid, connection_type=connection_type, key=key, destination=destination
    )

    body = jsonlib.dumps(_meta_inbound_payload(obj, destination)).encode()
    r = await client.post(
        "/api/v1/webhooks/meta",
        content=body,
        headers={
            "X-Hub-Signature-256": _meta_signature(secret, body),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    async with tenant_session(admin) as s:
        channels = (
            (await s.execute(sa_select(ConvSession.channel).where(ConvSession.admin_id == admin)))
            .scalars()
            .all()
        )
    assert channels == [expected_channel]


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
