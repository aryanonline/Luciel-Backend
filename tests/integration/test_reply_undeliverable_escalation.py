"""A reply Luciel wrote that the channel could not deliver becomes an escalation
(2026-09-05 audit F077).

The customer is waiting on a channel that cannot answer them. A warning line in the
data-plane log is not an employee's response; telling the owner is. The escalation is
recorded once per session (a retry storm must not page the owner ten times) and the
delivery failure reason rides the audit trail, never the customer-facing reply.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import inbound_dispatch
from app.channels.base import InboundMessage
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


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
                channels=[{"id": "email", "enabled": True}],
                tools=[],
                escalation={"primaryEmail": "owner@shop.example", "preferredChannel": "email"},
                personality={},
            )
        )
        await s.flush()
        return iid


def _email(admin: uuid.UUID, iid: uuid.UUID, text: str) -> InboundMessage:
    return InboundMessage(
        admin_id=admin,
        instance_id=iid,
        channel="email",
        session_id=None,
        customer_identifier="dana@lead.example",
        body=text,
        channel_metadata={"subject": "Quote?"},
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_undeliverable_reply_escalates_once_per_session():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    # No email identity is provisioned for this tenant, so every push fails honestly
    # with ``channel_not_provisioned`` — the customer never sees Luciel's answer.

    first = await inbound_dispatch.run_and_reply_full(_email(admin, iid, "Do you deliver?"))
    assert first.reply_text  # Luciel DID write an answer
    second = await inbound_dispatch.run_and_reply_full(_email(admin, iid, "Hello? Anyone?"))
    assert second.session_id == first.session_id

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(EscalationEvent).where(
                        EscalationEvent.admin_id == admin,
                        EscalationEvent.signal == "reply_undeliverable",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].session_id) == first.session_id
        assert rows[0].gate == "outcome"

        failures = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "escalation_delivery_failed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(failures) == 1
    payload = failures[0].event_payload
    assert payload["signal"] == "reply_undeliverable"
    assert payload["failure_reason"] == "channel_not_provisioned"
    assert payload["context"] == "customer_reply_undeliverable"
    assert "dana@lead.example" not in str(payload)
