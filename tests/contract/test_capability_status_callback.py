"""Voice status-callback contract under capability-URL verification (C11).

The finalize-at-hangup route accepts EITHER a valid X-Twilio-Signature or the
per-connection ``cap`` token that auto-config registered on the number's URLs.
A forged cap is a forgery signal against a NAMEABLE tenant, so it 403s and
audits ``inbound_capability_rejected`` under that tenant; and the route —
which bypasses pipeline admission entirely — now has its own per-number
flood control.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.tenant_twilio import FIELD_CAPABILITY_TOKEN
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.contract]


async def _seed_capability_tenant(number: str) -> uuid.UUID:
    admin = uuid.uuid4()
    iid = uuid.uuid4()
    ref = secret_ref_for(admin, iid, "sms_sender")
    await get_secret_store().put(
        ref, {"access_token": "bearer-tok", FIELD_CAPABILITY_TOKEN: "status-cap"}
    )
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Cap Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                provider="twilio",
                auth_class="oauth_token",
                status="connected",
                secret_ref=ref,
                non_secret_config={"accountSid": "AC" + "5" * 32, "destination": number},
            )
        )
        await s.flush()
    return admin


async def _audit_count(admin: uuid.UUID, event_type: str) -> int:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == event_type,
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_valid_cap_accepts_and_forged_cap_403s_with_audit(client):
    number = "+16045550173"
    admin = await _seed_capability_tenant(number)
    payload = {"To": number, "From": "+16045550002", "CallSid": "CA" + "1" * 32}

    ok = await client.post("/api/v1/webhooks/twilio/voice/status?cap=status-cap", data=payload)
    assert ok.status_code == 200

    forged = await client.post("/api/v1/webhooks/twilio/voice/status?cap=wrong", data=payload)
    assert forged.status_code == 403
    assert await _audit_count(admin, "inbound_capability_rejected") == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "rate_limit_clock_held")
async def test_status_route_rate_limits_per_number(client):
    number = "+16045550174"  # distinct number: the bucket store is process-global
    await _seed_capability_tenant(number)
    payload = {"To": number, "From": "+16045550002", "CallSid": "CA" + "2" * 32}
    last = None
    for _ in range(12):
        last = await client.post(
            "/api/v1/webhooks/twilio/voice/status?cap=status-cap", data=payload
        )
    assert last is not None and last.status_code == 429
