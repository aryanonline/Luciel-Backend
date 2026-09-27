"""SES inbound email routes to the provisioned tenant end-to-end (§3.1.3 / §3.1.6a).

The gap this pins: ``channel_resolve_destination`` matches
``non_secret_config->>'destination'`` — but ``provision_email`` only ever wrote
``{address, mode}``, so NO email_sender row carried a destination and every inbound
email bounced off "unknown destination" while the dashboard showed a configured
address. Nothing tested ``POST /api/v1/webhooks/ses/inbound`` beyond the route
registry, which is why a dead channel could look finished.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from app.channels import routing
from app.db.session import tenant_session
from app.models.conversation import Session as ConvSession
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


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
                channels=[{"id": "email", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return admin, iid


async def _provision(admin: uuid.UUID, iid: uuid.UUID) -> str:
    from app.connections import service

    async with tenant_session(admin) as s:
        conn = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="vm_subdomain",
            email_address=None,
            instance_name="Sarah Chen",
            user_id=None,
        )
        return str((conn.non_secret_config or {})["address"])


CUSTOMER_MESSAGE_ID = "<ses-inbound-1@lead.example>"


def _sns_notification(*, recipient: str, sender: str, subject: str, body: str) -> dict:
    content = base64.b64encode(
        (
            f"From: {sender}\r\nTo: {recipient}\r\nSubject: {subject}\r\n"
            f"Message-ID: {CUSTOMER_MESSAGE_ID}\r\nContent-Type: text/plain\r\n\r\n{body}\r\n"
        ).encode()
    ).decode()
    return {
        "Type": "Notification",
        "Message": json.dumps(
            {
                "mail": {
                    "destination": [recipient],
                    "source": sender,
                    "commonHeaders": {
                        "from": [sender],
                        "subject": subject,
                        # SES surfaces the RFC 5322 Message-ID here (real shape).
                        "messageId": CUSTOMER_MESSAGE_ID,
                    },
                },
                "receipt": {
                    "spfVerdict": {"status": "PASS"},
                    "dkimVerdict": {"status": "PASS"},
                    "dmarcVerdict": {"status": "PASS"},
                },
                "content": content,
            }
        ),
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_provisioning_binds_the_address_as_the_routing_destination(client):
    admin, iid = await _seed_active_instance()
    address = await _provision(admin, iid)

    async with tenant_session(admin) as s:
        ctx = await routing.resolve_destination(s, "email_sender", address)

    assert ctx.admin_id == admin
    assert ctx.instance_id == iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_inbound_email_to_the_provisioned_address_reaches_the_runtime(client):
    admin, iid = await _seed_active_instance()
    address = await _provision(admin, iid)

    r = await client.post(
        "/api/v1/webhooks/ses/inbound",
        json=_sns_notification(
            recipient=address,
            sender="dana@lead.example",
            subject="Do you deliver?",
            body="Do you deliver on Sundays?",
        ),
    )

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    async with tenant_session(admin) as s:
        session_row = (
            await s.execute(select(ConvSession).where(ConvSession.admin_id == admin))
        ).scalar_one()
    assert session_row.channel == "email"
    # Round 7 WP-3b item 2: the SES ingress leaves the reply context on the session,
    # so a takeover reply hours later threads under the customer's own mail.
    assert session_row.inbound_subject == "Do you deliver?"
    assert session_row.inbound_message_id == CUSTOMER_MESSAGE_ID


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_mail_to_an_unknown_address_is_not_routed(client):
    await _seed_active_instance()  # a tenant exists, but the address matches no one

    r = await client.post(
        "/api/v1/webhooks/ses/inbound",
        json=_sns_notification(
            recipient="luciel@nobody.reply.vantagemind.ai",
            sender="dana@lead.example",
            subject="hi",
            body="hello?",
        ),
    )

    assert r.status_code == 200
    assert r.json() == {"status": "not_routed"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_two_tenants_cannot_claim_the_same_own_domain_address(client):
    from app.connections import service

    admin_a, iid_a = await _seed_active_instance()
    admin_b, iid_b = await _seed_active_instance()
    async with tenant_session(admin_a) as s:
        await service.provision_email(
            s,
            admin_id=admin_a,
            instance_id=iid_a,
            mode="own_domain",
            email_address="hello@sarahchen.example",
            instance_name="Sarah Chen",
            user_id=None,
        )

    async with tenant_session(admin_b) as s:
        with pytest.raises(service.EmailAddressTaken):
            await service.provision_email(
                s,
                admin_id=admin_b,
                instance_id=iid_b,
                mode="own_domain",
                email_address="hello@sarahchen.example",
                instance_name="Imposter",
                user_id=None,
            )
