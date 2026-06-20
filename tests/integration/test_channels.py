"""Channel adapters + routing + inbound pipeline (Architecture §3.1).

Real DB for routing resolution + block audit. Fake providers/verifiers exercise
the adapter contract and the verify→admit→normalize edge.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import rate_limit, routing
from app.channels.base import InboundRejected, OutboundMessage
from app.channels.email_adapter import EmailAdapter
from app.channels.phone_lifecycle import ensure_sms_number
from app.channels.pipeline import AdmissionDenied, ingest
from app.channels.signatures import twilio_signature
from app.channels.sms_adapter import SmsAdapter
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]


class FakeTwilio:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_sms(self, from_number, to, body):
        self.sent.append((from_number, to, body))
        return "SM123"


class FakeSes:
    async def send_email(self, sender, to, subject, body):
        return "ses-msg-1"


class FakeProvisioner:
    async def provision_number(self, admin_id, instance_id):
        return "+15551239999"


async def _seed(admin: uuid.UUID) -> uuid.UUID:
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
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_phone_provision_idempotent_and_resolves():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        c1 = await ensure_sms_number(
            s, admin_id=admin, instance_id=iid, provisioner=FakeProvisioner()
        )
        c2 = await ensure_sms_number(
            s, admin_id=admin, instance_id=iid, provisioner=FakeProvisioner()
        )
        assert c1.connection_id == c2.connection_id  # idempotent — number retained
        # Axis6-F02 (§3.1.6): the number is provisioned immediately but A2P 10DLC
        # registration is pending, so the connection is NOT shown as connected/live.
        assert c1.status == "pending_carrier_registration"
        number = c1.non_secret_config["destination"]
    # Destination resolves back to the tenant via the SECURITY DEFINER resolver.
    async with tenant_session(admin) as s:
        ctx = await routing.resolve_destination(s, "sms_sender", number)
        assert ctx.admin_id == admin and ctx.instance_id == iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_embed_key_resolution_and_unresolvable_rejected():
    admin = uuid.uuid4()
    await _seed(admin)
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin))).scalar_one()
        key = inst.embed_key_public_id
    async with tenant_session(admin) as s:
        ctx = await routing.resolve_widget(s, key)
        assert ctx.admin_id == admin
    async with tenant_session(admin) as s:
        with pytest.raises(InboundRejected):
            await routing.resolve_widget(s, "vm_live_nonexistent")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_adapter_signature_verified_inbound_and_send():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    # provision number so the destination resolves
    async with tenant_session(admin) as s:
        conn = await ensure_sms_number(
            s, admin_id=admin, instance_id=iid, provisioner=FakeProvisioner()
        )
        number = conn.non_secret_config["destination"]

    twilio = FakeTwilio()
    adapter = SmsAdapter(twilio)
    token = "tw_token"  # noqa: S105
    url = "https://api.vantagemind.com/sms"
    params = {"To": number, "From": "+15557654321", "Body": "are you open?", "MessageSid": "SM1"}
    sig = twilio_signature(token, url, params)

    async def resolve(ct, dest):
        async with tenant_session(admin) as s:
            return await routing.resolve_destination(s, ct, dest)

    request = {
        "auth_token": token,
        "url": url,
        "params": params,
        "x_twilio_signature": sig,
        "resolve": resolve,
    }
    ctx = await adapter.verify_inbound(request)
    assert ctx.admin_id == admin
    msg = await adapter.receive(request, ctx)
    assert msg.channel == "sms" and msg.body == "are you open?"
    assert msg.customer_identifier == "+15557654321"

    # Invalid signature is rejected.
    bad = dict(request, x_twilio_signature="wrong")
    with pytest.raises(InboundRejected):
        await adapter.verify_inbound(bad)

    # Outbound send: first-of-conversation prepends AI-identity notice.
    receipt = await adapter.send(
        OutboundMessage(
            channel="sms",
            to="+15557654321",
            body="Yes!",
            metadata={"from_number": number, "is_first_of_conversation": True},
        )
    )
    assert receipt.accepted and receipt.provider_message_id == "SM123"
    assert "AI assistant" in twilio.sent[0][2]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_inbound_pipeline_admits_then_blocks_and_audits():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn = await ensure_sms_number(
            s, admin_id=admin, instance_id=iid, provisioner=FakeProvisioner()
        )
        number = conn.non_secret_config["destination"]

    rate_limit._store = rate_limit.TokenBucketStore()  # fresh limiter
    adapter = SmsAdapter(FakeTwilio())
    token = "tw"  # noqa: S105
    url = "https://x/sms"

    async def resolve(ct, dest):
        async with tenant_session(admin) as s:
            return await routing.resolve_destination(s, ct, dest)

    def make_req(body):
        params = {"To": number, "From": "+15550000001", "Body": body, "MessageSid": "S"}
        return {
            "auth_token": token,
            "url": url,
            "params": params,
            "x_twilio_signature": twilio_signature(token, url, params),
            "resolve": resolve,
        }

    # First 5 admitted (burst), then rate-limited (denial audited).
    results = []
    async with tenant_session(admin):
        pass
    for i in range(7):
        async with tenant_session(admin) as s:
            r = await ingest(adapter, make_req(f"m{i}"), s)
        results.append(r)
    denied = [r for r in results if isinstance(r, AdmissionDenied)]
    assert denied, "expected some inbound to be rate-limited"

    # A channel_abuse_blocked audit row exists for the tenant.
    async with tenant_session(admin) as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(AdminAuditLog.event_type == "channel_abuse_blocked")
            )
        ).scalar_one()
        assert n >= 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_adapter_send():
    adapter = EmailAdapter(
        FakeSes(), sns_verifier=type("V", (), {"verify": lambda self, m: True})()
    )
    receipt = await adapter.send(OutboundMessage(channel="email", to="lead@x.com", body="hello"))
    assert receipt.accepted and receipt.provider_message_id == "ses-msg-1"
